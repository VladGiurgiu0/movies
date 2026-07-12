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
    def test_build_dataset_weights_and_not_interested(self):
        movies = [
            {"id": 1, "rating": 3},   # liked -> positive
            {"id": 2, "rating": 2},   # indifferent -> negative
            {"id": 3, "rating": 1},   # disliked -> negative
            {"id": 4, "rating": 0},   # not seen -> excluded
        ]
        ni = [{"id": 5, "rating": None}]   # not interested -> negative when weight > 0
        w = {"like": 1.0, "indifferent": 0.5, "disliked": 1.0, "not_interested": 0.3}
        train, y, ww = rec.build_dataset(movies, w, ni)
        self.assertEqual([m["id"] for m in train], [1, 2, 3, 5])
        self.assertEqual(y, [1, 0, 0, 0])
        self.assertEqual(ww, [1.0, 0.5, 1.0, 0.3])     # each carries its reaction's weight
        # a reaction at weight 0 is dropped entirely
        w0 = {"like": 1.0, "indifferent": 0.0, "disliked": 1.0, "not_interested": 0.0}
        train2, _, _ = rec.build_dataset(movies, w0, ni)
        self.assertEqual([m["id"] for m in train2], [1, 3])   # meh + not-interested dropped

    def test_data_signature_changes_with_weights(self):
        movies = [{"id": 1, "rating": 3}, {"id": 2, "rating": 1}]
        a = rec.build_dataset(movies, {"like": 1.0, "indifferent": 0.5, "disliked": 1.0, "not_interested": 0.3}, [])
        b = rec.build_dataset(movies, {"like": 1.0, "indifferent": 0.5, "disliked": 0.4, "not_interested": 0.3}, [])
        self.assertNotEqual(rec.data_signature(*a), rec.data_signature(*b))   # weight change -> retrain

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


class TestWatchlistView(unittest.TestCase):
    WL_MD = (
        "# Watchlist\n\n<!-- TABLE-START -->\n\n"
        "| Title | Year | Genres | TMDb ID | Link | Added on |\n"
        "|-------|------|--------|---------|------|----------|\n"
        "| Older | 2001 | Drama | 11 | http://x/11 | 2026-01-01 |\n"
        "| Newer | 2002 | Comedy | 22 | http://x/22 | 2026-01-02 |\n\n"
        "<!-- TABLE-END -->\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wl = os.path.join(self.tmp, "watchlist.md")
        with open(self.wl, "w", encoding="utf-8") as f:
            f.write(self.WL_MD)
        self._orig = (tb.WATCHLIST_PATH, tb._poster_cache, tb._director_cache)
        tb.WATCHLIST_PATH = self.wl
        tb._poster_cache = {}      # nothing cached -> posters come back None
        tb._director_cache = {}

    def tearDown(self):
        (tb.WATCHLIST_PATH, tb._poster_cache, tb._director_cache) = self._orig

    def test_parses_newest_first_no_network(self):
        items = tb.get_watchlist()
        self.assertEqual([m["id"] for m in items], [22, 11])   # reversed = newest on top
        self.assertEqual(items[0]["title"], "Newer")
        self.assertIsNone(items[0]["poster"])                  # cache-only, no fetch
        self.assertEqual(items[1]["genres"], "Drama")


class TestWeights(unittest.TestCase):
    def test_save_weights_clamps_and_persists(self):
        orig = tb.WEIGHTS_PATH
        tb.WEIGHTS_PATH = os.path.join(tempfile.mkdtemp(), "model_weights.json")
        try:
            w = tb.save_weights({"like": 5, "indifferent": -1, "disliked": 1.4,
                                 "not_interested": 0.3, "bogus": 9})
            self.assertEqual(w["like"], 2.0)            # clamped to max 2
            self.assertEqual(w["indifferent"], 0.0)     # clamped to min 0
            self.assertEqual(w["disliked"], 1.4)        # in range, kept
            self.assertNotIn("bogus", w)                # unknown key ignored
            self.assertEqual(tb.load_weights()["disliked"], 1.4)
        finally:
            tb.WEIGHTS_PATH = orig

    def test_stats_includes_not_interested_count(self):
        self.assertIn("notint", tb.stats())


class TestQREncoder(unittest.TestCase):
    """The stdlib QR encoder behind 'Pair a phone'."""

    def test_reed_solomon_known_vector(self):
        # the classic HELLO WORLD v1-Q tutorial vector
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(tb._rs_ecc(data, 10),
                         [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_format_and_version_bch_known_vectors(self):
        f = ((0b01000 << 10) | tb._bch_remainder(0b01000, 0x537, 11, 15)) ^ 0x5412
        self.assertEqual(format(f, "015b"), "111011111000100")     # ECC-L, mask 0
        f = ((0b00000 << 10) | tb._bch_remainder(0b00000, 0x537, 11, 15)) ^ 0x5412
        self.assertEqual(format(f, "015b"), "101010000010010")     # ECC-M, mask 0
        vi = (7 << 12) | tb._bch_remainder(7, 0x1F25, 13, 18)
        self.assertEqual(format(vi, "018b"), "000111110010010100")  # version 7 info

    def test_pairing_urls_decode(self):
        try:
            import cv2
            import numpy as np
        except Exception:
            self.skipTest("OpenCV not installed")
        det = cv2.QRCodeDetector()
        for t in ("http://192.168.1.23:8765/?t=" + "0123456789abcdef" * 2,
                  "http://10.0.0.5:8765/?t=" + "f" * 32):
            M = tb._qr_matrix(t.encode())
            n, scale, border = len(M), 10, 4
            img = np.full(((n + 2 * border) * scale,) * 2, 255, np.uint8)
            for r in range(n):
                for c in range(n):
                    if M[r][c]:
                        img[(r + border) * scale:(r + border + 1) * scale,
                            (c + border) * scale:(c + border + 1) * scale] = 0
            decoded, _, _ = det.detectAndDecode(img)
            self.assertEqual(decoded, t)

    def test_svg_shape(self):
        svg = tb.qr_svg("http://192.168.0.2:8765/?t=" + "a" * 32)
        self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"))
        self.assertIn("crispEdges", svg)


WATCHLIST_SAMPLE_MD = (
    "# Watchlist\n\n<!-- TABLE-START -->\n\n"
    "| Title | Year | Genres | TMDb ID | Link | Added on |\n"
    "|-------|------|--------|---------|------|----------|\n"
    "| Wish One | 2019 | Drama | 777 | http://x/777 | 2026-01-02 |\n\n"
    "<!-- TABLE-END -->\n"
)


class TestShareExport(unittest.TestCase):
    """The share file: full versioned payload, model attached only when valid."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

        def w(name, text):
            p = os.path.join(self.tmp, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return p

        self._orig = (tb.MD_PATH, tb.WATCHLIST_PATH, tb.NOT_INTERESTED_PATH, tb.ML_DATA_DIR)
        tb.MD_PATH = w("movies.md", MOVIES_MD)
        tb.WATCHLIST_PATH = w("watchlist.md", WATCHLIST_SAMPLE_MD)
        tb.NOT_INTERESTED_PATH = w("not-interested.md", NOT_INTERESTED_OLD_MD)
        tb.ML_DATA_DIR = self.tmp     # model.json (if any) lives here during the test

    def tearDown(self):
        (tb.MD_PATH, tb.WATCHLIST_PATH, tb.NOT_INTERESTED_PATH, tb.ML_DATA_DIR) = self._orig

    def test_payload_shape_and_seen_ids(self):
        s = tb.share_export()
        self.assertEqual(s["app"], "tastebuds")
        self.assertEqual(s["version"], tb.SHARE_VERSION)
        self.assertEqual([m["id"] for m in s["likes"]], [111])
        self.assertEqual([m["id"] for m in s["dislikes"]], [222])
        self.assertEqual([m["id"] for m in s["watchlist"]], [777])
        self.assertEqual(s["seen"], [111, 222, 333])   # rated 1/2/3; the 0-shortlist is unseen
        self.assertNotIn("model", s)                   # no model.json -> nothing attached

    def test_model_attached_only_when_well_formed(self):
        good = {"vocab": ["genre=Drama"], "theta": [0.5, 0.1], "mu": [0.2], "sd": [1.0],
                "cv": {"auc": 0.7}, "version": "v1"}
        mp = os.path.join(self.tmp, "model.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(good, f)
        s = tb.share_export()
        self.assertEqual(s["model"]["schema"], "v1")
        self.assertEqual(s["model"]["vocab"], ["genre=Drama"])
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(dict(good, theta=[0.5]), f)      # theta must be len(vocab)+1
        self.assertNotIn("model", tb.share_export())

    def test_fm_model_exports_its_linear_twin(self):
        # when a factorization machine is active, Share sends the linear twin
        # stored inside it, so friends' importers keep working unchanged
        fm = {"kind": "fm", "vocab": ["genre=Drama"], "w": [0.4], "b": 0.0,
              "V": [[0.1, 0.2]], "mu": [0.2], "sd": [1.0], "version": "v1",
              "cv": {"auc": 0.8},
              "linear": {"theta": [0.5, 0.1], "mu": [0.2], "sd": [1.0], "cv": {"auc": 0.7}}}
        with open(os.path.join(self.tmp, "model.json"), "w", encoding="utf-8") as f:
            json.dump(fm, f)
        s = tb.share_export()
        self.assertEqual(s["model"]["theta"], [0.5, 0.1])      # the twin, not the FM
        self.assertEqual(s["model"]["cv"], {"auc": 0.7})
        fr = tb._clean_friend(dict(s, name="FMFriend"))
        self.assertIn("model", fr)                             # imports as plain linear


class TestCleanFriend(unittest.TestCase):
    """Importing a friend keeps the new fields, survives old files, rejects junk."""

    def test_full_record_round_trips(self):
        data = {"name": "Anna",
                "likes": [{"id": 1, "title": "A", "year": 2000, "genres": "Drama", "link": ""}],
                "dislikes": [{"id": 2, "title": "B", "year": "2001", "genres": "", "link": ""}],
                "watchlist": [{"id": 3, "title": "C", "year": "2002", "genres": "", "link": ""}],
                "seen": [2, "7", "junk", -1],
                "model": {"schema": "v1", "vocab": ["a", "b"], "theta": [1, 2, 3],
                          "mu": [0, 0], "sd": [1, 1], "cv": {"auc": 0.66}}}
        fr = tb._clean_friend(data)
        self.assertEqual(fr["name"], "Anna")
        self.assertEqual([m["id"] for m in fr["likes"]], [1])
        self.assertEqual([m["id"] for m in fr["dislikes"]], [2])
        self.assertEqual([m["id"] for m in fr["watchlist"]], [3])
        self.assertEqual(fr["seen"], [2, 7])           # ints kept; junk and negatives dropped
        self.assertEqual(fr["model"]["theta"], [1.0, 2.0, 3.0])

    def test_legacy_likes_only_file_still_imports(self):
        fr = tb._clean_friend({"name": "Old", "likes": [{"id": 9, "title": "X"}]})
        self.assertEqual([m["id"] for m in fr["likes"]], [9])
        self.assertEqual(fr["dislikes"], [])
        self.assertEqual(fr["watchlist"], [])
        self.assertEqual(fr["seen"], [])
        self.assertNotIn("model", fr)

    def test_malformed_model_is_dropped(self):
        base = {"name": "Bad", "likes": [],
                "model": {"schema": "v1", "vocab": ["a"], "theta": [1, 2], "mu": [0], "sd": [1]}}
        self.assertIn("model", tb._clean_friend(base))
        for breakit in ({"theta": [1]}, {"mu": [0, 0]}, {"vocab": []}, {"theta": ["x", "y"]}):
            bad = dict(base, model=dict(base["model"], **breakit))
            self.assertNotIn("model", tb._clean_friend(bad), breakit)


class TestShortlistReshow(unittest.TestCase):
    """The Channels toggle: 0-shortlist films can re-enter the rater, and
    re-rating them replaces the old row instead of duplicating it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.md = os.path.join(self.tmp, "movies.md")
        with open(self.md, "w", encoding="utf-8") as f:
            f.write(MOVIES_MD)
        self._orig = tb.MD_PATH
        tb.MD_PATH = self.md

    def tearDown(self):
        tb.MD_PATH = self._orig

    def test_seen_rated_ids_leaves_shortlist_out(self):
        self.assertEqual(tb.rated_ids(), {111, 222, 333, 444})
        self.assertEqual(tb.seen_rated_ids(), {111, 222, 333})   # 444 is Not seen (0)

    def test_record_rating_replaces_not_duplicates(self):
        movie = {"id": 444, "title": "On The List", "year": "2004",
                 "genres": "Drama", "link": "http://x/444"}
        tb.record_rating(movie, "3")                             # watched it, loved it
        rows = [l for l in tb._table_lines(self.md) if "| 444 |" in l]
        self.assertEqual(len(rows), 1)                           # replaced, not duplicated
        self.assertTrue(rows[0].strip().startswith("| 3 |"))
        self.assertEqual(tb.stats()["0"], 0)
        self.assertEqual(tb.stats()["3"], 2)


class TestRatedView(unittest.TestCase):
    """The Liked/Disliked sheets read movies.md by status, cache-only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.md = os.path.join(self.tmp, "movies.md")
        with open(self.md, "w", encoding="utf-8") as f:
            f.write(MOVIES_MD)
        self._orig = (tb.MD_PATH, tb._poster_cache, tb._director_cache)
        tb.MD_PATH = self.md
        tb._poster_cache = {}
        tb._director_cache = {}

    def tearDown(self):
        (tb.MD_PATH, tb._poster_cache, tb._director_cache) = self._orig

    def test_filters_by_status(self):
        self.assertEqual([m["id"] for m in tb.get_rated("3")], [111])
        self.assertEqual([m["id"] for m in tb.get_rated("1")], [222])
        self.assertEqual(tb.get_rated("9"), [])


@unittest.skipUnless(HAS_NUMPY, "NumPy not installed")
class TestMovieNight(unittest.TestCase):
    """Group picker: rank normalization, tiers, exclusions, least misery."""

    @staticmethod
    def _features_stub(ids, cache, api_key=None, allow_network=True):
        return {i: {} for i in ids}    # featurize falls back to base features (genres)

    def test_rank01_ties_and_range(self):
        r = rec._rank01([0.1, 0.9, 0.5, 0.5])
        self.assertEqual(r.min(), 0.0)
        self.assertEqual(r.max(), 1.0)
        self.assertAlmostEqual(r[2], r[3])             # ties share a rank
        self.assertTrue((rec._rank01([2.0]) == 1.0).all())

    def test_tiers_exclusions_and_least_misery(self):
        mv = lambda i, g: {"id": i, "title": "M%d" % i, "year": "2000", "genres": [g],
                           "link": "", "poster": "", "overview": ""}
        my_movies = [dict(mv(1, "Drama"), rating=3), dict(mv(2, "Horror"), rating=1)]
        my_watch = [mv(10, "Drama"), mv(11, "Horror"), mv(12, "Drama")]
        friend = {"name": "Anna",
                  "likes": [mv(3, "Drama")], "dislikes": [mv(4, "Horror")],
                  "watchlist": [mv(10, "Drama"), mv(11, "Horror"), mv(13, "Comedy")],
                  "seen": [12]}                        # Anna already saw 12 -> excluded
        orig = (rec.load_movies, rec.load_watchlist, rec.load_friends,
                rec.not_interested_ids, rec.tf.get_features, rec.MODEL_PATH)
        try:
            rec.load_movies = lambda path=None: my_movies
            rec.load_watchlist = lambda path=None: my_watch
            rec.load_friends = lambda: [friend]
            rec.not_interested_ids = lambda path=None: set()
            rec.tf.get_features = self._features_stub
            rec.MODEL_PATH = os.path.join(tempfile.mkdtemp(), "none.json")   # similarity for both
            res = rec.movie_night(["Anna"], n=10, offline=True)
        finally:
            (rec.load_movies, rec.load_watchlist, rec.load_friends,
             rec.not_interested_ids, rec.tf.get_features, rec.MODEL_PATH) = orig
        ids = [p["id"] for p in res["picks"]]
        self.assertNotIn(12, ids)
        self.assertEqual(set(ids), {10, 11, 13})
        tiers = {p["id"]: p["tier"] for p in res["picks"]}
        self.assertEqual((tiers[10], tiers[11], tiers[13]), (0, 0, 1))
        self.assertEqual(ids[0], 10)                   # both like Drama, both shun Horror
        p10 = next(p for p in res["picks"] if p["id"] == 10)
        self.assertEqual(p10["group_score"], min(p10["scores"].values()))   # least misery
        self.assertEqual(res["how"], {"You": "similarity", "Anna": "similarity"})
        self.assertEqual(res["missing"], [])

    def test_fm_learns_interactions_linear_cannot(self):
        # y = x1 XOR x2 is the canonical interaction: no linear model separates it,
        # a rank-k factorization machine does (Rendle 2010).
        import numpy as np
        rng = np.random.default_rng(3)
        X = rng.integers(0, 2, size=(240, 2)).astype(float)
        y = (X[:, 0].astype(int) ^ X[:, 1].astype(int)).astype(float)
        lin = rec.train_logreg(X, y)
        fm = rec.train_fm(X, y, k=4, iters=1500, lr=0.2, seed=1)
        auc_lin = rec.roc_auc(y, rec.proba(lin, X))
        auc_fm = rec.roc_auc(y, rec.fm_proba(fm, X))
        self.assertLess(auc_lin, 0.65)                 # linear can't do XOR
        self.assertGreater(auc_fm, 0.85)               # the FM can
        self.assertGreater(auc_fm, auc_lin + rec.FM_MARGIN)

    def test_model_kind_dispatch(self):
        import numpy as np
        X = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        y = np.array([1.0, 0.0, 1.0])
        lin = rec.train_logreg(X, y)
        fm = rec.train_fm(X, y, k=2, iters=200)
        self.assertEqual(rec.model_kind(lin), "linear")
        self.assertEqual(rec.model_kind(fm), "fm")
        self.assertEqual(len(rec.score_model(lin, X)), 3)
        self.assertEqual(len(rec.score_model(fm, X)), 3)
        self.assertEqual(len(rec.linear_weights(lin)), 2)
        self.assertEqual(len(rec.linear_weights(fm)), 2)
        self.assertEqual(rec.top_interactions(lin, ["a=1", "b=1"]), [])   # linear: none

    def test_choose_kind_pref_overrides_gate(self):
        lo = {"auc": 0.70}; hi = {"auc": 0.78}
        self.assertEqual(rec.choose_kind("auto", lo, hi), "fm")       # gate: FM wins
        self.assertEqual(rec.choose_kind("auto", hi, lo), "linear")   # gate: FM loses
        self.assertEqual(rec.choose_kind("auto", lo, {"auc": 0.705}), "linear")  # inside margin
        self.assertEqual(rec.choose_kind("linear", lo, hi), "linear")  # forced beats gate
        self.assertEqual(rec.choose_kind("fm", hi, lo), "fm")
        self.assertEqual(rec.choose_kind("auto", None, hi), "linear")  # no CV -> conservative

    def test_model_of_kind_pulls_twins_from_cache(self):
        fm_root = {"kind": "fm", "vocab": ["a"], "w": [0.1], "b": 0.0, "V": [[0.1]],
                   "mu": [0.0], "sd": [1.0],
                   "linear": {"theta": [0.2, 0.0], "mu": [0.1], "sd": [1.0]}}
        self.assertEqual(rec.model_kind(rec.model_of_kind(fm_root, "auto")), "fm")
        lin = rec.model_of_kind(fm_root, "linear")
        self.assertEqual(rec.model_kind(lin), "linear")
        self.assertEqual(lin["theta"], [0.2, 0.0])
        self.assertEqual(lin["vocab"], ["a"])                          # twin inherits vocab
        lin_root = {"theta": [0.2, 0.0], "vocab": ["a"], "mu": [0.1], "sd": [1.0],
                    "fm_alt": {"w": [0.1], "b": 0.0, "V": [[0.1]], "mu": [0.0], "sd": [1.0]}}
        fm = rec.model_of_kind(lin_root, "fm")
        self.assertEqual(rec.model_kind(fm), "fm")
        self.assertIsNone(rec.model_of_kind({"theta": [0.2, 0.0], "vocab": ["a"]}, "fm"))  # no twin

    def test_own_participant_honors_model_pref(self):
        import numpy as np
        fm_root = {"kind": "fm", "vocab": ["genre=Drama"], "w": [0.1], "b": 0.0,
                   "V": [[0.1, 0.1]], "mu": [0.0], "sd": [1.0], "version": rec.MODEL_VERSION,
                   "linear": {"theta": [0.2, 0.0], "mu": [0.1], "sd": [1.0]}}
        path = os.path.join(tempfile.mkdtemp(), "model.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fm_root, f)
        orig = rec.MODEL_PATH; rec.MODEL_PATH = path
        try:
            p_auto = rec._own_participant([], "auto")
            p_lin = rec._own_participant([], "linear")
            p_sim = rec._own_participant([], "similarity")
        finally:
            rec.MODEL_PATH = orig
        self.assertEqual(rec.model_kind(p_auto["model"]), "fm")        # auto -> cached root
        self.assertEqual(rec.model_kind(p_lin["model"]), "linear")     # forced -> the twin
        self.assertNotIn("model", p_sim)                               # forced similarity

    def test_friend_model_schema_gate(self):
        feats = [{"genre=Drama": 1.0}]
        good = {"schema": rec.MODEL_VERSION, "vocab": ["genre=Drama"],
                "theta": [1.0, 0.0], "mu": [0.5], "sd": [0.5]}
        _, how, whys = rec._person_scores({"model": good, "likes": []}, feats, {})
        self.assertEqual(how, "model")
        self.assertEqual(len(whys), 1)
        self.assertIn("Drama", whys[0])                # explanation names the driving feature
        _, how2, whys2 = rec._person_scores({"model": dict(good, schema="v999"), "likes": []}, feats, {})
        self.assertEqual(how2, "none")                 # wrong schema, no likes -> neutral
        self.assertEqual(whys2, ["no taste data yet"])

    def _night(self, combine):
        """movie_night on a fixture where average and least misery disagree:
        candidate 20 is loved by You, hated by Anna; candidate 21 is fine for both."""
        mv = lambda i, g: {"id": i, "title": "M%d" % i, "year": "2000", "genres": g if isinstance(g, list) else [g],
                           "link": "", "poster": "", "overview": ""}
        my_movies = [dict(mv(1, "Drama"), rating=3), dict(mv(5, "Comedy"), rating=3),
                     dict(mv(2, "Horror"), rating=1)]
        my_watch = [mv(20, ["Drama", "Drama"]), mv(21, "Comedy")]
        friend = {"name": "Anna", "likes": [mv(3, "Comedy")], "dislikes": [mv(4, "Drama")],
                  "watchlist": [mv(20, "Drama"), mv(21, "Comedy")], "seen": []}
        orig = (rec.load_movies, rec.load_watchlist, rec.load_friends,
                rec.not_interested_ids, rec.tf.get_features, rec.MODEL_PATH)
        try:
            rec.load_movies = lambda path=None: my_movies
            rec.load_watchlist = lambda path=None: my_watch
            rec.load_friends = lambda: [friend]
            rec.not_interested_ids = lambda path=None: set()
            rec.tf.get_features = self._features_stub
            rec.MODEL_PATH = os.path.join(tempfile.mkdtemp(), "none.json")
            return rec.movie_night(["Anna"], n=10, offline=True, combine=combine)
        finally:
            (rec.load_movies, rec.load_watchlist, rec.load_friends,
             rec.not_interested_ids, rec.tf.get_features, rec.MODEL_PATH) = orig

    def test_combine_strategies_rank_differently(self):
        # Anna hates Drama-heavy 20: least misery must put 21 first
        lm = self._night("least_misery")
        self.assertEqual(lm["combine"], "least_misery")
        self.assertEqual([p["id"] for p in lm["picks"]][0], 21)
        for p in lm["picks"]:                          # group score = min of members
            self.assertEqual(p["group_score"], min(p["scores"].values()))
        # avg_no_misery floors 20 (Anna's score below the floor) -> sinks below 21
        anm = self._night("avg_no_misery")
        ids = [p["id"] for p in anm["picks"]]
        self.assertLess(ids.index(21), ids.index(20))
        p20 = next(p for p in anm["picks"] if p["id"] == 20)
        self.assertTrue(p20.get("floored"))
        # every pick carries one why per participant
        for p in anm["picks"]:
            self.assertEqual(set(p["why"].keys()), {"You", "Anna"})
        # bad combine string falls back safely
        self.assertEqual(self._night("nonsense")["combine"], "least_misery")


if __name__ == "__main__":
    unittest.main()
