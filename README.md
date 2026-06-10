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
- **Your lists, one tap away** — the stats line sits above the card, visible on
  both faces. Click the **Watchlist** count to slide up a sheet of everything
  you've saved, each with its poster; mark one **watched** (it moves into your
  ratings), remove it, or open it on TMDb. The **Liked** and **Disliked** counts
  open the same sheet for your rating history, where you can re-rate any film.
- **Channels** — the pools your movies come from, built four ways: by **genre**,
  **"more like a movie"** you love (TMDb's neighbours of a seed film), **films by
  a person** (a director or actor), or a **keyword** (a real TMDb tag, picked from
  live results with film counts, with an *any / all* switch). Mix as many as you
  like; your taste model ranks whatever they bring in. Toggle, edit, or remove any
  of them; they persist to `channels.json`.
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
- **Share & friends** — **export your taste** to a small file (the **Share**
  button on the rater): your liked and disliked films, your watchlist, the ids
  of what you've seen, and — once trained — your **taste model**. **Import
  friends' files** (the **Friends** panel on the recommender) — one database per
  friend; older likes-only files still import fine. A **"from" menu** switches
  whose taste you recommend from: *your* taste or any friend's.
- **Movie night** — flip the **light switch** above the card and the lights go
  down: a projector screen takes the card's place. Check in the buddies who are
  there, start the projector, and it picks **one film for all of you**:
  candidates come from everyone's watchlists in tiers (on *everyone's* list
  first, then *most*, then *any*, then a fresh pick), filtered to your streaming
  services. Every participant's taste scores every candidate — their exported
  model when available, otherwise similarity to their likes and dislikes — and
  the group pick is decided by **least misery** (the film nobody would hate),
  with each person's match shown on screen.
- **Guided first run** — on a fresh start, a short onboarding helps you paste your
  TMDb key, make your first channel (just name a film you love), and learn how
  rating works — so there's something to do from minute one.

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

- **Option A (in the app):** just run it — the **first-run onboarding** asks for
  your key and saves it to `tmdb_key.txt` for you. No file editing needed.
- **Option B (file):** create a file named `tmdb_key.txt` next to `tastebuds.py`
  and paste the key as its only contents.
- **Option C (env var):** `export TMDB_API_KEY=your_key_here`

> Your key is personal — keep it private. `tmdb_key.txt` and `.env` are listed in
> `.gitignore` so they are never committed.

## Run it

```bash
python3 tastebuds.py
```

A browser tab opens automatically. **The first time**, a short setup walks you
through your key and your first channel; after that you go straight to rating.
Press `Ctrl+C` in the terminal to stop. Your ratings are written to `movies.md`
and saved titles to `watchlist.md` (both created automatically).

## Channels

Channels are the pools your movies are drawn from. Open **Channels** to add as
many as you like — and toggle, **edit**, or remove any. There are four kinds:

- **Genre** — pick one or more genres and a style.
- **More like a movie** — name a film you love; you get TMDb's neighbours of it.
- **Films by a person** — a director or actor; you get films they were cast or
  crew on.
- **Keyword** — search TMDb's tags and pick a real one (shown with its film
  count), with an **any / all** switch when you add several.

For the genre and keyword kinds, a **style** sets the sort and quality floor:

- *Popular* — sorted by popularity, modest quality floor
- *Acclaimed* — sorted by rating, higher vote threshold
- *Hidden gems* — high rating, lower popularity (surfaces obscure films)

Whatever your channels surface, the recommender ranks it by your taste — so the
pools just need to point in roughly the right direction. Channels live in
`channels.json`, so you can also edit them by hand.

## Files

| File | Tracked by git? | What it is |
|------|-----------------|------------|
| `tastebuds.py` | yes | the whole app |
| `channels.json` | **no** (git-ignored) | your channels — created when you add your first one (onboarding or the Channels panel) |
| `movies.md` | **no** (git-ignored) | your personal ratings |
| `watchlist.md` | **no** (git-ignored) | your personal watchlist |
| `not-interested.md` | **no** (git-ignored) | films you marked *Not interested* |
| `providers.json` | **no** (git-ignored) | your country + chosen streaming services |
| `friends.json` | **no** (git-ignored) | imported friends (one entry per friend: likes, dislikes, watchlist, seen ids, and their taste model when shared) |
| `director_cache.json` | **no** (git-ignored) | cached director names — auto-created, makes cards load faster |
| `poster_cache.json` | **no** (git-ignored) | cached poster paths for the watchlist view — auto-created |
| `model_weights.json` | **no** (git-ignored) | how strongly each reaction trains your model — set in the Model panel |
| `tmdb_key.txt` | **no** (git-ignored) | your private API key |

The data files are git-ignored so a public repo never leaks your taste log or
key. The script recreates empty `movies.md` / `watchlist.md` from built-in
templates whenever they're missing, so a fresh clone just works.

## Recommender

The [`ml-recommender/`](ml-recommender/) folder has a working recommender that
learns from your ratings — content-similarity while labels are few, switching to
a compact logistic-regression model once you've rated enough — and writes a
ranked `recommendations.md`. It's NumPy-only, with a Core ML export for on-device
use in the works; see its [README](ml-recommender/README.md) for how it works and
why a small linear model is the right tool at this scale.

## Tests

A small test suite covers the fragile parts (markdown parsing, atomic writes,
the ranking metrics, and how ratings become training labels). The rater tests
use only the standard library; the recommender tests need NumPy and skip
automatically if it isn't installed. From the repo root:

```bash
python3 -m unittest discover -s tests -v
```

## License

Released under the [MIT License](LICENSE) — © 2026 Vlad Giurgiu. You're free to
use, modify, and distribute it; it comes with no warranty.
