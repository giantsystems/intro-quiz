# Next steps

Where the work stands and what's worth doing next, easiest first. Written 2026-07-30 at
v1.33.0 — every claim below was checked against the code or the running server at that
point, with file references so you can re-check rather than trust it.

Nothing here is committed to. Each item says what's actually wrong, why it's worth
doing, and what it costs; if a better idea turns up, do that instead.

## Where things stand

v1.33.0 is tagged, released, and running in production. It shipped five improvements:
genre + decade round filters, a non-empty `easy` tier, cross-game track history,
abortable admin jobs with an honest 409, and the websocket handler table.

- **Tests:** 236 python + two node smokes (`make test`, `make test-js`). All green.
  (227 at the time of writing; item 2 added 9.) There is no CI — see
  [fork-changes.md](fork-changes.md#no-dependabot-either).
- **Library:** 23,083 tracks synced, 22,888 tiered
  (`easy` 1,286 / `medium` 5,720 / `hard` 7,841 / `tiebreak` 8,041).
- **Clips:** ~18,000 of 22,888 cut, 26G, with 69G free on the clip disk. The sweep
  resumes on each container start and **needs re-running after any deploy** that
  restarts the container mid-sweep.
- **Leaderboard:** empty. `rounds_played: 0` — no games have been played to completion
  since the database was last dealt with. That emptiness is what made item 2 free to fix.

Read [fork-changes.md](fork-changes.md) before touching anything that might conflict with
upstream, and [development.md](development.md) to get a local environment without
Navidrome, Home Assistant or Docker.

---

## 1. Make job progress tell the truth

**Cost:** half a day. **Do this first.**

Three different progress signals lie, and all three have already caused a wrong call:

- `_job_clips` ([main.py:335](../app/main.py#L335)) accepts `set_stage` and **throws it
  away**, so `stage` reads `"starting"` for the sweep's entire multi-hour run.
- `RunLogHandler.emit` ([jobs.py:84](../app/jobs.py#L84)) keeps the **first**
  `LOG_LINES_MAX` (100) lines and then appends `… (output capped)`. The "tail" is the
  head. An 11-hour-old last line means the log filled up, not that the job stalled.
- `/health`'s `tracks_playable` ([main.py:59](../app/main.py#L59)) counts
  **easy+medium only**, while the sweep cuts every tier — so it can sit still while
  real work happens.

**Why it's worth doing:** on 2026-07-30 these three proxies led to the sweep being
declared wedged three times, wrongly, and then aborted — costing ~11 hours of cutting
(resumable, so nothing was lost permanently). Right now the only reliable progress check
is SSHing in and counting files: `ls $CLIPS_HOST_DIR | wc -l` twice, ~50s apart, which
should show 15–25 new clips.

**Why it's cheap:** `clips.sweep` **already computes `remaining` on every batch**
([clips.py:238](../app/clips.py#L238)) and simply has nowhere to put it. Thread
`set_stage` through, and change the log sink to a `deque(maxlen=…)` so the tail is
genuinely the tail. Consider reporting all four tiers in `/health`, or renaming the
field so it stops reading like a total.

**Test it by** asserting `stage` changes between two batches of a faked sweep, and that
a log of 200 lines retains the *last* ones.

## 2. Fold leaderboard names by case — **DONE**

Done on 2026-07-30, inside the window: the production leaderboard was still empty
(`rounds_played: 0`), so no data migration was needed and none was written.

`all_time_leaderboard` now groups `COLLATE NOCASE`
([game.py:697](../app/game.py#L697)), and the lobby treats a case-variant of an existing
name as the SAME player ([game.py:333](../app/game.py#L333)) — joining as `robin` while
`Robin` is playing hands back Robin's seat and score instead of opening a second one.
That mattered beyond the leaderboard: `results` is keyed `(game_id, player)`
([db.py:49-56](../app/db.py#L49-L56)), so two spellings would have split one night's
score in two before the all-time query ever saw it.

Two decisions worth knowing about, both deliberate:

- **Display is Title Case, always** — one function, `display_name`
  ([game.py:55](../app/game.py#L55)). It is applied over the grouped rows because
  `GROUP BY ... COLLATE NOCASE` returns an *arbitrary* member spelling for the selected
  column, so without it the name on the board depended on which row SQLite happened to
  pick. The accepted cost is that it flattens `JB` to `Jb` and `McDonald` to `Mcdonald`.
  Refine it there if it ever grates; `resolve_name` folds on `casefold()` separately, so a
  refinement that stops being a total case fold still cannot split one player into two
  seats.
- **Every entry point resolves the name, not just `join`.** The phone keeps sending the
  spelling the player typed on `answer`, `ready` and the rest, and all of those check
  membership — folding only in `join` would have seated `robin` as `Robin` and then told
  them "join first" for the whole game. `on_join` re-claims the socket's name in the
  seated spelling too ([main.py:860](../app/main.py#L860)), or a master whose phone
  autocapitalised would be refused control of their own rounds. On the phone,
  `adoptSeatedName` ([quiz.js:97](../app/static/quiz.js#L97)) takes the server's spelling
  so the "is this me?" comparisons in `render()` keep working.

**Tests:** 9 new (236 python total). Each was verified by reverting the fix and watching
it fail — including the two subtle ones: `COLLATE NOCASE` *without* `display_name` still
passes a naive one-row assertion, and folding only in `join` passes everything except a
full game played under mixed spellings. The board needed no change; it renders snapshot
keys and the already-folded API rows.

## 3. Cover the HTTP surface, starting with the answer-leak guard

**Cost:** 2–3 days. **Do this before item 4.**

**36 of 44 routes have no test touching them.** `TestClient` is already used in
[test_ha.py:322](../tests/test_ha.py#L322) and
[test_jobs.py:148](../tests/test_jobs.py#L148), so the harness exists — this is writing
tests, not building infrastructure.

**Start with `/api/round/audio`.** Its phase gate
([main.py:1339-1346](../app/main.py#L1339-L1346)) is the only thing stopping a player
fetching the payoff clip during the question phase and hearing the answer early. The
original plan flagged this as a risk and it still has no test. It's load-bearing because
`/clips` is mounted as **ungated static** ([main.py:44](../app/main.py#L44)), so the
protection is two halves working together:

1. the phase gate on `/api/round/audio`, and
2. the question-phase snapshot not leaking the track id
   ([game.py:591](../app/game.py#L591) — the id ships only during `reveal`).

Both were verified holding on 2026-07-30, and `/clips/` directory listing returns 404 in
production. Nothing would tell you if a refactor broke either half. Phones must fetch
from `/api/round/audio` and never `/clips/{id}/...` — there's a comment saying so at
[quiz.js:106](../app/static/quiz.js#L106), and that comment is currently the only
enforcement.

Then the admin routes: `/api/admin/run/{name}`, `/api/admin/trivia/import`, and
`/api/admin/leaderboard/wipe` — that last one destroys all history behind a confirm
dialog and has no test at all.

## 4. Split `main.py` into routers

**Cost:** 3–4 days. **Blocked on item 3 — read the warning.**

1,653 lines and 49 routes; the only module in `app/` over 700 lines. It already has
comment-banner seams at [main.py:287](../app/main.py#L287), 453 and 710 — the file is
showing you where it wants to divide: admin/maintenance, game HTTP, board, websocket.

**Do not start this before item 3.** Moving 49 routes when 36 of them have no test is a
refactor you cannot verify. That is exactly the position the websocket `if/elif` chain was
in, and the reason its split was worth doing was that 34 tests went in *with* it. In the
right order this becomes boring; in the wrong order it's a gamble on a live system.

## 5. Survive a restart mid-game

**Cost:** about a week, and it needs a **design decision before any code**.

`Hub` holds the live game in memory, and the job slot is `_CURRENT: list = [None]`
([jobs.py:22](../app/jobs.py#L22)), also in memory. A container restart mid-game loses
the game entirely — scores, round position, who was ready — and players just see the
phone go dead with no explanation.

The hard part is not persistence, it's semantics. Does an in-flight round restart or
count as forfeit? What if one player's phone reconnects and two others don't? Is a game
resumable an hour later, or does it expire?

**Consider the cheap version first.** For a house-party quiz, how often do you really
restart mid-game? If the honest answer is "almost never", then a clear *"that game was
lost — start a new one"* on reconnect delivers most of the value for a fraction of the
work. Decide that before writing anything; the full version is only worth a week if
mid-game restarts actually happen.

---

## Known, deliberately not scheduled

- **GitHub Actions and Dependabot are off on purpose.** Don't re-enable either without
  asking — see [fork-changes.md](fork-changes.md#no-dependabot-either). Dependency bumps
  are manual: change the pin, then `make test` and `make test-js`.
- **`fastapi` is pinned to `==0.139.*`**; 0.140 and 0.141 are out. A Dependabot branch
  proposing 0.140 was deleted as stale (22 commits behind, would have reverted
  `tests/test_ws.py`). The bump itself is still valid whenever someone wants it.
- **The Home Assistant long-lived token wants rotating.**
- **`/data/retag-journal.txt` holds ~628 stale entries** with pre-anonymisation paths.
  Harmless, and deleting it was declined once already — leave it.
- **Orphaned pre-anonymisation commits** remain reachable on GitHub by direct SHA and on
  the PR #5/#6 diff pages. A Support request could purge them; the decision was to leave
  it. Don't publish those SHAs.

## Working rules that aren't obvious from the code

- **Never commit deployment specifics** — no IPs, hostnames, usernames or server paths in
  code, docs, comments, compose files, commit messages or PR bodies. They belong in the
  gitignored `.env`. This repo is public and its history has already been rewritten once
  to remove them.
- **Don't print secrets.** Source them (`set -a && . ./.env && set +a`) and reference
  `"$VAR"`; never echo a credential value into output.
- **PRs go to the fork**, `giantsystems/intro-quiz` — never upstream `colfin22`.
- **One job at a time.** A refused start is a 409 naming the holder; `POST
  /api/admin/abort` stops it. Abort is cooperative and the job resumes where it stopped.
- **Judge a background job by its output**, not its status fields — see item 1 for why.
- **Check the published port when curling the server.** If something else owns 8000 on
  the host, `curl :8000/health` can return a cheerful `OK` from *that* service and look
  like a healthy app. A real answer is JSON with a `version` field.
