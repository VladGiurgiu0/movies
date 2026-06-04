# Tastebuds

> **your movie taste — and your buddies'.** Private, on your machine, made with love.

**Tastebuds** is a tiny, local movie companion. You rate the films you've seen,
one at a time, and it learns *your* taste and starts recommending films
you'll actually love. The name is a little pun: **taste** — the palate it learns
from your ratings — and **buds** — your buddies, since you can swap libraries with
friends and get recommendations from theirs.

The whole point is **ownership**: you own your data *and* the model. Your ratings
are plain Markdown files you can open and read; the recommender is a small,
transparent model trained right on your laptop — not a black box in someone
else's cloud. No accounts, no tracking, no algorithm deciding for you in the dark.
Think of it as a cozy, own-your-data home for your film life — open source, and
made with love.

It runs in your browser but stays entirely local: a small Python web server, fed
by [The Movie Database (TMDb)](https://www.themoviedb.org). The rater itself uses
only the Python standard library; the optional recommender adds a single
dependency (NumPy).

## Features

- **One-at-a-time rating** with a clean, minimal UI (automatic light/dark),
  showing each film's poster, director, year, TMDb rating, and source channel.
- **One window that flips** — a single card with **Rate** on the front and
  **Recommendations** on the back; tap the flip button to turn it over (with a
  3D card-flip animation). Less on screen, more focus.
- **Four verdicts** on an ordinal scale plus a separate watchlist:
  - `3` Liked · `2` Indifferent · `1` Disliked · `0` Not seen
  - **Add to Watchlist** for "want to watch later"
- **Channels** — candidate pools defined by genres + keywords + a style
  (Popular / Acclaimed / Hidden gems). Toggle them on/off or add your own from
  the UI; they persist to `channels.json`.
- **Where you watch** — pick your **country and streaming services** (Netflix,
  MUBI, Disney+, …) and the films you discover, in *both* the rater and the
  recommender, are limited to titles you can actually stream. Leave it off to see
  everything. Powered by TMDb's JustWatch data; saved to `providers.json`.
- **Never repeats** — anything already in `movies.md`, `watchlist.md`, or
  `not-interested.md` is skipped, and **the recommender won't re-show a film
  you've already seen this session**, so every "Next" is fresh.
- **Undo last action** in *both* windows — the rater and the recommender each
  have their own, so a rating, a watchlist add, or a *Not interested* is always
  one click from being reversed.
- **Train** (in the **Model** panel) — learns a model from your ratings and shows
  its cross-validated quality and your top "taste drivers", with plain-language
  tooltips for the jargon.
- **Recommend** — surfaces films ranked by your taste, **one at a time** with a
  **Next** button and a **Safe / Balanced / Exploratory** control; rate /
  add-to-watchlist / mark *Not interested* on any pick inline. Powered by
  [`ml-recommender/`](ml-recommender/).
- **Share & friends** — **export your liked films** to a small file (the
  **Share** button on the rater), and **import friends' libraries** (the
  **Friends** panel on the recommender) — one database per friend. A **"from"
  menu** switches whose taste you recommend from: *your* taste or any friend's.

## Requirements

- **Python 3.8+** and a free TMDb API key (see below).
- **The rater (`tastebuds.py`) needs nothing else** — pure standard library,
  so `python3 tastebuds.py` just works.
- **The recommender** (the in-app **Train** and **Recommend** features) needs
  **NumPy** — install it once with:

  ```bash
  pip install -r ml-recommender/requirements.txt
  ```

  It's optional: skip it and the rater works fully; the Recommend panel will just
  prompt you to install NumPy when you first try it.

## Get your own TMDb API key

The app needs a TMDb API key to fetch movies, posters, and metadata. It's free
and takes about two minutes.

1. **Create an account** at <https://www.themoviedb.org/signup> and verify your
   email.
2. Open the API settings page: <https://www.themoviedb.org/settings/api>
   (Profile → **Settings** → **API**).
3. Click **Create** / **Request an API Key** and choose **Developer**.
4. Accept the terms, then fill in the short application form. For personal use
   you can keep it simple — e.g. application name "Tastebuds", type
   "Personal/Education", URL `http://localhost`, and a one-line description.
5. After submitting, copy the value labelled **API Key (v3 auth)** — a 32-character
   hex string (it looks like `0123456789abcdef0123456789abcdef`).

Then give the key to the app in **either** way:

- **Option A (file):** create a file named `tmdb_key.txt` next to `tastebuds.py`
  and paste the key as its only contents.
- **Option B (env var):** `export TMDB_API_KEY=your_key_here`

> Your key is personal — keep it private. `tmdb_key.txt` and `.env` are listed in
> `.gitignore` so they are never committed.

## Run it

```bash
python3 tastebuds.py
```

A browser tab opens automatically. Rate away; press `Ctrl+C` in the terminal to
stop. Your ratings are written to `movies.md` and saved titles to
`watchlist.md` (both created automatically on first run).

## Channels

Click **Channels** to manage the candidate pools:

- **Toggle** any channel on or off.
- **Add a channel** with a name, comma-separated **keywords** (TMDb resolves
  them, e.g. `coming-of-age, first love`), optional **genres**, and a **style**:
  - *Popular* — sorted by popularity, modest quality floor
  - *Acclaimed* — sorted by rating, higher vote threshold
  - *Hidden gems* — high rating, lower popularity (surfaces obscure films)
- A channel matches movies in **all** selected genres, narrowed by any keywords.

Channels live in `channels.json`, so you can also edit them by hand.

## Files

| File | Tracked by git? | What it is |
|------|-----------------|------------|
| `tastebuds.py` | yes | the whole app |
| `channels.json` | **no** (git-ignored) | your candidate pools — auto-created from defaults on first run |
| `movies.md` | **no** (git-ignored) | your personal ratings |
| `watchlist.md` | **no** (git-ignored) | your personal watchlist |
| `not-interested.md` | **no** (git-ignored) | films you marked *Not interested* |
| `providers.json` | **no** (git-ignored) | your country + chosen streaming services |
| `friends.json` | **no** (git-ignored) | imported friends' liked films (one entry per friend) |
| `tmdb_key.txt` | **no** (git-ignored) | your private API key |

The data files are git-ignored so a public repo never leaks your taste log or
key. The script recreates empty `movies.md` / `watchlist.md` from built-in
templates whenever they're missing, so a fresh clone just works.

## Recommender

The [`ml-recommender/`](ml-recommender/) folder has a working recommender that
learns from your ratings — content-similarity while labels are few, switching to
a compact logistic-regression model once you've rated enough — and writes a
ranked `recommendations.md`. It's NumPy-only and exports to Core ML for on-device
use; see its [README](ml-recommender/README.md) for how it works and why a small
linear model is the right tool at this scale.

## License

Released under the [MIT License](LICENSE) — © 2026 Vlad Giurgiu. You're free to
use, modify, and distribute it; it comes with no warranty.
