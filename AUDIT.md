# Audit — Tastebuds

A review of the current code and a forward-look at turning it into a native
**Swift + Core ML** app for modern laptops and phones. Severity tags:
**[High]** worth doing soon · **[Med]** worth doing · **[Low]** nice to have.

## What's solid

- Clean separation: the rater (`tastebuds.py`) is pure standard library; the ML
  lives in `ml-recommender/` and is shelled out to, so the basic app never breaks
  if NumPy isn't installed.
- Data is plain Markdown/JSON you own; the model is a small, interpretable linear
  model; the metadata cache means the API is queried once per film.
- Privacy hygiene is good: key + personal data + ML artifacts are git-ignored
  (verified), and the README no longer leaks a real key.
- The ML pipeline is validated (AUC 1.0 on separable synthetic data), trains on
  *seen* ratings only, balances classes, and falls back to similarity when the
  model isn't beating chance.

## Current code — potential problems

### Data integrity
- **[High] Non-atomic file writes.** Every write to `movies.md`, `watchlist.md`,
  `dismissed.md`, `channels.json`, and `features_cache.json` truncates and rewrites
  the whole file in place. A crash, full disk, or the process being killed mid-write
  can corrupt or truncate your taste log. **Fix:** write to a temp file in the same
  directory and `os.replace()` it over the target (atomic on macOS). Low effort,
  high payoff.
- ~~**[Low] Markdown parsing is positional.**~~ **Done** — both scripts now parse only
  the rows *between* the `TABLE-START` / `TABLE-END` markers, so legend tables or
  other content can never be miscounted.

### Local server exposure
- **[Med] No Origin/Host check (CSRF / DNS-rebinding).** The server binds to
  `127.0.0.1` on a random port (good — not reachable off-machine), but it accepts
  any request with no `Origin`/`Host` validation and no token. In principle a web
  page you visit could probe localhost ports and POST to it (writing to your files
  or triggering TMDb calls). Risk is low for a personal tool, but cheap to harden.
  **Fix:** reject requests whose `Origin`/`Host` isn't `127.0.0.1[:port]`, and/or
  require a random token minted at startup and embedded in the page.

### Performance / scale
- **[Med] The model retrains on every Recommend batch.** `recommend.py` re-parses
  the data, re-featurizes, and re-trains inside each `--source discover` run (not
  just on Train). It's fast now (seconds) but grows with your library. **Fix:**
  cache the trained `model.json` and reuse it for scoring; only retrain on Train (or
  when the data hash changes).
- **[Low] Director fetch is one synchronous TMDb call per shown film,** inside the
  `/api/next` handler, cached only in memory (re-fetched each session). It can make a
  card appear a beat slower and isn't persisted. **Fix:** persist director to the
  metadata cache (it's already fetched there for rated films), or prefetch for the
  buffered candidates.
- **[Low] `features_cache.json` is rewritten in full on every fetch batch.** Fine at
  hundreds of films; at tens of thousands, consider SQLite or append-only.

### Front-end / UX
- ~~**[Low] A few values are interpolated into HTML attributes unescaped**~~ **Done** —
  `poster`/`link` URLs are escaped before going into `src`/`href`.
- **[Low] Silent `except Exception`** in several places hides real errors (network,
  parse). Good for resilience, bad for debugging. Consider logging to stderr.
- **[Low] "Why this pick" shows raw tokens** like `cast=Ben Stiller, kw=nurse`.
  Readable but not pretty. Humanize (drop prefixes, "Because you liked films with …").
- **[Low] Similarity scores are tiny and vary in range** (e.g. 0.10–0.93). The new
  tooltip explains it, but a normalized **match %** would read better.

### Concurrency
- **[Low] Shared globals across threads.** `ThreadingHTTPServer` + module-level
  buffers/stacks are mostly guarded by locks; the single-user click pattern makes
  races unlikely, but the Train/Recommend subprocess reads `movies.md` while the
  server may be writing it — another reason atomic writes matter.

## ML reality check (not a bug, but set expectations)

- The model's quality is **data-bound**: with ~47 broad likes and only ~14
  "not-liked", cross-validated AUC sits near chance and the drivers are mostly
  "away" features. This is expected, not a defect. The fixes already in place
  (drop watchlist from labels, balance classes, similarity fallback, the live
  "rate ~N more" hint) are the right responses. The lever is **more negative
  ratings** (Indifferent/Disliked); the model auto-engages once it beats chance.

## Future: native Swift + Core ML app

The port is very feasible — plain-file data and a tiny linear model — but watch
these:

- **[High] Don't embed the TMDb API key in a shipped app.** A key compiled into a
  client binary is extractable and can be abused, rate-limited, or revoked. Options,
  best first: (a) a thin proxy/back-end you control that holds the key; (b) ask each
  user for their own key (as the current tool does); (c) TMDb guest sessions. Store
  whatever key you do keep in the **Keychain**, not plaintext.
- **[High] Fixed Core ML input vs. a growing vocabulary.** This is the main ML
  gotcha. A Core ML model has a **fixed input shape**, but your feature space
  (keywords, cast, directors) grows as you rate new films. Two clean fixes:
  - **Feature hashing ("the hashing trick")** — hash each `genre=/kw=/cast=` token
    into a fixed-size vector (say 2–8k dims). Input size becomes constant, new tokens
    "just work", and it ports 1:1 to Core ML. Worth adopting **in the Python version
    now** so the model is already export-stable.
  - Or freeze a vocabulary and periodically retrain on the Mac, shipping a new model.
- **[Med] On-device training is possible but constrained.** Core ML **updatable
  models** support exactly the layer types a logistic regression needs (a
  fully-connected layer + sigmoid), so on-device personalization via `MLUpdateTask`
  is realistic — *if* the input is fixed-size (see above). Simplest v1: train on the
  Mac, run inference on iPhone; add on-device updates later.
- **[Med] TMDb attribution & terms.** Distributed apps must follow TMDb's
  attribution ("uses the TMDB API but not endorsed/certified by TMDB" + logo) and
  terms; add a privacy note (ratings stored locally / iCloud).
- **[Med] Data sync.** Use iCloud/CloudKit (or a SwiftData store synced via iCloud)
  for `movies.md`-equivalent data so Mac and iPhone share one taste log.
- **[Med] Rate limits & posters.** Respect TMDb fair-use; cache metadata and posters
  on disk (the current per-film cache already maps to this); handle offline by
  falling back to similarity over cached features (no network needed).
- **[Low] Similarity without Core ML.** Cold-start similarity is just cosine over
  feature vectors — trivial to implement natively, so the app works on day one
  before any model exists.
- **[Low] Platform polish.** SwiftUI for the three windows, VoiceOver labels,
  Dynamic Type, light/dark (free in SwiftUI), and a normalized match-% score.

## Suggested priority order

1. ~~**Atomic writes** (`os.replace`)~~ — **done** (rater + recommender all use a temp-then-replace helper).
2. **Feature hashing** in `recommend.py` — fixed-size model, future-proofs Core ML.
   *(High for the app, medium now — deferred, pending a quality check.)*
3. ~~**Cache/reuse the trained model** for Recommend~~ — **done** (data-signature cache;
   Recommend reuses the model unless your ratings changed — ~13× faster in testing).
4. ~~**Origin/token check** on the local server~~ — **done** (per-run token; `/api/*`
   requires it, so other pages/processes can't drive the server).
5. ~~Persist director; humanize "why"; normalize scores~~ — **done** (director cached to
   `director_cache.json`; "why" reads as plain words; score shown as a 0–100 **match %**).

**Done since the audit:** atomic writes · model reuse · server token ·
director cache / humanized "why" / match % · robust table-marker parsing ·
attribute escaping · **"Not seen" recommender fix** (0-rated films are no longer
excluded from recommendations, though the rater still won't re-ask you to rate them).

**Remaining:** **(2) feature hashing** *(deferred — pending a quality A/B)*;
low-severity polish (error logging to stderr; SQLite only if the library grows
huge); and the Swift/Core ML app considerations above (deferred until the port).

None of these block daily use — the tool is solid as-is. They're about durability
and a smooth path to the native app.

## Audit pass 2 — after Find-a-movie, Model panel, Share/export

**Verified working as decided** (regression on the real library, offline):
- Counts / parsing read only rows between the table markers — liked / indifferent /
  disliked / not-seen / watchlist all correct.
- Recommender is now in **trained-model mode**: the library reached **91 liked /
  37 not-liked**, CV **ROC-AUC ≈ 0.73** (well above the 0.58 switch). Model reuse
  (data signature + version tag) works; only retrains when ratings change.
- "Not seen" (0) is no longer excluded from recommendations; the rater still skips it.
- Find-a-movie merges your library + TMDb, dedupes, set-status moves with no
  duplicates, and shows a TMDb link.
- Atomic writes (no temp leftovers); server token enforced on `/api/*`;
  `friend.json` / `channels.json` / `director_cache.json` git-ignored.
- Share/export: export likes; import a friend; recommend-from-friend excludes
  films you've already logged, fills poster/overview/director from metadata, and
  labels the reason "liked by <name>"; clear message when no friend is imported.

**New low-severity findings (to triage):**
- **[Low] Friend picks need network for visuals.** A friend's films usually aren't
  in your local cache, so poster/director/overview are fetched live; offline they
  render without them. Not a bug, just a degraded-offline note.
- ~~**[Low] No cap on imported friend likes.**~~ **Done** — imported files are capped at 5000 likes.
- **[Low] Metadata cache schema grew** (added `poster_path` / `overview`).
  Pre-existing cache entries lack these; only matters for already-cached friend
  picks (rare). **Rebuild metadata cache** refreshes them.
- ~~**[Low] `recProfile` not persisted across reloads**~~ **Done** — remembered via
  localStorage (persists across reloads within a run; resets on a fresh launch
  because the server port changes).
- ~~**[Low] Dead code: `/api/search` + `search_db()`**~~ **Done** — removed.
- **[Med] Auto-start runs the recommender on every page load** (discover + metadata
  fetch), even if you only intend to rate. Consider a small setting, or only
  auto-start when a trained model already exists.

All verified behaviors are in place and working; none of the new items block use.

## Audit pass 3 — freshness · streaming filter · share-split + friends · card-flip

Four features built together. Each was tested offline (Python import + a real
in-process HTTP server with TMDb mocked); the front end was JS-parsed
(`node --check`), id-cross-checked (every `getElementById` resolves), and the
page markup verified (no duplicate ids, balanced `<div>`s).

**Verified working as decided:**

- **Freshness.** The rater tracks recommendation ids already shown *this session*,
  per profile (`_rec_seen[profile]`), and passes them to `recommend.py --exclude`,
  which unions them into the candidate exclude set (discover, shortlist, and friend
  paths). Confirmed end-to-end: a second Recommend batch for the same profile drops
  every already-shown film (test returned an empty 2nd batch once the small friend
  pool was exhausted). Discover's pool is effectively unbounded, so "Next" stays
  fresh without forcing *Not interested*. The button was renamed **Dismiss →
  "Not interested"** and now shows an **Undo toast** (`/api/undo-dismiss` removes the
  row and lets the film resurface). Reset on restart — by design.
- **Streaming filter.** `providers.json` stores `{region, providers[]}`.
  `fetch_channel` adds `watch_region` + `with_watch_providers` (OR-joined) +
  `with_watch_monetization_types=flatrate` whenever a region *and* ≥1 provider is
  set — so **both** the rater and the recommender (which reuses `fetch_channel` via
  `discover_pool`) are filtered from one switch. `GET /api/providers?region=XX`
  lists services by TMDb display priority (Netflix=8, MUBI=11, … confirmed for AT);
  `POST` saves, de-dupes, clears `_buffer` and `_rec_seen` so the change applies at
  once. Verified GET/SAVE/GET-saved.
- **Share split + multiple friends.** Export moved to the **rater** ("Share");
  import + management moved to the **recommender** ("Friends"), with a visible
  **"from" dropdown** (Your taste / each friend). Storage moved to a `friends.json`
  *list*; a legacy single `friend.json` is migrated on first read. `save_friend`
  is keyed by name (re-importing a name replaces it, no duplicates); `--profile`
  now carries the friend's *name* (passed as a subprocess list arg — no shell
  injection). Verified: migration, add/replace/remove, recommend-by-name excludes
  your already-logged films and labels picks "liked by &lt;name&gt;".
- **Card-flip.** One flip card: front = rater, back = recommender, with a flip
  pill in each header and a CSS 3D `rotateY` animation. Faces are absolutely
  positioned; the wrapper height is kept in sync with the visible face via a
  `ResizeObserver` (+ a `min-height` so it never collapses on first paint). The
  five overlays (Channels, Find, Model, Share/Export, Friends, Where-you-watch)
  are body-level siblings, **outside** the 3D context, so the transform can't
  affect them.

**New low-severity findings (to triage):**

- **[Low] No per-movie "available on" badge.** The streaming filter works at the
  discover query level (you only ever see streamable films), but a card doesn't yet
  show *which* service it's on. Cheap to add later via `/movie/{id}/watch/providers`;
  skipped to keep cards snappy.
- **[Low] Niche provider sets shrink the pool.** Picking only a small service can
  leave channels with few matches → the rater's "no candidates right now" message
  appears sooner. Handled gracefully (14 fetch attempts, clear message); worth a
  future "your filter is very narrow" hint.
- **[Low] Friends are keyed by name.** Two friends with the *same* name collide
  (second overwrites); there's no rename UI. Fine for a handful of friends.
- **[Low] `_rec_seen` is in-memory and only the most-recent 2000 ids are passed**
  to the CLI (arg-length guard). A marathon single session could in principle let a
  very old pick reappear; negligible in practice, and it all resets on restart.
- **[Low] Card-flip height "morphs" when faces differ a lot in height.** The
  wrapper animates between the two faces' heights during a flip — looks like an
  intentional morph, not a bug, but noted.

**Still recommend (carried from pass 2):** the **[Med] auto-start on every page
load** now also recomputes on profile/provider changes — still acceptable for a
single user, but a "don't auto-run" preference would save TMDb calls. Feature
hashing for the Core ML port remains deferred pending an A/B.

All four features are in place and tested; none of the new items block daily use.

## Audit pass 4 — polish: rec undo button · "Not interested" rename · header layout

Four small tweaks, each tested (py-compile, `node --check`, id cross-check, and a
real in-process HTTP server with TMDb mocked).

- **Recommender "Undo last action" button** (replaces the transient toast from
  pass 3, for consistency with the rater's undo). The recommender now keeps its
  own one-level undo (`recUndo = {type, movie, idx}`); the new `/api/rec-undo`
  reverses the last rate / watchlist / Not-interested and the card steps **back**
  to that film. Recommender rate/watchlist calls now pass `stack:false` so they no
  longer push onto the *rater's* undo stack — verified the two undo systems are
  independent (a `stack:false` rate leaves `/api/undo` empty). The old
  `/api/undo-dismiss` + toast were removed.
- **"Not interested" rename — end to end.** `dismissed.md → not-interested.md`
  with a **one-time migration** in `main()` (`os.replace`, data preserved —
  verified). Renamed throughout: constants (`NOT_INTERESTED_PATH/_ID_COL/
  _TEMPLATE`), helpers (`append_not_interested`, `not_interested_ids`), the status
  token (`"not-interested"`), the endpoint (`/api/dismiss → /api/not-interested`),
  the template heading/column, the Find-a-movie status label, and `recommend.py`
  (`not_interested_ids`, with a **legacy `dismissed.md` fallback** so a recommend
  run before migration still excludes correctly — verified precedence + fallback).
  Docs + `.gitignore` updated (the legacy name stays git-ignored too).
- **Flip button → "Get recommendations"** on the rater face.
- **Recommender header rearranged.** Reads as a sentence —
  *Recommendations from [▾ Your taste / friend] using [Model]* — with the
  Safe/Balanced/Exploratory control + the source line dropped to a **second row**.
  Both flip buttons are pinned **top-right** of their headers (`align-items:
  flex-start`, left group allowed to wrap underneath), so they sit at the **same
  height** and flipping is a pure left↔right move. **Model** (and the "using"
  label) **hide when a friend is selected**, since the trained model only applies
  to your own taste.

No new defects found; the pass-3 items still stand (no per-card "available on"
badge; narrow filters shrink the pool; friends keyed by name).
