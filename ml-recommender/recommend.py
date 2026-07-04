#!/usr/bin/env python3
"""
recommend.py — a personal movie recommender trained on your own ratings.

Reads ../movies.md and ../watchlist.md, enriches each film with TMDb metadata
(genres, keywords, director, cast, language, year, rating), learns what you
like, and ranks candidate films by predicted affinity.

Candidates come, by default, from **fresh TMDb discover** across your enabled
channels (reusing tastebuds.py), excluding anything already in movies.md,
watchlist.md, or not-interested.md. With --source shortlist (or when offline) it
ranks the "not seen" (0) films already in movies.md instead.

Model selection is automatic:
  • few labels  -> rank by content similarity to your liked set (cold start)
  • enough labels -> a compact NumPy logistic-regression model predicting P(like),
    with cross-validated ROC-AUC / AP and interpretable taste drivers.

Why a linear model (not TensorFlow/PyTorch): at this data scale it generalizes
better, trains in milliseconds on a modern laptop, is interpretable, and exports
directly to Core ML for on-device inference. See README.md.

USAGE
    pip install -r requirements.txt          # numpy
    python3 recommend.py                      # discover-based recs -> recommendations.md
    python3 recommend.py --n 20
    python3 recommend.py --source shortlist   # rank your existing 0-shortlist
    python3 recommend.py --train-only         # just train + report metrics
    python3 recommend.py --json               # machine-readable (used by the rater UI)
"""

import os
import sys
import json
import math
import time
import random
import hashlib
import tempfile
import argparse
from datetime import date

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import tmdb_features as tf  # noqa: E402
try:
    import tastebuds as rater   # reused (stdlib only) for channel discovery + dedupe
except Exception:
    rater = None

MOVIES = os.path.join(ROOT, "movies.md")
WATCH = os.path.join(ROOT, "watchlist.md")
NOT_INTERESTED = os.path.join(ROOT, "not-interested.md")
NOT_INTERESTED_LEGACY = os.path.join(ROOT, "dismissed.md")  # pre-rename fallback
CACHE = os.path.join(HERE, "features_cache.json")
MODEL_PATH = os.path.join(HERE, "model.json")
OUT = os.path.join(HERE, "recommendations.md")
FRIENDS_PATH = os.path.join(ROOT, "friends.json")   # multi-friend list [{name, likes}]
FRIEND_PATH = os.path.join(ROOT, "friend.json")     # legacy single-friend file
WEIGHTS_PATH = os.path.join(ROOT, "model_weights.json")   # user's per-reaction training weights

# How strongly each reaction trains the model (strength only; direction is fixed
# by the reaction: Liked pulls toward, the rest push away). Editable in the UI.
DEFAULT_WEIGHTS = {"like": 1.0, "indifferent": 0.5, "disliked": 1.0, "not_interested": 0.3}
IMG_BASE = "https://image.tmdb.org/t/p/w500"

MIN_POS_FOR_MODEL = 8
MIN_NEG_FOR_MODEL = 5
POOL_CAP = 40  # how many fresh discover candidates to score


# --------------------------------------------------------------------------
# Parse the Markdown tables
# --------------------------------------------------------------------------
def _rows(path):
    """Data rows between the TABLE-START / TABLE-END markers only."""
    if not os.path.exists(path):
        return []
    out = []
    inside = False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if "TABLE-START" in line:
                inside = True
                continue
            if "TABLE-END" in line:
                break
            if inside and line.strip().startswith("|"):
                out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def _split_genres(s):
    return [g.strip() for g in s.split(",") if g.strip()]


def load_movies(path=None):
    path = path or MOVIES
    items = []
    for c in _rows(path):
        # | Rating | Status | Title | Year | Genres | TMDb ID | Link | Rated on |
        if len(c) >= 7 and c[0] in {"0", "1", "2", "3"} and c[5].isdigit():
            items.append({"id": int(c[5]), "rating": int(c[0]), "title": c[2],
                          "year": c[3], "genres": _split_genres(c[4]), "link": c[6],
                          "poster": "", "overview": ""})
    return items


def load_watchlist(path=None):
    path = path or WATCH
    items = []
    for c in _rows(path):
        # | Title | Year | Genres | TMDb ID | Link | Added on |
        if len(c) >= 5 and c[3].isdigit():
            items.append({"id": int(c[3]), "rating": None, "title": c[0],
                          "year": c[1], "genres": _split_genres(c[2]), "link": c[4]})
    return items


def not_interested_ids(path=None):
    path = path or (NOT_INTERESTED if os.path.exists(NOT_INTERESTED) else NOT_INTERESTED_LEGACY)
    ids = set()
    for c in _rows(path):
        if len(c) >= 4 and c[3].isdigit():
            ids.add(int(c[3]))
    return ids


def load_not_interested(path=None):
    """Films marked 'Not interested', as featurizable dicts (used as negatives
    when the not_interested weight is > 0)."""
    path = path or (NOT_INTERESTED if os.path.exists(NOT_INTERESTED) else NOT_INTERESTED_LEGACY)
    items = []
    for c in _rows(path):   # | Title | Year | Genres | TMDb ID | Link | Marked on |
        if len(c) >= 4 and c[3].isdigit():
            items.append({"id": int(c[3]), "rating": None, "title": c[0],
                          "year": c[1], "genres": _split_genres(c[2])})
    return items


def load_weights():
    """User's per-reaction training weights, clamped to [0, 2]; defaults if unset."""
    w = dict(DEFAULT_WEIGHTS)
    if os.path.exists(WEIGHTS_PATH):
        try:
            with open(WEIGHTS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k in w:
                if k in d:
                    w[k] = max(0.0, min(2.0, float(d[k])))
        except Exception:
            pass
    return w


# --------------------------------------------------------------------------
# Feature engineering
# --------------------------------------------------------------------------
def base_features(m):
    f = {}
    for g in m.get("genres", []):
        f["genre=" + g] = 1.0
    y = str(m.get("year") or "")
    if y.isdigit():
        yr = int(y)
        f["decade=%d" % (yr // 10 * 10)] = 1.0
        f["year"] = (yr - 2000) / 25.0
    return f


def meta_features(meta):
    f = {}
    for k in (meta.get("keywords") or [])[:12]:
        f["kw=" + k] = 1.0
    if meta.get("director"):
        f["dir=" + meta["director"]] = 1.0
    for p in (meta.get("cast") or [])[:5]:
        f["cast=" + p] = 1.0
    if meta.get("lang"):
        f["lang=" + meta["lang"]] = 1.0
    if meta.get("vote_average"):
        f["vote_avg"] = float(meta["vote_average"]) / 10.0
    if meta.get("runtime"):
        f["runtime"] = float(meta["runtime"]) / 120.0
    if meta.get("popularity"):
        f["log_pop"] = math.log1p(float(meta["popularity"])) / 5.0
    return f


def featurize(m, metas):
    f = base_features(m)
    f.update(meta_features(metas.get(m["id"], {})))
    return f


class Vectorizer:
    def __init__(self, vocab=None):
        self.vocab = list(vocab) if vocab else []
        self.idx = {k: i for i, k in enumerate(self.vocab)}

    def fit(self, dicts):
        keys = set()
        for d in dicts:
            keys.update(d.keys())
        self.vocab = sorted(keys)
        self.idx = {k: i for i, k in enumerate(self.vocab)}
        return self

    def transform(self, dicts):
        X = np.zeros((len(dicts), len(self.vocab)), dtype=float)
        for i, d in enumerate(dicts):
            for k, v in d.items():
                j = self.idx.get(k)
                if j is not None:
                    X[i, j] = v
        return X


# --------------------------------------------------------------------------
# Metrics + model (NumPy)
# --------------------------------------------------------------------------
def roc_auc(y, s):
    y = np.asarray(y); s = np.asarray(s, dtype=float)
    if (y == 1).sum() == 0 or (y == 0).sum() == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s)); ranks[order] = np.arange(1, len(s) + 1)
    ss = s[order]; i = 0
    while i < len(ss):
        j = i
        while j + 1 < len(ss) and ss[j + 1] == ss[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + 1 + j + 1) / 2.0
        i = j + 1
    n_pos = (y == 1).sum(); n_neg = (y == 0).sum()
    return (ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def average_precision(y, s):
    y = np.asarray(y); s = np.asarray(s, dtype=float)
    if y.sum() == 0:
        return float("nan")
    order = np.argsort(-s, kind="mergesort"); y = y[order]
    tp = np.cumsum(y); precision = tp / (np.arange(len(y)) + 1)
    return float((precision * (y / y.sum())).sum())


def train_logreg(X, y, w=None, l2=1.0, iters=4000, lr=0.5, class_balance=True):
    X = np.asarray(X, float); y = np.asarray(y, float)
    mu = X.mean(0); sd = X.std(0); sd[sd < 1e-8] = 1.0
    Xs = (X - mu) / sd
    n, d = Xs.shape
    Xb = np.hstack([Xs, np.ones((n, 1))])
    theta = np.zeros(d + 1)
    w = np.ones(n) if w is None else np.asarray(w, float).copy()
    if class_balance:                       # so the minority class isn't drowned out
        pos = (y == 1).sum(); neg = (y == 0).sum()
        if pos > 0 and neg > 0:
            w = w * np.where(y == 1, (pos + neg) / (2.0 * pos), (pos + neg) / (2.0 * neg))
    w = w / w.mean()
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Xb @ theta, -30, 30)))
        grad = Xb.T @ ((p - y) * w) / n
        grad[:-1] += (l2 / n) * theta[:-1]
        theta -= lr * grad
    return {"theta": theta.tolist(), "mu": mu.tolist(), "sd": sd.tolist()}


def proba(model, X):
    X = np.asarray(X, float)
    mu = np.array(model["mu"]); sd = np.array(model["sd"]); theta = np.array(model["theta"])
    Xb = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    return 1.0 / (1.0 + np.exp(-np.clip(Xb @ theta, -30, 30)))


# --------------------------------------------------------------------------
# Factorization machine (Rendle, ICDM 2010) — the linear model plus factorized
# pairwise feature interactions:
#     z = b + w.x + sum_{i<j} <v_i, v_j> x_i x_j
# Each feature gets a small latent vector v_i in R^k; the interaction strength
# of any feature pair is the dot product of their vectors, so interactions are
# learned even for pairs never observed together (the factorization is what
# makes this work under extreme sparsity). The pairwise sum is computed in
# linear time via 0.5*(||Vx||^2 - sum_i ||v_i||^2 x_i^2). An FM with k=0 is
# exactly the logistic model above. Activated only when it clearly beats the
# linear model in the same cross-validation (see FM_MARGIN).
# --------------------------------------------------------------------------
FM_K = 6          # latent dimension of the interaction factors
FM_MARGIN = 0.015 # the FM must beat the linear model's CV AUC by this to take over


def train_fm(X, y, w=None, k=FM_K, l2_w=1.0, l2_v=2.0, iters=2000, lr=0.1,
             class_balance=True, seed=0):
    # NOTE: unlike train_logreg, the FM trains on RAW features. Standardizing
    # one-hot columns blows up rare features ((1-mu)/sd is huge when a feature
    # appears once) and the interaction term squares that — divergence. Raw
    # binary/bounded features keep the pairwise term well-conditioned; mu=0 and
    # sd=1 are stored so downstream code treats both model kinds identically.
    X = np.asarray(X, float); y = np.asarray(y, float)
    n, d = X.shape
    sw = np.ones(n) if w is None else np.asarray(w, float).copy()
    if class_balance:                       # so the minority class isn't drowned out
        pos = (y == 1).sum(); neg = (y == 0).sum()
        if pos > 0 and neg > 0:
            sw = sw * np.where(y == 1, (pos + neg) / (2.0 * pos), (pos + neg) / (2.0 * neg))
    sw = sw / sw.mean()
    X2 = X * X
    for attempt in range(3):                # halve the step and retry on divergence
        rng = np.random.default_rng(seed + attempt)
        wv = np.zeros(d); b = 0.0
        V = rng.normal(0.0, 0.03, size=(d, k))
        step = lr / (2 ** attempt)
        ok = True
        for _ in range(iters):
            S1 = X @ V                                                # n x k
            z = b + X @ wv + 0.5 * (S1 * S1 - X2 @ (V * V)).sum(axis=1)
            if not np.isfinite(z).all():
                ok = False
                break
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            g = (p - y) * sw / n
            grad_w = X.T @ g + (l2_w / n) * wv
            grad_V = X.T @ (g[:, None] * S1) - V * (X2.T @ g)[:, None] + (l2_v / n) * V
            b -= step * float(g.sum())
            wv -= step * grad_w
            V -= step * grad_V
        if ok:
            break
    return {"kind": "fm", "w": wv.tolist(), "b": b, "V": V.tolist(),
            "mu": [0.0] * d, "sd": [1.0] * d}


def fm_proba(model, X):
    X = np.asarray(X, float)
    Xs = (X - np.array(model["mu"])) / np.array(model["sd"])   # identity for FMs (raw features)
    V = np.array(model["V"]); wv = np.array(model["w"])
    S1 = Xs @ V
    z = model["b"] + Xs @ wv + 0.5 * (S1 * S1 - (Xs * Xs) @ (V * V)).sum(axis=1)
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def model_kind(model):
    return "fm" if (isinstance(model, dict) and model.get("kind") == "fm") else "linear"


def score_model(model, X):
    """P(like) from either model kind."""
    return fm_proba(model, X) if model_kind(model) == "fm" else proba(model, X)


def linear_weights(model):
    """The linear-term weights of either kind — the interpretable part that
    feeds the taste drivers and the per-film 'why' lines."""
    return np.array(model["w"]) if model_kind(model) == "fm" else np.array(model["theta"])[:-1]


MODEL_PREFS = ("auto", "similarity", "linear", "fm")


def choose_kind(pref, cv_lin, cv_fm):
    """Which trained model ranks: the user's explicit choice, or the CV gate."""
    if pref == "linear":
        return "linear"
    if pref == "fm":
        return "fm"
    return "fm" if (cv_lin and cv_fm and cv_fm["auc"] > cv_lin["auc"] + FM_MARGIN) else "linear"


def model_of_kind(cached, pref):
    """Pull the requested model kind out of the cache. The auto pick sits at the
    root; the other kind rides along as a twin ('linear' / 'fm_alt'), so a
    manual switch in the Model panel needs no retraining. None if absent."""
    if not isinstance(cached, dict):
        return None
    kind = model_kind(cached)
    want = kind if pref in (None, "", "auto") else pref
    if want == kind:
        return cached
    vocab = cached.get("vocab")
    if want == "linear" and isinstance(cached.get("linear"), dict):
        return dict(cached["linear"], vocab=vocab)
    if want == "fm" and isinstance(cached.get("fm_alt"), dict):
        return dict(cached["fm_alt"], kind="fm", vocab=vocab)
    return None


def top_interactions(model, vocab, base=40, topn=5):
    """FM only: the strongest learned feature-pair interactions among the
    features with the largest linear weights, readable as 'A x B'."""
    if model_kind(model) != "fm":
        return []
    wv = np.abs(linear_weights(model)); V = np.array(model["V"])
    cand = [i for i in np.argsort(-wv)[:base] if "=" in vocab[i]]
    pairs = []
    for a in range(len(cand)):
        for c in range(a + 1, len(cand)):
            i, j = cand[a], cand[c]
            s = float(V[i] @ V[j])
            pairs.append((abs(s), s, vocab[i], vocab[j]))
    pairs.sort(reverse=True)
    return [[humanize_feature(fi) + " × " + humanize_feature(fj), round(s, 3)]
            for _, s, fi, fj in pairs[:topn]]


def stratified_folds(y, k, seed=0):
    rng = np.random.default_rng(seed)
    folds = [[] for _ in range(k)]
    for cls in (0, 1):
        idx = np.where(np.asarray(y) == cls)[0]; rng.shuffle(idx)
        for i, v in enumerate(idx):
            folds[i % k].append(int(v))
    return [np.array(sorted(f)) for f in folds]


def rank_with_explore(scores, n, explore):
    """Safe (explore~0) = top scores; higher explore = softmax sampling for variety."""
    scores = np.asarray(scores, dtype=float)
    n = min(n, len(scores))
    if explore <= 0.01 or n <= 0:
        return [int(i) for i in np.argsort(-scores)[:n]]
    T = 0.04 + 0.5 * float(explore)
    z = (scores - scores.max()) / max(T, 1e-3)
    p = np.exp(z); p = p / p.sum()
    idx = np.random.default_rng().choice(len(scores), size=n, replace=False, p=p)
    return [int(i) for i in idx]


def _cat_matrix(dicts, vocab):
    idx = {k: i for i, k in enumerate(vocab)}
    X = np.zeros((len(dicts), len(vocab)))
    for i, d in enumerate(dicts):
        for k in d:
            if k in idx:
                X[i, idx[k]] = 1.0
    nrm = np.linalg.norm(X, axis=1, keepdims=True); nrm[nrm == 0] = 1
    return X / nrm


def similarity_rank(cand, liked, disliked):
    cat = lambda d: [k for k in d if "=" in k]
    vocab = sorted({k for grp in (cand, liked, disliked) for d in grp for k in cat(d)})
    C = _cat_matrix([{k: 1 for k in cat(d)} for d in cand], vocab)
    L = _cat_matrix([{k: 1 for k in cat(d)} for d in liked], vocab)
    sim = C @ L.T if len(L) else np.zeros((len(C), 1))
    score = sim.mean(axis=1) if len(L) else np.zeros(len(C))
    if disliked:
        D = _cat_matrix([{k: 1 for k in cat(d)} for d in disliked], vocab)
        score = score - 0.5 * (C @ D.T).mean(axis=1)
    nearest = sim.argmax(axis=1) if len(L) else None
    return score, nearest


# --------------------------------------------------------------------------
# Candidate sourcing
# --------------------------------------------------------------------------
def discover_pool(exclude_ids, cap=POOL_CAP):
    """Fresh, unseen candidates from TMDb discover via tastebuds channels."""
    if rater is None:
        return []
    try:
        chans = [c for c in rater.load_channels() if c.get("enabled", True)]
    except Exception:
        chans = []
    if not chans:
        return []
    pool = {}
    attempts = 0
    while len(pool) < cap and attempts < max(8, len(chans) * 3):
        attempts += 1
        ch = random.choice(chans)
        try:
            cands = rater.fetch_channel(ch)
        except Exception:
            continue
        for m in cands:
            if m["id"] in exclude_ids or m["id"] in pool:
                continue
            pool[m["id"]] = {
                "id": m["id"], "title": m.get("title", ""), "year": m.get("year", ""),
                "genres": _split_genres(m.get("genres", "")) if isinstance(m.get("genres"), str) else m.get("genres", []),
                "link": m.get("link", ""), "poster": m.get("poster", ""),
                "overview": m.get("overview", ""),
            }
    return list(pool.values())[:cap]


def candidates_for(source, offline, movies, watchlist, extra_exclude=None):
    # Exclude only films you've SEEN (rated 1/2/3), plus watchlist and not-interested.
    # "Not seen" (0) films are intentionally NOT excluded, so they can be
    # recommended again — even though the rater still won't re-ask you to rate them.
    # `extra_exclude` carries films already shown this session, so a new batch is
    # always fresh (the rater passes them via --exclude).
    extra = set(extra_exclude or [])
    seen = {m["id"] for m in movies if m["rating"] in (1, 2, 3)}
    exclude = seen | {m["id"] for m in watchlist} | not_interested_ids() | extra
    if source == "discover" and not offline:
        pool = discover_pool(exclude)
        if pool:
            return pool, "discover"
    return [m for m in movies if m["rating"] == 0 and m["id"] not in extra], "shortlist"


def load_friends():
    """All imported friends as a list of {name, likes}. Reads the multi-friend
    friends.json, falling back to a legacy single-friend friend.json."""
    if os.path.exists(FRIENDS_PATH):
        try:
            with open(FRIENDS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        except Exception:
            return []
    if os.path.exists(FRIEND_PATH):
        try:
            with open(FRIEND_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return [d]
        except Exception:
            return []
    return []


def friend_by_name(name):
    friends = load_friends()
    if name:
        for d in friends:
            if (d.get("name") or "a friend") == name:
                return d
    return friends[0] if friends else None


def friend_candidates(movies, watchlist, name=None, extra_exclude=None):
    """Candidates = a chosen friend's liked films you haven't logged yet."""
    fr = friend_by_name(name)
    fname = (fr or {}).get("name") or "a friend"
    label = fname + "'s likes"
    if not fr or not fr.get("likes"):
        return [], label
    exclude = ({m["id"] for m in movies if m["rating"] in (1, 2, 3)}
               | {m["id"] for m in watchlist} | not_interested_ids() | set(extra_exclude or []))
    out = []
    for m in fr["likes"]:
        mid = m.get("id")
        if mid is None or mid in exclude:
            continue
        g = m.get("genres", [])
        if isinstance(g, str):
            g = _split_genres(g)
        out.append({"id": mid, "title": m.get("title", ""), "year": m.get("year", ""),
                    "genres": g, "link": m.get("link", ""), "poster": "", "overview": ""})
    return out[:80], label


def build_dataset(movies, weights=None, not_interested=None):
    """Build the labelled training set, each example carrying a per-reaction weight
    (its 'strength'). Direction is fixed by the reaction:
      Liked (3) = positive ;  Disliked (1) / Indifferent (2) = negative.
    'Not interested' films join as negatives when their weight > 0. A reaction with
    weight 0 is dropped entirely. Watchlist / Not-seen are never training labels.
    The watchlist is only used elsewhere to exclude candidates."""
    weights = weights or DEFAULT_WEIGHTS
    wl = float(weights.get("like", 1.0)); wi = float(weights.get("indifferent", 0.5))
    wd = float(weights.get("disliked", 1.0)); wn = float(weights.get("not_interested", 0.3))
    train, y, w = [], [], []
    for m in movies:
        if m["rating"] == 3 and wl > 0:
            train.append(m); y.append(1); w.append(wl)
        elif m["rating"] == 2 and wi > 0:
            train.append(m); y.append(0); w.append(wi)
        elif m["rating"] == 1 and wd > 0:
            train.append(m); y.append(0); w.append(wd)
    if wn > 0 and not_interested:
        for m in not_interested:
            train.append(m); y.append(0); w.append(wn)
    return train, y, w


def _disp_genres(m):
    g = m.get("genres", [])
    return ", ".join(g) if isinstance(g, list) else str(g)


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------
_LANG = {"en": "English", "fr": "French", "es": "Spanish", "ja": "Japanese", "ko": "Korean",
         "de": "German", "it": "Italian", "zh": "Chinese", "sv": "Swedish", "da": "Danish",
         "pt": "Portuguese", "ru": "Russian", "hi": "Hindi", "nl": "Dutch", "no": "Norwegian",
         "fi": "Finnish", "pl": "Polish", "cs": "Czech"}


def humanize_feature(tok):
    if "=" not in tok:
        return tok
    k, v = tok.split("=", 1)
    if k == "dir":
        return "directed by " + v
    if k == "lang":
        return _LANG.get(v, v) + "-language"
    if k == "decade":
        return v + "s"
    return v  # genre / kw / cast read fine as-is


def humanize_why(tokens):
    return ", ".join(humanize_feature(t) for t in tokens)


# Scale (continuous) features read as "per unit increase" — a negative weight on
# runtime means "the LONGER, the less you like it". Give them direction-aware
# names so the taste drivers panel says what the weight actually measures.
_SCALE_DRIVER = {"runtime": "longer films", "year": "newer films",
                 "vote_avg": "higher TMDb rating", "log_pop": "more popular films"}


def humanize_driver(tok):
    return _SCALE_DRIVER.get(tok, humanize_feature(tok))


def _atomic_write(path, text):
    """Write atomically: temp file in the same dir, then os.replace()."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


MODEL_VERSION = "v1"   # bump when featurization / model code changes -> invalidates cached models


def data_signature(train, y, w):
    """Stable hash of the labelled, weighted training set (plus a code version), so a
    cached model is reused only when your ratings, the weights, and the pipeline are
    all unchanged."""
    key = MODEL_VERSION + "|" + ";".join(
        "%d:%d:%.3f" % (m["id"], l, ww)
        for m, l, ww in sorted(zip(train, y, w), key=lambda t: t[0]["id"]))
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def load_cached_model(sig):
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        with open(MODEL_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return None
    if m.get("data_sig") == sig and "vocab" in m and ("theta" in m or m.get("kind") == "fm"):
        return m
    return None


def run(n=15, source="discover", offline=False, train_only=False, explore=0.0,
        profile="you", exclude_ids=None, model_pref="auto"):
    # profile is "you" (your taste) or a friend's name. exclude_ids = films already
    # shown this session, so a fresh Recommend batch never repeats them.
    # model_pref: "auto" (CV gate decides) or a forced "similarity"/"linear"/"fm".
    model_pref = model_pref if model_pref in MODEL_PREFS else "auto"
    exclude_ids = set(exclude_ids or [])
    is_friend = profile != "you"
    movies = load_movies(); watchlist = load_watchlist()
    weights = load_weights()
    ni_items = load_not_interested()
    train, y, w = build_dataset(movies, weights, ni_items)
    n_pos = int(sum(1 for v in y if v == 1)); n_neg = int(sum(1 for v in y if v == 0))

    candidates, cand_source = ([], "-")
    if not train_only:
        if is_friend:
            candidates, cand_source = friend_candidates(movies, watchlist, profile, exclude_ids)
        else:
            candidates, cand_source = candidates_for(source, offline, movies, watchlist, exclude_ids)

    all_ids = sorted({m["id"] for m in train} | {m["id"] for m in candidates})
    api_key = tf.resolve_api_key()
    metas = tf.get_features(all_ids, CACHE, api_key=api_key, allow_network=not offline)
    enriched = sum(1 for i in all_ids if metas.get(i, {}).get("keywords"))

    enough = n_pos >= MIN_POS_FOR_MODEL and n_neg >= MIN_NEG_FOR_MODEL
    use_model = enough and model_pref != "similarity"
    result = {"mode": "model" if use_model else "similarity", "n_pos": n_pos, "n_neg": n_neg,
              "candidate_source": cand_source, "candidate_count": len(candidates),
              "enriched": enriched, "total_ids": len(all_ids), "model_pref": model_pref,
              "cv": None, "drivers": [], "recommendations": [], "message": ""}

    if not train and not candidates:
        result["message"] = "No data yet — rate some films first."
        return result

    scores = None; why = {}
    if use_model:
        sig = data_signature(train, y, w)
        cached = None if train_only else load_cached_model(sig)
        model = model_of_kind(cached, model_pref) if cached is not None else None
        if model is not None:
            # reuse a model trained earlier on this exact data — no retrain
            # (the cache carries both kinds, so a manual switch also lands here)
            result["cv_linear"] = cached.get("cv_linear"); result["cv_fm"] = cached.get("cv_fm")
            result["cv"] = ((result["cv_fm"] if model_kind(model) == "fm" else result["cv_linear"])
                            or cached.get("cv"))
            result["model_kind_auto"] = model_kind(cached)
            vocab = cached["vocab"]
        else:
            tfe = [featurize(m, metas) for m in train]
            vec = Vectorizer().fit(tfe)
            Xtr = vec.transform(tfe); ytr = np.array(y); wtr = np.array(w)
            k = min(5, n_pos, n_neg)
            cv_lin = cv_fm = None
            if k >= 2:
                folds = stratified_folds(ytr, k)

                def cv_of(trainer, scorer):
                    aucs, aps = [], []
                    for fold in folds:
                        te = set(fold.tolist()); tr = np.array([i for i in range(len(ytr)) if i not in te])
                        if len(np.unique(ytr[tr])) < 2 or len(np.unique(ytr[fold])) < 2:
                            continue
                        mdl = trainer(Xtr[tr], ytr[tr], wtr[tr])
                        ps = scorer(mdl, Xtr[fold])
                        aucs.append(roc_auc(ytr[fold], ps)); aps.append(average_precision(ytr[fold], ps))
                    return ({"auc": float(np.nanmean(aucs)), "ap": float(np.nanmean(aps)), "k": k}
                            if aucs else None)

                cv_lin = cv_of(train_logreg, proba)
                cv_fm = cv_of(lambda X_, y_, w_: train_fm(X_, y_, w_, iters=800), fm_proba)
            # the gate decides the AUTO pick; a manual choice overrides what RANKS,
            # but both kinds are always trained and cached, so switching is free
            kind_auto = choose_kind("auto", cv_lin, cv_fm)
            kind_used = choose_kind(model_pref, cv_lin, cv_fm)
            result["model_kind_auto"] = kind_auto
            result["cv"] = cv_fm if kind_used == "fm" else cv_lin
            result["cv_linear"] = cv_lin; result["cv_fm"] = cv_fm
            lin = train_logreg(Xtr, ytr, wtr)
            fmm = train_fm(Xtr, ytr, wtr)
            model = fmm if kind_used == "fm" else lin
            # cache the AUTO pick at the root; the other kind rides along as a
            # twin. The linear twin is also what Share exports when the FM leads,
            # so friends' importers (which expect the linear shape) still work.
            cmodel = dict(fmm if kind_auto == "fm" else lin)
            if kind_auto == "fm":
                cmodel["linear"] = {"theta": lin["theta"], "mu": lin["mu"], "sd": lin["sd"], "cv": cv_lin}
            else:
                cmodel["fm_alt"] = {"w": fmm["w"], "b": fmm["b"], "V": fmm["V"],
                                    "mu": fmm["mu"], "sd": fmm["sd"], "cv": cv_fm}
            cmodel["vocab"] = vec.vocab; cmodel["data_sig"] = sig
            cmodel["cv"] = cv_fm if kind_auto == "fm" else cv_lin
            cmodel["cv_linear"] = cv_lin; cmodel["cv_fm"] = cv_fm
            cmodel["version"] = MODEL_VERSION   # rides along into share exports
            _atomic_write(MODEL_PATH, json.dumps(cmodel))
            vocab = vec.vocab

        theta = linear_weights(model)
        result["model_kind"] = model_kind(model)
        result["drivers"] = [[humanize_driver(nm), round(float(c), 3)] for nm, c in
                             sorted(zip(vocab, theta), key=lambda t: -abs(t[1]))[:8]]
        result["drivers"] += top_interactions(model, vocab)   # FM only; 'A × B' pairs
        cv_auc = result["cv"]["auc"] if result["cv"] else None

        if candidates:
            if cv_auc is not None and cv_auc >= 0.58:
                # model is beating chance -> use it to rank
                vec2 = Vectorizer(vocab)
                cfe = [featurize(m, metas) for m in candidates]
                Xc = vec2.transform(cfe); scores = score_model(model, Xc)
                Xcs = (Xc - np.array(model["mu"])) / np.array(model["sd"])
                for i, m in enumerate(candidates):
                    pairs = [(nm, c) for nm, c in zip(vocab, Xcs[i] * theta) if "=" in nm and c > 0]
                    pairs.sort(key=lambda t: -t[1])
                    why[m["id"]] = humanize_why([nm for nm, _ in pairs[:3]])
            else:
                # model not reliable yet -> rank by similarity to your liked films
                liked = [mm for mm, l in zip(train, y) if l == 1]
                disliked = [mm for mm, l in zip(train, y) if l == 0]
                scores, nearest = similarity_rank([featurize(m, metas) for m in candidates],
                                                  [featurize(m, metas) for m in liked],
                                                  [featurize(m, metas) for m in disliked])
                for i, m in enumerate(candidates):
                    if nearest is not None:
                        why[m["id"]] = "similar to films like " + liked[int(nearest[i])]["title"]
                result["mode"] = "similarity"
                result["message"] = ("The trained model isn't beating chance yet (too few 'not-liked' "
                                     "ratings), so these are ranked by similarity to films you liked.")
    else:
        liked = [m for m, l in zip(train, y) if l == 1]
        disliked = [m for m, l in zip(train, y) if l == 0]
        if train_only:
            if model_pref == "similarity" and enough:
                result["message"] = "Similarity mode — your choice in the Model panel."
                return result
            need = []
            if n_pos < MIN_POS_FOR_MODEL: need.append(f"{MIN_POS_FOR_MODEL - n_pos} more liked")
            if n_neg < MIN_NEG_FOR_MODEL: need.append(f"{MIN_NEG_FOR_MODEL - n_neg} more not-liked")
            result["message"] = "Similarity mode (cold start). To train a model, rate " + " and ".join(need) + "."
            return result
        if not liked and not is_friend:
            result["message"] = "Rate at least one Liked (3) film to seed recommendations."
            return result
        if candidates:
            if liked:
                scores, nearest = similarity_rank([featurize(m, metas) for m in candidates],
                                                  [featurize(m, metas) for m in liked],
                                                  [featurize(m, metas) for m in disliked])
                for i, m in enumerate(candidates):
                    if nearest is not None:
                        why[m["id"]] = "similar to: " + liked[int(nearest[i])]["title"]
            else:
                # friend mode with no ratings yet — present the friend's picks in order
                scores = np.arange(len(candidates), 0, -1, dtype=float)

    if is_friend and not train_only:
        fname = (friend_by_name(profile) or {}).get("name") or "your friend"
        for m in candidates:
            why[m["id"]] = "liked by " + fname

    if train_only:
        if not result["message"]:
            result["message"] = "Model trained."
        if use_model and n_neg < 10:
            result["message"] += (" Note: only %d 'not-liked' ratings, so accuracy is "
                                  "limited — rate more Indifferent/Disliked films to sharpen it." % n_neg)
        return result

    if scores is None or not len(candidates):
        result["message"] = ("No friend picks to show — import a friend's likes, or you've seen them all."
                             if is_friend else "No candidates to recommend right now.")
        return result

    order = rank_with_explore(scores, n, explore)
    sel = [float(scores[int(i)]) for i in order]
    smin, smax = (min(sel), max(sel)) if sel else (0.0, 1.0)
    is_model = result["mode"] == "model"

    def to_match(s):
        if is_model:                       # probability -> percent
            return int(round(max(0.0, min(1.0, s)) * 100))
        if smax > smin:                    # similarity -> relative match within this batch
            return int(round(40 + 55 * (s - smin) / (smax - smin)))
        return 70

    for i in order:
        m = candidates[int(i)]
        meta = metas.get(m["id"], {})
        sc = float(scores[int(i)])
        poster = m.get("poster") or ((IMG_BASE + meta["poster_path"]) if meta.get("poster_path") else "")
        overview = m.get("overview") or meta.get("overview") or ""
        result["recommendations"].append({
            "id": m["id"], "title": m["title"], "year": m.get("year", ""),
            "genres": _disp_genres(m), "link": m.get("link", ""), "poster": poster,
            "overview": overview, "director": meta.get("director"),
            "rating": round(float(meta.get("vote_average") or 0), 1),
            "score": round(sc, 3), "match": to_match(sc), "why": why.get(m["id"], ""),
        })
    write_recommendations(result)
    return result


# --------------------------------------------------------------------------
# Movie night — one film for the whole group.
#
# Everyone present is a participant: you, plus the imported friends named in
# --party. Candidates come from the union of watchlists, in tiers (on
# everyone's list -> on most -> on any -> fresh discover), filtered to your
# streaming services. Every participant's taste scores every candidate — their
# exported model when schema-compatible, else similarity to their likes and
# dislikes — scores are rank-normalized onto a common [0,1] scale (rank
# aggregation, cf. Baltrunas et al., RecSys 2010), and the group score is the
# MINIMUM over participants: least misery (O'Connor et al., PolyLens 2001).
# Nobody's evening gets sacrificed for the mean.
# --------------------------------------------------------------------------
PROVIDERS_CACHE = os.path.join(HERE, "providers_cache.json")
PROVIDERS_TTL = 7 * 86400   # availability moves slowly; re-check weekly


def _rank01(scores):
    """Percentile ranks in [0,1] (ties averaged) — puts every participant's
    scores on a common scale before group aggregation."""
    s = np.asarray(scores, float)
    n = len(s)
    if n <= 1:
        return np.ones(n)
    order = np.argsort(s, kind="stable")
    r = np.empty(n)
    r[order] = np.arange(n, dtype=float)
    u, inv = np.unique(s, return_inverse=True)
    sums = np.zeros(len(u)); cnts = np.zeros(len(u))
    np.add.at(sums, inv, r); np.add.at(cnts, inv, 1.0)
    return (sums / cnts)[inv] / (n - 1)


def _person_scores(person, cand_feats, metas):
    """One participant's affinity for every candidate, plus a short per-candidate
    reason: their exported model when its schema matches this code (reason = the
    top feature contributions, same machinery as the individual taste drivers),
    else similarity to their likes/dislikes (reason = the nearest liked film).
    Returns (scores, how, whys)."""
    n = len(cand_feats)
    mdl = person.get("model")
    if isinstance(mdl, dict) and str(mdl.get("schema")) == MODEL_VERSION:
        try:
            vocab = mdl["vocab"]
            X = Vectorizer(vocab).transform(cand_feats)
            sc = score_model(mdl, X)           # linear or FM, whichever they run
            Xs = (X - np.array(mdl["mu"])) / np.array(mdl["sd"])
            theta = linear_weights(mdl)
            whys = []
            for i in range(n):
                pairs = [(nm, c) for nm, c in zip(vocab, Xs[i] * theta) if "=" in nm and c > 0]
                pairs.sort(key=lambda t: -t[1])
                whys.append(("likes " + humanize_why([nm for nm, _ in pairs[:3]]))
                            if pairs else "nothing here their model loves")
            return sc, "model", whys
        except Exception:
            pass
    liked = [featurize(m, metas) for m in (person.get("likes") or [])]
    disliked = [featurize(m, metas) for m in (person.get("dislikes") or [])]
    if liked:
        sc, nearest = similarity_rank(cand_feats, liked, disliked)
        titles = [m.get("title", "") for m in (person.get("likes") or [])]
        whys = []
        for i in range(n):
            t = titles[int(nearest[i])] if nearest is not None and titles else ""
            whys.append(("similar to " + t + ", which they liked") if t
                        else "similar to their liked films")
        return np.asarray(sc, float), "similarity", whys
    return np.zeros(n), "none", ["no taste data yet"] * n


def _own_participant(movies, model_pref="auto"):
    """You, as a movie-night participant: the cached trained model when present
    (even a slightly stale one beats retraining mid-party), else likes/dislikes.
    Honors the Model panel's manual choice, including forced similarity."""
    person = {"name": "You",
              "likes": [m for m in movies if m["rating"] == 3],
              "dislikes": [m for m in movies if m["rating"] == 1]}
    if model_pref == "similarity":
        return person
    try:
        with open(MODEL_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        m = model_of_kind(raw, model_pref)
        if m is None:
            return person
        ok_linear = (isinstance(m.get("theta"), list)
                     and len(m["theta"]) == len(m.get("vocab") or []) + 1)
        ok_fm = (m.get("kind") == "fm" and isinstance(m.get("V"), list))
        if isinstance(m.get("vocab"), list) and (ok_linear or ok_fm):
            person["model"] = dict(m, schema=str(raw.get("version", MODEL_VERSION)))
    except Exception:
        pass
    return person


def _available_ids(ids, region, providers):
    """Subset of ids streamable (flatrate) on the given providers in the given
    region — the same semantics as the discover filter. Cached for a week; on a
    failed lookup the film is kept (better a maybe than a wrongly dropped pick)."""
    try:
        with open(PROVIDERS_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}
    now = time.time()
    out, dirty = set(), False
    for mid in ids:
        key = "%d:%s" % (mid, region)
        ent = cache.get(key)
        if not isinstance(ent, dict) or now - ent.get("ts", 0) > PROVIDERS_TTL:
            try:
                d = rater.tmdb_get("movie/%d/watch/providers" % mid, {}) or {}
                flat = [p.get("provider_id") for p in
                        (((d.get("results") or {}).get(region) or {}).get("flatrate") or [])]
                ent = {"ts": now, "flatrate": [int(x) for x in flat if x is not None]}
                cache[key] = ent; dirty = True
            except Exception:
                out.add(mid)      # unknown -> keep, don't cache
                continue
        if set(ent.get("flatrate") or []) & providers:
            out.add(mid)
    if dirty:
        try:
            _atomic_write(PROVIDERS_CACHE, json.dumps(cache))
        except Exception:
            pass
    return out


# How the group combines individual scores. All three operate on the same
# rank-normalized score matrix; they are the strategies people naturally use
# when deciding for their own groups (Masthoff 2015), and the empirically best
# one depends on how much the group's tastes diverge (Barile et al. 2023):
#   least_misery  — group score = the lowest fan's score (nobody suffers)
#   average       — the mean (crowd pleaser)
#   avg_no_misery — the mean, but films below anyone's floor sink to the back
COMBINE_OPS = ("least_misery", "average", "avg_no_misery")
MISERY_FLOOR = 0.25   # avg_no_misery: a film under this percentile for anyone is floored


def movie_night(party, n=8, offline=False, combine="least_misery", model_pref="auto"):
    """Pick films for the group: you + the named imported friends."""
    combine = combine if combine in COMBINE_OPS else "least_misery"
    model_pref = model_pref if model_pref in MODEL_PREFS else "auto"
    movies = load_movies()
    watchlist = load_watchlist()
    by_name = {(f.get("name") or "a friend"): f for f in load_friends()}
    people = [_own_participant(movies, model_pref)]
    missing = []
    for name in party:
        if name in by_name:
            p = dict(by_name[name]); p["name"] = name
            people.append(p)
        else:
            missing.append(name)
    result = {"participants": [p["name"] for p in people], "missing": missing,
              "picks": [], "how": {}, "combine": combine,
              "filtered_by_providers": False, "message": ""}

    # exclusions: anything anyone has seen, or you've dismissed
    seen = {m["id"] for m in movies if m["rating"] in (1, 2, 3)} | not_interested_ids()
    for p in people[1:]:
        seen |= {int(x) for x in (p.get("seen") or []) if isinstance(x, int)}
        seen |= {m["id"] for m in (p.get("dislikes") or [])}   # older exports carry no "seen"

    # candidates from the union of watchlists, tiered by how many lists carry them
    k = len(people)
    wls = [("You", watchlist)] + [(p["name"], p.get("watchlist") or []) for p in people[1:]]
    cand, onlists = {}, {}
    for pname, wl in wls:
        for m in wl:
            mid = m.get("id")
            if not isinstance(mid, int) or mid in seen:
                continue
            g = m.get("genres")
            cand.setdefault(mid, {"id": mid, "title": m.get("title", ""), "year": str(m.get("year", "")),
                                  "genres": g if isinstance(g, list) else _split_genres(g or ""),
                                  "link": m.get("link", ""), "poster": "", "overview": ""})
            onlists.setdefault(mid, set()).add(pname)

    # where-to-watch filter (yours — you're all in the same room)
    if not offline and rater is not None and cand:
        try:
            prefs = rater.load_providers() or {}
        except Exception:
            prefs = {}
        region = prefs.get("region"); provs = {int(x) for x in (prefs.get("providers") or [])}
        if region and provs:
            keep = _available_ids(sorted(cand), region, provs)
            cand = {i: m for i, m in cand.items() if i in keep}
            result["filtered_by_providers"] = True

    pool = list(cand.values())
    tier_of = {m["id"]: k - len(onlists[m["id"]]) for m in pool}   # 0 = on everyone's list

    # final tier: fresh discover (channels already apply the providers filter)
    if len(pool) < n and not offline:
        for m in discover_pool(seen | set(cand), cap=POOL_CAP):
            tier_of[m["id"]] = k
            pool.append(m)

    if not pool:
        result["message"] = ("Nothing left to pick from — add films to your watchlists, "
                             "or relax the streaming filter.")
        return result

    # everyone scores everything
    need = {m["id"] for m in pool}
    for p in people:
        mdl = p.get("model")
        if not (isinstance(mdl, dict) and str(mdl.get("schema")) == MODEL_VERSION):
            need |= {m["id"] for m in (p.get("likes") or [])}
            need |= {m["id"] for m in (p.get("dislikes") or [])}
    metas = tf.get_features(sorted(need), CACHE, api_key=tf.resolve_api_key(),
                            allow_network=not offline)
    cand_feats = [featurize(m, metas) for m in pool]
    rows, whys_all = [], []
    for p in people:
        sc, how, whys = _person_scores(p, cand_feats, metas)
        rows.append(_rank01(sc))
        whys_all.append(whys)
        result["how"][p["name"]] = how
    P = np.vstack(rows)                     # participants x candidates
    if combine == "average":
        group = P.mean(axis=0)
    elif combine == "avg_no_misery":
        group = P.mean(axis=0)
        floored = P.min(axis=0) < MISERY_FLOOR
        group = np.where(floored, group - 1.0, group)   # sink, never vanish
    else:
        group = P.min(axis=0)               # least misery
    weakest = P.argmin(axis=0)

    order = sorted(range(len(pool)), key=lambda i: (tier_of[pool[i]["id"]], -group[i]))
    for i in order[:n]:
        m = dict(pool[i])
        mid = m["id"]
        m.update({"tier": tier_of[mid], "on": sorted(onlists.get(mid, ())),
                  "group_score": round(float(group[i]), 3),
                  "scores": {p["name"]: round(float(P[j, i]), 3) for j, p in enumerate(people)},
                  "why": {p["name"]: whys_all[j][i] for j, p in enumerate(people)},
                  "weakest": people[int(weakest[i])]["name"]})
        if combine == "avg_no_misery" and bool(P[:, i].min() < MISERY_FLOOR):
            m["floored"] = True
        result["picks"].append(m)
    return result


def write_recommendations(result):
    head = "trained model" if result["mode"] == "model" else "content similarity"
    lines = ["# Recommendations\n",
             f"_Generated {date.today().isoformat()} · {head} · source: {result['candidate_source']}_\n",
             "| # | Title | Year | Score | Why |", "|---|-------|------|-------|-----|"]
    for i, r in enumerate(result["recommendations"], 1):
        lines.append(f"| {i} | {r['title'].replace('|','/')} | {r['year']} | {r['score']:.3f} | {r['why'].replace('|','/')} |")
    _atomic_write(OUT, "\n".join(lines) + "\n")


def _print_human(result):
    print("\n  Movie recommender\n  " + "-" * 17)
    print(f"  labels: {result['n_pos']} liked / {result['n_neg']} not-liked · "
          f"mode: {result['mode']} · candidates: {result['candidate_count']} ({result['candidate_source']})")
    if result["cv"]:
        print(f"  {result['cv']['k']}-fold CV — ROC-AUC {result['cv']['auc']:.3f} · AP {result['cv']['ap']:.3f}")
    if result["drivers"]:
        print("  taste drivers:")
        for nm, c in result["drivers"]:
            print(f"    {'+' if c >= 0 else '-'} {nm} ({c:+.2f})")
    if result["message"]:
        print("  " + result["message"])
    for i, r in enumerate(result["recommendations"], 1):
        print(f"   {i:2d}. {r['title']} ({r['year']})  score {r['score']:.3f}  {r['why']}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Personal movie recommender trained on your ratings.")
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--source", choices=["discover", "shortlist"], default="discover")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--train-only", action="store_true")
    ap.add_argument("--explore", type=float, default=0.0)
    ap.add_argument("--profile", default="you", help='"you" or a friend\'s name')
    ap.add_argument("--exclude", default="", help="comma-separated ids already shown this session")
    ap.add_argument("--rebuild-cache", action="store_true", help="delete the metadata cache and refetch")
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout (for the rater UI)")
    ap.add_argument("--movie-night", action="store_true", help="pick films for a group (you + --party)")
    ap.add_argument("--party", default="[]", help='JSON list of imported friend names joining movie night')
    ap.add_argument("--combine", default="least_misery", choices=list(COMBINE_OPS),
                    help="how the group combines individual scores")
    ap.add_argument("--model", default="auto", choices=list(MODEL_PREFS),
                    help="which model ranks: auto (CV gate) or a forced choice")
    args = ap.parse_args()
    if args.rebuild_cache:
        try:
            if os.path.exists(CACHE):
                os.remove(CACHE)
        except Exception:
            pass
    if args.movie_night:
        try:
            party = [str(x) for x in json.loads(args.party or "[]")]
        except Exception:
            party = []
        result = movie_night(party, n=args.n, offline=args.offline, combine=args.combine,
                             model_pref=args.model)
        print(json.dumps(result) if args.json else json.dumps(result, indent=2))
        return
    excl = {int(x) for x in args.exclude.split(",") if x.strip().isdigit()}
    result = run(n=args.n, source=args.source, offline=args.offline,
                 train_only=args.train_only, explore=args.explore,
                 profile=args.profile, exclude_ids=excl, model_pref=args.model)
    if args.json:
        print(json.dumps(result))
    else:
        _print_human(result)


if __name__ == "__main__":
    main()
