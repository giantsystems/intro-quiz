# Next steps

Where the work stands and what's worth doing next. Written 2026-07-30, current as of
v1.34.0 — every claim below was checked against the code or the running server at that
point, with file references so you can re-check rather than trust it.

Two sections. **Items 1–5** are improvements found by reading the code, easiest first.
**Items 6–13** were requested by the project's owner, listed in the order asked. The split
matters: the second group is what someone actually wants, so prefer it when the two
compete, even though several are dearer.

Nothing here is committed to. Each item says what's actually wrong, why it's worth
doing, and what it costs; if a better idea turns up, do that instead.

**Cheap wins, if you want a short list:** item 8 may be mostly layout. Items 4 and 11 have
hard prerequisites, stated in place.

## Where things stand

**v1.34.0** is running in production, deployed 2026-07-30. It shipped items 1, 2 and 6:
honest job progress, case-folded player names, and genre exclusion. v1.33.0 before it
brought genre + decade round filters, a non-empty `easy` tier, cross-game track history,
abortable admin jobs with an honest 409, and the websocket handler table.

**Item 13 (choose the number of rounds) is done on `master` and not yet released** — the
round-count wall, the preflight measured against the chosen count, and the two warnings a
short game now needs. Details in item 13 below.

- **Tests:** 259 python + two node smokes (`make test`, `make test-js`). All green.
  There is no CI — see [fork-changes.md](fork-changes.md#no-dependabot-either).
- **Library:** 23,083 tracks synced, 22,888 tiered.
- **Clips: the sweep is finished.** `clips_remaining: 0`, and `playable_by_tier` reads
  `easy` 1,174 / `medium` 5,433 / `hard` 7,481 / `tiebreak` 7,776 — 21,864 of 22,888 tiered
  tracks have a clip, the rest being rows the cutter can't use. Earlier notes here warned
  the sweep needed re-running after every deploy; that no longer applies, and those fields
  are how you'd tell rather than counting files over SSH.
- **Leaderboard:** empty at the time item 2 landed (`rounds_played: 0`), which is what made
  the case fold free to fix — no rows to migrate.

Read [fork-changes.md](fork-changes.md) before touching anything that might conflict with
upstream, and [development.md](development.md) to get a local environment without
Navidrome, Home Assistant or Docker.

---

## 1. Make job progress tell the truth — DONE

All three lies are fixed. What they were, and what replaced each:

- `_job_clips` accepted `set_stage` and threw it away, so `stage` read `"starting"` for
  the sweep's entire multi-hour run. `clips.sweep` now takes the callback and reports
  cut-so-far and remaining — **per clip**, not just per batch, because a 100-clip batch
  is up to an hour and a frozen field for an hour is exactly what got misread. A stalled
  sweep now says `backing off … (attempt 2 of 6)`, which is the one state that
  legitimately looks stuck. `sync_library` had the same hole and got the same treatment.
- `RunLogHandler.emit` kept the **first** 100 lines, so the "tail" was the head and its
  newest line was hours old the moment the log filled. The sink is a `deque(maxlen=…)`;
  dropped lines are counted in `log_dropped` and spelled out as a leading note where the
  API serves it, so truncation is still distinguishable from a short log.
- `/health`'s `tracks_playable` counts easy+medium while the sweep cuts every tier.
  **Its meaning is unchanged** — external watchers read that key and `ready_to_play` is
  the same population — so `playable_by_tier`, `tracks_playable_all_tiers` and
  `clips_remaining` were added alongside it instead. `clips_remaining` is the direct
  answer to "is the sweep making progress": it counts the same rows the cutter's next
  batch will draw from. The name `tracks_playable` still reads like a total and still
  isn't one; renaming it would break the watchers, so it stays.

The cost of the old proxies, for the record: on 2026-07-30 they had the sweep declared
wedged three times, wrongly, and aborted — ~11 hours of cutting, resumable, so nothing
was lost permanently. `ls $CLIPS_HOST_DIR | wc -l` twice a minute apart is no longer the
only trustworthy check, though it remains the ground truth the fields are judged against.

Every claim above has a test that fails if the fix is reverted; each was proved by
injecting the old behaviour back. The log-tail test asserts *which* lines survived
rather than how many — a length check passes with a first-N sink too.

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
  ([game.py:71](../app/game.py#L71)). It is applied over the grouped rows because
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

**Tests:** 9 new. Each was verified by reverting the fix and watching
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

# Requested features

Asked for on 2026-07-30, listed in the order they were requested rather than by cost.
The cost estimates and the notes on what's already in place were checked against the code
the same day. Items 6 and 8 are cheap; item 13 was, and is now done. Item 11 is explicitly
a maybe-never.

## 6. Exclude genres from a game

**Cost:** a day. **Cheap, and there's a concrete reason to want it.**

`filter_sql` ([game.py:80-82](../app/game.py#L80-L82)) only supports `genre IN (...)` —
include-only. Excluding means adding a `NOT IN` arm, plus somewhere in the UI to express
it, plus threading it through `pool_count` and the preflight the same way genres already
are.

**The use case is already visible in the library:** the tags include **`NotForKids` with
144 tracks**. Excluding one tag is the natural way to say "family game, skip those",
and today the only way to get that is to tick the other 19 genres.

**The subtlety that decides the design:** exclude is *not* the same as ticking everything
else. The picker only offers genres with at least `min_tracks` (25) tracks — 20 of them,
covering 6,327 of the 6,607 quizzable tracks. **280 tracks (4%) have a genre that isn't
in the picker at all**, or none. Ticking all 20 includes those 280 nowhere; `NOT IN
('NotForKids')` includes them all. Both are defensible, but they're different games, and
whichever you pick should be what the UI plainly says. Note also that a track has one
`genre` value, so exclusion can't miss a track via a second tag.

Watch the interaction with the existing preflight: exclusion shrinks the pool too, so
`enough_for_10` must account for it, and the `filter_label`
([game.py:580](../app/game.py#L580)) needs to read sensibly for a negative filter.

## 7. Optional admin login on the landing page, with navigation

**Cost:** 1–2 days. **The auth primitive already exists — this is mostly UI.**

Today `/admin` is reachable only by knowing the URL, and admin auth is a token in
`localStorage` sent as an `X-Admin-Token` header ([admin.js:13,29](../app/static/admin.js#L13),
checked by `require_admin` at [main.py:25](../app/main.py#L25)). `admin.js` already
`prompt()`s for the password on a 401 and stores it. So the mechanism works; what's
missing is a way in from the front page and a way to move around once you're in.

**The requirement that shapes it:** logging in must be entirely optional, and a normal
player who ignores it sees today's join screen unchanged. So this is an unobtrusive
control on the landing page — not a login wall — that, once used, reveals links to
`/admin`, `/health`, `/board` and **back to the game in progress**, switchable without
retyping a URL.

Two things worth deciding early. First, `ADMIN_PASSWORD` may be **unset**, in which case
`check_token` ([jobs.py:68-70](../app/jobs.py#L68)) treats everything as open — decide
whether the button appears at all then, or admits everyone. Second, this is a real
security boundary being made discoverable: today obscurity is part of the protection, and
a visible button removes that. `X-Admin-Token` is a header rather than a cookie, so it
isn't sent cross-site, but it also isn't a session — there's no expiry and no logout.
Add a logout that clears `localStorage` at minimum.

## 8. Show more on the scoreboard

**Cost:** 1–2 days. **Partly already there — check before rebuilding.**

The request is: who's local and who's remote, a sensible no-game-active state, the current
round, and who got the last one right. Three of those four exist in some form:

- **Remote marker** — 🌐 already renders per player
  ([board.html:367](../app/static/board.html#L367)).
- **Who got it right** — already shown at reveal as `name ✅ +points` / `name ❌`
  ([board.html:334-340](../app/static/board.html#L334)).
- **Round number** — `round` and `total_rounds` are both in the snapshot
  ([game.py:596-597](../app/game.py#L596)); whether they're *prominent* is the question.
- **Idle state** — there is a `#b-idle` card ([board.html:46](../app/static/board.html#L46)),
  currently the join QR.

So read the board on a real screen first and decide what's genuinely missing versus what's
present but too small or too transient. This is likely a layout and prominence job rather
than new plumbing — and if any new *data* is needed, that's a `snapshot()` change
([game.py:591](../app/game.py#L591)) which the phone UI and both node smokes also read.
Keep `make test-js` green; it renders every phase for exactly this reason.

## 9. Discover Chromecasts from /admin and pick one in flight

**Cost:** 2–3 days. **The networking approach is decided — see below.**

Today `DISPLAYS` is parsed from the environment **at import time** as hardcoded `Name=ip`
pairs ([board_cast.py:12-19](../app/board_cast.py#L12-L19)) — there is no discovery. Worth
knowing: **`DISPLAYS` is not set in production**, so casting is currently unconfigured
altogether. This item is what would make it usable without hand-collecting IPs.

The pinned `pychromecast` 14.0.10 does expose discovery — `get_chromecasts()`,
`discover_chromecasts()`, `start_discovery()` — so the library can do it.

### The networking constraint, and the decided approach

Discovery is mDNS/zeroconf multicast, and `docker-compose.yml` uses **default bridge
networking with a published port** ([docker-compose.yml:12](../docker-compose.yml#L12)).
Multicast does not cross a bridge network, so `get_chromecasts()` from inside the container
finds **nothing**. The existing `get_chromecast_from_host()` path is unaffected, because it
dials a known IP directly.

**Decision (owner, 2026-07-30): give the container its own real LAN IP** — a macvlan (or
ipvlan) network — **as an opt-in extra in `docker-compose.yml`, only needed if you want
scanning.** The LAN in question is known to carry mDNS. Default deployment stays on bridge
with today's published port, so nothing changes for anyone who doesn't want discovery.

Why a real IP rather than `network_mode: host`: host mode puts the app straight onto the
host's port 8000, and **8000 is already taken on this host** — that's the collision
`QUIZ_PORT` exists to dodge (see the port trap in the working rules below). A macvlan
address gives the container its own port 8000 with nothing to collide with, and keeps
multicast intact.

**Consequences to handle, not discover later:**

- **The app's address changes** on the macvlan path — it's the container's LAN IP, not
  `host:QUIZ_PORT`. `BOARD_URL`, `JOIN_URL` and `APP_BASE_URL` are all absolute and would
  need to match, as would whatever reverse proxy fronts it (`APP_BASE_URL` is currently an
  HTTPS name, so the proxy target moves). Get this wrong and the board still loads while
  the join QR points somewhere unreachable.
- **Macvlan needs a parent interface named in the compose file**, which is host-specific —
  exactly the kind of deployment detail that must come from `.env`, never a committed
  literal. See the working rules below.
- **The host usually cannot reach its own macvlan container** without an extra route. Bear
  it in mind when a health check from the host itself starts failing for no apparent
  reason.
- **Make it genuinely optional.** Compose can't toggle a network block with a plain
  variable, so this likely wants a documented override file (e.g. an opt-in
  `docker-compose.macvlan.yml` used with `-f`) rather than edits to the committed default.
- Keep `known_hosts` / manual `DISPLAYS` working regardless, as the fallback for anyone on
  bridge — discovery should *add* to that path, not replace it.

Probe before building UI: bring the container up on the macvlan and confirm
`get_chromecasts()` actually returns devices. Everything above assumes it does; the LAN
carries mDNS, but this is the one step worth proving rather than assuming.

Also note the existing "pick in flight" pattern to copy rather than reinvent: the `hub`
already holds a mutable `display` and the `set_display` websocket handler already swaps
it, hiding the old board first ([main.py:786-788](../app/main.py#L786)). Discovery should
feed that mechanism, not replace it. Cast failures must stay non-fatal — `show_board`
swallows exceptions on purpose ([board_cast.py:68](../app/board_cast.py#L68)), because the
board is cosmetic and must never break a game.

## 10. A display-only dashboard page for AirPlay

**Cost:** 1–2 days. **The pragmatic answer to the Apple TV problem, and it's a real gap.**

Apple TV was investigated before and **shelved as genuinely impossible**: HA can control
the TVs, but tvOS has no browser in any source list, so `select_source` and `play_media`
cannot put a web page on one. Mirroring a browser from another device sidesteps that
entirely, and needs nothing from HA.

`/board` is already a plain page any browser can open
([main.py:1047](../app/main.py#L1047)), so you can *almost* do this today. **The reason a
separate page is the right call:** `/board` plays the round audio — it owns an
`AudioContext` and fetches every clip ([board.html:123-194](../app/static/board.html#L123)),
including the "Tap anywhere for sound" unlock overlay. AirPlay-mirroring it would send the
clip out of the TV *as well as* the room speaker: echo, and a second unlock tap on a
device nobody is holding.

So the deliverable is a **display-only** view: same state feed over the websocket, same
scoreboard, **no audio path at all** and no unlock overlay. Factor the render out of
`board.html` rather than forking it, or the two drift. This pairs naturally with item 8 —
same rendering work, and a mirrored screen is exactly where a better scoreboard pays off.

Set expectations honestly in the doc you write: starting the mirror is a manual
Control Centre action on the iPad or Mac. The server cannot initiate it, and that's a
platform limit, not a shortcoming to fix later.

## 11. Multiple concurrent games

**Cost:** weeks. **Explicitly a maybe-never — the requester said so, and the code agrees.**

`hub = Hub()` is a **module-level singleton** ([main.py:624](../app/main.py#L624))
referenced **121 times** in `main.py`. Every websocket handler takes its hub from the
session, every HTTP route reads the global, and neither `board.html` nor `quiz.js` has any
concept of a room or game id — **zero occurrences** in either. So this isn't a feature
you add, it's a change of shape: every route, both clients, and the websocket protocol all
grow a room dimension.

It's worse than a naming problem, because real single-instance resources are bound to that
singleton and cannot simply be duplicated: `hub.display` is one cast display,
`group_snapshot` is one set of grouped speakers, and the house speaker is one speaker. Two
concurrent games cannot both own them. That's a product decision — do rooms share a board,
or does only one game get audio? — not a refactor.

**If it's ever wanted, do items 3 (route coverage) and 4 (split `main.py`) first.** Having
the routes tested and the module split is the difference between a hard change and an
unsafe one. Until then this is a note about why the code looks the way it does, not a plan.

## 12. Bring the player-facing instructions up to date with the fork

**Cost:** half a day, most of it deciding what to leave out.

The **"How to play" card** on the join screen
([index.html:17](../app/static/index.html#L17)) still described upstream's game. One line
was actively wrong — *"Stuck? Ask for a few more seconds"* implies a player-facing control,
but the replay is the game master's, and it fires only when nobody has answered
([main.py:627](../app/main.py#L627)). Four fork features were missing: remote players
hearing audio on their own phone, boost rounds, half-time trivia, and the all-time table.
That card is now fixed; **the rest of this item is the same job everywhere else.**

Worth checking, in rough order of how visible it is to a player:

- **`v-master`** ([index.html:55](../app/static/index.html#L55)) lists what the master runs.
  It reads well, but verify it against what the buttons now do — it predates the payoff-skip
  change, where upstream's hard 12-second lock became a two-second grace.
- **The `/board` screens** — idle, question, reveal, half time, finished. A TV in a room full
  of people is read by everyone at once, and it's the one surface nobody can ask questions
  about.
- **`README.md`** — the feature list is a fork/upstream mix. [fork-changes.md](fork-changes.md)
  is accurate and current; the README is what someone reads first.
- **[setup.md](setup.md)** — accurate as far as it goes, but written incrementally per
  feature. Remote players, speaker grouping, genre filters and exclusions each landed
  separately, so a first-time reader gets the pieces in implementation order rather than
  the order they'd set them up.

**Why it's worth doing:** every one of these is a promise to a player. A wrong instruction
costs more than a missing one — the player follows it, it doesn't work, and they conclude
the app is broken rather than the text. The "ask for a few more seconds" line was that bug
in miniature for however long it sat there.

**Test it by** reading each screen as somebody who has never played, and by running
`make test-js` — the two node smokes render every phase, so a broken tag fails there rather
than on the night.

## 13. Choose the number of rounds — **DONE**

Ten was never a rule, just the only number the phone could send: `Game.__init__` had taken
`rounds` all along ([game.py:326](../app/game.py#L326)) and `new_game` had always read it,
but `startGame` hardcoded `rounds: 10` and the Play again button hardcoded it a second time.
Both are gone. The idle card now has a **How many rounds?** wall
([index.html:50-52](../app/static/index.html#L50-L52)) rendered by `renderRounds`
([quiz.js:524-537](../app/static/quiz.js#L524-L537)).

The choices are served, not hardcoded in the client: `ROUND_CHOICES` and `DEFAULT_ROUNDS`
([game.py:31-32](../app/game.py#L31-L32)) come back from `/api/round-filters`
([main.py:211-215](../app/main.py#L211-L215)) alongside `halftime_min_rounds`, because a
count the buttons offer but the server's preflight refuses is a Start button that locks for
no stated reason. The count arriving over the websocket is clamped rather than trusted —
`MIN_ROUNDS` / `MAX_ROUNDS` ([game.py:37-38](../app/game.py#L37-L38)), checked at
[main.py:869-875](../app/main.py#L869-L875) — because `rounds: 0` finishes on the first tap
and `rounds: 5000` stalls the lobby in the picker.

**The preflight bug is fixed, and that was the real defect here.**
`/api/round-filters/count` measured every filter combination against 10, so a theme with 6
playable tracks locked Start even for a 5-round game that would have played perfectly. It
now takes `rounds=` and answers `enough` against that count
([main.py:220-252](../app/main.py#L220-L252)). `enough_for_10` is still returned, unchanged
and still always against 10, because it is a published key; the phone prefers `enough` and
falls back to it ([quiz.js:595](../app/static/quiz.js#L595)) so a stale cached script keeps
working instead of locking Start on an `undefined`. The round count is part of the fetch
dedupe key ([quiz.js:577-580](../app/static/quiz.js#L577-L580)) — the filters that can't
fill 20 may fill 5, so a shorter game has to re-ask rather than inherit the locked Start.

Three decisions, and the reasoning behind each:

- **Preset buttons, not a stepper or a number field.** 3 / 5 / 10 / 15 / 20 is one tap and
  no keyboard, and it reuses the wall idiom the genre and decade pickers already established
  on the same card — a stepper means five taps to get from 10 to 15, and a free input means
  a phone keyboard covering the card plus a validation story for "seven hundred". The cost
  is that 7 rounds is not offerable without a code change; that was accepted. The wall does
  differ from its neighbours in one way, deliberately: tapping the chosen count again leaves
  it alone rather than toggling off ([quiz.js:486-491](../app/static/quiz.js#L486-L491)),
  because a game has to be *some* length.
- **The 6-round half-time floor stayed where it was.** It was a bare `6` inside `is_halfway`
  and is now `HALFTIME_MIN_ROUNDS` ([game.py:42](../app/game.py#L42), read at
  [game.py:696-698](../app/game.py#L696-L698)), but the number didn't move. Lowering it was
  considered and rejected: a facts-and-three-questions break in a 3-round game is more
  interruption than interval, and it would land after round one. So a short game has no
  trivia, on purpose — and the fix for the surprise is the UI saying so ("too short for
  half-time trivia", `roundsNote` at
  [quiz.js:500-503](../app/static/quiz.js#L500-L503)) rather than the absence looking like
  a bug the master should report. The "How to play" card promises a break, which is what
  makes silence here actively misleading rather than merely quiet.
- **Boosts are randomised when they don't all fit.** `build_rounds` keeps at least one
  neutral round, so a game of *n* rounds has *n−1* boost slots —
  `boosts_available()` ([game.py:315-322](../app/game.py#L315-L322)), which exists so the
  phone's warning and the loop that enforces the cap can't drift apart. Five players in a
  3-round game means two go without. The previous behaviour wasn't neutral about who: it
  walked `self.players`, which is insertion order, so the quickest phone to join reliably
  got a boost and the last to arrive reliably didn't, every game. `build_rounds` now
  shuffles the contenders first ([game.py:401-417](../app/game.py#L401-L417)). The warning
  carries the numbers ("only 2 of 5 boost rounds fit in 3 rounds") and lives in the
  **lobby**, not on the idle card — `lobbyBoostNote`
  ([quiz.js:512-522](../app/static/quiz.js#L512-L522)) — because on the idle card
  `state.players` is empty every time: players only arrive after `new_game`. That needed a
  new snapshot key, `rounds_planned` ([game.py:750](../app/game.py#L750)), since rounds are
  built lazily at the first `start_round` and `total_rounds` is 0 for the whole lobby. The
  master can still abandon and restart longer, which is the only fix available.

Play again carries the round count forward and deliberately not the filters
([quiz.js:612-619](../app/static/quiz.js#L612-L619)): a group that just enjoyed a 5-round
game wants another 5-round game, whereas the theme is what they came back to the idle card
to choose — and the finished screen has no filter wall to preflight from, so sending no
filters keeps it to the one case that never needs checking.

**What a test has to show,** beyond `total_rounds` being honoured at 3 and at 20: that half
time fires at 6 rounds and not at 5; that a pool of 6 tracks is accepted for a 5-round game
and refused for a 10-round one; that a lobby larger than `boosts_available()` still builds a
full game with a neutral round in it; and that a junk `rounds` value off a phone comes back
as a `GameError` rather than a built game. The node smokes render the idle card, so the
round wall and its warning line are covered by `make test-js` as well.

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
