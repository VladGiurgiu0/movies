#!/usr/bin/env python3
"""
tastebuds.py - a small local browser app to rate movies into movies.md
========================================================================

WHAT IT DOES
    Opens a clean page in your browser that shows one movie at a time
    (poster + title + short synopsis + a TMDb link) with these actions:

        Liked              seen and liked        -> movies.md  (code 3)
        Indifferent        seen, not memorable   -> movies.md  (code 2)
        Disliked           seen and disliked     -> movies.md  (code 1)
        Not seen           haven't seen it       -> movies.md  (code 0)
        Add to Watchlist   want to watch later   -> watchlist.md

    Anything already in movies.md OR watchlist.md is never shown again.

CHANNELS
    Candidates come from "channels" - pools defined by genres + keywords + a
    style (Popular / Acclaimed / Hidden gems). Edit them live from the
    "Channels" button in the top bar, or by hand in channels.json (created next
    to this script on first run). Disabled channels are skipped.

ONE-TIME SETUP  (about 2 minutes, free)
    1. Make a free account at https://www.themoviedb.org/signup
    2. Go to https://www.themoviedb.org/settings/api and request an
       "API Key (Developer)". Approval is instant.
    3. Put the "API Key (v3 auth)" in EITHER:
         a) a file called  tmdb_key.txt  next to this script, OR
         b) an env var:  export TMDB_API_KEY=your_key_here

HOW TO RUN
    python3 tastebuds.py
    (a browser tab opens automatically; press Ctrl+C in the terminal to stop)

KEYBOARD SHORTCUTS
    3 / L = liked    2 / I = indifferent    1 / D = disliked    0 / N / Space = not seen
    W = add to watchlist     U = undo the last action     C = open Channels

No third-party packages required. Python 3.8+.
"""

import os
import sys
import json
import random
import secrets
import tempfile
import subprocess
import threading
import webbrowser
import urllib.parse
import urllib.request
import urllib.error
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Config / paths
# --------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MD_PATH = os.path.join(SCRIPT_DIR, "movies.md")
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.md")
NOT_INTERESTED_PATH = os.path.join(SCRIPT_DIR, "not-interested.md")
NOT_INTERESTED_LEGACY_PATH = os.path.join(SCRIPT_DIR, "dismissed.md")  # migrated on first run
CHANNELS_PATH = os.path.join(SCRIPT_DIR, "channels.json")
ML_DIR = os.path.join(SCRIPT_DIR, "ml-recommender")
RECOMMEND_PY = os.path.join(ML_DIR, "recommend.py")
DIRECTOR_CACHE_PATH = os.path.join(SCRIPT_DIR, "director_cache.json")
FRIENDS_PATH = os.path.join(SCRIPT_DIR, "friends.json")   # multi-friend list [{name, likes}]
FRIEND_PATH = os.path.join(SCRIPT_DIR, "friend.json")     # legacy single-friend file (migrated on read)
PROVIDERS_PATH = os.path.join(SCRIPT_DIR, "providers.json")  # {region, providers:[ids]} streaming filter
TABLE_END_MARKER = "<!-- TABLE-END -->"

MD_ID_COL = 5          # TMDb ID column index in movies.md rows
WATCH_ID_COL = 3       # TMDb ID column index in watchlist.md rows
NOT_INTERESTED_ID_COL = 3   # TMDb ID column index in not-interested.md rows

# rating code -> status label written to movies.md
STATUS = {"3": "Liked", "2": "Indifferent", "1": "Disliked", "0": "Not seen"}

# Templates to (re)create data files if they are missing. movies.md and
# watchlist.md are git-ignored (personal data), so they regenerate per user.
MOVIES_TEMPLATE = """# My Movie Ratings

A personal movie taste log. The companion script `tastebuds.py` appends rows here.

## Rating code

| Code | Meaning                  | Used for                                  |
|------|--------------------------|-------------------------------------------|
| 3    | Seen & **liked**         | Strong positive signal                    |
| 2    | Seen & **indifferent**   | Forgettable - weak / neutral signal       |
| 1    | Seen & **disliked**      | Negative signal                           |
| 0    | **Not seen** (shortlist) | Pre-vetted candidate to maybe watch later |

<!-- TABLE-START: do not delete this marker. The script appends rows below the header. -->

| Rating | Status | Title | Year | Genres | TMDb ID | Link | Rated on |
|--------|--------|-------|------|--------|---------|------|----------|

<!-- TABLE-END -->
"""

WATCHLIST_TEMPLATE = """# My Watchlist

Films saved from the rater to watch later. The rater skips anything already listed.

<!-- TABLE-START: do not delete this marker. The script appends rows below the header. -->

| Title | Year | Genres | TMDb ID | Link | Added on |
|-------|------|--------|---------|------|----------|

<!-- TABLE-END -->
"""

NOT_INTERESTED_TEMPLATE = """# Not interested

Films marked "Not interested" from the Recommendations panel. They won't be
recommended or shown for rating again.

<!-- TABLE-START: do not delete this marker. The script appends rows below the header. -->

| Title | Year | Genres | TMDb ID | Link | Marked on |
|-------|------|--------|---------|------|-----------|

<!-- TABLE-END -->
"""

WATCH_REGION = "AT"    # default region for the streaming filter (editable in the UI)
LANGUAGE = "en-US"
IMG_BASE = "https://image.tmdb.org/t/p/w500"
IMG_BASE_W92 = "https://image.tmdb.org/t/p/w92"   # small provider logos
TMDB_API = "https://api.themoviedb.org/3"
MAX_PAGE = 18          # random page within a channel, for variety

# Style presets: a friendly label -> sort + quality thresholds.
STYLES = {
    "popular":   {"sort": "popularity.desc",   "vote_count_gte": 60,  "vote_avg_gte": 6.0},
    "acclaimed": {"sort": "vote_average.desc",  "vote_count_gte": 100, "vote_avg_gte": 7.0},
    "gems":      {"sort": "vote_average.desc",  "vote_count_gte": 30,  "vote_avg_gte": 6.5},
}

# Seed channels (written to channels.json on first run; editable afterwards).
DEFAULT_CHANNELS = [
    {"name": "Sundance-style romcom", "enabled": True, "genres": [10749, 35],
     "keywords": [], "style": "popular", "vote_count_gte": 40, "vote_avg_gte": 6.0, "sort": "popularity.desc"},
    {"name": "Coming-of-age", "enabled": True, "genres": [18],
     "keywords": ["coming-of-age"], "style": "gems", "vote_count_gte": 30, "vote_avg_gte": 6.2, "sort": "vote_average.desc"},
    {"name": "Indie / arthouse", "enabled": True, "genres": [18],
     "keywords": ["independent film"], "style": "acclaimed", "vote_count_gte": 80, "vote_avg_gte": 6.8, "sort": "vote_average.desc"},
    {"name": "Romantic drama", "enabled": True, "genres": [10749, 18],
     "keywords": [], "style": "popular", "vote_count_gte": 60, "vote_avg_gte": 6.4, "sort": "popularity.desc"},
    {"name": "Quirky comedy", "enabled": True, "genres": [35],
     "keywords": [], "style": "gems", "vote_count_gte": 60, "vote_avg_gte": 6.5, "sort": "vote_average.desc"},
]


# --------------------------------------------------------------------------
# API key
# --------------------------------------------------------------------------
def load_api_key():
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key:
        return key
    keyfile = os.path.join(SCRIPT_DIR, "tmdb_key.txt")
    if os.path.exists(keyfile):
        with open(keyfile, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


API_KEY = load_api_key()
TOKEN = secrets.token_hex(16)   # minted per run; the page embeds it and /api/* requires it


# --------------------------------------------------------------------------
# Channels: load / save / sanitize
# --------------------------------------------------------------------------
_io_lock = threading.Lock()


def _num(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _numf(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def sanitize_channel(c):
    style = c.get("style") if c.get("style") in STYLES else None
    preset = STYLES.get(style, {})
    genres = c.get("genres", [])
    keywords = c.get("keywords", [])
    return {
        "name": (str(c.get("name", "")).strip() or "Untitled")[:60],
        "enabled": bool(c.get("enabled", True)),
        "genres": [int(g) for g in genres if str(g).strip().isdigit()][:5] if isinstance(genres, list) else [],
        "keywords": [str(k).strip() for k in keywords if str(k).strip()][:6] if isinstance(keywords, list) else [],
        "style": style or "custom",
        "vote_count_gte": _num(c.get("vote_count_gte", preset.get("vote_count_gte")), 40),
        "vote_avg_gte": _numf(c.get("vote_avg_gte", preset.get("vote_avg_gte")), 6.0),
        "sort": str(c.get("sort", preset.get("sort", "popularity.desc"))),
    }


def save_channels(channels):
    _atomic_write(CHANNELS_PATH, json.dumps(channels, indent=2, ensure_ascii=False))


def load_channels():
    if os.path.exists(CHANNELS_PATH):
        try:
            with open(CHANNELS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return [sanitize_channel(c) for c in data if isinstance(c, dict)]
        except Exception:
            pass
    seed = [sanitize_channel(c) for c in DEFAULT_CHANNELS]
    save_channels(seed)
    return seed


# --------------------------------------------------------------------------
# TMDb client (stdlib only)
# --------------------------------------------------------------------------
def tmdb_get(path, params):
    params = dict(params)
    params["api_key"] = API_KEY
    url = f"{TMDB_API}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "tastebuds/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


_genre_map = None
def genre_map():
    global _genre_map
    if _genre_map is None:
        try:
            data = tmdb_get("genre/movie/list", {"language": LANGUAGE})
            _genre_map = {g["id"]: g["name"] for g in data.get("genres", [])}
        except Exception:
            _genre_map = {}
    return _genre_map


_keyword_cache = {}
def resolve_keyword(name):
    name = name.strip().lower()
    if name in _keyword_cache:
        return _keyword_cache[name]
    try:
        data = tmdb_get("search/keyword", {"query": name})
        results = data.get("results", [])
        kid = results[0]["id"] if results else None
    except Exception:
        kid = None
    _keyword_cache[name] = kid
    return kid


def _movie_from_tmdb(m):
    if not m.get("poster_path"):
        return None
    gm = genre_map()
    year = (m.get("release_date") or "")[:4]
    genres = ", ".join(gm.get(gid, "") for gid in m.get("genre_ids", []) if gm.get(gid))
    return {
        "id": m["id"],
        "title": m.get("title") or m.get("original_title") or "Untitled",
        "year": year,
        "genres": genres or "-",
        "overview": m.get("overview") or "",
        "poster": IMG_BASE + m["poster_path"],
        "link": f"https://www.themoviedb.org/movie/{m['id']}",
        "rating": round(m.get("vote_average", 0), 1),
    }


def fetch_channel(channel):
    """Return a shuffled list of candidate movie dicts from one channel."""
    params = {
        "language": LANGUAGE,
        "include_adult": "false",
        "sort_by": channel.get("sort", "popularity.desc"),
        "vote_count.gte": channel.get("vote_count_gte", 40),
        "vote_average.gte": channel.get("vote_avg_gte", 6.0),
        "page": random.randint(1, MAX_PAGE),
    }
    if channel.get("genres"):
        params["with_genres"] = ",".join(str(g) for g in channel["genres"])
    kw_ids = [resolve_keyword(n) for n in channel.get("keywords", [])]
    kw_ids = [k for k in kw_ids if k]
    if kw_ids:
        params["with_keywords"] = ",".join(str(k) for k in kw_ids)

    # Streaming filter: only films available on the user's selected platforms
    # (their subscriptions, in their country). Applies to every discover pull, so
    # both the rater and the recommender (which reuses this) respect it.
    prefs = load_providers()
    if prefs["region"] and prefs["providers"]:
        params["watch_region"] = prefs["region"]
        params["with_watch_providers"] = "|".join(str(p) for p in prefs["providers"])
        params["with_watch_monetization_types"] = "flatrate"

    data = tmdb_get("discover/movie", params)
    results = data.get("results", [])
    if not results and params["page"] != 1:
        params["page"] = 1
        data = tmdb_get("discover/movie", params)
        results = data.get("results", [])

    out = []
    for m in results:
        mv = _movie_from_tmdb(m)
        if mv:
            mv["channel"] = channel["name"]
            out.append(mv)
    random.shuffle(out)
    return out


# --------------------------------------------------------------------------
# Markdown table read / write
# --------------------------------------------------------------------------
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


def ensure_file(path, template):
    if not os.path.exists(path):
        _atomic_write(path, template)


def _read_lines(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.readlines()


def _table_lines(path):
    """Only the data rows between the TABLE-START and TABLE-END markers
    (so legend tables or other content are never parsed as data)."""
    if not os.path.exists(path):
        return []
    out = []
    inside = False
    for line in _read_lines(path):
        if "TABLE-START" in line:
            inside = True
            continue
        if TABLE_END_MARKER in line:
            break
        if inside and line.strip().startswith("|"):
            out.append(line)
    return out


def _ids_in(path, id_col):
    ids = set()
    for line in _table_lines(path):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > id_col and cells[id_col].isdigit():
            ids.add(int(cells[id_col]))
    return ids


def rated_ids():
    return _ids_in(MD_PATH, MD_ID_COL)


def watchlist_ids():
    return _ids_in(WATCHLIST_PATH, WATCH_ID_COL)


def not_interested_ids():
    return _ids_in(NOT_INTERESTED_PATH, NOT_INTERESTED_ID_COL)


def stats():
    counts = {"3": 0, "2": 0, "1": 0, "0": 0}
    for line in _table_lines(MD_PATH):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) > MD_ID_COL and cells[MD_ID_COL].isdigit() and cells[0] in counts:
            counts[cells[0]] += 1
    counts["watch"] = len(watchlist_ids())
    return counts


def _insert_row(path, row):
    lines = _read_lines(path)
    for i, line in enumerate(lines):
        if TABLE_END_MARKER in line:
            lines.insert(i, row)
            break
    else:
        lines.append(row)
    _atomic_write(path, "".join(lines))


def append_rating(movie, rating):
    ensure_file(MD_PATH, MOVIES_TEMPLATE)
    rating = str(rating)
    status = STATUS.get(rating, "?")
    title = str(movie.get("title", "")).replace("|", "/")
    genres = str(movie.get("genres", "")).replace("|", "/")
    row = (f"| {rating} | {status} | {title} | {movie.get('year','')} | "
           f"{genres} | {movie['id']} | {movie.get('link','')} | {date.today().isoformat()} |\n")
    _insert_row(MD_PATH, row)


def append_watchlist(movie):
    ensure_file(WATCHLIST_PATH, WATCHLIST_TEMPLATE)
    title = str(movie.get("title", "")).replace("|", "/")
    genres = str(movie.get("genres", "")).replace("|", "/")
    row = (f"| {title} | {movie.get('year','')} | {genres} | {movie['id']} | "
           f"{movie.get('link','')} | {date.today().isoformat()} |\n")
    _insert_row(WATCHLIST_PATH, row)


def append_not_interested(movie):
    ensure_file(NOT_INTERESTED_PATH, NOT_INTERESTED_TEMPLATE)
    title = str(movie.get("title", "")).replace("|", "/")
    genres = str(movie.get("genres", "")).replace("|", "/")
    row = (f"| {title} | {movie.get('year','')} | {genres} | {movie['id']} | "
           f"{movie.get('link','')} | {date.today().isoformat()} |\n")
    _insert_row(NOT_INTERESTED_PATH, row)


def remove_row_by_id(path, id_col, movie_id):
    if not os.path.exists(path):
        return
    lines = _read_lines(path)
    target = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) > id_col and cells[id_col].isdigit() and int(cells[id_col]) == movie_id:
            target = i
    if target is not None:
        del lines[target]
        _atomic_write(path, "".join(lines))


# --------------------------------------------------------------------------
# Library editing (search + change a film's status)
# --------------------------------------------------------------------------
def _library_entries():
    """All logged films, keyed by id, with their current status."""
    by_id = {}
    for line in _table_lines(MD_PATH):  # | Rating | Status | Title | Year | Genres | TMDb ID | Link | ... |
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) > MD_ID_COL and c[MD_ID_COL].isdigit() and c[0] in ("0", "1", "2", "3"):
            by_id[int(c[MD_ID_COL])] = {"id": int(c[MD_ID_COL]), "title": c[2], "year": c[3],
                                        "genres": c[4], "link": c[6] if len(c) > 6 else "", "status": c[0]}
    for path, st in ((WATCHLIST_PATH, "watchlist"), (NOT_INTERESTED_PATH, "not-interested")):
        for line in _table_lines(path):  # | Title | Year | Genres | TMDb ID | Link | ... |
            c = [x.strip() for x in line.strip().strip("|").split("|")]
            if len(c) > 3 and c[3].isdigit():
                by_id[int(c[3])] = {"id": int(c[3]), "title": c[0], "year": c[1],
                                    "genres": c[2], "link": c[4] if len(c) > 4 else "", "status": st}
    return by_id


def find_movies(q):
    """Unified search: your library first (with its status), then TMDb matches you
    haven't logged yet (status None). Dedupes by id, so a film already in your
    library appears once, with its current status."""
    q = (q or "").strip()
    if not q:
        return []
    ql = q.lower()
    lib = _library_entries()
    results = []
    seen = set()
    for e in sorted(lib.values(), key=lambda x: x["title"].lower()):
        if ql in e["title"].lower():
            d = dict(e); d["in_library"] = True
            results.append(d); seen.add(e["id"])
    if API_KEY:
        try:
            data = tmdb_get("search/movie", {"query": q, "include_adult": "false", "language": LANGUAGE})
            gm = genre_map()
            for m in data.get("results", [])[:25]:
                mid = m.get("id")
                if mid is None or mid in seen:
                    continue
                genres = ", ".join(gm.get(g, "") for g in m.get("genre_ids", []) if gm.get(g))
                results.append({
                    "id": mid, "title": m.get("title") or m.get("original_title") or "Untitled",
                    "year": (m.get("release_date") or "")[:4], "genres": genres,
                    "link": f"https://www.themoviedb.org/movie/{mid}",
                    "status": None, "in_library": False,
                })
                seen.add(mid)
        except Exception:
            pass
    return results[:40]


def set_status(movie, status):
    """Move a film to a new status: remove it from every file, then add to the target."""
    mid = int(movie["id"])
    with _io_lock:
        remove_row_by_id(MD_PATH, MD_ID_COL, mid)
        remove_row_by_id(WATCHLIST_PATH, WATCH_ID_COL, mid)
        remove_row_by_id(NOT_INTERESTED_PATH, NOT_INTERESTED_ID_COL, mid)
        if status in ("0", "1", "2", "3"):
            append_rating(movie, status)
        elif status == "watchlist":
            append_watchlist(movie)
        elif status == "not-interested":
            append_not_interested(movie)


# --------------------------------------------------------------------------
# Share / friends (export your likes, import friends' — one database per friend)
# --------------------------------------------------------------------------
def liked_export():
    """Your liked films, in a small shareable shape."""
    return [{"id": e["id"], "title": e["title"], "year": e["year"], "genres": e["genres"], "link": e["link"]}
            for e in _library_entries().values() if e["status"] == "3"]


def _clean_friend(data):
    """Sanitize one imported friend record (name + capped likes)."""
    name = (str(data.get("name") or "").strip() or "a friend")[:60]
    likes = []
    for m in (data.get("likes") or [])[:5000]:   # cap an imported file to a sane size
        if isinstance(m, dict) and str(m.get("id", "")).isdigit():
            likes.append({"id": int(m["id"]), "title": str(m.get("title", "")), "year": str(m.get("year", "")),
                          "genres": m.get("genres", ""), "link": str(m.get("link", ""))})
    return {"name": name, "likes": likes}


def load_friends():
    """All imported friends as a list. Migrates a legacy single friend.json into
    the multi-friend friends.json the first time it's read."""
    if not os.path.exists(FRIENDS_PATH) and os.path.exists(FRIEND_PATH):
        try:
            with open(FRIEND_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("likes") is not None:
                _atomic_write(FRIENDS_PATH, json.dumps([_clean_friend(d)], ensure_ascii=False))
        except Exception:
            pass
    if os.path.exists(FRIENDS_PATH):
        try:
            with open(FRIENDS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and d.get("likes") is not None]
        except Exception:
            return []
    return []


def friends_info():
    """Lightweight list for the UI: name + how many liked films, no payload."""
    return [{"name": d.get("name") or "a friend", "count": len(d.get("likes") or [])}
            for d in load_friends()]


def save_friend(data):
    """Add or replace one friend (keyed by name) in friends.json."""
    fr = _clean_friend(data)
    friends = [d for d in load_friends() if (d.get("name") or "a friend") != fr["name"]]
    friends.append(fr)
    _atomic_write(FRIENDS_PATH, json.dumps(friends, ensure_ascii=False))
    return {"name": fr["name"], "count": len(fr["likes"]), "friends": friends_info()}


def remove_friend(name=None):
    """Remove one friend by name, or all friends when name is None."""
    if name is None:
        friends = []
    else:
        friends = [d for d in load_friends() if (d.get("name") or "a friend") != name]
    _atomic_write(FRIENDS_PATH, json.dumps(friends, ensure_ascii=False))
    return friends_info()


# --------------------------------------------------------------------------
# Streaming providers (filter discover to what you can actually stream)
# --------------------------------------------------------------------------
def load_providers():
    if os.path.exists(PROVIDERS_PATH):
        try:
            with open(PROVIDERS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            region = str(d.get("region") or "").upper()[:2]
            ids = [int(x) for x in (d.get("providers") or []) if str(x).strip().isdigit()]
            return {"region": region, "providers": ids}
        except Exception:
            pass
    return {"region": "", "providers": []}


def save_providers(region, providers):
    region = str(region or "").upper()[:2]
    ids, seen = [], set()
    for x in (providers or []):
        if str(x).strip().isdigit() and int(x) not in seen:
            seen.add(int(x)); ids.append(int(x))
    ids = ids[:40]
    _atomic_write(PROVIDERS_PATH, json.dumps({"region": region, "providers": ids}, ensure_ascii=False))
    return {"region": region, "providers": ids}


def list_providers(region):
    """Streaming services available in `region`, ordered by TMDb display priority."""
    region = str(region or "").upper()[:2]
    if not API_KEY or not region:
        return []
    try:
        data = tmdb_get("watch/providers/movie", {"language": LANGUAGE, "watch_region": region})
    except Exception:
        return []
    provs = sorted(data.get("results", []), key=lambda p: p.get("display_priority", 999))
    out = []
    for p in provs[:60]:
        out.append({"id": p.get("provider_id"), "name": p.get("provider_name", ""),
                    "logo": (IMG_BASE_W92 + p["logo_path"]) if p.get("logo_path") else ""})
    return out


# --------------------------------------------------------------------------
# Candidate buffer + session state
# --------------------------------------------------------------------------
_buffer = []
_shown_session = set()
_last_shown = None
_action_stack = []
_buf_lock = threading.Lock()
_rec_seen = {}   # profile -> set of recommendation ids already shown this session (kept fresh)


def next_candidate():
    global _last_shown
    with _buf_lock:
        chans = [c for c in load_channels() if c.get("enabled", True)]
        if not chans:
            return {"error": "nochannels"}
        with _io_lock:
            already = rated_ids() | watchlist_ids() | not_interested_ids() | _shown_session
        attempts = 0
        while not _buffer and attempts < 14:
            attempts += 1
            channel = random.choice(chans)
            try:
                cands = fetch_channel(channel)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    return {"error": "auth"}
                continue
            except Exception:
                continue
            for c in cands:
                if c["id"] not in already:
                    _buffer.append(c)
        if not _buffer:
            return None
        movie = _buffer.pop(0)
        _shown_session.add(movie["id"])
        _last_shown = movie
        return movie


# --------------------------------------------------------------------------
# Director lookup (one small cached call per shown film)
# --------------------------------------------------------------------------
_director_cache = None
def _directors():
    global _director_cache
    if _director_cache is None:
        _director_cache = {}
        if os.path.exists(DIRECTOR_CACHE_PATH):
            try:
                with open(DIRECTOR_CACHE_PATH, "r", encoding="utf-8") as f:
                    _director_cache = json.load(f)
            except Exception:
                _director_cache = {}
    return _director_cache


def fetch_director(movie_id):
    cache = _directors(); key = str(movie_id)
    if key in cache:
        return cache[key]
    director = None
    if API_KEY:
        try:
            data = tmdb_get(f"movie/{movie_id}/credits", {})
            director = next((c["name"] for c in data.get("crew", []) if c.get("job") == "Director"), None)
        except Exception:
            director = None
    cache[key] = director
    try:
        _atomic_write(DIRECTOR_CACHE_PATH, json.dumps(cache))
    except Exception:
        pass
    return director


# --------------------------------------------------------------------------
# ML recommender bridge (runs ml-recommender/recommend.py as a subprocess so
# the rater itself stays dependency-free)
# --------------------------------------------------------------------------
def run_recommender(extra_args, timeout=600):
    if not os.path.exists(RECOMMEND_PY):
        return False, "The ml-recommender folder isn't here."
    try:
        proc = subprocess.run([sys.executable, RECOMMEND_PY, "--json"] + extra_args,
                              cwd=ML_DIR, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "The recommender took too long and was stopped."
    except Exception as e:
        return False, str(e)
    if proc.returncode != 0:
        err = proc.stderr or ""
        if "ModuleNotFoundError" in err and "numpy" in err:
            return False, ("NumPy isn't installed (the recommender needs it). In Terminal:\n"
                           "pip install -r ml-recommender/requirements.txt")
        tail = "\n".join(err.strip().splitlines()[-3:])
        return False, tail or "The recommender failed."
    out = (proc.stdout or "").strip()
    try:
        return True, json.loads(out)
    except Exception:
        try:
            return True, json.loads(out.splitlines()[-1])
        except Exception:
            return False, "Couldn't parse the recommender's output."


# --------------------------------------------------------------------------
# Web page
# --------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tastebuds</title>
<style>
  :root{
    --bg:#f5f5f7; --card:#ffffff; --txt:#1d1d1f; --muted:#86868b;
    --line:rgba(0,0,0,.10); --shadow:0 18px 50px rgba(0,0,0,.12);
    --blue:#0071e3; --red:#d70015; --gray-tint:rgba(120,120,128,.14);
    /* button palette (constant across light/dark) */
    --teal:#128f86; --amber:#e7a531; --coral:#dd5a44; --violet:#6f51e0; --graphite:#45454c;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#000000; --card:#1c1c1e; --txt:#f5f5f7; --muted:#98989d;
      --line:rgba(255,255,255,.12); --shadow:0 18px 55px rgba(0,0,0,.6);
      --blue:#0a84ff; --red:#ff453a; --gray-tint:rgba(120,120,128,.26);
    }
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0;background:var(--bg);color:var(--txt);
    font-family:system-ui,"Helvetica Neue",Arial,sans-serif;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;min-height:100%;
  }
  .page{max-width:820px;margin:0 auto;padding:24px 18px 60px}
  .rail{flex:0 0 132px;position:sticky;top:24px;display:flex;flex-direction:column;align-items:center}
  .col{flex:1;min-width:0;display:flex;flex-direction:column;gap:16px}
  .step{display:flex;flex-direction:column;align-items:center;cursor:pointer;text-align:center}
  .step .dot{width:34px;height:34px;border-radius:50%;background:var(--gray-tint);color:var(--muted);
             display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;transition:.2s}
  .step .lbl{font-size:12px;font-weight:600;color:var(--txt);margin-top:6px;letter-spacing:-.01em;line-height:1.2}
  .step.active .dot{background:var(--teal);color:#fff}
  .step:not(.active){opacity:.5}
  .rail .line{width:2px;height:84px;background:var(--line);margin:6px 0}
  .win{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:22px;box-shadow:var(--shadow);scroll-margin-top:18px}
  .win-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:14px}
  .win-head>.hd-left{display:flex;align-items:center;gap:8px;flex-wrap:wrap;flex:1;min-width:0}
  .explore-row{display:flex;align-items:center;gap:10px;margin:0 0 12px;flex-wrap:wrap}
  .explore-row #rec-meta{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .win-title{font-size:17px;font-weight:600;letter-spacing:-.02em}
  .wnum{display:inline-flex;width:26px;height:26px;border-radius:50%;background:var(--gray-tint);color:var(--txt);
        font-weight:700;font-size:13px;align-items:center;justify-content:center;flex:0 0 auto}
  .win-sub{font-size:12px;color:var(--muted);letter-spacing:-.01em}
  .srcchip{display:inline-block;background:rgba(18,143,134,.12);color:var(--teal);font-size:11.5px;font-weight:600;
           padding:3px 10px;border-radius:980px;margin-bottom:10px;letter-spacing:-.01em}
  .cardfoot{display:flex;justify-content:flex-end;margin-top:12px}
  #rec-body{min-height:542px}
  .loading{min-height:542px;display:flex;flex-direction:column;align-items:center;justify-content:center;
           color:var(--muted);font-size:14px;letter-spacing:-.01em;text-align:center}
  .dots{display:flex;gap:7px;margin-bottom:14px}
  .dots span{width:9px;height:9px;border-radius:50%;background:var(--violet);opacity:.25;animation:dotpulse 1s infinite ease-in-out}
  .dots span:nth-child(2){animation-delay:.15s}
  .dots span:nth-child(3){animation-delay:.3s}
  @keyframes dotpulse{0%,80%,100%{opacity:.25;transform:translateY(0)}40%{opacity:1;transform:translateY(-6px)}}
  .rec-foot{display:flex;justify-content:center;margin-top:16px}
  .refresh-btn{appearance:none;border:none;border-radius:13px;padding:12px 20px;font:inherit;font-size:14.5px;
               font-weight:600;color:#fff;background:var(--blue);cursor:pointer;display:inline-flex;
               align-items:center;gap:9px;letter-spacing:-.01em;transition:filter .15s ease, transform .06s ease}
  .refresh-btn:hover{filter:brightness(1.06)}
  .refresh-btn:active{transform:translateY(1px)}
  .drawer-head{cursor:pointer;margin-bottom:0}
  .chev{color:var(--muted);transition:transform .18s ease;display:inline-block;font-size:13px}
  .drawer.open .chev{transform:rotate(90deg)}
  .drawer-body{display:none;margin-top:16px}
  .drawer.open .drawer-body{display:block}
  .slider-row{display:flex;align-items:center;gap:10px;margin:2px 0 14px}
  .slider-row span{font-size:11.5px;color:var(--muted);letter-spacing:-.01em}
  .slider-row input[type=range]{flex:1;accent-color:var(--violet)}
  .primarybtn{appearance:none;border:none;border-radius:12px;padding:11px 16px;font:inherit;font-size:14px;
              font-weight:600;color:#fff;background:var(--teal);cursor:pointer;letter-spacing:-.01em}
  .primarybtn.violet{background:var(--violet)}
  .primarybtn:hover{filter:brightness(1.06)}
  .bar{width:100%;max-width:860px;display:flex;justify-content:space-between;
       align-items:center;gap:14px;padding:24px 24px 8px}
  .bar .left{display:flex;align-items:center;gap:12px}
  .bar .name{font-size:15px;font-weight:600;letter-spacing:-.01em;white-space:nowrap}
  .ghost{appearance:none;background:var(--gray-tint);border:none;color:var(--txt);
         font:inherit;font-size:12.5px;font-weight:560;padding:6px 12px;border-radius:980px;
         cursor:pointer;letter-spacing:-.01em}
  .ghost:hover{filter:brightness(1.05)}
  .tally{font-size:12.5px;color:var(--muted);letter-spacing:-.01em;text-align:right}
  .tally b{color:var(--txt);font-weight:600}
  .card{width:100%;max-width:860px;background:var(--card);border:1px solid var(--line);
        border-radius:26px;padding:30px;margin:0;box-shadow:var(--shadow)}
  .row{display:flex;gap:30px;height:390px}
  .poster{height:100%;width:auto;flex:0 0 auto;border-radius:16px;background:rgba(120,120,128,.12);
          aspect-ratio:2/3;object-fit:cover;box-shadow:0 10px 30px rgba(0,0,0,.22)}
  .meta{min-width:0;flex:1;display:flex;flex-direction:column;min-height:0}
  .meta h1{font-size:30px;font-weight:600;letter-spacing:-.022em;line-height:1.1;margin:0 0 8px}
  .sub{color:var(--muted);font-size:15px;margin-bottom:14px;letter-spacing:-.01em}
  .chips{margin-bottom:10px}
  .chip{display:inline-block;background:var(--gray-tint);color:var(--muted);
        font-size:12.5px;padding:4px 11px;border-radius:980px;margin:0 7px 7px 0;letter-spacing:-.01em}
  .overview{font-size:16px;line-height:1.55;color:var(--txt);opacity:.88;
            margin:10px 0 14px;flex:1 1 auto;min-height:0;overflow:auto}
  a.link{color:var(--blue);text-decoration:none;font-size:15px;font-weight:500;letter-spacing:-.01em}
  a.link:hover{text-decoration:underline}
  .rate-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:24px}
  .second-row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
  .btn{appearance:none;border:none;border-radius:15px;padding:17px 14px;font-size:16.5px;
       font-weight:600;letter-spacing:-.012em;color:#fff;cursor:pointer;font-family:inherit;
       box-shadow:0 1px 2px rgba(0,0,0,.16), 0 9px 22px -12px rgba(0,0,0,.55);
       transition:transform .06s ease, box-shadow .18s ease, filter .18s ease}
  .btn:hover{filter:brightness(1.07)}
  .btn:active{transform:translateY(1px) scale(.995);box-shadow:0 1px 2px rgba(0,0,0,.2)}
  .like{background:var(--teal)}
  .meh{background:var(--amber);color:#241a06}
  .dislike{background:var(--coral)}
  .skip{background:var(--graphite)}
  .watch{background:var(--violet)}
  .hint{margin-top:14px;text-align:center;font-size:12px;color:var(--muted);letter-spacing:-.01em}
  .foot{max-width:860px;width:100%;padding:0 24px 36px;display:flex;
        justify-content:space-between;align-items:center}
  .foot .pool{font-size:12.5px;color:var(--muted);letter-spacing:-.01em}
  .undo{background:none;border:none;color:var(--muted);font:inherit;font-size:13px;
        cursor:pointer;padding:6px 0;letter-spacing:-.01em}
  .undo:hover{color:var(--txt)}
  .msg{padding:48px 14px;text-align:center;color:var(--muted);line-height:1.6;font-size:15px}
  .msg code{background:var(--gray-tint);padding:2px 7px;border-radius:6px;color:var(--txt);font-size:13px}

  /* Channels overlay */
  .overlay{position:fixed;inset:0;background:rgba(0,0,0,.32);display:none;
           align-items:flex-start;justify-content:center;padding:40px 16px;z-index:10;
           overflow:auto;-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px)}
  .overlay.open{display:flex}
  .panel{width:100%;max-width:560px;background:var(--card);border:1px solid var(--line);
         border-radius:20px;box-shadow:var(--shadow);padding:22px}
  .panel h2{font-size:19px;font-weight:600;letter-spacing:-.02em;margin:0}
  .panel .phead{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
  .panel .desc{color:var(--muted);font-size:12.5px;margin:0 0 14px;letter-spacing:-.01em}
  .x{background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;line-height:1;padding:2px 6px}
  .x:hover{color:var(--txt)}
  .ch{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
  .ch input[type=checkbox]{width:18px;height:18px;accent-color:var(--blue);flex:0 0 auto}
  .ch .info{flex:1;min-width:0}
  .ch .cn{font-size:14px;font-weight:600;letter-spacing:-.01em}
  .ch .cs{font-size:11.5px;color:var(--muted);letter-spacing:-.01em;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ch .del{background:none;border:none;color:var(--red);font:inherit;font-size:12px;cursor:pointer}
  .ch .del:hover{text-decoration:underline}
  .addbox{margin-top:16px;padding:16px;background:var(--gray-tint);border-radius:14px}
  .addbox h3{margin:0 0 10px;font-size:13px;font-weight:600;letter-spacing:-.01em}
  .fld{margin-bottom:10px}
  .fld label{display:block;font-size:11.5px;color:var(--muted);margin-bottom:4px;letter-spacing:-.01em}
  .fld input{width:100%;padding:9px 11px;border-radius:10px;border:1px solid var(--line);
             background:var(--card);color:var(--txt);font:inherit;font-size:14px}
  .gpick{display:flex;flex-wrap:wrap;gap:6px}
  .gchip{font-size:12px;padding:5px 11px;border-radius:980px;border:1px solid var(--line);
         color:var(--muted);background:var(--card);cursor:pointer;letter-spacing:-.01em;user-select:none}
  .gchip.on{background:var(--blue);border-color:var(--blue);color:#fff}
  .seg{display:inline-flex;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .seg button{appearance:none;background:none;border:none;font:inherit;font-size:12.5px;
              padding:7px 12px;color:var(--txt);cursor:pointer}
  .seg button.on{background:var(--blue);color:#fff}
  .prow{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
  .pbtn{appearance:none;border:none;border-radius:11px;padding:11px 18px;font:inherit;
        font-size:14px;font-weight:600;cursor:pointer;letter-spacing:-.01em}
  .pbtn.primary{background:var(--blue);color:#fff}
  .pbtn.cancel{background:var(--gray-tint);color:var(--txt)}
  .note{font-size:11.5px;color:var(--muted);margin-top:8px;letter-spacing:-.01em}
  #edit-results{max-height:52vh;overflow:auto;margin-top:6px}
  .er{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
  .er .ei{flex:1;min-width:0}
  .er .et{font-size:14px;font-weight:600;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .er .es{font-size:11.5px;color:var(--muted);font-weight:500}
  .er .estat{font-size:11.5px;color:var(--muted);letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .er select{font:inherit;font-size:12.5px;padding:6px 8px;border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--txt);flex:0 0 auto}
  .er .er-link{font-size:12px;color:var(--blue);text-decoration:none;flex:0 0 auto;white-space:nowrap}
  .er .er-link:hover{text-decoration:underline}

  /* ML panels (Train / Recommend) */
  .ml-msg{color:var(--muted);font-size:13px;letter-spacing:-.01em;padding:8px 0;line-height:1.5;white-space:pre-wrap}
  .stat{font-size:13.5px;color:var(--txt);margin:3px 0;letter-spacing:-.01em}
  .stat b{font-weight:600}
  .drv{display:flex;align-items:center;gap:9px;margin:5px 0;font-size:12px}
  .drv .nm{flex:0 0 160px;color:var(--txt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .drv .bar{height:9px;border-radius:6px;background:var(--teal);min-width:2px}
  .drv .bar.neg{background:var(--coral)}
  .rec{display:flex;gap:13px;padding:13px 0;border-bottom:1px solid var(--line);align-items:flex-start}
  .rec img,.rec .ph{width:56px;height:84px;flex:0 0 56px;border-radius:9px;object-fit:cover;background:var(--gray-tint)}
  .rec .ri{flex:1;min-width:0}
  .rec .rt{font-size:14.5px;font-weight:600;letter-spacing:-.01em}
  .rec .rs{font-size:11.5px;color:var(--muted);font-weight:500}
  .rec .rw{font-size:12px;color:var(--muted);margin-top:3px;letter-spacing:-.01em}
  .rec .ra{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
  .rec .ra button,.rec .ra a{font:inherit;font-size:12px;font-weight:600;border:none;border-radius:9px;
        padding:6px 11px;cursor:pointer;text-decoration:none;letter-spacing:-.01em}
  .rec .add{background:var(--violet);color:#fff}
  .rec .dis{background:var(--gray-tint);color:var(--txt)}
  .rec .lnk{background:transparent;color:var(--blue);padding-left:2px}
  .rec .ralabel{font-size:11px;color:var(--muted);align-self:center;margin-right:2px}
  .rec .v{color:#fff}
  .rec .v.like{background:var(--teal)}
  .rec .v.meh{background:var(--amber);color:#241a06}
  .rec .v.dislike{background:var(--coral)}
  #rec-refresh{font-size:12.5px;padding:8px 14px}
  .tip{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;
       background:var(--gray-tint);color:var(--muted);font-size:10px;font-weight:700;font-style:normal;
       cursor:help;position:relative;margin-left:6px;user-select:none;flex:0 0 auto;vertical-align:middle}
  .tip::after{content:attr(data-tip);position:absolute;left:50%;bottom:150%;transform:translateX(-50%);
       background:var(--txt);color:var(--bg);font-size:11.5px;font-weight:500;line-height:1.45;padding:8px 10px;
       border-radius:9px;width:230px;max-width:62vw;opacity:0;pointer-events:none;transition:opacity .12s ease;
       z-index:60;box-shadow:0 10px 28px rgba(0,0,0,.35);text-align:left;letter-spacing:-.01em}
  .tip:hover::after,.tip.show::after{opacity:1}

  /* Flip card: rater on the front, recommender on the back */
  .flip-wrap{position:relative;perspective:2200px;min-height:560px;transition:height .45s cubic-bezier(.4,0,.2,1)}
  .flip{position:relative;width:100%;height:100%;transition:transform .72s cubic-bezier(.45,.05,.2,1);transform-style:preserve-3d}
  .flip.flipped{transform:rotateY(180deg)}
  .face{position:absolute;top:0;left:0;width:100%;backface-visibility:hidden;-webkit-backface-visibility:hidden}
  .face.back{transform:rotateY(180deg)}
  .flipbtn{appearance:none;border:none;border-radius:980px;padding:8px 15px;font:inherit;font-size:12.5px;
           font-weight:600;color:#fff;background:var(--violet);cursor:pointer;display:inline-flex;align-items:center;
           gap:7px;letter-spacing:-.01em;white-space:nowrap;box-shadow:0 6px 16px -9px rgba(0,0,0,.7);
           transition:filter .15s ease, transform .06s ease}
  .flipbtn.teal{background:var(--teal)}
  .flipbtn:hover{filter:brightness(1.07)}
  .flipbtn:active{transform:translateY(1px)}

  /* profile dropdown in the recommender */
  .prof-select{font:inherit;font-size:12.5px;font-weight:560;padding:6px 26px 6px 12px;border-radius:980px;
       border:1px solid var(--line);color:var(--txt);cursor:pointer;-webkit-appearance:none;appearance:none;
       letter-spacing:-.01em;background:var(--card) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='3' stroke-linecap='round'><path d='M6 9l6 6 6-6'/></svg>") no-repeat right 10px center}

  /* provider picker + friends list */
  .prov-grid{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
  .prov{display:flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:980px;
        padding:5px 13px 5px 6px;cursor:pointer;background:var(--card);user-select:none;font-size:12.5px;
        letter-spacing:-.01em;color:var(--txt)}
  .prov img{width:22px;height:22px;border-radius:6px;object-fit:cover;background:var(--gray-tint);flex:0 0 auto}
  .prov.on{background:var(--blue);border-color:var(--blue);color:#fff}
  .friend-row{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
  .friend-row .fn{flex:1;min-width:0;font-size:14px;font-weight:600;letter-spacing:-.01em;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .friend-row .fc{font-size:11.5px;color:var(--muted);flex:0 0 auto}

  @media(max-width:680px){
    .page{flex-direction:column;gap:14px}
    .rail{flex-direction:row;position:static;width:100%;justify-content:center;gap:6px;flex:none}
    .rail .line{width:34px;height:2px}
    .step .lbl{display:none}
  }
  @media(max-width:620px){
    .row{flex-direction:column;height:auto}
    .poster{width:100%;height:auto;flex:none;max-width:260px;margin:0 auto}
    .overview{flex:none}
    .rate-row{grid-template-columns:1fr}
    .second-row{grid-template-columns:1fr}
  }
</style></head>
<body>
  <div class="page">
    <div class="flip-wrap" id="flipwrap">
     <div class="flip" id="flip">

      <div class="face front" id="face-front">
      <div class="win" id="win-rate">
        <div class="win-head">
          <div class="hd-left">
            <span class="wnum">1</span>
            <div class="win-title">Rate Movies</div>
            <button class="ghost" id="open-settings">Channels</button>
            <button class="ghost" id="open-edit">Find a movie</button>
            <button class="ghost" id="open-providers">Where you watch</button>
            <button class="ghost" id="open-export">Share</button>
          </div>
          <button class="flipbtn" id="to-rec">Get recommendations<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M13 5l7 7-7 7"/><path d="M20 12H4"/></svg></button>
        </div>
        <div class="tally" id="tally" style="text-align:left;margin:-2px 0 12px"></div>
        <div id="card"><div class="msg">Loading.</div></div>
        <div class="cardfoot"><button class="undo" id="undo"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:5px"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-2"/></svg>Undo last action</button></div>
      </div>
      </div>

      <div class="face back" id="face-back">
      <div class="win" id="win-rec">
        <div class="win-head">
          <div class="hd-left">
            <span class="wnum">2</span><div class="win-title">Recommendations</div>
            <span class="win-sub">from</span>
            <select class="prof-select" id="profile-select"><option value="you">Your taste</option></select>
            <span class="win-sub" id="using-label">using</span>
            <button class="ghost" id="open-model">Model</button>
            <button class="ghost" id="open-friends">Friends</button>
          </div>
          <button class="flipbtn teal" id="to-rate"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5l-7 7 7 7"/><path d="M4 12h16"/></svg>Rate movies</button>
        </div>
        <div class="explore-row">
          <div class="seg" id="explore-seg">
            <button data-x="0">Safe</button>
            <button data-x="0.45" class="on">Balanced</button>
            <button data-x="0.85">Exploratory</button>
          </div>
          <span class="tip" data-tip="Safe shows the highest-confidence picks. Balanced mixes in some variety. Exploratory leans into less-mainstream surprises.">i</span>
          <span class="win-sub" id="rec-meta"></span>
        </div>
        <div id="rec-body"></div>
        <div class="cardfoot"><button class="undo" id="rec-undo"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:5px"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-2"/></svg>Undo last action</button></div>
        <div class="rec-foot"><button class="refresh-btn" id="do-recs"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v5h-5"/></svg>Next recommendation</button></div>
      </div>
      </div>

     </div>
    </div>
  </div>

  <div class="overlay" id="overlay">
    <div class="panel" id="panel">
      <div class="phead"><h2>Channels</h2><button class="x" id="close-settings">&times;</button></div>
      <p class="desc">Turn pools on or off, or add your own. A channel matches movies in <b>all</b> chosen genres, narrowed by any keywords.</p>
      <div id="ch-list"></div>
      <div class="addbox">
        <h3>Add a channel</h3>
        <div class="fld"><label>Name (optional)</label><input id="ch-name" placeholder="e.g. First-love stories"></div>
        <div class="fld"><label>Keywords (comma-separated, optional)</label><input id="ch-kw" placeholder="coming-of-age, first love"></div>
        <div class="fld"><label>Genres (optional)</label><div class="gpick" id="ch-genres"></div></div>
        <div class="fld"><label>Style</label>
          <div class="seg" id="ch-style">
            <button data-s="popular" class="on">Popular</button>
            <button data-s="acclaimed">Acclaimed</button>
            <button data-s="gems">Hidden gems</button>
          </div>
        </div>
        <button class="pbtn primary" id="ch-add" style="width:100%">Add channel</button>
        <div class="note" id="ch-note"></div>
      </div>
      <div class="prow">
        <button class="pbtn cancel" id="ch-cancel">Cancel</button>
        <button class="pbtn primary" id="ch-save">Save &amp; close</button>
      </div>
    </div>
  </div>

  <div class="overlay" id="edit-overlay">
    <div class="panel" id="edit-panel">
      <div class="phead"><h2>Find a movie</h2><button class="x" id="close-edit">&times;</button></div>
      <p class="desc">Search TMDb or your own library. Add a new film with a status, or change the status of one you've already logged.</p>
      <div class="fld"><input id="edit-q" placeholder="Search by title…" autocomplete="off"></div>
      <div id="edit-results"></div>
    </div>
  </div>

  <div class="overlay" id="model-overlay">
    <div class="panel" id="model-panel">
      <div class="phead"><h2>Model</h2><button class="x" id="close-model">&times;</button></div>
      <p class="desc">Train a model from your ratings to sharpen recommendations. You rarely need this — once after rating a batch is plenty, and it's quick after the first run.</p>
      <div id="train-hint" class="ml-msg"></div>
      <button class="primarybtn" id="do-train">Train now</button>
      <div id="train-out"></div>
    </div>
  </div>

  <div class="overlay" id="export-overlay">
    <div class="panel" id="export-panel">
      <div class="phead"><h2>Share your library</h2><button class="x" id="close-export">&times;</button></div>
      <p class="desc">Export the films you've <b>liked</b> to a small file you can send a friend. They import it under Recommendations → <b>Friends</b> to get recommended what you loved.</p>
      <div class="fld"><label>Your name (optional — shown to friends)</label><input id="share-name" placeholder="e.g. Vlad" autocomplete="off"></div>
      <button class="primarybtn" id="do-export">Export my likes</button>
      <span class="note" id="export-note"></span>
    </div>
  </div>

  <div class="overlay" id="friends-overlay">
    <div class="panel" id="friends-panel">
      <div class="phead"><h2>Friends</h2><button class="x" id="close-friends">&times;</button></div>
      <p class="desc">Import a friend's exported likes — <b>one database per friend</b>. Then switch whose taste you recommend from with the <b>“from”</b> menu in this window's header.</p>
      <div class="fld"><label>Import a friend's likes (.json)</label><input type="file" id="friend-file" accept=".json,application/json"></div>
      <div class="note" id="friend-import-note"></div>
      <div id="friends-list" style="margin-top:10px"></div>
    </div>
  </div>

  <div class="overlay" id="providers-overlay">
    <div class="panel" id="providers-panel">
      <div class="phead"><h2>Where you watch</h2><button class="x" id="close-providers">&times;</button></div>
      <p class="desc">Pick your country and the streaming services you have. New films you <b>discover</b> — in both the rater and the recommender — are then limited to titles you can actually stream. Leave it off to see everything.</p>
      <div class="fld" style="max-width:240px"><label>Country (2-letter code)</label><input id="prov-region" placeholder="e.g. AT, US, GB" autocomplete="off" maxlength="2" style="text-transform:uppercase"></div>
      <div class="note" id="prov-note">Enter a country code to load its services.</div>
      <div class="prov-grid" id="prov-grid"></div>
      <div class="prow">
        <button class="pbtn cancel" id="prov-clear">Turn off filter</button>
        <button class="pbtn primary" id="prov-save">Save &amp; close</button>
      </div>
    </div>
  </div>

<script>
const TOKEN='__CW_TOKEN__';
const _origFetch = window.fetch.bind(window);
window.fetch = (u, o)=>{ o = o || {}; if(typeof u === 'string' && u.indexOf('/api') === 0){ o.headers = Object.assign({}, o.headers || {}, {'X-Token': TOKEN}); } return _origFetch(u, o); };
let current = null, busy = false, settingsOpen = false, editOpen = false, editTimer = null, modelOpen = false, exportOpen = false, friendsOpen = false, providersOpen = false;
const STYLES = {
  popular:   {sort:"popularity.desc",   vote_count_gte:60,  vote_avg_gte:6.0},
  acclaimed: {sort:"vote_average.desc",  vote_count_gte:100, vote_avg_gte:7.0},
  gems:      {sort:"vote_average.desc",  vote_count_gte:30,  vote_avg_gte:6.5}
};
let chDraft = [];      // working copy of channels
let genreList = [];    // [{id,name}]
let addSel = new Set();
let addStyle = "popular";

function esc(s){ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function escAttr(s){ return String(s==null?'':s).replace(/"/g,'%22'); }
function tip(t){ return '<span class="tip" data-tip="'+t+'">i</span>'; }
const MODE_TIP='Trained model = learned from your ratings. Similarity = cold-start ranking by closeness to films you liked, used until you have enough ratings.';
const AUC_TIP='ROC-AUC: the chance the model ranks a film you would like above one you would not. 0.5 is a coin flip, 1.0 is perfect.';
const AP_TIP='Average precision: how well the very top-scored films are ones you would actually like. Rewards putting good picks first.';
const DRV_TIP='The features that most push a film up (toward like) or down, learned from your ratings.';
const SCORE_TIP='How well this film matches your taste. With a trained model it is your estimated chance of liking it; otherwise it is a relative match within this batch.';

function setTally(s){
  const el = document.getElementById('tally');
  if(s){
    el.innerHTML =
      'Liked <b>'+s['3']+'</b> &middot; Indifferent <b>'+s['2']+'</b> &middot; ' +
      'Disliked <b>'+s['1']+'</b> &middot; Not seen <b>'+s['0']+'</b> &middot; ' +
      'Watchlist <b>'+s['watch']+'</b>';
  } else { el.innerHTML=''; }
  updateTrainHint(s);
}
function updateTrainHint(s){
  const el = document.getElementById('train-hint'); if(!el || !s) return;
  const nl = (s['1']||0) + (s['2']||0); const target = 20;
  if(nl >= target){
    el.innerHTML = 'You have <b>'+nl+'</b> “not-liked” ratings — enough for the model to learn what you avoid. Train away.';
    return;
  }
  const pct = Math.round(nl/target*100);
  el.innerHTML = 'The model sharpens as you rate films you didn’t love. You have <b>'+nl+'</b> '+
    'Indifferent/Disliked — about <b>'+(target-nl)+'</b> more unlocks a reliable model.'+
    '<div style="height:6px;border-radius:4px;background:var(--gray-tint);margin-top:8px;overflow:hidden">'+
    '<div style="height:100%;width:'+pct+'%;background:var(--teal)"></div></div>';
}

function render(d){
  if(d && d.stats) setTally(d.stats);
  const card = document.getElementById('card');
  if(d && d.error === 'auth'){
    card.innerHTML='<div class="msg">No valid TMDb API key found.<br><br>'+
      'Put your key in a file named <code>tmdb_key.txt</code> next to the script, '+
      'or run <code>export TMDB_API_KEY=your_key</code>, then restart.</div>';
    current=null; return;
  }
  if(d && d.error === 'nochannels'){
    card.innerHTML='<div class="msg">No channels are enabled.<br>Open <b>Channels</b> and switch at least one on.</div>';
    current=null; return;
  }
  if(!d || !d.movie){
    card.innerHTML='<div class="msg">No more candidates right now.<br>Try again in a moment, or widen your channels.</div>';
    current=null; return;
  }
  const m = d.movie; current = m;
  const chips = m.genres.split(',').map(g=>g.trim()).filter(Boolean)
                 .map(g=>'<span class="chip">'+esc(g)+'</span>').join('');
  const dir = m.director ? (' &middot; Directed by '+esc(m.director)) : '';
  card.innerHTML =
   '<div class="row">'+
     '<img class="poster" src="'+escAttr(m.poster)+'" alt="poster" onerror="this.style.visibility=\'hidden\'">'+
     '<div class="meta">'+
       (m.channel?'<span class="srcchip">from '+esc(m.channel)+'</span>':'')+
       '<h1>'+esc(m.title)+'</h1>'+
       '<div class="sub">'+(m.year||'')+dir+' &middot; rated '+m.rating+' on TMDb</div>'+
       '<div class="chips">'+chips+'</div>'+
       '<div class="overview">'+(esc(m.overview)||'<i>No synopsis available.</i>')+'</div>'+
       '<a class="link" href="'+escAttr(m.link)+'" target="_blank" rel="noopener">View details on TMDb</a>'+
     '</div>'+
   '</div>'+
   '<div class="rate-row">'+
     '<button class="btn like"    onclick="rate(3)">Liked</button>'+
     '<button class="btn meh"     onclick="rate(2)">Indifferent</button>'+
     '<button class="btn dislike" onclick="rate(1)">Disliked</button>'+
   '</div>'+
   '<div class="second-row">'+
     '<button class="btn skip"  onclick="rate(0)">Not seen</button>'+
     '<button class="btn watch" onclick="watch()">Add to Watchlist</button>'+
   '</div>';
}

async function loadNext(){
  busy=true;
  const r = await fetch('/api/next'); const d = await r.json();
  render(d); busy=false;
}
async function rate(v){
  if(busy||!current||settingsOpen) return; busy=true;
  await fetch('/api/rate',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({rating:v, movie:current})});
  await loadNext();
}
async function watch(){
  if(busy||!current||settingsOpen) return; busy=true;
  await fetch('/api/watchlist',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({movie:current})});
  await loadNext();
}
async function undo(){
  if(busy||settingsOpen) return; busy=true;
  const r = await fetch('/api/undo',{method:'POST'}); const d = await r.json();
  render(d); busy=false;
}

/* ---- Channels panel ---- */
function genreName(id){ const g=genreList.find(x=>x.id===id); return g?g.name:('#'+id); }

function chSummary(c){
  const gs = (c.genres||[]).map(genreName).join(', ');
  const kw = (c.keywords||[]).join(', ');
  const parts = [];
  if(gs) parts.push(gs);
  if(kw) parts.push('"'+kw+'"');
  parts.push((c.style||'custom'));
  return parts.join(' · ');
}

function renderChannels(){
  const list = document.getElementById('ch-list');
  list.innerHTML = chDraft.map((c,i)=>
    '<div class="ch">'+
      '<input type="checkbox" '+(c.enabled?'checked':'')+' onchange="chToggle('+i+',this.checked)">'+
      '<div class="info"><div class="cn">'+esc(c.name)+'</div>'+
        '<div class="cs">'+esc(chSummary(c))+'</div></div>'+
      '<button class="del" onclick="chDelete('+i+')">Remove</button>'+
    '</div>').join('') || '<div class="note">No channels yet — add one below.</div>';
}
function chToggle(i,on){ chDraft[i].enabled = on; }
function chDelete(i){ chDraft.splice(i,1); renderChannels(); }

function renderGenrePicker(){
  const wrap = document.getElementById('ch-genres');
  if(!genreList.length){ wrap.innerHTML='<div class="note">Genre list needs a TMDb key. Keyword-only channels still work.</div>'; return; }
  wrap.innerHTML = genreList.map(g=>
    '<span class="gchip'+(addSel.has(g.id)?' on':'')+'" onclick="gToggle('+g.id+',this)">'+esc(g.name)+'</span>').join('');
}
function gToggle(id,el){ if(addSel.has(id)){addSel.delete(id);el.classList.remove('on');} else {addSel.add(id);el.classList.add('on');} }

function chAdd(){
  const name = document.getElementById('ch-name').value.trim();
  const kw = document.getElementById('ch-kw').value.split(',').map(s=>s.trim()).filter(Boolean);
  const genres = [...addSel];
  if(!kw.length && !genres.length){ document.getElementById('ch-note').textContent='Add at least one keyword or genre.'; return; }
  const preset = STYLES[addStyle];
  const auto = genres.map(genreName).concat(kw).slice(0,3).join(' / ') || 'Channel';
  chDraft.push({name:name||auto, enabled:true, genres, keywords:kw, style:addStyle,
                sort:preset.sort, vote_count_gte:preset.vote_count_gte, vote_avg_gte:preset.vote_avg_gte});
  document.getElementById('ch-name').value='';
  document.getElementById('ch-kw').value='';
  document.getElementById('ch-note').textContent='';
  addSel.clear(); renderGenrePicker(); renderChannels();
}

async function openSettings(){
  const r = await fetch('/api/channels'); const d = await r.json();
  chDraft = JSON.parse(JSON.stringify(d.channels||[]));
  genreList = d.genres||[];
  addSel.clear(); addStyle='popular';
  document.querySelectorAll('#ch-style button').forEach(b=>b.classList.toggle('on', b.dataset.s==='popular'));
  renderChannels(); renderGenrePicker();
  document.getElementById('overlay').classList.add('open'); settingsOpen=true;
}
function closeSettings(){ document.getElementById('overlay').classList.remove('open'); settingsOpen=false; }
async function saveChannels(){
  await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({channels:chDraft})});
  closeSettings(); await loadNext();
}

document.getElementById('open-settings').onclick = openSettings;
document.getElementById('close-settings').onclick = closeSettings;
document.getElementById('ch-cancel').onclick = closeSettings;
document.getElementById('ch-save').onclick = saveChannels;
document.getElementById('ch-add').onclick = chAdd;
document.getElementById('overlay').onclick = e=>{ if(e.target.id==='overlay') closeSettings(); };
document.querySelectorAll('#ch-style button').forEach(b=>{
  b.onclick = ()=>{ addStyle=b.dataset.s; document.querySelectorAll('#ch-style button').forEach(x=>x.classList.toggle('on',x===b)); };
});
document.getElementById('undo').onclick = undo;

document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ if(settingsOpen) closeSettings(); if(editOpen) closeEdit(); if(modelOpen) closeModel(); if(exportOpen) closeExport(); if(friendsOpen) closeFriends(); if(providersOpen) closeProviders(); }
});

/* ---- Model (train) panel ---- */
function openModel(){ document.getElementById('model-overlay').classList.add('open'); modelOpen = true; }
function closeModel(){ document.getElementById('model-overlay').classList.remove('open'); modelOpen = false; }
document.getElementById('open-model').onclick = openModel;
document.getElementById('close-model').onclick = closeModel;
document.getElementById('model-overlay').onclick = e=>{ if(e.target.id==='model-overlay') closeModel(); };
document.getElementById('do-train').onclick = ()=> runTrain('/api/train');
async function runTrain(url){
  const out = document.getElementById('train-out');
  out.innerHTML = '<div class="ml-msg">Working… the first run fetches TMDb metadata for your films, which can take up to a minute.</div>';
  let d; try { d = await (await fetch(url,{method:'POST'})).json(); }
  catch(e){ out.innerHTML = '<div class="ml-msg">Request failed.</div>'; return; }
  if(!d.ok){ out.innerHTML = '<div class="ml-msg">'+esc(d.error||'Failed.')+'</div>'; return; }
  const r = d.data; let h='';
  h += '<div class="stat">Mode: <b>'+esc(r.mode)+'</b>'+tip(MODE_TIP)+' &middot; '+r.n_pos+' liked / '+r.n_neg+' not-liked</div>';
  if(r.cv){ h += '<div class="stat">'+r.cv.k+'-fold CV — ROC-AUC <b>'+r.cv.auc.toFixed(3)+'</b>'+tip(AUC_TIP)+' &middot; AP <b>'+r.cv.ap.toFixed(3)+'</b>'+tip(AP_TIP)+'</div>'; }
  if(r.message){ h += '<div class="ml-msg">'+esc(r.message)+'</div>'; }
  if(r.drivers && r.drivers.length){
    const mx = Math.max.apply(null, r.drivers.map(x=>Math.abs(x[1]))) || 1;
    h += '<div class="stat" style="margin-top:12px">Taste drivers'+tip(DRV_TIP)+' <span class="rs">(green = toward like, coral = away)</span></div>';
    r.drivers.forEach(x=>{ const w = Math.round(Math.abs(x[1])/mx*150);
      h += '<div class="drv"><div class="nm">'+esc(x[0])+'</div><div class="bar'+(x[1]<0?' neg':'')+'" style="width:'+w+'px"></div></div>'; });
  }
  h += '<div style="display:flex;align-items:center;gap:4px;margin-top:16px">'+
       '<button class="pbtn cancel" id="rebuild-cache">Rebuild metadata cache</button>'+
       tip('Rarely needed. Deletes the cached TMDb data and re-fetches everything on the next train — can be slow for a big library. Use only if some movie details look wrong.')+'</div>';
  out.innerHTML = h;
  document.getElementById('rebuild-cache').onclick = ()=> runTrain('/api/rebuild-cache');
}

/* ---- Recommendations (inline window, one movie at a time) ---- */
let recBatch = [], recIdx = -1, recCurrent = null, recDirty = true, recBusy = false, recMeta = {}, recProfile = "you";
function exploreVal(){ const b=document.querySelector('#explore-seg button.on'); return b ? parseFloat(b.dataset.x) : 0.45; }
async function fetchRecBatch(){
  const d = await (await fetch('/api/recommend',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({n:18, explore:exploreVal(), profile:recProfile})})).json();
  if(!d.ok) throw new Error(d.error||'Failed.');
  recMeta = {mode:d.data.mode, source:d.data.candidate_source, message:d.data.message};
  return d.data.recommendations || [];
}
function renderRecMovie(m){
  const b = document.getElementById('rec-body');
  const chips = (m.genres||'').split(',').map(g=>g.trim()).filter(Boolean).map(g=>'<span class="chip">'+esc(g)+'</span>').join('');
  const dir = m.director ? (' &middot; Directed by '+esc(m.director)) : '';
  const score = (m.match!=null ? (' &middot; '+m.match+'% match') : '');
  b.innerHTML =
   '<div class="row">'+
     (m.poster ? '<img class="poster" src="'+escAttr(m.poster)+'" alt="poster" onerror="this.style.visibility=\'hidden\'">' : '<div class="poster"></div>')+
     '<div class="meta">'+
       '<h1>'+esc(m.title)+'</h1>'+
       '<div class="sub">'+(m.year||'')+dir+score+tip(SCORE_TIP)+'</div>'+
       '<div class="chips">'+chips+'</div>'+
       (m.why ? '<div class="rw" style="margin:0 0 8px">Why this pick: '+esc(m.why)+'</div>' : '')+
       '<div class="overview">'+(esc(m.overview)||'<i>No synopsis available.</i>')+'</div>'+
       '<a class="link" href="'+escAttr(m.link)+'" target="_blank" rel="noopener">View details on TMDb</a>'+
     '</div>'+
   '</div>'+
   '<div class="rate-row">'+
     '<button class="btn like"    onclick="recRate(3)">Liked</button>'+
     '<button class="btn meh"     onclick="recRate(2)">Indifferent</button>'+
     '<button class="btn dislike" onclick="recRate(1)">Disliked</button>'+
   '</div>'+
   '<div class="second-row">'+
     '<button class="btn skip"  onclick="recNotInterested()">Not interested</button>'+
     '<button class="btn watch" onclick="recWatch()">Add to Watchlist</button>'+
   '</div>';
}
async function advanceRec(){
  const b = document.getElementById('rec-body');
  try {
    if(recDirty || recIdx + 1 >= recBatch.length){
      b.innerHTML = '<div class="loading"><div class="dots"><span></span><span></span><span></span></div>'+
        '<div>Finding films matched to your taste…</div>'+
        '<div style="font-size:11.5px;margin-top:5px;opacity:.7">the first run is a little slower</div></div>';
      recBatch = await fetchRecBatch(); recDirty = false; recIdx = -1;
      document.getElementById('rec-meta').textContent =
        (recMeta.mode||'') + ' · from ' + (recMeta.source||'') + (recMeta.message ? (' · '+recMeta.message) : '');
    }
  } catch(e){ b.innerHTML = '<div class="loading">'+esc(e.message||'Could not load.')+'</div>'; return; }
  recIdx++;
  if(!recBatch.length || recIdx >= recBatch.length){
    b.innerHTML = '<div class="loading">No recommendations right now — adjust your channels or rate a few more.</div>';
    recCurrent = null; return;
  }
  recCurrent = recBatch[recIdx];
  renderRecMovie(recCurrent);
}
async function nextRec(){ if(recBusy) return; recBusy = true; await advanceRec(); recBusy = false; }
async function recAct(url, body, type){
  if(recBusy || !recCurrent) return; recBusy = true;
  const m = recCurrent, idx = recIdx;   // capture before we advance
  try {
    const res = await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(res.stats) setTally(res.stats);
    if(type){ recUndo = {type:type, movie:m, idx:idx}; }
  } catch(e){}
  await advanceRec(); recBusy = false;
}
function recRate(v){ recAct('/api/rate', {rating:v, movie:recCurrent, stack:false}, 'rate'); }
function recWatch(){ recAct('/api/watchlist', {movie:recCurrent, stack:false}, 'watch'); }
function recNotInterested(){ recAct('/api/not-interested', {movie:recCurrent}, 'not-interested'); }

/* ---- recommender undo (mirrors the rater's "Undo last action") ---- */
let recUndo = null;   // {type, movie, idx} — the last action taken here
async function recUndoAction(){
  if(recBusy || !recUndo) return; recBusy = true;
  const u = recUndo;
  try {
    const res = await (await fetch('/api/rec-undo',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:u.type, movie:u.movie})})).json();
    if(res.stats) setTally(res.stats);
  } catch(e){}
  recUndo = null;
  if(recBatch[u.idx] && recBatch[u.idx].id === u.movie.id) recIdx = u.idx;  // step back to it
  recCurrent = u.movie; renderRecMovie(u.movie);
  recBusy = false;
}

document.getElementById('do-recs').onclick = nextRec;
document.getElementById('rec-undo').onclick = recUndoAction;
document.querySelectorAll('#explore-seg button').forEach(b=>{
  b.onclick = ()=>{ document.querySelectorAll('#explore-seg button').forEach(x=>x.classList.toggle('on', x===b)); recDirty = true; };
});

/* ---- tooltips (tap to toggle, for touch) ---- */
document.addEventListener('click', e=>{
  const t = e.target.closest('.tip');
  document.querySelectorAll('.tip.show').forEach(el=>{ if(el!==t) el.classList.remove('show'); });
  if(t){ t.classList.toggle('show'); e.stopPropagation(); }
});

/* ---- Edit library ---- */
const STAT = [['3','Liked'],['2','Indifferent'],['1','Disliked'],['0','Not seen'],['watchlist','Watchlist'],['not-interested','Not interested']];
const STAT_LABEL = {}; STAT.forEach(s=>{ STAT_LABEL[s[0]] = s[1]; });
function openEdit(){
  document.getElementById('edit-overlay').classList.add('open'); editOpen = true;
  const q = document.getElementById('edit-q'); q.value = '';
  document.getElementById('edit-results').innerHTML = '';
  setTimeout(()=>q.focus(), 50);
}
function closeEdit(){ document.getElementById('edit-overlay').classList.remove('open'); editOpen = false; }
function findLine(m){
  const g = m.genres ? (esc(m.genres) + ' &middot; ') : '';
  return g + (m.in_library ? 'Already in your library' : 'not in your library');
}
async function doEditSearch(){
  const q = document.getElementById('edit-q').value.trim();
  const box = document.getElementById('edit-results');
  if(!q){ box.innerHTML = ''; return; }
  box.innerHTML = '<div class="note">Searching…</div>';
  let d; try { d = await (await fetch('/api/find?q='+encodeURIComponent(q))).json(); }
  catch(e){ box.innerHTML = '<div class="note">Search failed.</div>'; return; }
  const list = d.results || [];
  if(!list.length){ box.innerHTML = '<div class="note">No matches found.</div>'; return; }
  box.innerHTML = list.map(m=>{
    const isNew = !m.in_library;
    const opts = (isNew ? '<option value="" disabled selected>Set status…</option>' : '') +
      STAT.map(s=>'<option value="'+s[0]+'"'+((!isNew && s[0]===m.status)?' selected':'')+'>'+s[1]+'</option>').join('');
    return '<div class="er" data-id="'+m.id+'"><div class="ei">'+
             '<div class="et">'+esc(m.title)+' <span class="es">('+esc(String(m.year||''))+')</span></div>'+
             '<div class="estat">'+findLine(m)+'</div>'+
           '</div>'+
           (m.link ? '<a class="er-link" href="'+escAttr(m.link)+'" target="_blank" rel="noopener">TMDb</a>' : '')+
           '<select>'+opts+'</select></div>';
  }).join('');
  list.forEach(m=>{
    const row = box.querySelector('.er[data-id="'+m.id+'"]'); if(!row) return;
    const sel = row.querySelector('select');
    sel.onchange = async ()=>{
      if(!sel.value) return;
      const res = await (await fetch('/api/setstatus',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({movie:{id:m.id,title:m.title,year:m.year,genres:m.genres,link:m.link}, status:sel.value})})).json();
      if(res.stats) setTally(res.stats);
      m.status = sel.value; m.in_library = true;
      row.querySelector('.estat').innerHTML = findLine(m);
    };
  });
}
document.getElementById('open-edit').onclick = openEdit;
document.getElementById('close-edit').onclick = closeEdit;
document.getElementById('edit-overlay').onclick = e=>{ if(e.target.id==='edit-overlay') closeEdit(); };
document.getElementById('edit-q').oninput = ()=>{ clearTimeout(editTimer); editTimer = setTimeout(doEditSearch, 250); };

/* ---- Export your library (rater) ---- */
function openExport(){ document.getElementById('export-overlay').classList.add('open'); exportOpen = true; }
function closeExport(){ document.getElementById('export-overlay').classList.remove('open'); exportOpen = false; }
async function doExport(){
  const note = document.getElementById('export-note');
  let d; try { d = await (await fetch('/api/likes')).json(); } catch(e){ note.textContent = ' Export failed.'; return; }
  const name = document.getElementById('share-name').value.trim();
  const payload = {app:'movie-rater', name:name, exported:new Date().toISOString().slice(0,10), likes:(d.likes||[])};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
  const url = URL.createObjectURL(blob); const a = document.createElement('a');
  a.href = url; a.download = (name ? name.replace(/[^a-z0-9]+/gi,'-').toLowerCase()+'-' : '') + 'likes.json';
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  note.textContent = ' Exported '+(d.likes||[]).length+' liked films.';
}
document.getElementById('open-export').onclick = openExport;
document.getElementById('close-export').onclick = closeExport;
document.getElementById('export-overlay').onclick = e=>{ if(e.target.id==='export-overlay') closeExport(); };
document.getElementById('do-export').onclick = doExport;

/* ---- Friends (import / manage; one database per friend) ---- */
let friends = [];   // [{name, count}]
function openFriends(){ document.getElementById('friends-overlay').classList.add('open'); friendsOpen = true; refreshFriends(); }
function closeFriends(){ document.getElementById('friends-overlay').classList.remove('open'); friendsOpen = false; }
function renderFriendsList(){
  const box = document.getElementById('friends-list'); if(!box) return;
  if(!friends.length){ box.innerHTML = '<div class="note">No friends imported yet.</div>'; return; }
  box.innerHTML = friends.map((f,i)=>
    '<div class="friend-row" data-i="'+i+'"><div class="fn">'+esc(f.name)+'</div>'+
      '<div class="fc">'+f.count+' liked</div>'+
      '<button class="ghost frm">Remove</button></div>').join('');
  box.querySelectorAll('.friend-row').forEach(row=>{
    const i = parseInt(row.dataset.i,10);
    row.querySelector('.frm').onclick = ()=> removeFriend(friends[i].name);
  });
}
function updateModelVisibility(){
  // "Model" trains on YOUR ratings, so it only applies to your taste — hide it for a friend.
  const friend = recProfile !== 'you';
  const ul = document.getElementById('using-label'); const mb = document.getElementById('open-model');
  if(ul) ul.style.display = friend ? 'none' : '';
  if(mb) mb.style.display = friend ? 'none' : '';
}
function populateProfileSelect(){
  const sel = document.getElementById('profile-select'); if(!sel) return;
  sel.innerHTML = '';
  const you = document.createElement('option'); you.value='you'; you.textContent='Your taste'; sel.appendChild(you);
  friends.forEach(f=>{ const o=document.createElement('option'); o.value=f.name; o.textContent=f.name; sel.appendChild(o); });
  if(recProfile!=='you' && !friends.some(f=>f.name===recProfile)){
    recProfile='you'; recDirty=true; try{ localStorage.setItem('recProfile','you'); }catch(e){}
  }
  sel.value = recProfile;
  updateModelVisibility();
}
async function refreshFriends(){
  try { friends = (await (await fetch('/api/friends')).json()).friends || []; } catch(e){ friends = []; }
  renderFriendsList(); populateProfileSelect();
}
function doImport(ev){
  const file = ev.target.files && ev.target.files[0]; if(!file) return;
  const note = document.getElementById('friend-import-note'); note.textContent = 'Importing…';
  const reader = new FileReader();
  reader.onload = async ()=>{
    let data; try { data = JSON.parse(reader.result); } catch(e){ note.textContent = 'That file is not valid JSON.'; return; }
    try {
      const res = await (await fetch('/api/import-friend',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:data.name||'', likes:data.likes||[]})})).json();
      friends = res.friends || []; renderFriendsList(); populateProfileSelect();
      note.textContent = 'Imported '+(res.name||'a friend')+' ('+(res.count||0)+' liked films). Pick them in the “from” menu.';
    } catch(e){ note.textContent = 'Import failed.'; }
  };
  reader.readAsText(file); ev.target.value = '';
}
async function removeFriend(name){
  try {
    const res = await (await fetch('/api/remove-friend',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name})})).json();
    friends = res.friends || [];
  } catch(e){}
  renderFriendsList(); populateProfileSelect();
}
document.getElementById('open-friends').onclick = openFriends;
document.getElementById('close-friends').onclick = closeFriends;
document.getElementById('friends-overlay').onclick = e=>{ if(e.target.id==='friends-overlay') closeFriends(); };
document.getElementById('friend-file').onchange = doImport;
document.getElementById('profile-select').onchange = (e)=>{
  recProfile = e.target.value || 'you'; recDirty = true;
  try { localStorage.setItem('recProfile', recProfile); } catch(e){}
  updateModelVisibility();
  nextRec();
};

/* ---- Where you watch (streaming providers) ---- */
let provSel = new Set(), provRegion = '';
function setProvNote(){
  const note = document.getElementById('prov-note');
  note.textContent = provSel.size ? (provSel.size+' selected — only these will be shown.') : 'Tap the services you subscribe to.';
}
function openProviders(){ document.getElementById('providers-overlay').classList.add('open'); providersOpen = true; loadProviders(); }
function closeProviders(){ document.getElementById('providers-overlay').classList.remove('open'); providersOpen = false; }
async function loadProviders(region){
  const note = document.getElementById('prov-note'), grid = document.getElementById('prov-grid');
  let url = '/api/providers'; if(region!=null) url += '?region='+encodeURIComponent(region);
  let d; try { d = await (await fetch(url)).json(); } catch(e){ note.textContent='Could not load services.'; return; }
  if(region==null){
    provRegion = (d.prefs && d.prefs.region) || '';
    provSel = new Set((d.prefs && d.prefs.providers) || []);
    document.getElementById('prov-region').value = provRegion;
  } else {
    provRegion = (region||'').toUpperCase();
  }
  const avail = d.available || [];
  if(!provRegion){ note.textContent='Enter a country code to load its services.'; grid.innerHTML=''; return; }
  if(!avail.length){ note.textContent='No services found for “'+esc(provRegion)+'” — check the code (a TMDb key is needed).'; grid.innerHTML=''; return; }
  setProvNote();
  grid.innerHTML = avail.map(p=>
    '<div class="prov'+(provSel.has(p.id)?' on':'')+'" data-id="'+p.id+'">'+
      (p.logo?'<img src="'+escAttr(p.logo)+'" alt="">':'<span style="width:22px;height:22px;border-radius:6px;background:var(--gray-tint)"></span>')+
      esc(p.name)+'</div>').join('');
  grid.querySelectorAll('.prov').forEach(el=>{
    el.onclick = ()=>{ const id=parseInt(el.dataset.id,10);
      if(provSel.has(id)){ provSel.delete(id); el.classList.remove('on'); } else { provSel.add(id); el.classList.add('on'); }
      setProvNote();
    };
  });
}
async function saveProviders(region, providers){
  await fetch('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({region:region, providers:providers})});
  recDirty = true;
}
document.getElementById('open-providers').onclick = openProviders;
document.getElementById('close-providers').onclick = closeProviders;
document.getElementById('providers-overlay').onclick = e=>{ if(e.target.id==='providers-overlay') closeProviders(); };
document.getElementById('prov-region').oninput = (e)=>{
  const v = e.target.value.trim().toUpperCase();
  if(v.length===2) loadProviders(v);
};
document.getElementById('prov-save').onclick = async ()=>{
  await saveProviders(provRegion, [...provSel]);
  closeProviders(); await loadNext();
};
document.getElementById('prov-clear').onclick = async ()=>{
  provSel = new Set();
  await saveProviders('', []);
  closeProviders(); await loadNext();
};

/* ---- Flip card (front = rater, back = recommender) ---- */
const flipwrap = document.getElementById('flipwrap'), flipEl = document.getElementById('flip');
const faceFront = document.getElementById('face-front'), faceBack = document.getElementById('face-back');
function activeFace(){ return flipEl.classList.contains('flipped') ? faceBack : faceFront; }
function sizeFlip(){ if(flipwrap) flipwrap.style.height = activeFace().offsetHeight + 'px'; }
function flipTo(toBack){ flipEl.classList.toggle('flipped', toBack); sizeFlip(); }
document.getElementById('to-rec').onclick = ()=> flipTo(true);
document.getElementById('to-rate').onclick = ()=> flipTo(false);
if(window.ResizeObserver){
  const ro = new ResizeObserver(()=> sizeFlip());
  ro.observe(faceFront); ro.observe(faceBack);
}
window.addEventListener('resize', sizeFlip);
setTimeout(sizeFlip, 60);

loadNext();
(async function initRecommender(){
  try { recProfile = localStorage.getItem('recProfile') || 'you'; } catch(e){}
  await refreshFriends();   // populates the profile dropdown; resets to 'you' if that friend is gone
  const sel = document.getElementById('profile-select'); if(sel) sel.value = recProfile;
  nextRec();                // auto-produce the first batch on startup
})();
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.replace("__CW_TOKEN__", TOKEN), "text/html; charset=utf-8")
            return
        if self.headers.get("X-Token") != TOKEN:
            self._send(403, json.dumps({"error": "forbidden"}))
            return
        if self.path.startswith("/api/next"):
            m = next_candidate()
            if isinstance(m, dict) and m.get("error"):
                payload = {"error": m["error"], "stats": stats()}
            else:
                if m:
                    m["director"] = fetch_director(m["id"])
                payload = {"movie": m, "stats": stats()}
            self._send(200, json.dumps(payload))
        elif self.path.startswith("/api/channels"):
            gm = genre_map()
            genres = sorted(({"id": k, "name": v} for k, v in gm.items()), key=lambda x: x["name"])
            self._send(200, json.dumps({"channels": load_channels(), "genres": genres}))
        elif self.path.startswith("/api/find"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, json.dumps({"results": find_movies(q)}))
        elif self.path.startswith("/api/likes"):
            self._send(200, json.dumps({"likes": liked_export()}))
        elif self.path.startswith("/api/friends"):
            self._send(200, json.dumps({"friends": friends_info()}))
        elif self.path.startswith("/api/providers"):
            from urllib.parse import urlparse, parse_qs
            prefs = load_providers()
            region = parse_qs(urlparse(self.path).query).get("region", [prefs["region"]])[0]
            self._send(200, json.dumps({"prefs": prefs, "region": (region or "").upper()[:2],
                                        "available": list_providers(region)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        global _last_shown
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if self.headers.get("X-Token") != TOKEN:
            self._send(403, json.dumps({"error": "forbidden"}))
            return

        if self.path.startswith("/api/rate"):
            try:
                body = json.loads(raw.decode("utf-8"))
                with _io_lock:
                    append_rating(body["movie"], body["rating"])
                    if body.get("stack", True):   # recommender manages its own undo
                        _action_stack.append({"type": "rate", "movie": body["movie"]})
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "stats": stats()}))

        elif self.path.startswith("/api/watchlist"):
            try:
                body = json.loads(raw.decode("utf-8"))
                with _io_lock:
                    append_watchlist(body["movie"])
                    if body.get("stack", True):   # recommender manages its own undo
                        _action_stack.append({"type": "watch", "movie": body["movie"]})
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "stats": stats()}))

        elif self.path.startswith("/api/not-interested"):
            try:
                body = json.loads(raw.decode("utf-8"))
                with _io_lock:
                    append_not_interested(body["movie"])
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "stats": stats()}))

        elif self.path.startswith("/api/rec-undo"):
            # reverse the last action taken in the recommender (rate / watch / not-interested)
            try:
                body = json.loads(raw.decode("utf-8"))
                mv = body.get("movie") or {}
                mid = int(mv["id"]); t = body.get("type")
                with _io_lock:
                    if t == "rate":
                        remove_row_by_id(MD_PATH, MD_ID_COL, mid)
                    elif t == "watch":
                        remove_row_by_id(WATCHLIST_PATH, WATCH_ID_COL, mid)
                    elif t == "not-interested":
                        remove_row_by_id(NOT_INTERESTED_PATH, NOT_INTERESTED_ID_COL, mid)
                for s in _rec_seen.values():   # let it surface again
                    s.discard(mid)
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "stats": stats()}))

        elif self.path.startswith("/api/setstatus"):
            try:
                body = json.loads(raw.decode("utf-8"))
                set_status(body["movie"], str(body["status"]))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "stats": stats()}))

        elif self.path.startswith("/api/rebuild-cache"):
            ok, data = run_recommender(["--train-only", "--rebuild-cache"])
            self._send(200, json.dumps({"ok": ok, "data": data} if ok else {"ok": False, "error": data}))

        elif self.path.startswith("/api/train"):
            ok, data = run_recommender(["--train-only"])
            self._send(200, json.dumps({"ok": ok, "data": data} if ok else {"ok": False, "error": data}))

        elif self.path.startswith("/api/recommend"):
            n, explore, profile, fresh = 18, 0.3, "you", True
            try:
                if raw:
                    body = json.loads(raw)
                    n = int(body.get("n", 18))
                    explore = float(body.get("explore", 0.3))
                    profile = str(body.get("profile") or "you")[:60]
                    fresh = bool(body.get("fresh", True))
            except Exception:
                pass
            seen = _rec_seen.setdefault(profile, set())
            args = ["--source", "discover", "--n", str(n), "--explore", str(explore),
                    "--profile", profile]
            if fresh and seen:
                args += ["--exclude", ",".join(str(i) for i in list(seen)[:2000])]
            ok, data = run_recommender(args)
            if ok and isinstance(data, dict):
                for r in data.get("recommendations", []):
                    if isinstance(r.get("id"), int):
                        seen.add(r["id"])
            self._send(200, json.dumps({"ok": ok, "data": data} if ok else {"ok": False, "error": data}))

        elif self.path.startswith("/api/import-friend"):
            try:
                info = save_friend(json.loads(raw.decode("utf-8")))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "name": info["name"],
                                        "count": info["count"], "friends": info["friends"]}))

        elif self.path.startswith("/api/remove-friend"):
            name = None
            try:
                if raw:
                    name = json.loads(raw.decode("utf-8")).get("name")
            except Exception:
                name = None
            friends = remove_friend(name)
            self._send(200, json.dumps({"ok": True, "friends": friends}))

        elif self.path.startswith("/api/providers"):
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                prefs = save_providers(body.get("region", ""), body.get("providers", []))
                with _buf_lock:
                    _buffer.clear()        # apply the new filter to the next card
                _rec_seen.clear()          # and to recommendations
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "prefs": prefs}))

        elif self.path.startswith("/api/channels"):
            try:
                body = json.loads(raw.decode("utf-8"))
                clean = [sanitize_channel(c) for c in body.get("channels", []) if isinstance(c, dict)]
                with _io_lock:
                    save_channels(clean)
                with _buf_lock:
                    _buffer.clear()
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "channels": clean}))

        elif self.path.startswith("/api/undo"):
            movie = None
            with _io_lock:
                if _action_stack:
                    act = _action_stack.pop()
                    mv = act["movie"]
                    if act["type"] == "watch":
                        remove_row_by_id(WATCHLIST_PATH, WATCH_ID_COL, mv["id"])
                    else:
                        remove_row_by_id(MD_PATH, MD_ID_COL, mv["id"])
                    _shown_session.discard(mv["id"])
                    _last_shown = mv
                    movie = mv
            self._send(200, json.dumps({"movie": movie, "stats": stats()}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    if not API_KEY:
        print("\n  Note: no TMDb API key found.")
        print("        Put it in tmdb_key.txt next to this script, or run:")
        print("          export TMDB_API_KEY=your_key_here")
        print("        Free key: https://www.themoviedb.org/settings/api")
        print("        Starting anyway so you can see the UI.\n")
    load_channels()  # ensure channels.json exists
    # one-time migration: dismissed.md -> not-interested.md (keeps your existing data)
    if not os.path.exists(NOT_INTERESTED_PATH) and os.path.exists(NOT_INTERESTED_LEGACY_PATH):
        try:
            os.replace(NOT_INTERESTED_LEGACY_PATH, NOT_INTERESTED_PATH)
        except Exception:
            pass
    ensure_file(MD_PATH, MOVIES_TEMPLATE)
    ensure_file(WATCHLIST_PATH, WATCHLIST_TEMPLATE)
    ensure_file(NOT_INTERESTED_PATH, NOT_INTERESTED_TEMPLATE)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"  Rating UI running at {url}")
    print("  (a browser tab should open; press Ctrl+C here to stop)\n")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Ratings are in movies.md, saved titles in watchlist.md.\n")
        server.shutdown()


if __name__ == "__main__":
    main()
