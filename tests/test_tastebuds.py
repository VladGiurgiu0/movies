#!/usr/bin/env python3
"""
Tests for Tastebuds.

Run from the repo root with no extra dependencies for the rater tests:

    python3 -m unittest discover -s tests -v

The recommender tests additionally need NumPy (the recommender's only
dependency); they skip automatically if it isn't installed.

These cover the parts most likely to break silently on a future change:
markdown table parsing & counts, atomic writes, the post-rename parsing of
not-interested.md, the ranking metrics (ROC-AUC / average precision), how
ratings become training labels, and the "why this pick" humanizer.
"""

import os
import sys
import json
import tempfile
import unittest

# Make the repo importable regardless of where the tests are run from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ML_DIR = os.path.join(ROOT, "ml-recommender")
for p in (ROOT, ML_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import tastebuds as tb  # noqa: E402

try:
    import numpy  # noqa: F401
    import recommend as rec  # noqa: E402
    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False


# --------------------------------------------------------------------------
# Sample data builders
# --------------------------------------------------------------------------
MOVIES_MD = """# My Movie Ratings

## Rating code  (this legend table is OUTSIDE the markers and must be ignored)

| Code | Meaning |
|------|---------|
| 3    | liked   |

<!-- TABLE-START -->

| Rating | Status | Title | Year | Genres | TMDb ID | Link | Rated on |
|--------|--------|-------|------|--------|---------|------|----------|
| 3 | Liked | Aftersun | 2022 | Drama | 111 | http://x/111 | 2026-01-01 |
| 1 | Disliked | Loud Movie | 2002 | Comedy | 222 | http://x/222 | 2026-01-01 |
| 2 | Indifferent | Meh Film | 2003 | Drama | 333 | http://x/333 | 2026-01-01 |
| 0 | Not seen | On The List | 2004 | Drama | 444 | http://x/444 | 2026-01-01 |

<!-- TABLE-END -->

| 9 | This row is after TABLE-END and must also be ignored | x | 1 | y | 999 | z | w |
"""

# An old (pre-rename) not-interested file: heading says "Dismissed", but the
# rows between the markers must still parse. This is the state the in-place
# dismissed.md -> not-interested.md migration leaves behind.
NOT_INTERESTED_OLD_MD = """# Dismissed

<!-- TABLE-START -->

| Title | Year | Genres | TMDb ID | Link | Dismissed on |
|-------|------|--------|---------|------|--------------|
| Skip One | 2010 | Drama | 555 | http://x/555 | 2026-01-01 |
| Skip Two | 2011 | Comedy | 666 | http://x/666 | 2026-01-01 |

<!-- TABLE-END -->
"""


class TestRaterParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.movies = os.path.join(self.tmp, "movies.md")
        self.notint = os.path.join(self.tmp, "not-interested.md")
        with open(self.movies, "w", encoding="utf-8") as f:
            f.write(MOVIES_MD)
        with open(self.notint, "w", encoding="utf-8") as f:
            f.write(NOT_INTERESTED_OLD_MD)

    def test_ids_only_between_markers(self):
        # The legend row (3 cells) and the post-END row (id 999) must be ignored.
        ids = tb._ids_in(self.movies, tb.MD_ID_COL)
        self.assertEqual(ids, {111, 222, 333, 444})
        self.assertNotIn(999, ids)

    def test_stats_counts_each_code_once(self):
        # stats() reads the module-level MD_PATH; point it at our fixture.
        orig_md, orig_w = tb.MD_PATH, tb.WATCHLIST_PATH
        tb.MD_PATH = self.movies
        tb.WATCHLIST_PATH = os.path.join(self.tmp, "watchlist.md")
        try:
            s = tb.stats()
        finally:
            tb.MD_PATH, tb.WATCHLIST_PATH = orig_md, orig_w
        self.assertEqual((s["3"], s["2"], s["1"], s["0"]), (1, 1, 1, 1))
        self.assertEqual(s["watch"], 0)

    def test_not_interested_parses_after_rename(self):
        # Old "Dismissed" heading, but rows still parse (id column index 3).
        ids = tb._ids_in(self.notint, tb.NOT_INTERESTED_ID_COL)
        self.assertEqual(ids, {555, 666})


class TestAtomicWrite(unittest.TestCase):
    def test_writes_content_and_leaves_no_temp(self):
        tmp = tempfile.mkdtemp()
        target = os.path.join(tmp, "out.json")
        tb._atomic_write(target, '{"hello": 1}')
        with open(target, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"hello": 1})
        # the temp-then-replace must not leave a ".tmp_*.swap" behind
        leftovers = [n for n in os.listdir(tmp) if n.startswith(".tmp_")]
        self.assertEqual(leftovers, [])

    def test_overwrite_is_clean(self):
        tmp = tempfile.mkdtemp()
        target = os.path.join(tmp, "out.txt")
        tb._atomic_write(target, "first")
        tb._atomic_write(target, "second")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "second")


class TestRowEscaping(unittest.TestCase):
    def test_pipe_in_title_does_not_break_a_row(self):
        tmp = tempfile.mkdtemp()
        orig = tb.MD_PATH
        tb.MD_PATH = os.path.join(tmp, "movies.md")
        try:
            tb.append_rating({"id": 777, "title": "Either|Or", "year": "2020",
                              "genres": "Drama|Comedy", "link": "http://x/777"}, 3)
            ids = tb._ids_in(tb.MD_PATH, tb.MD_ID_COL)
        finally:
            tb.MD_PATH = orig
        self.assertEqual(ids, {777})  # one clean row, pipe replaced, id intact


class TestSanitizeChannel(unittest.TestCase):
    def test_defaults_and_clamps(self):
        c = tb.sanitize_channel({"name": "", "genres": [1, 2, 3, 4, 5, 6, 7],
                                 "keywords": ["a"] * 10, "style": "bogus"})
        self.assertEqual(c["name"], "Untitled")
        self.assertLessEqual(len(c["genres"]), 5)
        self.assertLessEqual(len(c["keywords"]), 6)
        self.assertEqual(c["style"], "custom")     # unknown style falls back
        self.assertIn("vote_avg_gte", c)


@unittest.skipUnless(HAS_NUMPY, "NumPy required for the recommender tests")
class TestRecommenderMetrics(unittest.TestCase):
    def test_roc_auc_perfect_and_inverted(self):
        y = [0, 0, 1, 1]
        self.assertAlmostEqual(rec.roc_auc(y, [0.1, 0.2, 0.3, 0.4]), 1.0)
        self.assertAlmostEqual(rec.roc_auc(y, [0.4, 0.3, 0.2, 0.1]), 0.0)

    def test_roc_auc_handles_ties(self):
        # all-equal scores -> no ranking information -> 0.5
        self.assertAlmostEqual(rec.roc_auc([0, 1, 0, 1], [0.5, 0.5, 0.5, 0.5]), 0.5)

    def test_average_precision_perfect(self):
        self.assertAlmostEqual(rec.average_precision([0, 0, 1, 1], [0.1, 0.2, 0.3, 0.4]), 1.0)


@unittest.skipUnless(HAS_NUMPY, "NumPy required for the recommender tests")
class TestRecommenderLabelling(unittest.TestCase):
    def test_build_dataset_label_mapping(self):
        movies = [
            {"id": 1, "rating": 3},   # liked -> positive
            {"id": 2, "rating": 2},   # indifferent -> negative
            {"id": 3, "rating": 1},   # disliked -> negative
            {"id": 4, "rating": 0},   # not seen -> excluded
        ]
        train, y, w = rec.build_dataset(movies, watchlist=[])
        ids = [m["id"] for m in train]
        self.assertEqual(ids, [1, 2, 3])          # 0-rated dropped
        self.assertEqual(y, [1, 0, 0])            # 3 -> 1, {1,2} -> 0
        self.assertEqual(len(w), 3)

    def test_humanize_feature(self):
        self.assertEqual(rec.humanize_feature("dir=Greta Gerwig"), "directed by Greta Gerwig")
        self.assertEqual(rec.humanize_feature("lang=fr"), "French-language")
        self.assertEqual(rec.humanize_feature("decade=1990"), "1990s")
        self.assertEqual(rec.humanize_feature("genre=Drama"), "Drama")
        self.assertEqual(rec.humanize_feature("kw=heist"), "heist")
        self.assertEqual(rec.humanize_feature("year"), "year")  # no "=" -> unchanged


@unittest.skipUnless(HAS_NUMPY, "NumPy required for the recommender tests")
class TestRecommenderParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.movies = os.path.join(self.tmp, "movies.md")
        with open(self.movies, "w", encoding="utf-8") as f:
            f.write(MOVIES_MD)

    def test_load_movies_reads_ratings_and_ids(self):
        items = rec.load_movies(self.movies)
        by_id = {m["id"]: m for m in items}
        self.assertEqual(set(by_id), {111, 222, 333, 444})
        self.assertEqual(by_id[111]["rating"], 3)
        self.assertEqual(by_id[111]["genres"], ["Drama"])


class TestChannelKinds(unittest.TestCase):
    """The redesigned channels: genre / movie / person / keyword, with
    backward-compatible legacy channels and the keyword any/all (OR/AND) switch."""

    def setUp(self):
        self._calls = []
        self._orig = (tb.tmdb_get, tb.resolve_keyword, tb.load_providers, tb._genre_map, tb.API_KEY)
        tb.load_providers = lambda: {"region": "", "providers": []}   # no streaming filter
        tb.resolve_keyword = lambda n: {"coming of age": 10683, "first love": 157303}.get(n.strip().lower())
        tb._genre_map = {18: "Drama", 35: "Comedy"}
        tb.API_KEY = "test"

        def fake(path, params=None):
            self._calls.append((path, dict(params or {})))
            if path.endswith("/recommendations"):
                return {"results": [{"id": 9, "title": "Rec", "poster_path": "/r.jpg", "genre_ids": [35],
                                     "vote_average": 6.5, "release_date": "2019-01-01", "overview": "o"}]}
            if path.endswith("/similar"):
                return {"results": []}
            return {"results": [{"id": 1, "title": "X", "poster_path": "/p.jpg", "genre_ids": [18],
                                 "vote_average": 7.0, "release_date": "2020-01-01", "overview": "o"}],
                    "total_results": 42}
        tb.tmdb_get = fake

    def tearDown(self):
        (tb.tmdb_get, tb.resolve_keyword, tb.load_providers, tb._genre_map, tb.API_KEY) = self._orig

    def _last_discover(self):
        for p, pr in reversed(self._calls):
            if p == "discover/movie":
                return pr
        return None

    def test_legacy_channel_maps_to_discover(self):
        c = tb.sanitize_channel({"name": "Indie", "genres": [18], "keywords": ["independent film"], "style": "acclaimed"})
        self.assertEqual(c["kind"], "discover")
        self.assertEqual(c["match"], "all")          # default preserves old AND behaviour
        self.assertEqual(c["genres"], [18])

    def test_keyword_any_is_or_joined(self):
        c = tb.sanitize_channel({"kind": "discover", "keywords": ["coming of age", "first love"], "match": "any"})
        tb.fetch_channel(c)
        self.assertEqual(self._last_discover().get("with_keywords"), "10683|157303")

    def test_keyword_all_is_and_joined(self):
        c = tb.sanitize_channel({"kind": "discover", "keywords": ["coming of age", "first love"], "match": "all"})
        tb.fetch_channel(c)
        self.assertEqual(self._last_discover().get("with_keywords"), "10683,157303")

    def test_person_channel_uses_with_people(self):
        c = tb.sanitize_channel({"kind": "person", "person_id": 1243, "person_name": "Woody Allen"})
        self.assertEqual(c["kind"], "person")
        tb.fetch_channel(c)
        d = self._last_discover()
        self.assertEqual(d.get("with_people"), "1243")
        self.assertNotIn("with_keywords", d)

    def test_movie_seed_uses_recommendations_not_discover(self):
        c = tb.sanitize_channel({"kind": "movie", "movie_id": 5, "movie_title": "Aftersun"})
        out = tb.fetch_channel(c)
        paths = [p for p, _ in self._calls]
        self.assertTrue(any("movie/5/recommendations" in p for p in paths))
        self.assertFalse(any(p == "discover/movie" for p in paths))
        self.assertGreaterEqual(len(out), 1)

    def test_search_helpers_shapes(self):
        def fake2(path, params=None):
            if path == "search/movie":
                return {"results": [{"id": 5, "title": "Aftersun", "release_date": "2022-05-01", "vote_average": 7.7}]}
            if path == "search/person":
                return {"results": [{"id": 1243, "name": "Woody Allen", "known_for_department": "Directing"}]}
            if path == "search/keyword":
                return {"results": [{"id": 10683, "name": "coming of age"}]}
            return {"results": [], "total_results": 7}
        tb.tmdb_get = fake2
        self.assertEqual(tb.search_movies("after")[0]["year"], "2022")
        self.assertEqual(tb.search_people("woody")[0]["dept"], "Directing")
        kw = tb.search_keywords("coming")[0]
        self.assertEqual((kw["name"], kw["count"]), ("coming of age", 7))


class TestOnboarding(unittest.TestCase):
    """First-run state + in-app key save (no real key/network touched)."""

    def test_save_api_key_validates_and_persists(self):
        orig = (tb.KEY_PATH, tb._test_key, tb.API_KEY, tb._genre_map)
        tmp = tempfile.mkdtemp()
        tb.KEY_PATH = os.path.join(tmp, "tmdb_key.txt")
        tb._test_key = lambda k: None                       # pretend TMDb accepted it
        try:
            ok, _ = tb.save_api_key("0123456789abcdef0123456789abcdef")
            self.assertTrue(ok)
            self.assertEqual(open(tb.KEY_PATH).read().strip(), "0123456789abcdef0123456789abcdef")
            self.assertEqual(tb.API_KEY, "0123456789abcdef0123456789abcdef")

            ok_short, _ = tb.save_api_key("abc")             # too short -> rejected, not persisted
            self.assertFalse(ok_short)

            import urllib.error
            def reject(k):
                raise urllib.error.HTTPError("u", 401, "no", {}, None)
            tb._test_key = reject
            ok_bad, _ = tb.save_api_key("z" * 32)            # TMDb says no -> rejected
            self.assertFalse(ok_bad)
            # the good key from earlier is still the one on disk
            self.assertEqual(open(tb.KEY_PATH).read().strip(), "0123456789abcdef0123456789abcdef")
        finally:
            (tb.KEY_PATH, tb._test_key, tb.API_KEY, tb._genre_map) = orig

    def test_needs_onboarding_logic(self):
        orig = (tb.load_channels, tb.rated_ids, tb.watchlist_ids)
        try:
            tb.load_channels = lambda: []
            tb.rated_ids = lambda: set()
            tb.watchlist_ids = lambda: set()
            self.assertTrue(tb.needs_onboarding())           # fresh: no channels, nothing rated
            tb.load_channels = lambda: [{"kind": "movie"}]
            self.assertFalse(tb.needs_onboarding())          # has a channel
            tb.load_channels = lambda: []
            tb.rated_ids = lambda: {1}
            self.assertFalse(tb.needs_onboarding())          # has a rating
        finally:
            (tb.load_channels, tb.rated_ids, tb.watchlist_ids) = orig


if __name__ == "__main__":
    unittest.main()
