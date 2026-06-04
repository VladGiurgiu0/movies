#!/usr/bin/env python3
"""
tmdb_features.py — fetch + cache TMDb metadata used as ML features.

Only the standard library is used here. Metadata for each movie id is fetched
once from TMDb (movie details + keywords + credits) and cached to
`features_cache.json`, so repeat runs are offline and fast.

The fetcher is injectable (`fetch=`) so the pipeline can be tested without
network access.
"""

import os
import json
import urllib.parse
import urllib.request

TMDB_API = "https://api.themoviedb.org/3"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # the movies/ folder


def resolve_api_key():
    key = os.environ.get("TMDB_API_KEY", "").strip()
    if key:
        return key
    for p in (os.path.join(ROOT, "tmdb_key.txt"), os.path.join(HERE, "tmdb_key.txt")):
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
    return ""


def _http_fetch(movie_id, api_key):
    """Default network fetcher: one call with appended sub-resources."""
    params = {"api_key": api_key, "append_to_response": "keywords,credits", "language": "en-US"}
    url = f"{TMDB_API}/movie/{movie_id}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "ml-recommender/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize(raw):
    """Reduce a raw TMDb movie payload to the fields we featurize on."""
    kws = [k["name"].lower() for k in raw.get("keywords", {}).get("keywords", [])]
    credits = raw.get("credits", {})
    director = next((c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"), None)
    cast = [c["name"] for c in credits.get("cast", [])[:5]]
    return {
        "keywords": kws,
        "director": director,
        "cast": cast,
        "lang": raw.get("original_language"),
        "runtime": raw.get("runtime") or 0,
        "vote_average": raw.get("vote_average") or 0.0,
        "popularity": raw.get("popularity") or 0.0,
        "poster_path": raw.get("poster_path"),
        "overview": raw.get("overview") or "",
    }


def get_features(ids, cache_path, api_key=None, allow_network=True, fetch=None):
    """Return {id: metadata}. Fetches only ids missing from the cache."""
    fetch = fetch or _http_fetch
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    def needs(i):
        e = cache.get(str(i))
        return e is None or (isinstance(e, dict) and e.get("_error"))

    todo = [i for i in ids if needs(i)]
    if todo and allow_network and api_key:
        attempted = 0
        for i in todo:
            try:
                cache[str(i)] = normalize(fetch(i, api_key))
            except Exception:
                cache[str(i)] = {"_error": True}  # mark failed; retried on a later run
            attempted += 1
        if attempted:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=0)

    # strip failure markers so callers just see "no metadata"
    return {int(k): ({} if (isinstance(v, dict) and v.get("_error")) else v) for k, v in cache.items()}
