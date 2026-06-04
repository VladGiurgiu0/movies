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


if __name__ == "__main__":
    unittest.main()
