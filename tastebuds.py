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
POSTER_CACHE_PATH = os.path.join(SCRIPT_DIR, "poster_cache.json")   # cached poster paths (watchlist view)
FRIENDS_PATH = os.path.join(SCRIPT_DIR, "friends.json")   # multi-friend list [{name, likes}]
FRIEND_PATH = os.path.join(SCRIPT_DIR, "friend.json")     # legacy single-friend file (migrated on read)
PROVIDERS_PATH = os.path.join(SCRIPT_DIR, "providers.json")  # {region, providers:[ids]} streaming filter
KEY_PATH = os.path.join(SCRIPT_DIR, "tmdb_key.txt")          # where a pasted TMDb key is stored
WEIGHTS_PATH = os.path.join(SCRIPT_DIR, "model_weights.json")  # per-reaction training weights
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

# A fresh install starts with NO channels — the first-run onboarding (or the
# Channels panel) creates the user's own first channel, so nobody inherits a
# stranger's taste.


# --------------------------------------------------------------------------
# API key
# --------------------------------------------------------------------------
def load_api_key():
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


API_KEY = load_api_key()
TOKEN = secrets.token_hex(16)   # minted per run; the page embeds it and /api/* requires it


def _test_key(key):
    """Hit a cheap authenticated TMDb endpoint; raises on a bad key / no network."""
    url = TMDB_API + "/configuration?" + urllib.parse.urlencode({"api_key": key})
    req = urllib.request.Request(url, headers={"User-Agent": "tastebuds/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def save_api_key(key):
    """Validate a pasted TMDb key against the API, then persist it to tmdb_key.txt
    and activate it for this running process. Returns (ok, message)."""
    global API_KEY, _genre_map
    key = (key or "").strip()
    if not (10 <= len(key) <= 200):
        return False, "That doesn't look like a TMDb key."
    try:
        _test_key(key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "TMDb didn't accept that key — double-check you copied the v3 key."
        return False, "Couldn't reach TMDb just now. Check your connection and try again."
    except Exception:
        return False, "Couldn't reach TMDb just now. Check your connection and try again."
    try:
        _atomic_write(KEY_PATH, key + "\n")
    except Exception:
        return False, "Couldn't save the key file."
    API_KEY = key
    _genre_map = None   # re-fetch the genre list with the new key
    return True, "ok"


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
    # A channel is one of three kinds:
    #   "discover" - genres + keywords + style (the original; also covers Genre-only
    #                and Keyword-only channels). Legacy channels (no "kind") map here.
    #   "person"   - films a person was cast OR crew on (TMDb with_people).
    #   "movie"    - TMDb "more like this" neighbours of a seed film.
    kind = c.get("kind") if c.get("kind") in ("discover", "person", "movie") else "discover"
    style = c.get("style") if c.get("style") in STYLES else None
    preset = STYLES.get(style, {})
    out = {
        "kind": kind,
        "name": (str(c.get("name", "")).strip() or "Untitled")[:80],
        "enabled": bool(c.get("enabled", True)),
        "style": style or "custom",
        "vote_count_gte": _num(c.get("vote_count_gte", preset.get("vote_count_gte")), 40),
        "vote_avg_gte": _numf(c.get("vote_avg_gte", preset.get("vote_avg_gte")), 6.0),
        "sort": str(c.get("sort", preset.get("sort", "popularity.desc"))),
    }
    if kind == "person":
        out["person_id"] = _num(c.get("person_id"), 0)
        out["person_name"] = str(c.get("person_name", "")).strip()[:80]
    elif kind == "movie":
        out["movie_id"] = _num(c.get("movie_id"), 0)
        out["movie_title"] = str(c.get("movie_title", "")).strip()[:120]
    else:
        genres = c.get("genres", [])
        keywords = c.get("keywords", [])
        out["genres"] = [int(g) for g in genres if str(g).strip().isdigit()][:5] if isinstance(genres, list) else []
        out["keywords"] = [str(k).strip() for k in keywords if str(k).strip()][:6] if isinstance(keywords, list) else []
        # how multiple keywords combine: "any" = OR (wider), "all" = AND (narrow).
        # Default "all" preserves the behaviour of channels saved before this field existed.
        out["match"] = "any" if c.get("match") == "any" else "all"
    return out


def save_channels(channels):
    _atomic_write(CHANNELS_PATH, json.dumps(channels, indent=2, ensure_ascii=False))


def load_channels():
    if os.path.exists(CHANNELS_PATH):
        try:
            with open(CHANNELS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [sanitize_channel(c) for c in data if isinstance(c, dict)]
        except Exception:
            pass
    return []   # fresh install: no channels until onboarding / the UI creates one


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
    """Return a shuffled list of candidate movie dicts from one channel.
    Branches on channel kind: 'movie' uses TMDb's per-film recommendations;
    'person' and 'discover' both use discover/movie with different filters."""
    if channel.get("kind") == "movie":
        return _fetch_movie_seed(channel)

    params = {
        "language": LANGUAGE,
        "include_adult": "false",
        "sort_by": channel.get("sort", "popularity.desc"),
        "vote_count.gte": channel.get("vote_count_gte", 40),
        "vote_average.gte": channel.get("vote_avg_gte", 6.0),
        "page": random.randint(1, MAX_PAGE),
    }
    if channel.get("kind") == "person":
        if not channel.get("person_id"):
            return []
        params["with_people"] = str(channel["person_id"])   # cast OR crew
    else:
        if channel.get("genres"):
            params["with_genres"] = ",".join(str(g) for g in channel["genres"])
        kw_ids = [resolve_keyword(n) for n in channel.get("keywords", [])]
        kw_ids = [k for k in kw_ids if k]
        if kw_ids:
            joiner = "|" if channel.get("match") == "any" else ","   # any = OR, all = AND
            params["with_keywords"] = joiner.join(str(k) for k in kw_ids)

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


def _fetch_movie_seed(channel):
    """Candidates = TMDb's neighbours of a seed film (its 'recommendations', then
    'similar' as a fallback). That endpoint takes no discover filters, so the
    streaming filter and quality floor don't apply here; the model ranks what
    comes back."""
    mid = channel.get("movie_id")
    if not mid:
        return []
    out = []
    for endpoint in ("recommendations", "similar"):
        results = []
        for page in (random.randint(1, 3), 1):
            try:
                data = tmdb_get(f"movie/{mid}/{endpoint}", {"language": LANGUAGE, "page": page})
            except Exception:
                continue
            results = data.get("results", [])
            if results:
                break
        for m in results:
            mv = _movie_from_tmdb(m)
            if mv:
                mv["channel"] = channel["name"]
                out.append(mv)
        if out:
            break
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
    counts["notint"] = len(not_interested_ids())
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
# Seed search (for building movie / person / keyword channels)
# --------------------------------------------------------------------------
def search_movies(q):
    """TMDb title search for a seed-film picker: [{id, title, year, rating}]."""
    q = (q or "").strip()
    if not q or not API_KEY:
        return []
    try:
        data = tmdb_get("search/movie", {"query": q, "include_adult": "false", "language": LANGUAGE})
    except Exception:
        return []
    out = []
    for m in data.get("results", [])[:10]:
        if m.get("id") is None:
            continue
        out.append({"id": m["id"],
                    "title": m.get("title") or m.get("original_title") or "Untitled",
                    "year": (m.get("release_date") or "")[:4],
                    "rating": round(m.get("vote_average", 0), 1)})
    return out


def search_people(q):
    """TMDb person search for a person-channel picker: [{id, name, dept}]."""
    q = (q or "").strip()
    if not q or not API_KEY:
        return []
    try:
        data = tmdb_get("search/person", {"query": q, "include_adult": "false", "language": LANGUAGE})
    except Exception:
        return []
    out = []
    for p in data.get("results", [])[:10]:
        if p.get("id") is None:
            continue
        out.append({"id": p["id"], "name": p.get("name", ""),
                    "dept": p.get("known_for_department", "")})
    return out


def search_keywords(q):
    """TMDb keyword search with each tag's film count, so the user can pick a real,
    well-populated tag instead of a blind top hit: [{id, name, count}]."""
    q = (q or "").strip()
    if not q or not API_KEY:
        return []
    try:
        data = tmdb_get("search/keyword", {"query": q})
    except Exception:
        return []
    out = []
    for k in data.get("results", [])[:6]:
        kid = k.get("id")
        if kid is None:
            continue
        count = None
        try:
            c = tmdb_get("discover/movie", {"with_keywords": str(kid), "vote_count.gte": 0})
            count = c.get("total_results")
        except Exception:
            count = None
        out.append({"id": kid, "name": k.get("name", ""), "count": count})
    return out


# --------------------------------------------------------------------------
# Share / friends (export your likes, import friends' — one database per friend)
# --------------------------------------------------------------------------
def liked_export():
    """Your liked films, in a small shareable shape."""
    return [{"id": e["id"], "title": e["title"], "year": e["year"], "genres": e["genres"], "link": e["link"]}
            for e in _library_entries().values() if e["status"] == "3"]


SHARE_VERSION = 2   # share-file payload format


def _portable_model():
    """Your trained model in a friend-portable shape (vocab + weights + scaling +
    schema version), or None when no model has been trained yet."""
    path = os.path.join(ML_DIR, "model.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            m = json.load(f)
        vocab, theta, mu, sd = m.get("vocab"), m.get("theta"), m.get("mu"), m.get("sd")
        if not (isinstance(vocab, list) and isinstance(theta, list) and len(theta) == len(vocab) + 1):
            return None
        if not all(isinstance(v, list) and len(v) == len(vocab) for v in (mu, sd)):
            return None
        return {"schema": str(m.get("version", "v1")), "vocab": vocab, "theta": theta,
                "mu": mu, "sd": sd, "cv": m.get("cv")}
    except Exception:
        return None


def share_export():
    """Everything a friend needs — likes, dislikes, watchlist, seen ids, and (when
    trained) your taste model — in one portable, versioned payload."""
    lib = list(_library_entries().values())
    def pick(e):
        return {"id": e["id"], "title": e["title"], "year": e["year"],
                "genres": e["genres"], "link": e["link"]}
    out = {"app": "tastebuds", "version": SHARE_VERSION,
           "likes":     [pick(e) for e in lib if e["status"] == "3"],
           "dislikes":  [pick(e) for e in lib if e["status"] == "1"],
           "watchlist": [pick(e) for e in lib if e["status"] == "watchlist"],
           "seen":      sorted(e["id"] for e in lib if e["status"] in ("1", "2", "3"))}
    model = _portable_model()
    if model:
        out["model"] = model
    return out


def _clean_friend(data):
    """Sanitize one imported friend record: name, capped film lists, seen ids,
    and (when present and well-formed) their portable taste model. Old likes-only
    share files import fine — the extra fields just stay empty."""
    name = (str(data.get("name") or "").strip() or "a friend")[:60]

    def films(key, cap):
        out = []
        for m in (data.get(key) or [])[:cap]:   # cap an imported file to a sane size
            if isinstance(m, dict) and str(m.get("id", "")).isdigit():
                out.append({"id": int(m["id"]), "title": str(m.get("title", "")), "year": str(m.get("year", "")),
                            "genres": m.get("genres", ""), "link": str(m.get("link", ""))})
        return out

    fr = {"name": name, "likes": films("likes", 5000), "dislikes": films("dislikes", 5000),
          "watchlist": films("watchlist", 2000),
          "seen": sorted({int(x) for x in (data.get("seen") or [])[:20000]
                          if (isinstance(x, int) and x > 0)
                          or (isinstance(x, str) and x.isdigit())})}
    mdl = data.get("model")
    if isinstance(mdl, dict):
        vocab, theta, mu, sd = mdl.get("vocab"), mdl.get("theta"), mdl.get("mu"), mdl.get("sd")
        if (isinstance(vocab, list) and isinstance(theta, list) and 0 < len(vocab) <= 20000
                and len(theta) == len(vocab) + 1
                and all(isinstance(v, list) and len(v) == len(vocab) for v in (mu, sd))
                and all(isinstance(t, (int, float)) for t in theta)):
            fr["model"] = {"schema": str(mdl.get("schema", "")), "vocab": [str(v) for v in vocab],
                           "theta": [float(t) for t in theta],
                           "mu": [float(v) for v in mu], "sd": [float(v) for v in sd],
                           "cv": mdl.get("cv") if isinstance(mdl.get("cv"), dict) else None}
    return fr


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
    """Lightweight list for the UI: name + counts + whether a model came along."""
    return [{"name": d.get("name") or "a friend", "count": len(d.get("likes") or []),
             "watch": len(d.get("watchlist") or []), "model": bool(d.get("model"))}
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


# --------------------------------------------------------------------------
# Model training weights (how strongly each reaction counts)
# --------------------------------------------------------------------------
DEFAULT_WEIGHTS = {"like": 1.0, "indifferent": 0.5, "disliked": 1.0, "not_interested": 0.3}


def load_weights():
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


def save_weights(d):
    w = load_weights()
    for k in DEFAULT_WEIGHTS:
        if isinstance(d, dict) and k in d:
            try:
                w[k] = max(0.0, min(2.0, float(d[k])))
            except (TypeError, ValueError):
                pass
    _atomic_write(WEIGHTS_PATH, json.dumps(w, ensure_ascii=False))
    return w


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
# First-run onboarding
# --------------------------------------------------------------------------
def needs_onboarding():
    """True only on a genuinely fresh start: no channels yet and nothing logged."""
    try:
        if load_channels():
            return False
        return len(rated_ids()) == 0 and len(watchlist_ids()) == 0
    except Exception:
        return False


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


def _director_cached(movie_id):
    """Director from the cache only (no network) — for fast list rendering."""
    return _directors().get(str(movie_id))


# --------------------------------------------------------------------------
# Poster lookup (cached; used by the watchlist view)
# --------------------------------------------------------------------------
_poster_cache = None
def _posters():
    global _poster_cache
    if _poster_cache is None:
        _poster_cache = {}
        if os.path.exists(POSTER_CACHE_PATH):
            try:
                with open(POSTER_CACHE_PATH, "r", encoding="utf-8") as f:
                    _poster_cache = json.load(f)
            except Exception:
                _poster_cache = {}
    return _poster_cache


def _poster_cached(movie_id):
    """Poster URL from the cache only (no network); None if not cached or none exists."""
    p = _posters().get(str(movie_id))
    return (IMG_BASE + p) if p else None


def fetch_poster(movie_id):
    """Poster URL, cache-first then one TMDb fetch (cached so it never re-hits)."""
    cache = _posters(); key = str(movie_id)
    if key in cache:
        return (IMG_BASE + cache[key]) if cache[key] else None
    path = None
    if API_KEY:
        try:
            data = tmdb_get(f"movie/{movie_id}", {})
            path = data.get("poster_path")
        except Exception:
            path = None
    cache[key] = path
    try:
        _atomic_write(POSTER_CACHE_PATH, json.dumps(cache))
    except Exception:
        pass
    return (IMG_BASE + path) if path else None


def get_watchlist():
    """Saved films, newest first, enriched from caches only (posters fill in lazily
    via /api/poster so the sheet opens instantly)."""
    items = []
    for line in _table_lines(WATCHLIST_PATH):   # | Title | Year | Genres | TMDb ID | Link | Added on |
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) > WATCH_ID_COL and c[WATCH_ID_COL].isdigit():
            mid = int(c[WATCH_ID_COL])
            items.append({"id": mid, "title": c[0], "year": c[1], "genres": c[2],
                          "link": c[4] if len(c) > 4 else f"https://www.themoviedb.org/movie/{mid}",
                          "poster": _poster_cached(mid), "director": _director_cached(mid)})
    items.reverse()   # most recently added on top
    return items


def get_rated(status):
    """Films you rated with a given status ('3' liked … '0' not seen), newest
    first, enriched from caches only (posters fill in lazily via /api/poster)."""
    items = []
    for line in _table_lines(MD_PATH):   # | Rating | Status | Title | Year | Genres | TMDb ID | Link | ... |
        c = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(c) > MD_ID_COL and c[MD_ID_COL].isdigit() and c[0] == status:
            mid = int(c[MD_ID_COL])
            items.append({"id": mid, "title": c[2], "year": c[3], "genres": c[4],
                          "link": c[6] if len(c) > 6 else f"https://www.themoviedb.org/movie/{mid}",
                          "poster": _poster_cached(mid), "director": _director_cached(mid)})
    items.reverse()   # most recently rated on top
    return items


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

  /* Channels: kind-aware list + collapsible add cards */
  .chlist-empty{font-size:12.5px;color:var(--muted);padding:6px 0;letter-spacing:-.01em}
  .ch .ic{width:32px;height:32px;border-radius:9px;background:var(--gray-tint);display:flex;align-items:center;justify-content:center;color:var(--muted);flex:0 0 auto}
  .ch .ic svg{width:17px;height:17px}
  .ch .nm-in{font:inherit;font-size:14px;font-weight:600;border:1px solid var(--blue);border-radius:7px;padding:3px 7px;background:var(--card);color:var(--txt);width:100%}
  .ch .sw{width:38px;height:22px;border-radius:999px;background:var(--gray-tint);position:relative;border:none;cursor:pointer;flex:0 0 auto;padding:0}
  .ch .sw::after{content:"";position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.18s}
  .ch .sw.on{background:var(--teal)}
  .ch .sw.on::after{left:18px}
  .ch .iconbtn{background:none;border:none;color:var(--muted);cursor:pointer;padding:4px;border-radius:7px;display:inline-flex;align-items:center}
  .ch .iconbtn:hover{color:var(--txt);background:var(--gray-tint)}
  .ch .iconbtn svg{width:16px;height:16px}
  .addlabel{font-size:12.5px;color:var(--muted);margin:16px 0 8px;letter-spacing:-.01em}
  .acard{border:1px solid var(--line);border-radius:14px;margin-bottom:10px;overflow:hidden}
  .acard .ahead{display:flex;align-items:center;gap:9px;padding:12px 14px;cursor:pointer;font-size:14px;font-weight:600;letter-spacing:-.01em;user-select:none}
  .acard .ahead svg.tic{width:18px;height:18px;color:var(--muted);flex:0 0 auto}
  .acard .ahead .chev{margin-left:auto;color:var(--muted);transition:transform .2s}
  .acard.open .ahead .chev{transform:rotate(180deg)}
  .acard .abody{display:none;padding:2px 14px 14px}
  .acard.open .abody{display:block}
  .picker{margin-top:4px}
  .pk{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 10px;border-radius:9px;cursor:pointer;font-size:13.5px;letter-spacing:-.01em}
  .pk:hover{background:var(--gray-tint)}
  .pk .meta{color:var(--muted);font-size:12px;flex:0 0 auto;white-space:nowrap}
  .chosen{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .ktag{display:inline-flex;align-items:center;gap:7px;background:rgba(18,143,134,.12);color:var(--teal);font-size:12.5px;padding:4px 10px;border-radius:980px;letter-spacing:-.01em}
  .ktag .kx{cursor:pointer;font-weight:700;opacity:.8}
  .ktag .kx:hover{opacity:1}

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

  /* First-run onboarding */
  #onb-overlay .panel{max-width:440px}
  .onb-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
  .odots{display:flex;gap:6px}
  .odot{width:7px;height:7px;border-radius:50%;background:var(--gray-tint)}
  .odot.on{background:var(--teal)}
  .onb-count{font-size:12px;color:var(--muted)}
  .onb-step{display:none}
  .onb-step.on{display:block}
  .onb-ic{color:var(--teal);display:block;margin-bottom:4px}
  .onb-ic svg{width:28px;height:28px}
  .onb-h{font-size:20px;font-weight:600;letter-spacing:-.02em;margin:6px 0 8px}
  .onb-p{font-size:14px;color:var(--muted);line-height:1.6;margin:0 0 12px}
  .onb-field{width:100%;padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--txt);font:inherit;font-size:14px}
  .onb-row{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:20px}
  .onb-err{font-size:12px;color:var(--coral);margin-top:8px;min-height:15px;letter-spacing:-.01em}
  .onb-note{font-size:12px;color:var(--muted);margin-top:10px;line-height:1.5}
  .onb-picked{font-size:12.5px;color:var(--teal);margin-top:8px;min-height:15px;letter-spacing:-.01em}
  .onb-tip{font-size:13px;color:var(--txt);background:var(--gray-tint);padding:10px 12px;border-radius:10px;line-height:1.55;margin:4px 0 18px}
  .onb-verd{display:flex;align-items:center;gap:9px;padding:6px 0;font-size:13.5px;letter-spacing:-.01em}
  .onb-verd .vd{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .onb-verd b{font-weight:600}
  .onb-skip{background:none;border:none;color:var(--muted);font:inherit;font-size:12.5px;cursor:pointer;text-decoration:underline;padding:0;letter-spacing:-.01em}
  .pbtn:disabled{opacity:.45;cursor:default}

  /* Watchlist bottom sheet */
  .tally-wl{background:none;border:none;font:inherit;font-size:12.5px;color:var(--muted);cursor:pointer;padding:0;letter-spacing:-.01em}
  .tally-wl b{color:var(--txt);font-weight:600}
  .tally-wl:hover{color:var(--txt);text-decoration:underline}
  #wl-overlay{align-items:flex-end;justify-content:center;padding:0}
  .wl-sheet{width:100%;max-width:860px;background:var(--card);border:1px solid var(--line);border-bottom:none;
            border-radius:22px 22px 0 0;box-shadow:var(--shadow);max-height:86vh;display:flex;flex-direction:column;
            transform:translateY(100%);transition:transform .42s cubic-bezier(.4,0,.2,1)}
  .wl-sheet.up{transform:translateY(0)}
  .wl-grip{width:38px;height:5px;border-radius:999px;background:var(--line);margin:10px auto 4px;cursor:pointer;flex:0 0 auto}
  .wl-head{display:flex;align-items:center;justify-content:space-between;padding:4px 22px 12px;flex:0 0 auto}
  .wl-head h2{font-size:19px;font-weight:600;letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:10px}
  .wl-cnt{font-size:12px;color:var(--muted);background:var(--gray-tint);padding:2px 9px;border-radius:980px;font-weight:500}
  .wl-list{overflow-y:auto;padding:2px 18px 22px}
  .wl-item{display:flex;gap:13px;padding:13px 2px;border-bottom:1px solid var(--line)}
  .wl-thumb{width:48px;height:72px;border-radius:9px;background:var(--gray-tint);flex:0 0 auto;object-fit:cover;
            display:flex;align-items:center;justify-content:center;color:var(--muted)}
  .wl-thumb svg{width:18px;height:18px}
  .wl-info{flex:1;min-width:0}
  .wl-row1{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
  .wl-t{font-size:15px;font-weight:600;letter-spacing:-.01em;min-width:0}
  .wl-t .yr{color:var(--muted);font-weight:400}
  .wl-ic{display:flex;gap:2px;flex:0 0 auto}
  .wl-ic button,.wl-ic a{background:none;border:none;color:var(--muted);cursor:pointer;padding:5px;border-radius:8px;display:inline-flex;text-decoration:none}
  .wl-ic button:hover,.wl-ic a:hover{color:var(--txt);background:var(--gray-tint)}
  .wl-ic svg{width:16px;height:16px}
  .wl-g{font-size:12.5px;color:var(--muted);margin:2px 0 9px;letter-spacing:-.01em}
  .wl-watch{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .wl-watch .lbl{font-size:11.5px;color:var(--muted)}
  .wl-vb{background:transparent;font-size:12px;padding:4px 11px;border-radius:980px;cursor:pointer;border:1px solid currentColor;line-height:1;font-family:inherit}
  .wl-vb.like{color:var(--teal)}
  .wl-vb.meh{color:var(--amber)}
  .wl-vb.dis{color:var(--coral)}
  .wl-empty{padding:46px 16px;text-align:center;color:var(--muted);font-size:14px;line-height:1.65}

  /* Model panel: data -> weights -> train -> results */
  .mstage{font-size:12px;color:var(--muted);margin:14px 0 8px;letter-spacing:-.01em}
  .mstage-row{display:flex;justify-content:space-between;align-items:baseline}
  .mreset{background:none;border:none;color:var(--blue);font:inherit;font-size:12px;cursor:pointer;padding:0}
  .mflow{text-align:center;color:var(--muted);font-size:14px;margin:6px 0;opacity:.55}
  .mchips{display:flex;gap:8px;flex-wrap:wrap}
  .mchip{background:var(--gray-tint);border-radius:10px;padding:7px 11px;font-size:12.5px;display:flex;align-items:center;gap:7px;letter-spacing:-.01em}
  .mchip b{font-weight:600}
  .wdot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
  .mnote{font-size:11.5px;color:var(--muted);margin-top:8px;letter-spacing:-.01em}
  .wgrp{font-size:12px;font-weight:600;margin:12px 0 4px;letter-spacing:-.01em}
  .wgrp.toward{color:var(--teal)}
  .wgrp.away{color:var(--coral)}
  .wrow{display:flex;align-items:center;gap:10px;margin:7px 0}
  .wrow .wnm{width:96px;flex:0 0 auto;font-size:13px;letter-spacing:-.01em}
  .wrow input[type=range]{flex:1;accent-color:var(--violet)}
  .wrow .wv{width:40px;text-align:right;font-size:13px;font-weight:600}
  .mwarn{font-size:12px;color:var(--coral);margin-top:8px;min-height:14px;letter-spacing:-.01em}
  .primarybtn:disabled{opacity:.45;cursor:default}

  /* Movie night: the lights go down and a projector takes the card's place */
  .topline{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:0 0 10px;padding:0 8px}
  .nightctl{display:flex;align-items:center;gap:8px;flex:0 0 auto}
  .nlabel{font-size:12.5px;color:var(--muted);letter-spacing:-.01em}
  .lswitch{appearance:none;width:46px;height:26px;border-radius:980px;border:1px solid var(--line);
           background:var(--gray-tint);position:relative;cursor:pointer;padding:0;transition:background .3s ease}
  .lswitch .knob{position:absolute;top:2.5px;left:3px;width:19px;height:19px;border-radius:50%;
           background:var(--card);box-shadow:0 1px 4px rgba(0,0,0,.35);transition:left .3s cubic-bezier(.4,0,.2,1)}
  .lswitch.on{background:var(--violet);border-color:var(--violet)}
  .lswitch.on .knob{left:23px}
  body.night{background:#0b0b0d}
  body.night::after{content:'';position:fixed;inset:0;z-index:1;pointer-events:none;
           background:radial-gradient(ellipse at 50% 28%, transparent 35%, rgba(0,0,0,.6) 100%)}
  body.night .bar{opacity:.3}
  body.night .tally{opacity:0;pointer-events:none;transition:opacity .5s ease}
  body.night .nlabel{color:#b9b5aa}
  .night-wrap{position:relative;z-index:2}
  .screen-frame{background:#141416;border:1px solid #29292e;border-radius:26px;padding:30px;
           box-shadow:0 40px 90px -30px rgba(0,0,0,.9)}
  .screen{position:relative;border-radius:14px;min-height:470px;overflow:hidden;display:flex;
           align-items:center;justify-content:center;background:
           radial-gradient(ellipse at 50% -12%, rgba(255,255,248,.5), transparent 55%),
           linear-gradient(180deg,#f1eee4,#d9d5c7)}
  .screen .grain{position:absolute;inset:-75%;z-index:3;pointer-events:none;opacity:.09;
           background:url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240"><filter id="n"><feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" stitchTiles="stitch"/></filter><rect width="100%" height="100%" filter="url(%23n)"/></svg>');
           animation:grain .85s steps(5) infinite}
  @keyframes grain{0%{transform:translate(0,0)}20%{transform:translate(-2%,1.6%)}40%{transform:translate(1.4%,-2.2%)}
           60%{transform:translate(-1.8%,-1%)}80%{transform:translate(2.2%,1.2%)}100%{transform:translate(0,0)}}
  .proj{position:relative;z-index:2;text-align:center;color:#272219;padding:30px 26px;max-width:600px}
  .proj.roll{animation:flick 1.05s linear}
  @keyframes flick{0%{opacity:0;filter:brightness(2.6) contrast(.55)}7%{opacity:.85}11%{opacity:.15}
           17%{opacity:.95;filter:brightness(1.7) contrast(.8)}24%{opacity:.45}33%{opacity:1;filter:brightness(1.2)}
           52%{filter:brightness(.96)}70%{filter:brightness(1.04)}100%{opacity:1;filter:none}}
  .proj .pp{width:190px;border-radius:10px;box-shadow:0 18px 44px rgba(0,0,0,.38);margin:0 auto 16px;display:block}
  .proj .pp-ph{width:190px;height:285px;border-radius:10px;background:rgba(60,50,30,.14);margin:0 auto 16px;
           display:flex;align-items:center;justify-content:center;color:rgba(60,50,30,.4)}
  .proj .pp-ph svg{width:34px;height:34px}
  .proj h1{font-size:27px;font-weight:600;letter-spacing:-.02em;margin:0 0 6px;line-height:1.12}
  .proj h1 .yr{font-weight:400;opacity:.55}
  .proj .pmeta{font-size:13.5px;opacity:.65;margin-bottom:10px;letter-spacing:-.01em}
  .proj .ptier{font-size:13px;font-weight:600;margin-bottom:4px;letter-spacing:-.01em}
  .proj .pscores{font-size:12px;opacity:.6;letter-spacing:-.01em}
  .proj .pidle{font-size:15px;opacity:.55;line-height:1.7;max-width:380px;margin:0 auto}
  .proj a{color:inherit}
  .night-panel{margin-top:14px;background:#141416;border:1px solid #29292e;border-radius:18px;
           padding:15px 18px;color:#d8d4c9}
  .np-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
  .np-lbl{font-size:12px;color:#8d897f;letter-spacing:-.01em;margin-right:2px}
  .npf{appearance:none;font:inherit;font-size:12.5px;letter-spacing:-.01em;cursor:pointer;border-radius:980px;
           padding:5px 13px;border:1px solid #34343b;background:#1d1d21;color:#cfccc2}
  .npf.on{background:var(--blue);border-color:var(--blue);color:#fff}
  .npf.you{cursor:default;background:#2a2a30;color:#efece2}
  .night-panel .ghost{background:#26262b;color:#d8d4c9}
  .np-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .night-panel .note{color:#8d897f}
  .npx{margin-left:7px;opacity:.45;font-size:13px;line-height:1;display:inline-block}
  .npf:hover .npx{opacity:.9}
  .npx.arm{opacity:1;color:var(--coral);font-size:11px;font-weight:600}
  .night-panel .seg{background:#1d1d21;border-color:#34343b}
  .night-panel .seg button{color:#cfccc2}
  .night-panel .seg button.on{color:#fff}
  .proj .pwhy{font-size:12px;opacity:.55;letter-spacing:-.01em;margin-top:3px}
  .proj .pscores + .pwhy{margin-top:9px}
</style></head>
<body>
  <div class="page">
    <div class="topline">
      <div class="tally" id="tally" style="text-align:left"></div>
      <div class="nightctl"><span class="nlabel">Movie night</span><button class="lswitch" id="night-switch" aria-label="Toggle movie night" title="Movie night: one film for you and your buddies"><span class="knob"></span></button></div>
    </div>
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

    <div class="night-wrap" id="night-wrap" hidden>
      <div class="screen-frame">
        <div class="screen">
          <div class="proj" id="proj"></div>
          <div class="grain"></div>
        </div>
      </div>
      <div class="night-panel">
        <div class="np-row" id="night-party"></div>
        <div class="np-row">
          <span class="np-lbl">How to decide:</span>
          <div class="seg" id="night-combine">
            <button data-c="least_misery" class="on">Nobody suffers</button>
            <button data-c="average">Crowd pleaser</button>
            <button data-c="avg_no_misery">With a floor</button>
          </div>
          <span class="tip" data-tip="How everyone's scores merge into one pick. Nobody suffers ranks films by their LOWEST fan, so no one gets a film they'd hate. Crowd pleaser ranks by the average — highest overall enthusiasm. With a floor averages too, but any film someone scores very low sinks to the back.">i</span>
        </div>
        <div class="np-actions">
          <button class="primarybtn" id="night-pick">Start the projector</button>
          <button class="ghost" id="night-providers">Where you watch</button>
          <span class="note" id="night-note"></span>
        </div>
      </div>
    </div>
  </div>

  <div class="overlay" id="overlay">
    <div class="panel" id="panel">
      <div class="phead"><h2>Channels</h2><button class="x" id="close-settings">&times;</button></div>
      <p class="desc">The pools your movies are drawn from. Add as many as you like — your taste model ranks whatever they bring in.</p>
      <div id="ch-list"></div>
      <div class="addlabel">Add a channel</div>
      <div id="add-cards">

        <div class="acard" id="card-genre">
          <div class="ahead"><svg class="tic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/></svg>Genre<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></div>
          <div class="abody">
            <div class="fld"><label>Name (optional)</label><input id="g-name" placeholder="e.g. Quirky comedies"></div>
            <div class="fld"><label>Genres</label><div class="gpick" id="g-genres"></div></div>
            <div class="fld"><label>Style</label><div class="seg" id="g-style"><button data-s="popular" class="on">Popular</button><button data-s="acclaimed">Acclaimed</button><button data-s="gems">Hidden gems</button></div></div>
            <button class="pbtn primary" id="g-add" style="width:100%">Add channel</button>
            <div class="note" id="g-note"></div>
          </div>
        </div>

        <div class="acard" id="card-movie">
          <div class="ahead"><svg class="tic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2.4"/><path d="M3 9.5h18M8.5 4v16"/></svg>More like a movie<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></div>
          <div class="abody">
            <div class="fld"><label>Search a film you love</label><input id="m-q" placeholder="e.g. Aftersun" autocomplete="off"></div>
            <div class="picker" id="m-res"></div>
          </div>
        </div>

        <div class="acard" id="card-person">
          <div class="ahead"><svg class="tic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8.5" r="3.7"/><path d="M5 20c0-3.6 3.2-5.5 7-5.5s7 1.9 7 5.5"/></svg>Films by a person<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></div>
          <div class="abody">
            <div class="fld"><label>Search a director or actor <span style="color:var(--muted)">— cast or crew</span></label><input id="p-q" placeholder="e.g. Greta Gerwig" autocomplete="off"></div>
            <div class="picker" id="p-res"></div>
          </div>
        </div>

        <div class="acard" id="card-keyword">
          <div class="ahead"><svg class="tic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.6 13.4 12.8 21.2a1.8 1.8 0 0 1-2.6 0L3 14V4h10l7.6 7.4a1.8 1.8 0 0 1 0 2z"/><circle cx="7.5" cy="7.5" r="1.4"/></svg>Keyword<svg class="chev" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg></div>
          <div class="abody">
            <div class="fld"><label>Name (optional)</label><input id="k-name" placeholder="e.g. Coming-of-age"></div>
            <div class="fld"><label>Find a tag <span style="color:var(--muted)">— with its film count</span></label><input id="k-q" placeholder="e.g. coming of age" autocomplete="off"></div>
            <div class="picker" id="k-res"></div>
            <div class="chosen" id="k-chosen"></div>
            <div class="fld" style="margin-top:10px"><label>Match</label><div class="seg" id="k-match"><button data-m="any" class="on">Any of these</button><button data-m="all">All of these</button></div></div>
            <div class="fld"><label>Style</label><div class="seg" id="k-style"><button data-s="popular" class="on">Popular</button><button data-s="acclaimed">Acclaimed</button><button data-s="gems">Hidden gems</button></div></div>
            <button class="pbtn primary" id="k-add" style="width:100%">Add channel</button>
            <div class="note" id="k-note"></div>
          </div>
        </div>

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
      <p class="desc">Tune how strongly each reaction trains your taste model. Your data, your weights, your model.</p>

      <div class="mstage">1 &middot; Your data — what's feeding it</div>
      <div class="mchips" id="model-data"></div>
      <div class="mnote">Not seen &amp; watchlist aren't used — they're unwatched.</div>

      <div class="mflow">&#8595;</div>

      <div class="mstage-row"><div class="mstage" style="margin-bottom:0">2 &middot; Weights — how strongly each reaction counts</div><button class="mreset" id="w-reset">Reset</button></div>
      <div class="wgrp toward">Pulls toward your taste</div>
      <div class="wrow"><span class="wdot" style="background:var(--teal)"></span><span class="wnm">Liked</span><input type="range" id="w-like" min="0" max="2" step="0.1"><span class="wv" id="wv-like"></span></div>
      <div class="wgrp away">Pushes away</div>
      <div class="wrow"><span class="wdot" style="background:var(--amber)"></span><span class="wnm">Indifferent</span><input type="range" id="w-meh" min="0" max="2" step="0.1"><span class="wv" id="wv-meh"></span></div>
      <div class="wrow"><span class="wdot" style="background:var(--coral)"></span><span class="wnm">Disliked</span><input type="range" id="w-dis" min="0" max="2" step="0.1"><span class="wv" id="wv-dis"></span></div>
      <div class="wrow"><span class="wdot" style="background:var(--graphite)"></span><span class="wnm">Not interested</span><input type="range" id="w-ni" min="0" max="2" step="0.1"><span class="wv" id="wv-ni"></span></div>
      <div class="mnote">Strength only — the direction is fixed by the reaction. &times;1.0 = full strength, &times;0 = ignore it.</div>
      <div class="mwarn" id="w-warn"></div>

      <div class="mflow">&#8595;</div>

      <div class="mstage">3 &middot; Train</div>
      <button class="primarybtn" id="do-train" style="width:100%">Train model</button>

      <div class="mflow">&#8595;</div>

      <div class="mstage">4 &middot; Results</div>
      <div id="train-out"><div class="ml-msg">Press <b>Train</b> to see your model's quality and what drives it.</div></div>
    </div>
  </div>

  <div class="overlay" id="export-overlay">
    <div class="panel" id="export-panel">
      <div class="phead"><h2>Share your library</h2><button class="x" id="close-export">&times;</button></div>
      <p class="desc">Export your taste to a small file you can send a friend: your <b>liked</b> and <b>disliked</b> films, your <b>watchlist</b>, and (once trained) your <b>taste model</b>. They import it under Recommendations → <b>Friends</b> — for recommendations from your taste, and for <b>Movie night</b>.</p>
      <div class="fld"><label>Your name (optional — shown to friends)</label><input id="share-name" placeholder="e.g. Vlad" autocomplete="off"></div>
      <button class="primarybtn" id="do-export">Export my taste</button>
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

  <div class="overlay" id="onb-overlay">
    <div class="panel" id="onb-panel">
      <div class="onb-top">
        <div class="odots" id="onb-dots"></div>
        <span class="onb-count" id="onb-count"></span>
      </div>

      <div class="onb-step" id="onb-welcome">
        <span class="onb-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2.4"/><path d="M3 9.5h18M8.5 4v16"/></svg></span>
        <div class="onb-h">Welcome to Tastebuds</div>
        <p class="onb-p">Rate the films you've seen — Tastebuds learns your taste and starts recommending films you'll actually love.</p>
        <p class="onb-note">Everything stays on your computer. Your ratings are plain files you own — no account, no cloud.</p>
        <div class="onb-row" style="justify-content:flex-end"><button class="pbtn primary" id="onb-start">Get started</button></div>
      </div>

      <div class="onb-step" id="onb-key">
        <span class="onb-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="15" r="4"/><path d="M10.85 12.15 20 3M16 7l3 3M14.5 8.5l2.5 2.5"/></svg></span>
        <div class="onb-h">Connect The Movie Database</div>
        <p class="onb-p">Tastebuds uses TMDb for posters, cast, and details. Paste your free API key to begin.</p>
        <input class="onb-field" id="onb-key-input" placeholder="TMDb API key (v3 auth)" autocomplete="off">
        <div class="onb-err" id="onb-key-err"></div>
        <p class="onb-note"><a class="link" href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener">How to get one — about 2 minutes</a> · saved only on your machine, in tmdb_key.txt</p>
        <div class="onb-row"><button class="pbtn cancel" id="onb-key-back">Back</button><button class="pbtn primary" id="onb-key-continue">Continue</button></div>
      </div>

      <div class="onb-step" id="onb-channel">
        <span class="onb-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/></svg></span>
        <div class="onb-h">Make your first channel</div>
        <p class="onb-p">Channels are where your movies come from. The easiest start: name a film you love, and Tastebuds builds a channel of films like it.</p>
        <input class="onb-field" id="onb-q" placeholder="Search a film you love — e.g. Aftersun" autocomplete="off">
        <div class="picker" id="onb-res"></div>
        <div class="onb-picked" id="onb-picked"></div>
        <p class="onb-note">We'll also count it as a film you liked, to give your recommendations a head start.</p>
        <div class="onb-row"><button class="onb-skip" id="onb-self">Set up channels myself</button><button class="pbtn primary" id="onb-create" disabled>Create channel</button></div>
      </div>

      <div class="onb-step" id="onb-rate">
        <span class="onb-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3l2.6 5.6 6 .8-4.4 4.2 1.1 6L12 16.8 6.7 19.6l1.1-6L3.4 9.4l6-.8z"/></svg></span>
        <div class="onb-h">How rating works</div>
        <p class="onb-p">You'll see one film at a time. For each, tell Tastebuds:</p>
        <div class="onb-verd"><span class="vd" style="background:var(--teal)"></span><b>Liked</b> · seen and loved it</div>
        <div class="onb-verd"><span class="vd" style="background:var(--amber)"></span><b>Indifferent</b> · seen, didn't stick</div>
        <div class="onb-verd"><span class="vd" style="background:var(--coral)"></span><b>Disliked</b> · seen and disliked</div>
        <div class="onb-verd" style="margin-bottom:10px"><span class="vd" style="background:var(--graphite)"></span><b>Not seen</b> · haven't watched it</div>
        <div class="onb-tip">Tip: rate films you didn't love, too — the dislikes are what sharpen your recommendations.</div>
        <div class="onb-row"><button class="pbtn cancel" id="onb-rate-back">Back</button><button class="pbtn primary" id="onb-finish">Start rating</button></div>
      </div>

    </div>
  </div>

  <div class="overlay" id="wl-overlay">
    <div class="wl-sheet" id="wl-sheet">
      <div class="wl-grip" id="wl-grip"></div>
      <div class="wl-head">
        <h2><span id="wl-title">Watchlist</span> <span class="wl-cnt" id="wl-cnt"></span></h2>
        <button class="x" id="wl-close">&times;</button>
      </div>
      <div class="wl-list" id="wl-list"></div>
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
  if(s) window._lastStats = s;
  const el = document.getElementById('tally');
  if(s){
    el.innerHTML =
      '<button class="tally-wl" onclick="openListSheet(\'3\')" title="View the films you liked">Liked <b>'+s['3']+'</b></button> &middot; ' +
      'Indifferent <b>'+s['2']+'</b> &middot; ' +
      '<button class="tally-wl" onclick="openListSheet(\'1\')" title="View the films you disliked">Disliked <b>'+s['1']+'</b></button> &middot; ' +
      'Not seen <b>'+s['0']+'</b> &middot; ' +
      '<button class="tally-wl" onclick="openListSheet(\'watch\')" title="View your watchlist">Watchlist <b>'+s['watch']+'</b></button>';
  } else { el.innerHTML=''; }
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

/* ---- Channels panel (kinds: genre / movie / person / keyword) ---- */
let kChosen = [];          // chosen keyword tags [{id,name}] for the Keyword card
let kMatch = 'any', kStyle = 'popular';
let editIndex = null;      // index in chDraft being edited (null = adding new)
let searchTimer = null;

const ICN = {
  genre:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="7" rx="1.4"/><rect x="14" y="14" width="7" height="7" rx="1.4"/><rect x="3" y="14" width="7" height="7" rx="1.4"/></svg>',
  movie:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2.4"/><path d="M3 9.5h18M8.5 4v16"/></svg>',
  person:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8.5" r="3.7"/><path d="M5 20c0-3.6 3.2-5.5 7-5.5s7 1.9 7 5.5"/></svg>',
  keyword:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20.6 13.4 12.8 21.2a1.8 1.8 0 0 1-2.6 0L3 14V4h10l7.6 7.4a1.8 1.8 0 0 1 0 2z"/><circle cx="7.5" cy="7.5" r="1.4"/></svg>'
};
const EDIT_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2 2 0 0 1 3 3L7.3 18.7 3 20l1.3-4.3z"/></svg>';
const TRASH_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M18 7l-1 13H7L6 7"/></svg>';

function genreName(id){ const g=genreList.find(x=>x.id===id); return g?g.name:('#'+id); }
function dispKind(c){ if(c.kind==='movie')return 'movie'; if(c.kind==='person')return 'person'; if((c.keywords||[]).length) return 'keyword'; return 'genre'; }
function chSummary(c){
  if(c.kind==='movie') return 'Seed film';
  if(c.kind==='person') return 'Person · cast or crew';
  const parts=[];
  const gs=(c.genres||[]).map(genreName).join(', '); if(gs) parts.push(gs);
  const kw=(c.keywords||[]); if(kw.length){ parts.push('“'+kw.join('”, “')+'”'); if(kw.length>1) parts.push(c.match==='any'?'any':'all'); }
  parts.push(c.style||'custom');
  return parts.join(' · ');
}
function renderChannels(){
  const list=document.getElementById('ch-list');
  if(!chDraft.length){ list.innerHTML='<div class="chlist-empty">No channels yet — add one below.</div>'; return; }
  list.innerHTML = chDraft.map((c,i)=>
    '<div class="ch" data-i="'+i+'">'+
      '<span class="ic">'+ICN[dispKind(c)]+'</span>'+
      '<div class="info"><div class="cn">'+esc(c.name)+'</div><div class="cs">'+esc(chSummary(c))+'</div></div>'+
      '<button class="sw'+(c.enabled?' on':'')+'" data-tgl="'+i+'" aria-label="Toggle channel"></button>'+
      '<button class="iconbtn" data-edit="'+i+'" aria-label="Edit name">'+EDIT_SVG+'</button>'+
      '<button class="iconbtn" data-del="'+i+'" aria-label="Remove">'+TRASH_SVG+'</button>'+
    '</div>').join('');
  list.querySelectorAll('[data-tgl]').forEach(b=>b.onclick=()=>{ const i=+b.dataset.tgl; chDraft[i].enabled=!chDraft[i].enabled; b.classList.toggle('on',chDraft[i].enabled); });
  list.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>{ chDraft.splice(+b.dataset.del,1); renderChannels(); });
  list.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>startEdit(+b.dataset.edit));
}

/* genre card */
function renderGenrePicker(){
  const wrap=document.getElementById('g-genres');
  if(!genreList.length){ wrap.innerHTML='<div class="note">Genre list needs a TMDb key.</div>'; return; }
  wrap.innerHTML = genreList.map(g=>'<span class="gchip'+(addSel.has(g.id)?' on':'')+'" data-g="'+g.id+'">'+esc(g.name)+'</span>').join('');
  wrap.querySelectorAll('[data-g]').forEach(el=>el.onclick=()=>{ const id=+el.dataset.g;
    if(addSel.has(id)){addSel.delete(id);el.classList.remove('on');}else{addSel.add(id);el.classList.add('on');} });
}
function gAdd(){
  const genres=[...addSel];
  if(!genres.length){ document.getElementById('g-note').textContent='Pick at least one genre.'; return; }
  const name=document.getElementById('g-name').value.trim();
  const preset=STYLES[addStyle];
  const auto=genres.map(genreName).join(' / ')||'Genre';
  commitChannel({kind:'discover', name:name||auto, enabled:true, genres:genres, keywords:[], match:'all',
                 style:addStyle, sort:preset.sort, vote_count_gte:preset.vote_count_gte, vote_avg_gte:preset.vote_avg_gte});
  document.getElementById('g-name').value=''; document.getElementById('g-note').textContent='';
  addSel=new Set(); renderGenrePicker();
}

/* generic picker (movie / person / keyword search results) */
function renderPicker(boxId, items, onPick){
  const box=document.getElementById(boxId);
  if(!items.length){ box.innerHTML=''; return; }
  box.innerHTML=items.map((it,idx)=>'<div class="pk" data-idx="'+idx+'"><span>'+esc(it.label)+'</span>'+(it.meta?'<span class="meta">'+esc(it.meta)+'</span>':'')+'</div>').join('');
  box.querySelectorAll('.pk').forEach(el=>el.onclick=()=>onPick(items[+el.dataset.idx]));
}
function debounceSearch(el, fn){ if(!el) return; el.oninput=()=>{ clearTimeout(searchTimer); searchTimer=setTimeout(fn,260); }; }

/* movie seed card */
async function doMovieSearch(){
  const q=document.getElementById('m-q').value.trim();
  if(!q){ document.getElementById('m-res').innerHTML=''; return; }
  let d; try{ d=await (await fetch('/api/search-movie?q='+encodeURIComponent(q))).json(); }catch(e){ return; }
  renderPicker('m-res',(d.results||[]).map(m=>({raw:m,label:m.title,meta:(m.year||'')+(m.rating?(' · '+m.rating+' ★'):'')})),pickMovie);
}
function pickMovie(it){ const m=it.raw;
  commitChannel({kind:'movie', name:'More like '+m.title, enabled:true, movie_id:m.id, movie_title:m.title,
                 style:'custom', sort:'popularity.desc', vote_count_gte:0, vote_avg_gte:0});
  document.getElementById('m-q').value=''; document.getElementById('m-res').innerHTML='';
}

/* person card */
async function doPersonSearch(){
  const q=document.getElementById('p-q').value.trim();
  if(!q){ document.getElementById('p-res').innerHTML=''; return; }
  let d; try{ d=await (await fetch('/api/search-person?q='+encodeURIComponent(q))).json(); }catch(e){ return; }
  renderPicker('p-res',(d.results||[]).map(p=>({raw:p,label:p.name,meta:p.dept||''})),pickPerson);
}
function pickPerson(it){ const p=it.raw; const preset=STYLES['popular'];
  commitChannel({kind:'person', name:'Films by '+p.name, enabled:true, person_id:p.id, person_name:p.name,
                 style:'popular', sort:preset.sort, vote_count_gte:preset.vote_count_gte, vote_avg_gte:preset.vote_avg_gte});
  document.getElementById('p-q').value=''; document.getElementById('p-res').innerHTML='';
}

/* keyword card */
function renderChosen(){
  const box=document.getElementById('k-chosen');
  box.innerHTML=kChosen.map((k,i)=>'<span class="ktag">'+esc(k.name)+'<span class="kx" data-kx="'+i+'">×</span></span>').join('');
  box.querySelectorAll('[data-kx]').forEach(el=>el.onclick=()=>{ kChosen.splice(+el.dataset.kx,1); renderChosen(); });
}
async function doKeywordSearch(){
  const q=document.getElementById('k-q').value.trim();
  if(!q){ document.getElementById('k-res').innerHTML=''; return; }
  let d; try{ d=await (await fetch('/api/search-keyword?q='+encodeURIComponent(q))).json(); }catch(e){ return; }
  renderPicker('k-res',(d.results||[]).map(k=>({raw:k,label:k.name,meta:(k.count!=null?(k.count.toLocaleString()+' films'):'')})),pickKeyword);
}
function pickKeyword(it){ const k=it.raw; if(!kChosen.some(x=>x.name===k.name)) kChosen.push({id:k.id,name:k.name});
  document.getElementById('k-q').value=''; document.getElementById('k-res').innerHTML=''; renderChosen(); }
function kAdd(){
  if(!kChosen.length){ document.getElementById('k-note').textContent='Pick at least one keyword.'; return; }
  const name=document.getElementById('k-name').value.trim();
  const preset=STYLES[kStyle];
  const kws=kChosen.map(k=>k.name);
  commitChannel({kind:'discover', name:name||kws.slice(0,3).join(' / '), enabled:true, genres:[], keywords:kws,
                 match:kMatch, style:kStyle, sort:preset.sort, vote_count_gte:preset.vote_count_gte, vote_avg_gte:preset.vote_avg_gte});
  document.getElementById('k-name').value=''; document.getElementById('k-note').textContent='';
  kChosen=[]; renderChosen();
}

/* commit (add new, or replace when editing) */
function commitChannel(obj){
  if(editIndex!=null){ obj.enabled=chDraft[editIndex].enabled; chDraft[editIndex]=obj; editIndex=null;
    document.getElementById('g-add').textContent='Add channel'; document.getElementById('k-add').textContent='Add channel'; }
  else { chDraft.push(obj); }
  renderChannels();
}

/* edit: genre/keyword reopen their card prefilled; movie/person rename inline */
function setSeg(segId, val, attr){ document.querySelectorAll('#'+segId+' button').forEach(b=>b.classList.toggle('on', b.dataset[attr]===val)); }
function openCard(kind){ const el=document.getElementById('card-'+kind); if(el) el.classList.add('open'); }
function closeCards(){ document.querySelectorAll('#add-cards .acard').forEach(c=>c.classList.remove('open')); }
function startEdit(i){
  const c=chDraft[i], k=dispKind(c);
  if(k==='movie'||k==='person'){ inlineRename(i); return; }
  editIndex=i; closeCards();
  if(k==='genre'){
    openCard('genre');
    document.getElementById('g-name').value=c.name||'';
    addSel=new Set(c.genres||[]); addStyle=(c.style&&STYLES[c.style])?c.style:'popular';
    setSeg('g-style',addStyle,'s'); renderGenrePicker();
    document.getElementById('g-add').textContent='Update channel';
    document.getElementById('card-genre').scrollIntoView({block:'nearest'});
  } else {
    openCard('keyword');
    document.getElementById('k-name').value=c.name||'';
    kChosen=(c.keywords||[]).map(n=>({id:null,name:n})); renderChosen();
    kMatch=(c.match==='all')?'all':'any'; kStyle=(c.style&&STYLES[c.style])?c.style:'popular';
    setSeg('k-match',kMatch,'m'); setSeg('k-style',kStyle,'s');
    document.getElementById('k-add').textContent='Update channel';
    document.getElementById('card-keyword').scrollIntoView({block:'nearest'});
  }
}
function inlineRename(i){
  const row=document.querySelector('.ch[data-i="'+i+'"]'); if(!row) return;
  const cn=row.querySelector('.cn'), cur=chDraft[i].name;
  cn.innerHTML='<input class="nm-in" value="'+escAttr(cur)+'">';
  const inp=cn.querySelector('input'); inp.focus(); inp.select();
  let done=false;
  const finish=(save)=>{ if(done) return; done=true; if(save){ const v=inp.value.trim(); if(v) chDraft[i].name=v; } renderChannels(); };
  inp.onblur=()=>finish(true);
  inp.onkeydown=(e)=>{ if(e.key==='Enter') finish(true); else if(e.key==='Escape') finish(false); };
}

async function openSettings(){
  const r=await fetch('/api/channels'); const d=await r.json();
  chDraft=JSON.parse(JSON.stringify(d.channels||[]));
  genreList=d.genres||[];
  editIndex=null;
  document.getElementById('g-add').textContent='Add channel'; document.getElementById('k-add').textContent='Add channel';
  addSel=new Set(); addStyle='popular'; kChosen=[]; kMatch='any'; kStyle='popular';
  ['g-name','k-name','m-q','p-q','k-q'].forEach(id=>{const el=document.getElementById(id); if(el) el.value='';});
  ['m-res','p-res','k-res','g-note','k-note','k-chosen'].forEach(id=>{const el=document.getElementById(id); if(el) el.innerHTML='';});
  setSeg('g-style','popular','s'); setSeg('k-style','popular','s'); setSeg('k-match','any','m');
  closeCards(); renderGenrePicker(); renderChannels();
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
document.getElementById('overlay').onclick = e=>{ if(e.target.id==='overlay') closeSettings(); };
document.querySelectorAll('#add-cards .ahead').forEach(h=>h.onclick=()=>h.parentElement.classList.toggle('open'));
document.getElementById('g-add').onclick = gAdd;
document.getElementById('k-add').onclick = kAdd;
debounceSearch(document.getElementById('m-q'), doMovieSearch);
debounceSearch(document.getElementById('p-q'), doPersonSearch);
debounceSearch(document.getElementById('k-q'), doKeywordSearch);
document.querySelectorAll('#g-style button').forEach(b=>b.onclick=()=>{ addStyle=b.dataset.s; setSeg('g-style',addStyle,'s'); });
document.querySelectorAll('#k-style button').forEach(b=>b.onclick=()=>{ kStyle=b.dataset.s; setSeg('k-style',kStyle,'s'); });
document.querySelectorAll('#k-match button').forEach(b=>b.onclick=()=>{ kMatch=b.dataset.m; setSeg('k-match',kMatch,'m'); });
document.getElementById('undo').onclick = undo;

document.addEventListener('keydown', e=>{
  if(e.key==='Escape'){ if(settingsOpen) closeSettings(); if(editOpen) closeEdit(); if(modelOpen) closeModel(); if(exportOpen) closeExport(); if(friendsOpen) closeFriends(); if(providersOpen) closeProviders(); if(watchlistOpen) closeWatchlist(); }
});

/* ---- Model panel (data -> weights -> train -> results) ---- */
const W_DEFAULTS = {like:1.0, indifferent:0.5, disliked:1.0, not_interested:0.3};
const W_IDS = {like:'w-like', indifferent:'w-meh', disliked:'w-dis', not_interested:'w-ni'};
let modelWeights = Object.assign({}, W_DEFAULTS);
let lastAuc = null;   // remembered this session for the before/after delta

function renderWeightSliders(){
  for(const k in W_IDS){
    const s=document.getElementById(W_IDS[k]); s.value = modelWeights[k];
    document.getElementById('wv-'+W_IDS[k].slice(2)).textContent = '×'+Number(modelWeights[k]).toFixed(1);
  }
  checkLikeGuard();
}
function checkLikeGuard(){
  const warn=document.getElementById('w-warn'), t=document.getElementById('do-train');
  if(parseFloat(document.getElementById('w-like').value) <= 0){
    warn.textContent='Liked can’t be 0 — the model needs something to aim toward.'; t.disabled=true;
  } else { warn.textContent=''; t.disabled=false; }
}
function renderModelData(s){
  const box=document.getElementById('model-data'); if(!box) return;
  if(!s){ box.innerHTML='<span class="mnote">Rate a few films to start.</span>'; return; }
  box.innerHTML =
    '<div class="mchip"><span class="wdot" style="background:var(--teal)"></span>Liked <b>'+(s['3']||0)+'</b></div>'+
    '<div class="mchip"><span class="wdot" style="background:var(--amber)"></span>Indifferent <b>'+(s['2']||0)+'</b></div>'+
    '<div class="mchip"><span class="wdot" style="background:var(--coral)"></span>Disliked <b>'+(s['1']||0)+'</b></div>'+
    '<div class="mchip"><span class="wdot" style="background:var(--graphite)"></span>Not interested <b>'+(s['notint']||0)+'</b></div>';
}
async function openModel(){
  document.getElementById('model-overlay').classList.add('open'); modelOpen = true;
  renderModelData(window._lastStats);
  try{ const d=await (await fetch('/api/weights')).json(); if(d.weights) modelWeights=Object.assign({}, W_DEFAULTS, d.weights); }catch(e){}
  renderWeightSliders();
}
function closeModel(){ document.getElementById('model-overlay').classList.remove('open'); modelOpen = false; }
document.getElementById('open-model').onclick = openModel;
document.getElementById('close-model').onclick = closeModel;
document.getElementById('model-overlay').onclick = e=>{ if(e.target.id==='model-overlay') closeModel(); };
for(const k in W_IDS){
  const s=document.getElementById(W_IDS[k]);
  s.oninput = ()=>{ modelWeights[k]=parseFloat(s.value);
    document.getElementById('wv-'+W_IDS[k].slice(2)).textContent='×'+Number(s.value).toFixed(1);
    if(k==='like') checkLikeGuard(); };
}
document.getElementById('w-reset').onclick = ()=>{ modelWeights=Object.assign({}, W_DEFAULTS); renderWeightSliders(); };
document.getElementById('do-train').onclick = ()=> runTrain('/api/train');
async function runTrain(url){
  const out = document.getElementById('train-out');
  out.innerHTML = '<div class="ml-msg">Saving your weights and training… the first run can fetch some TMDb metadata.</div>';
  try{ await fetch('/api/weights',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({weights:modelWeights})}); }catch(e){}
  let d; try { d = await (await fetch(url,{method:'POST'})).json(); }
  catch(e){ out.innerHTML = '<div class="ml-msg">Request failed.</div>'; return; }
  if(!d.ok){ out.innerHTML = '<div class="ml-msg">'+esc(d.error||'Failed.')+'</div>'; return; }
  const r = d.data; let h='';
  h += '<div class="stat">Mode: <b>'+esc(r.mode)+'</b>'+tip(MODE_TIP)+' &middot; '+r.n_pos+' toward / '+r.n_neg+' away</div>';
  if(r.cv){
    const auc=r.cv.auc; let delta='';
    if(lastAuc!=null){ const dl=auc-lastAuc; const up=dl>=0;
      delta=' <span style="font-size:12px;color:'+(up?'var(--teal)':'var(--coral)')+'">'+(up?'▲ +':'▼ −')+Math.abs(dl).toFixed(3)+' vs last ('+lastAuc.toFixed(3)+')</span>'; }
    h += '<div class="stat">'+r.cv.k+'-fold CV — ROC-AUC <b>'+auc.toFixed(3)+'</b>'+delta+tip(AUC_TIP)+' &middot; AP <b>'+r.cv.ap.toFixed(3)+'</b>'+tip(AP_TIP)+'</div>';
    lastAuc=auc;
  }
  if(r.message){ h += '<div class="ml-msg">'+esc(r.message)+'</div>'; }
  if(r.drivers && r.drivers.length){
    const mx = Math.max.apply(null, r.drivers.map(x=>Math.abs(x[1]))) || 1;
    h += '<div class="stat" style="margin-top:12px">Taste drivers'+tip(DRV_TIP)+' <span class="rs">(green = toward, coral = away)</span></div>';
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
  let payload; try { payload = (await (await fetch('/api/share')).json()).share; } catch(e){ note.textContent = ' Export failed.'; return; }
  if(!payload){ note.textContent = ' Export failed.'; return; }
  const name = document.getElementById('share-name').value.trim();
  payload.name = name; payload.exported = new Date().toISOString().slice(0,10);
  const blob = new Blob([JSON.stringify(payload)], {type:'application/json'});
  const url = URL.createObjectURL(blob); const a = document.createElement('a');
  a.href = url; a.download = (name ? name.replace(/[^a-z0-9]+/gi,'-').toLowerCase()+'-' : '') + 'tastebuds.json';
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  note.textContent = ' Exported '+(payload.likes||[]).length+' liked, '+(payload.dislikes||[]).length+' disliked, '+
    (payload.watchlist||[]).length+' watchlist'+(payload.model?', and your taste model.':'.');
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
      '<div class="fc">'+f.count+' liked'+(f.watch?' · '+f.watch+' watchlist':'')+(f.model?' · model':'')+'</div>'+
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
  renderFriendsList(); populateProfileSelect(); renderNightParty();
}
function doImport(ev){
  const file = ev.target.files && ev.target.files[0]; if(!file) return;
  const note = document.getElementById('friend-import-note'); note.textContent = 'Importing…';
  const reader = new FileReader();
  reader.onload = async ()=>{
    let data; try { data = JSON.parse(reader.result); } catch(e){ note.textContent = 'That file is not valid JSON.'; return; }
    try {
      const res = await (await fetch('/api/import-friend',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(data)})).json();
      friends = res.friends || []; renderFriendsList(); populateProfileSelect(); renderNightParty();
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
  renderFriendsList(); populateProfileSelect(); renderNightParty();
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

/* ---- Watchlist bottom sheet ---- */
let watchlistOpen=false, wlPosterObserver=null;
const WL_FILM='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2.4"/><path d="M3 9.5h18M8.5 4v16"/></svg>';
const WL_LINK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>';

let wlKind='watch';   // 'watch' | '3' (liked) | '1' (disliked)
const WL_META={watch:{title:'Watchlist', url:'/api/watchlist',
                      empty:'Nothing saved yet.<br>Tap <b>Add to Watchlist</b> on a film to keep it here.'},
               '3':{title:'Liked', url:'/api/rated?status=3',
                      empty:'No liked films yet.<br>Rate a few — the ones you <b>like</b> collect here.'},
               '1':{title:'Disliked', url:'/api/rated?status=1',
                      empty:'No disliked films yet.<br>Rate the misses too — dislikes sharpen your recommendations.'}};
async function openListSheet(kind){
  wlKind = WL_META[kind] ? kind : 'watch';
  const overlay=document.getElementById('wl-overlay'), sheet=document.getElementById('wl-sheet'), list=document.getElementById('wl-list');
  document.getElementById('wl-title').textContent = WL_META[wlKind].title;
  list.innerHTML='<div class="wl-empty">Loading…</div>';
  overlay.classList.add('open'); watchlistOpen=true;
  requestAnimationFrame(()=>requestAnimationFrame(()=>sheet.classList.add('up')));
  let items=[]; try{ items=(await (await fetch(WL_META[wlKind].url)).json()).items||[]; }catch(e){}
  renderWatchlist(items);
}
function openWatchlist(){ return openListSheet('watch'); }
function closeWatchlist(){
  const overlay=document.getElementById('wl-overlay'), sheet=document.getElementById('wl-sheet');
  sheet.classList.remove('up'); watchlistOpen=false;
  setTimeout(()=>overlay.classList.remove('open'), 420);
}
function wlCount(n){ document.getElementById('wl-cnt').textContent = n+(n===1?' film':' films'); }
function renderWatchlist(items){
  const list=document.getElementById('wl-list');
  wlCount(items.length);
  if(!items.length){ list.innerHTML='<div class="wl-empty">'+WL_META[wlKind].empty+'</div>'; return; }
  const isWatch = wlKind==='watch';
  const verdicts = isWatch
    ? '<span class="lbl">Mark watched</span>'+
      '<button class="wl-vb like" data-r="3">Liked</button>'+
      '<button class="wl-vb meh" data-r="2">Indifferent</button>'+
      '<button class="wl-vb dis" data-r="1">Disliked</button>'
    : '<span class="lbl">Re-rate</span>'+
      (wlKind!=='3'?'<button class="wl-vb like" data-r="3">Liked</button>':'')+
      '<button class="wl-vb meh" data-r="2">Indifferent</button>'+
      (wlKind!=='1'?'<button class="wl-vb dis" data-r="1">Disliked</button>':'');
  list.innerHTML=items.map(m=>{
    const genres=(m.genres||'').split(',').map(g=>g.trim()).filter(Boolean).map(esc).join(' · ');
    const dir=m.director?esc(m.director):'';
    const meta=[genres,dir].filter(Boolean).join(' · ');
    const thumb=m.poster ? '<img class="wl-thumb" src="'+escAttr(m.poster)+'" alt="">'
                         : '<div class="wl-thumb" data-pid="'+m.id+'">'+WL_FILM+'</div>';
    return '<div class="wl-item" data-id="'+m.id+'">'+thumb+
      '<div class="wl-info">'+
        '<div class="wl-row1"><div class="wl-t">'+esc(m.title)+' <span class="yr">('+esc(String(m.year||''))+')</span></div>'+
          '<div class="wl-ic">'+(isWatch?'<button class="wl-rm" aria-label="Remove from watchlist">'+TRASH_SVG+'</button>':'')+
            '<a href="'+escAttr(m.link)+'" target="_blank" rel="noopener" aria-label="Open on TMDb">'+WL_LINK+'</a></div></div>'+
        (meta?('<div class="wl-g">'+meta+'</div>'):'<div class="wl-g"></div>')+
        '<div class="wl-watch">'+verdicts+'</div>'+
      '</div></div>';
  }).join('');
  list.querySelectorAll('.wl-item').forEach(it=>{
    const id=+it.dataset.id, m=items.find(x=>x.id===id);
    it.querySelectorAll('.wl-vb').forEach(b=>b.onclick=()=>wlAct(it,'/api/setstatus',
      {movie:{id:m.id,title:m.title,year:m.year,genres:m.genres,link:m.link}, status:b.dataset.r}));
    const rm=it.querySelector('.wl-rm'); if(rm) rm.onclick=()=>wlAct(it,'/api/wl-remove',{id:m.id});
  });
  lazyPosters();
}
async function wlAct(it, url, body){
  try{ const d=await (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json(); if(d.stats) setTally(d.stats); }catch(e){}
  it.style.transition='opacity .2s'; it.style.opacity='0';
  setTimeout(()=>{ it.remove(); const n=document.querySelectorAll('#wl-list .wl-item').length; if(!n) renderWatchlist([]); else wlCount(n); }, 200);
}
function lazyPosters(){
  const ph=document.querySelectorAll('#wl-list .wl-thumb[data-pid]'); if(!ph.length) return;
  if(wlPosterObserver) wlPosterObserver.disconnect();
  wlPosterObserver=new IntersectionObserver((ents,obs)=>{
    ents.forEach(async e=>{
      if(!e.isIntersecting) return;
      const el=e.target, pid=el.dataset.pid; obs.unobserve(el); el.removeAttribute('data-pid');
      try{ const d=await (await fetch('/api/poster?id='+pid)).json();
        if(d.poster){ const img=new Image(); img.className='wl-thumb'; img.alt=''; img.src=d.poster; el.replaceWith(img); } }catch(e){}
    });
  }, {root:document.getElementById('wl-list'), rootMargin:'250px'});
  ph.forEach(el=>wlPosterObserver.observe(el));
}
document.getElementById('wl-close').onclick = closeWatchlist;
document.getElementById('wl-grip').onclick = closeWatchlist;
document.getElementById('wl-overlay').onclick = e=>{ if(e.target.id==='wl-overlay') closeWatchlist(); };

/* ---- Movie night: lights off, projector on ---- */
let nightOn=false, nightPicks=[], nightIdx=-1, nightParty=new Set();
try{ nightParty=new Set(JSON.parse(localStorage.getItem('nightParty')||'[]')); }catch(e){}
function nightToggle(){
  nightOn=!nightOn;
  document.body.classList.toggle('night', nightOn);
  document.getElementById('night-switch').classList.toggle('on', nightOn);
  document.getElementById('flipwrap').style.display = nightOn?'none':'';
  document.getElementById('night-wrap').hidden = !nightOn;
  if(nightOn){ projIdle(); refreshFriends(); }
}
function projIdle(){
  nightPicks=[]; nightIdx=-1;
  const btn=document.getElementById('night-pick'); btn.textContent='Start the projector'; btn.disabled=false;
  document.getElementById('night-note').textContent='';
  const el=document.getElementById('proj'); el.classList.remove('roll');
  el.innerHTML='<div class="pidle">The lights are off. Check in the buddies who are here tonight, then start the projector — it picks one film for all of you.</div>';
}
function renderNightParty(){
  const row=document.getElementById('night-party'); if(!row) return;
  nightParty=new Set([...nightParty].filter(n=>friends.some(f=>f.name===n)));
  try{ localStorage.setItem('nightParty', JSON.stringify([...nightParty])); }catch(e){}
  row.innerHTML='<span class="np-lbl">Tonight:</span><button class="npf you" tabindex="-1">You</button>'+
    friends.map(f=>'<button class="npf'+(nightParty.has(f.name)?' on':'')+'" data-n="'+escAttr(f.name)+'" title="'+
      (f.model?'Taste model imported':'No taste model in their file — scored by similarity to their likes')+'">'+esc(f.name)+
      '<span class="npx" title="Remove '+escAttr(f.name)+' and their imported file">&times;</span></button>').join('')+
    '<button class="ghost" id="night-add">Add a buddy</button>';
  row.querySelectorAll('.npf[data-n]').forEach(b=>b.onclick=(e)=>{
    const n=b.dataset.n, x=b.querySelector('.npx');
    if(e.target===x){                                    // the little x: two taps to remove
      if(x.classList.contains('arm')){
        removeFriend(n).then(()=>{ if(nightPicks.length) projIdle(); });
      } else {
        x.classList.add('arm'); x.textContent='Remove?';
        setTimeout(()=>{ if(x.isConnected){ x.classList.remove('arm'); x.innerHTML='&times;'; } }, 2600);
      }
      return;
    }
    if(nightParty.has(n)) nightParty.delete(n); else nightParty.add(n);
    const had=nightPicks.length; renderNightParty(); if(had) projIdle();   // party changed -> picks are stale
  });
  const add=document.getElementById('night-add');
  if(add) add.onclick=()=>document.getElementById('friend-file').click();
}
function tierLine(p){
  const k=p.scores?Object.keys(p.scores).length:1;
  if(p.tier===0) return k>1?"On everyone's watchlist":"On your watchlist";
  if(p.tier>=k) return "A fresh pick — on nobody's list yet";
  return 'On '+(k-p.tier)+' of '+k+' watchlists'+(p.on&&p.on.length?' — '+p.on.map(esc).join(', '):'');
}
function project(p){
  const el=document.getElementById('proj');
  el.classList.remove('roll'); void el.offsetWidth;
  const scores=p.scores?Object.entries(p.scores).map(([n,v])=>esc(n)+' '+Math.round(v*100)+'%').join(' · '):'';
  const whys=p.why?Object.entries(p.why).map(([n,w])=>'<div class="pwhy"><b>'+esc(n)+'</b> — '+esc(w)+'</div>').join(''):'';
  const poster=p.poster?'<img class="pp" src="'+escAttr(p.poster)+'" alt="">'
                       :'<div class="pp-ph" id="proj-poster-ph">'+WL_FILM+'</div>';
  const genres=Array.isArray(p.genres)?p.genres.join(' · '):String(p.genres||'');
  el.innerHTML=poster+
    '<h1>'+esc(p.title)+' <span class="yr">('+esc(String(p.year||''))+')</span></h1>'+
    (genres?'<div class="pmeta">'+esc(genres)+'</div>':'')+
    '<div class="ptier">'+tierLine(p)+'</div>'+
    (scores?'<div class="pscores">'+scores+(p.weakest&&Object.keys(p.scores).length>1?' · weakest fan: '+esc(p.weakest):'')+
      (p.floored?' · below someone\'s floor':'')+'</div>':'')+
    whys+
    (p.link?'<div class="pmeta" style="margin-top:10px"><a href="'+escAttr(p.link)+'" target="_blank" rel="noopener">Open on TMDb</a></div>':'');
  el.classList.add('roll');
  if(!p.poster && p.id){
    fetch('/api/poster?id='+p.id).then(r=>r.json()).then(d=>{
      if(d.poster){ p.poster=d.poster;
        const ph=document.getElementById('proj-poster-ph');
        if(ph && nightPicks[nightIdx]===p){ const img=new Image(); img.className='pp'; img.alt=''; img.src=d.poster; ph.replaceWith(img); } }
    }).catch(()=>{});
  }
}
async function nightPick(){
  const btn=document.getElementById('night-pick'), note=document.getElementById('night-note');
  if(nightPicks.length){
    nightIdx=(nightIdx+1)%nightPicks.length; project(nightPicks[nightIdx]);
    note.textContent = nightIdx===nightPicks.length-1 ? 'That was the last one — next wraps around.' : '';
    return;
  }
  btn.disabled=true; note.textContent='The projector is warming up…';
  let d=null;
  try{ d=await (await fetch('/api/movie-night',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({party:[...nightParty], n:12, combine:nightCombine})})).json(); }catch(e){}
  btn.disabled=false;
  if(!d||!d.ok){ note.textContent=(d&&d.error)?String(d.error):'The projector jammed — try again.'; return; }
  nightPicks=(d.data&&d.data.picks)||[]; nightIdx=-1;
  if(!nightPicks.length){
    document.getElementById('proj').innerHTML='<div class="pidle">'+esc((d.data&&d.data.message)||'Nothing to pick from yet — fill those watchlists.')+'</div>';
    note.textContent=''; return;
  }
  note.textContent=(d.data.filtered_by_providers?'Limited to your streaming services. ':'')+
    ((d.data.missing&&d.data.missing.length)?('Skipped (not imported): '+d.data.missing.map(esc).join(', ')+'.'):'');
  btn.textContent='Next pick';
  nightIdx=0; project(nightPicks[0]);
}
document.getElementById('night-switch').onclick = nightToggle;
document.getElementById('night-pick').onclick = nightPick;
document.getElementById('night-providers').onclick = openProviders;
let nightCombine='least_misery';
try{ const c=localStorage.getItem('nightCombine');
  if(['least_misery','average','avg_no_misery'].indexOf(c)>=0) nightCombine=c; }catch(e){}
(function(){
  const seg=document.getElementById('night-combine');
  seg.querySelectorAll('button').forEach(b=>{
    b.classList.toggle('on', b.dataset.c===nightCombine);
    b.onclick=()=>{
      nightCombine=b.dataset.c;
      seg.querySelectorAll('button').forEach(x=>x.classList.toggle('on', x===b));
      try{ localStorage.setItem('nightCombine', nightCombine); }catch(e){}
      if(nightPicks.length) projIdle();    // strategy changed -> re-roll under the new rule
    };
  });
})();

/* ---- First-run onboarding ---- */
let onbSeed=null, onbSteps=[], onbIdx=0, onboardingActive=false, onbSearchTimer=null;
function onbShow(){
  document.querySelectorAll('#onb-overlay .onb-step').forEach(s=>s.classList.remove('on'));
  const el=document.getElementById('onb-'+onbSteps[onbIdx]); if(el) el.classList.add('on');
  document.getElementById('onb-dots').innerHTML = onbSteps.map((s,i)=>'<span class="odot'+(i<=onbIdx?' on':'')+'"></span>').join('');
  document.getElementById('onb-count').textContent = 'Step '+(onbIdx+1)+' of '+onbSteps.length;
}
function onbNext(){ if(onbIdx<onbSteps.length-1){ onbIdx++; onbShow(); } }
function onbBack(){ if(onbIdx>0){ onbIdx--; onbShow(); } }
function onbOpen(showKey){
  onbSteps = showKey ? ['welcome','key','channel','rate'] : ['welcome','channel','rate'];
  onbIdx=0; onbSeed=null;
  document.getElementById('onb-create').disabled=true;
  document.getElementById('onb-picked').textContent='';
  document.getElementById('onb-res').innerHTML='';
  document.getElementById('onb-key-input').value='';
  document.getElementById('onb-q').value='';
  document.getElementById('onb-overlay').classList.add('open');
  onbShow();
}
function onbCloseOverlay(){ document.getElementById('onb-overlay').classList.remove('open'); onboardingActive=false; }

async function onbSaveKey(){
  const err=document.getElementById('onb-key-err');
  const key=document.getElementById('onb-key-input').value.trim();
  if(!key){ err.textContent='Paste your key to continue.'; return; }
  err.textContent='Checking…';
  let d; try{ d=await (await fetch('/api/set-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})})).json(); }
  catch(e){ err.textContent='Could not reach the app. Is it still running?'; return; }
  if(!d.ok){ err.textContent=d.error||'That key didn’t work.'; return; }
  err.textContent=''; onbNext();
}
async function onbSearch(){
  const q=document.getElementById('onb-q').value.trim(), box=document.getElementById('onb-res');
  if(!q){ box.innerHTML=''; return; }
  let d; try{ d=await (await fetch('/api/search-movie?q='+encodeURIComponent(q))).json(); }catch(e){ return; }
  const list=d.results||[];
  box.innerHTML=list.map((m,i)=>'<div class="pk" data-i="'+i+'"><span>'+esc(m.title)+'</span><span class="meta">'+(m.year||'')+(m.rating?(' · '+m.rating+' ★'):'')+'</span></div>').join('');
  box.querySelectorAll('.pk').forEach(el=>el.onclick=()=>{
    onbSeed=list[+el.dataset.i];
    document.getElementById('onb-q').value=onbSeed.title; box.innerHTML='';
    document.getElementById('onb-picked').textContent='Selected: '+onbSeed.title+(onbSeed.year?(' ('+onbSeed.year+')'):'');
    document.getElementById('onb-create').disabled=false;
  });
}
async function onbCreate(){
  if(!onbSeed) return;
  const btn=document.getElementById('onb-create'); btn.disabled=true; btn.textContent='Creating…';
  const ch={kind:'movie', name:'More like '+onbSeed.title, enabled:true, movie_id:onbSeed.id, movie_title:onbSeed.title, style:'custom', sort:'popularity.desc', vote_count_gte:0, vote_avg_gte:0};
  try{ await fetch('/api/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channels:[ch]})}); }catch(e){}
  const mv={id:onbSeed.id, title:onbSeed.title, year:onbSeed.year||'', genres:'', link:'https://www.themoviedb.org/movie/'+onbSeed.id};
  try{ const r=await fetch('/api/rate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rating:3, movie:mv, stack:false})}); const d=await r.json(); if(d.stats) setTally(d.stats); }catch(e){}
  btn.textContent='Create channel';
  onbNext();
}
function onbFinish(){ onbCloseOverlay(); loadNext(); nextRec(); }
function onbSetupSelf(){ onbCloseOverlay(); loadNext(); openSettings(); }

document.getElementById('onb-start').onclick = onbNext;
document.getElementById('onb-key-continue').onclick = onbSaveKey;
document.getElementById('onb-key-back').onclick = onbBack;
document.getElementById('onb-key-input').addEventListener('keydown', e=>{ if(e.key==='Enter') onbSaveKey(); });
document.getElementById('onb-q').oninput = ()=>{ clearTimeout(onbSearchTimer); onbSearchTimer=setTimeout(onbSearch,260); };
document.getElementById('onb-create').onclick = onbCreate;
document.getElementById('onb-self').onclick = onbSetupSelf;
document.getElementById('onb-rate-back').onclick = onbBack;
document.getElementById('onb-finish').onclick = onbFinish;
document.getElementById('onb-overlay').onclick = e=>{ if(e.target.id==='onb-overlay'){ onbCloseOverlay(); loadNext(); } };

(async function boot(){
  let st={}; try{ st=await (await fetch('/api/state')).json(); }catch(e){}
  onboardingActive = !!st.needs_onboarding;   // only on a genuinely fresh install
  if(onboardingActive){ onbOpen(!st.has_key); } else { loadNext(); }
  try { recProfile = localStorage.getItem('recProfile') || 'you'; } catch(e){}
  await refreshFriends();
  const sel = document.getElementById('profile-select'); if(sel) sel.value = recProfile;
  if(!onboardingActive) nextRec();
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

    def _host_ok(self):
        # Accept only loopback Host headers. This closes DNS-rebinding: a remote
        # page can re-point its own hostname at 127.0.0.1, but its requests still
        # carry Host: that-hostname, so they're rejected here before anything runs
        # (including serving the page that embeds the token).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("", "127.0.0.1", "localhost", "::1")

    def do_GET(self):
        if not self._host_ok():
            self._send(403, json.dumps({"error": "forbidden"}))
            return
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
        elif self.path.startswith("/api/search-movie"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, json.dumps({"results": search_movies(q)}))
        elif self.path.startswith("/api/search-person"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, json.dumps({"results": search_people(q)}))
        elif self.path.startswith("/api/search-keyword"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._send(200, json.dumps({"results": search_keywords(q)}))
        elif self.path.startswith("/api/state"):
            self._send(200, json.dumps({"needs_onboarding": needs_onboarding(), "has_key": bool(API_KEY)}))
        elif self.path.startswith("/api/weights"):
            self._send(200, json.dumps({"weights": load_weights()}))
        elif self.path.startswith("/api/watchlist"):
            self._send(200, json.dumps({"items": get_watchlist()}))
        elif self.path.startswith("/api/rated"):
            from urllib.parse import urlparse, parse_qs
            st = parse_qs(urlparse(self.path).query).get("status", ["3"])[0]
            st = st if st in ("3", "2", "1", "0") else "3"
            self._send(200, json.dumps({"items": get_rated(st)}))
        elif self.path.startswith("/api/poster"):
            from urllib.parse import urlparse, parse_qs
            pid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            self._send(200, json.dumps({"poster": fetch_poster(int(pid)) if pid.isdigit() else None}))
        elif self.path.startswith("/api/likes"):
            self._send(200, json.dumps({"likes": liked_export()}))
        elif self.path.startswith("/api/share"):
            self._send(200, json.dumps({"share": share_export()}))
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
        if not self._host_ok():
            self._send(403, json.dumps({"error": "forbidden"}))
            return
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

        elif self.path.startswith("/api/set-key"):
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                ok, msg = save_api_key(body.get("key", ""))
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True} if ok else {"ok": False, "error": msg}))

        elif self.path.startswith("/api/wl-remove"):
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                mid = int(body["id"])
                with _io_lock:
                    remove_row_by_id(WATCHLIST_PATH, WATCH_ID_COL, mid)
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "stats": stats()}))

        elif self.path.startswith("/api/weights"):
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                w = save_weights(body.get("weights", {}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"ok": True, "weights": w}))

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

        elif self.path.startswith("/api/movie-night"):
            party, n, combine = [], 8, "least_misery"
            try:
                if raw:
                    body = json.loads(raw)
                    party = [str(x)[:60] for x in (body.get("party") or [])][:12]
                    n = max(1, min(int(body.get("n", 8)), 20))
                    c = str(body.get("combine") or "")
                    if c in ("least_misery", "average", "avg_no_misery"):
                        combine = c
            except Exception:
                pass
            ok, data = run_recommender(["--movie-night", "--party", json.dumps(party),
                                        "--n", str(n), "--combine", combine])
            if ok and isinstance(data, dict):
                for p in data.get("picks", []):
                    if isinstance(p.get("id"), int) and not p.get("poster"):
                        p["poster"] = _poster_cached(p["id"]) or ""
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
    load_channels()  # (fresh installs have none yet; onboarding/UI creates the first)
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
