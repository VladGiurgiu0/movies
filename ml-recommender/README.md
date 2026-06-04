# ML recommender

A personal movie recommender that learns from your own ratings in
[`../movies.md`](../movies.md) and [`../watchlist.md`](../watchlist.md) and ranks
candidate films by how much you're likely to like them. By default it surfaces
**fresh films from TMDb discover** (via your rater channels) that you haven't
logged yet; with `--source shortlist` it ranks your existing "not seen" list.

You can run it from the command line, or straight from the rater's **Train** and
**Recommend** buttons (the rater shells out to this script).

It is deliberately lightweight: **NumPy only** (no pandas, no TensorFlow/PyTorch).
For a few dozen to a few hundred labels, a compact linear model is the right
tool — fast on any modern laptop, hard to overfit, interpretable, and directly
exportable to Core ML for on-device inference.

## How it works

1. **Parse** your ratings (films you've actually seen).
   - Liked (3) → positive · Disliked (1) and Indifferent (2) → negative
   - Watchlist and Not seen (0) are unseen, so they are **not** training labels.
     Seen films (rated), watchlist, and not-interested are excluded from candidates;
     **"Not seen" (0) films are not excluded**, so they can be recommended again.
   - Classes are balanced so a small "not-liked" set isn't drowned out by the likes
2. **Featurize** each film from TMDb metadata (cached locally after first fetch):
   genres, keywords, director, top cast, original language, decade, runtime,
   TMDb rating, popularity.
3. **Model** — chosen automatically by how much data you have:
   - **Cold start (< 8 likes):** rank candidates by **content similarity** to your
     liked set, minus similarity to your disliked set.
   - **Trained (≥ 8 likes & ≥ 5 not-liked):** a **logistic-regression** model
     (L2-regularized, NumPy) predicting P(like). Reports cross-validated
     ROC-AUC / average precision and the **top taste drivers** (which features
     push toward "like"). If the model isn't beating chance in CV (common until
     you've rated enough films you *didn't* love), recommendations fall back to
     similarity ranking automatically.
4. **Recommend** — writes the ranked top-N to `recommendations.md`, each with a
   short *why* (top contributing features, or the nearest liked film).

## Run it

```bash
cd ml-recommender
pip install -r requirements.txt
python3 recommend.py                 # fresh discover recs -> recommendations.md
python3 recommend.py --n 20          # more picks
python3 recommend.py --source shortlist   # rank your existing 0-shortlist instead
python3 recommend.py --train-only    # just train + report metrics
python3 recommend.py --offline       # only cached metadata / md fields
```

The first run fetches TMDb metadata for your films (needs `../tmdb_key.txt` or
`$TMDB_API_KEY`) and caches it to `features_cache.json`; later runs are faster.
Recommendations dedupe against `movies.md`, `watchlist.md`, and
`not-interested.md`, so you never see something you've already rated, saved, or
marked Not interested.

## On-device inference

- **Training on your laptop** is trivial — the model is tiny and pure NumPy, so it
  trains in well under a second on a modern laptop with no GPU or special build.
- **On-device inference (iPhone/Mac)** via Core ML: the trained model is just
  weights + bias + standardization, which maps exactly onto a Core ML linear
  classifier. `export_coreml.py` (optional, `pip install coremltools`) folds the
  standardization into the weights and writes `MovieAffinity.mlpackage`, which a
  Swift app can run entirely on-device. Full on-device *training* is a further
  step (Core ML updatable models / MLUpdateTask) and is left as future work —
  retraining on the Mac after each rating session is simpler and plenty fast.

## Why not TensorFlow / a neural net?

With this much data a deep model would overfit and add heavy dependencies and
build friction for no benefit. A regularized linear model generalizes better at
small N, trains instantly, and—crucially—tells you *why* it recommends something.
If the dataset ever grows into the thousands (or you add multiple users), the
natural upgrades are gradient-boosted trees (LightGBM) or a two-tower embedding
model; the feature pipeline here transfers directly.

## Files

| File | What it is |
|------|------------|
| `recommend.py` | the pipeline + CLI (NumPy) |
| `tmdb_features.py` | TMDb metadata fetch + cache |
| `export_coreml.py` | optional Core ML export for on-device use |
| `requirements.txt` | numpy (coremltools optional, for Core ML export) |
| `features_cache.json` | cached TMDb metadata (git-ignored) |
| `model.json` | the trained model (git-ignored) |
| `recommendations.md` | latest ranked picks (git-ignored) |

## Notes

- Recommendations are only as good as your labels — the more you rate, the
  sooner it switches from similarity to the trained model and the sharper it
  gets.
- Fresh recommendations need network (to discover candidates and fetch their
  metadata); offline it falls back to ranking your existing shortlist.
- In the rater UI, **Train** reports the model's CV metrics and taste drivers,
  and **Recommend** shows ranked picks with rate / Add-to-Watchlist /
  Not interested / TMDb.
