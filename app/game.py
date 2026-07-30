"""Game engine — pure logic, no I/O beyond the DB handle it's given.

One game at a time (it's a kitchen, not a casino). The websocket layer in
main.py drives this and broadcasts snapshots; timing uses an injectable
clock so tests don't sleep.
"""
import logging
import os
import random
import time
from datetime import datetime, timezone

from . import library, trivia

LOGGER = logging.getLogger(__name__)

ANSWER_WINDOW_S = 20
PAYOFF_S = 12          # mirrors clips.PAYOFF_LEN — how long the reveal payoff runs
# The payoff is a sing-along, not a cutscene: the host can move on early. This
# grace only stops the reveal being skipped before anyone has read it (a stray
# double-tap on the answer that triggered the reveal), so it's deliberately
# short — a locked button reads as a broken one, and every extra second of it
# is a second the host is jabbing at a button that ignores them.
PAYOFF_GRACE_S = 2
TF_COUNT = 3           # true/false questions at half time
TF_POINTS = 50         # enough to shake the standings, not to decide the game
MAX_DURATION_S = int(os.environ.get("MAX_DURATION_S", "720"))  # longer = DJ mix / live jam, not quizzable
# Too SHORT is the mirror problem and was unguarded: a 20s intro clip plus a 12s
# payoff needs a song appreciably longer than 32s, or the "intro" IS the whole
# track and the payoff overlaps it — the clip hands over the answer. Skits,
# stings and album interludes live down here (65 of them in this library, the
# shortest 8s). library.MIN_DURATION_S is the same number for the cutter.
MIN_DURATION_S = int(os.environ.get("MIN_DURATION_S", "32"))
# over-long DJ mixes / live jams never enter the quiz (compilations are fine)
QUIZZABLE = ("active=1 AND banned=0 AND clipped_at IS NOT NULL "
             f"AND (duration IS NULL OR duration BETWEEN {MIN_DURATION_S} AND {MAX_DURATION_S})")
BASE_POINTS = 100
SPEED_BONUS_MAX = 50  # linear decay to 0 across the window
# How many recently-asked tracks the picker tries to avoid. Roughly 20 games of 10 rounds:
# far enough back that a weekly quiz never repeats itself, small enough that the pool still
# feels like the whole library. It is a preference, not a filter — see _freshest().
RECENT_MEMORY = 200
# Remote players stream the clip to their own phone, so their audio starts a
# beat after the room's — buffering, then decode. Their speed bonus is measured
# from when their audio actually started instead of from the room clock, but the
# credit is capped: audio_started is client-reported, and without a ceiling a
# phone could claim a very late start and mint a full-speed bonus at leisure.
REMOTE_LATENCY_CREDIT_MAX_S = 5.0


class GameError(RuntimeError):
    pass


def display_name(name: str) -> str:
    """The one spelling of a player's name the whole app shows: Title Case.

    Names are folded by case, so `robin` and `Robin` are one player with one
    all-time row. They used to accumulate as two, and a returning player lost
    their history by typing a different capitalisation. Something then has to
    choose the spelling to display, and Title Case is chosen for being
    predictable rather than clever.

    Deliberately simple, with a known cost: it flattens `JB` to `Jb` and
    `McDonald` to `Mcdonald`. That was accepted, not overlooked — a table of
    exceptions is upkeep with no end for a house quiz with a handful of
    regulars. Refine it HERE if it ever grates; this is the only place the rule
    lives. Game.join folds on casefold() separately, so a refinement that stops
    being a total case fold still cannot split one player into two lobby slots.
    """
    return name.strip()[:24].title()


# A year this far outside living memory is a broken tag, not a release date. Real junk in
# this library: AFI's 'Miss Murder' is tagged 1212. Left alone it would be a "1210s" entry
# in the decade picker, and a track that no decade filter could ever legitimately match.
YEAR_MIN, YEAR_MAX = 1900, 2030


def exclusion_sql(exclude_genres: list[str] | None = None) -> tuple[str, list]:
    """The "everything EXCEPT these" half of the round filter, on its own.

    Separate from filter_sql because it outlives the theme: pick_decoys widens to the
    unfiltered pool when a narrow theme can't fill four options, and the widening may drop
    the theme but must never drop the exclusion.

    Not replaceable by "tick the other nineteen genres". The picker only offers genres
    holding at least min_tracks, so a few hundred quizzable tracks carry a tag no checkbox
    ever shows — ticking everything on offer quietly loses them, and excluding by name
    doesn't.

    The `genre IS NULL` arm is LOAD-BEARING; it is not defensive noise. SQL's three-valued
    logic makes `NULL NOT IN ('X')` evaluate to NULL rather than true, so a bare NOT IN
    silently drops every untagged track. Checked against SQLite over the rows
    ['Pop', 'X', NULL, 'Obscure']: `genre NOT IN ('X')` returns Pop and Obscure and loses
    the NULL row; the form below returns Pop, NULL and Obscure. The chosen semantics are
    "exclude only what I named", and an untagged track was not named. Do not simplify this
    back to a bare NOT IN.
    """
    if not exclude_genres:
        return "", []
    qmarks = ",".join("?" * len(exclude_genres))
    return f" AND (genre IS NULL OR genre NOT IN ({qmarks}))", list(exclude_genres)


def filter_sql(genres: list[str] | None = None, year_from: int | None = None,
               year_to: int | None = None,
               exclude_genres: list[str] | None = None) -> tuple[str, list]:
    """Build the round-filter SQL fragment and its params: genres and/or a year range.

    Deliberately SEPARATE from QUIZZABLE rather than folded into it. QUIZZABLE describes
    what is permanently playable and is read by /health and the artist wall (main.py), which
    must keep measuring the WHOLE library — a "60s only" game must not make /health report
    the library as nearly empty, or the artist wall shrink to the artists of one decade.

    Genres match EXACTLY. The tags are freeform (70 distinct over the quizzable pool, with
    'Rock', 'Hard Rock', 'Alternative Rock' and 'Rock & Roll' all separate), so a substring
    match on 'Rock' would silently pull in four genres the user didn't tick. Exact matching
    is predictable, and the picker UI is built from the real values with counts, so nobody
    has to guess what exists.

    A year filter also excludes tracks whose year is missing or junk (493 quizzable tracks
    have no year at all). You cannot honestly claim an untagged track belongs to the 90s.

    exclude_genres is the mirror image and is NOT symmetric with the include list — see
    exclusion_sql for why an untagged track survives an exclusion but not an inclusion.
    A genre named in BOTH lists is EXCLUDED: the two fragments are ANDed, so a veto always
    beats a request. That falls out of the SQL rather than being special-cased, which is the
    point — there is no branch here that could drift from it. "Rock, but not Rock" then
    counts zero tracks and the preflight locks Start, which is honest; the alternative
    (quietly dropping the exclusion) would ignore something the host explicitly asked for.
    """
    frag, params = "", []
    if genres:
        frag += f" AND genre IN ({','.join('?' * len(genres))})"
        params += list(genres)
    xfrag, xparams = exclusion_sql(exclude_genres)
    frag += xfrag
    params += xparams
    if year_from is not None or year_to is not None:
        lo = max(int(year_from), YEAR_MIN) if year_from is not None else YEAR_MIN
        hi = min(int(year_to), YEAR_MAX) if year_to is not None else YEAR_MAX
        frag += " AND year IS NOT NULL AND year BETWEEN ? AND ?"
        params += [lo, hi]
    return frag, params


def pool_count(conn, tiers: list[str], genres: list[str] | None = None,
               year_from: int | None = None, year_to: int | None = None,
               exclude_genres: list[str] | None = None) -> int:
    """How many tracks a game with these filters could draw on.

    Exists so the UI can warn BEFORE the game starts. Without it the only feedback was
    Game.__init__ raising GameError at the moment someone tapped "Start a new game" —
    'Reggae + 1960s' is 56 and 208 tracks respectively and their intersection may be zero,
    which is a fine thing to want and a terrible way to find out.
    """
    frag, fparams = filter_sql(genres, year_from, year_to, exclude_genres)
    qmarks = ",".join("?" * len(tiers))
    return conn.execute(
        f"SELECT COUNT(*) c FROM tracks WHERE {QUIZZABLE} AND tier IN ({qmarks}){frag}",
        (*tiers, *fparams)).fetchone()["c"]


def recent_track_ids(conn, limit: int = RECENT_MEMORY) -> list[str]:
    """The most recently ASKED track ids, newest first — see the `plays` table.

    Returned as an ordered list rather than a set because "how recently" is what makes
    the fallback graceful: when a pool is too small to avoid repeats entirely, the
    freshest repeats are the ones to give back first.
    """
    return [r["track_id"] for r in conn.execute(
        "SELECT track_id, MAX(played_at) m FROM plays GROUP BY track_id "
        "ORDER BY m DESC LIMIT ?", (limit,))]


def _freshest(rows: list, recent: list[str], n: int) -> list:
    """Take n rows, preferring ones not played lately.

    Recency SORTS rather than filters, on purpose. A hard exclude looks equivalent on a
    14,000-track pool and breaks badly on a small one: a genre+decade filter can cut the
    pool to a few dozen, where excluding the last 200 plays would leave too few tracks and
    fail the game outright at start. Sorting can always fill the round — worst case it
    hands back the least-recently-asked repeats, which is exactly what a human would do.
    """
    rank = {tid: i for i, tid in enumerate(recent)}   # 0 = most recent
    fresh_rank = len(recent)                          # never played beats every repeat
    # rows arrive already shuffled (ORDER BY RANDOM()), so equal-recency rows stay random
    return sorted(rows, key=lambda r: -rank.get(r["id"], fresh_rank))[:n]


def pick_tracks(conn, rounds: int, tiers: list[str], exclude: set | None = None,
                filters: tuple[str, list] | None = None) -> list[dict]:
    qmarks = ",".join("?" * len(tiers))
    ex = exclude or set()
    exq = f"AND id NOT IN ({','.join('?' * len(ex))}) " if ex else ""
    ffrag, fparams = filters or ("", [])
    # Deliberately NOT "LIMIT rounds": the freshness sort needs candidates to choose
    # BETWEEN. Limiting in SQL would hand back 10 random rows and leave nothing to prefer,
    # so the whole history would have no effect. Capped so a huge library stays cheap.
    rows = conn.execute(
        f"SELECT * FROM tracks WHERE {QUIZZABLE} AND tier IN ({qmarks}) {exq}{ffrag} "
        f"ORDER BY RANDOM() LIMIT ?",
        (*tiers, *ex, *fparams, max(rounds * 20, 200))).fetchall()
    if len(rows) < rounds:
        what = f"tiers {tiers}" + (" with the chosen filters" if ffrag else "")
        raise GameError(f"only {len(rows)} clipped tracks in {what} — need {rounds}")
    return [dict(r) for r in _freshest(rows, recent_track_ids(conn), rounds)]


def pick_artist_track(conn, artists: list[str], exclude: set,
                      filters: tuple[str, list] | None = None) -> dict | None:
    """One quizzable track by any of the player's chosen artists (any tier).

    Matched on library.artist_key rather than the literal tag: picking 'AC/DC'
    off the wall should reach the tracks tagged 'AC, DC' and 'AC-DC' too (52
    tracks vs 77 in this library). SQLite can't index on a Python function, so
    the key comparison happens in Python over the candidate rows — the pool is
    thousands of rows, not millions, and this runs once per player per game.
    """
    if not artists:
        return None
    wanted = {library.artist_key(a) for a in artists}
    ex = exclude or set()
    exq = f"AND id NOT IN ({','.join('?' * len(ex))}) " if ex else ""
    ffrag, fparams = filters or ("", [])
    rows = conn.execute(
        f"SELECT * FROM tracks WHERE {QUIZZABLE} {exq}{ffrag} ORDER BY RANDOM()",
        (*ex, *fparams)).fetchall()
    matches = [r for r in rows if library.artist_key(r["artist"]) in wanted]
    if not matches:
        # A boost round is a bonus, not a promise. With filters on, a player's favourite
        # artists may have nothing in the chosen genre or decade — that's a normal outcome,
        # and build_rounds just fills the slot from the pool instead.
        return None
    # Same freshness preference as the main pool. It matters MORE here: a player picks the
    # same three favourite artists most weeks, so without this their boost round is drawn
    # from a handful of tracks and lands on the same song repeatedly.
    return dict(_freshest(matches, recent_track_ids(conn), 1)[0])


def pick_decoys(conn, track: dict, n: int = 3,
                filters: tuple[str, list] | None = None,
                exclusions: tuple[str, list] | None = None) -> list[dict]:
    """Plausible wrong answers: same tier, different artist, prefer same decade.

    The SQL exclusion is a literal string compare, which a variant spelling of
    the answer's own artist walks straight through: a 'Back In Black — AC/DC'
    round could offer 'Back In Black — AC, DC' as a decoy, two options that read
    the same with only one scored right. So the artist filtering below is done on
    library.artist_key, not on the raw tag, and a decoy whose title matches the
    answer's is dropped outright (11 songs in this library sit on two spellings).

    Decoys obey the round filters too, and that is a fairness fix rather than tidiness:
    in a 60s-only game, three decoys drawn from the whole library would be recognisably
    modern, so the answer is the one old-sounding option and the question is free. Same
    for a genre round. The filtered query is tried FIRST and falls back to the unfiltered
    one, because four options beat a failed round — a narrow filter might not hold enough
    distinct artists to fill the decoys.

    An EXCLUDED genre is not part of that trade. Widening is allowed to give up the theme
    (a modern decoy in a 60s round is only a bit of a giveaway) but never the exclusion: an
    excluded genre is "don't put this in front of the room tonight", and a decoy is read out
    loud like every other option. So `exclusions` is applied to the widened query too.
    """
    decade = (track["year"] or 0) // 10
    ffrag, fparams = filters or ("", [])
    xfrag, xparams = exclusions or ("", [])
    base = ("SELECT DISTINCT title, artist, year FROM tracks WHERE active=1 "
            "AND tier IS NOT NULL AND artist != ? AND title != ?")
    rows = []
    if ffrag:
        rows = conn.execute(f"{base}{ffrag} ORDER BY RANDOM() LIMIT 60",
                            (track["artist"], track["title"], *fparams)).fetchall()
    # too few DISTINCT ARTISTS in the filtered pool to fill the options — widen rather
    # than fail, since a round with two choices is worse than one with modern decoys
    if len({library.artist_key(r["artist"]) for r in rows}) <= n:
        rows = conn.execute(f"{base}{xfrag} ORDER BY RANDOM() LIMIT 60",
                            (track["artist"], track["title"], *xparams)).fetchall()
    same_decade = [r for r in rows if (r["year"] or 0) // 10 == decade]
    picked: list[dict] = []
    answer_title = (track["title"] or "").strip().lower()
    seen_artists = {library.artist_key(track["artist"])}
    for pool in (same_decade, rows):
        for r in pool:
            if len(picked) == n:
                break
            if library.artist_key(r["artist"]) in seen_artists:
                continue
            if (r["title"] or "").strip().lower() == answer_title:
                continue  # same song under a variant artist tag — a duplicate option
            picked.append({"title": r["title"], "artist": r["artist"]})
            seen_artists.add(library.artist_key(r["artist"]))
    if len(picked) < n:
        raise GameError("not enough tiered tracks for decoys")
    return picked


class Game:
    def __init__(self, conn, rounds: int = 10, tiers: list[str] | None = None,
                 clock=time.monotonic, genres: list[str] | None = None,
                 year_from: int | None = None, year_to: int | None = None,
                 exclude_genres: list[str] | None = None):
        self.tiers = tiers or ["easy", "medium"]
        self.n_rounds = rounds
        self.clock = clock
        # Round filters, held as the built fragment so every picker in this game applies
        # exactly the same one. Kept for the snapshot too, so the phones and the board can
        # say what kind of game this is ("Rock · the 90s") rather than looking identical.
        self.genres = list(genres) if genres else []
        self.exclude_genres = list(exclude_genres) if exclude_genres else []
        self.year_from, self.year_to = year_from, year_to
        self.filters = filter_sql(self.genres, year_from, year_to, self.exclude_genres)
        # Held separately as well, because pick_decoys is allowed to abandon self.filters to
        # fill four options and must still honour the exclusion when it does.
        self.exclusions = exclusion_sql(self.exclude_genres)
        self.rounds: list[dict] = []  # built lazily at first start_round, after artist picks
        # fail fast if the pool can't even fill a plain game
        pick_tracks(conn, rounds, self.tiers, filters=self.filters)
        self.players: dict[str, dict] = {}  # name -> {score, correct, fastest_ms, artists}
        self.current = -1
        self.host: str | None = None  # the player who started the game — runs the rounds
        self.phase = "lobby"  # lobby | question | reveal | break | finished
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.revealed_at: float | None = None  # clock() at reveal — gates "next" behind the payoff
        # half-time trivia (populated by start_break)
        self.break_facts: dict[str, str] = {}  # player -> fact to read aloud
        self.tf_qs: list[dict] = []
        self.tf_index = -1  # -1 = facts stage, else current T/F question

    def set_artists(self, name: str, artists: list[str]) -> None:
        if self.phase != "lobby":
            raise GameError("artists can only be picked in the lobby")
        name = self.resolve_name(name)
        if name not in self.players:
            raise GameError("join first")
        self.players[name]["artists"] = [a for a in artists if isinstance(a, str)][:3]
        self.players[name]["ready"] = True

    def set_ready(self, name: str) -> None:
        name = self.resolve_name(name)
        if name not in self.players:
            raise GameError("join first")
        self.players[name]["ready"] = True

    def waiting_on(self) -> list[str]:
        return [n for n, p in self.players.items() if not p.get("ready")]

    def _mk_round(self, conn, t: dict) -> dict:
        options = (pick_decoys(conn, t, filters=self.filters, exclusions=self.exclusions)
                   + [{"title": t["title"], "artist": t["artist"]}])
        random.shuffle(options)
        return {
            "track": t,
            "options": options,
            "correct": next(i for i, o in enumerate(options)
                            if o["title"] == t["title"] and o["artist"] == t["artist"]),
            "answers": {},        # player -> {choice, elapsed_ms, points}
            "audio_started": {},  # remote player -> clock() their own clip began
            "started_at": None,   # clock() when the clip started
            "deadline_at": None,  # clock() when the answer window shuts (moves on extend)
            "clip_len": 5,
        }

    def build_rounds(self, conn) -> None:
        """One boost round per player from their chosen artists, rest from the pool."""
        if self.rounds:
            return
        picked: list[dict] = []
        ids: set = set()
        for p in self.players.values():
            if len(picked) >= self.n_rounds - 1:
                break  # keep at least one neutral round
            t = pick_artist_track(conn, p.get("artists") or [], ids, filters=self.filters)
            if t:
                picked.append(t)
                ids.add(t["id"])
        picked += pick_tracks(conn, self.n_rounds - len(picked), self.tiers, exclude=ids,
                              filters=self.filters)
        random.shuffle(picked)  # boost rounds indistinguishable
        self.rounds = [self._mk_round(conn, t) for t in picked]

    # -- lobby ---------------------------------------------------------------
    def resolve_name(self, name: str) -> str:
        """The single key this player is stored under, whatever spelling arrived.

        Every entry point that takes a name runs it through here, so a phone that
        joined as `robin` is not a stranger to `answer` or `set_artists` when the
        lobby holds `Robin` — those membership checks would raise "join first"
        mid-game. An unknown name comes back in display form, so a genuine
        stranger still fails the checks that follow.

        The case-insensitive scan is not redundant with display_name's Title Case,
        it is what makes it safe to refine: if display_name ever stops being a
        total case fold (to keep `McDonald`), two spellings must still land on one
        player rather than quietly opening a second slot and a second score.
        """
        want = display_name(name)
        for existing in self.players:
            if existing.casefold() == want.casefold():
                return existing
        return want

    def join(self, name: str, remote: bool = False) -> None:
        """Take, or retake, a seat.

        A case-variant of a name already in the lobby is the SAME player: joining
        as `robin` while `Robin` is playing hands back Robin's slot and score
        instead of opening a second one. Two slots would also put two spellings
        into `results` for one game — the primary key there is (game_id, player),
        so it would happily take both (db.py).
        """
        name = self.resolve_name(name)
        if not name:
            raise GameError("empty name")
        fresh = name not in self.players
        self.players.setdefault(name, {"score": 0, "correct": 0, "fastest_ms": None,
                                       "artists": [], "ready": False, "remote": False})
        if fresh:
            # only on a first join: a reconnect re-sends `join`, and that must not
            # silently flip a player back to local mid-game
            self.players[name]["remote"] = bool(remote)

    def set_remote(self, name: str, remote: bool) -> None:
        """In the room or not. Changeable any time — someone can wander off with
        their phone mid-game, or turn up in person after joining from the car."""
        name = self.resolve_name(name)
        if name not in self.players:
            raise GameError("join first")
        self.players[name]["remote"] = bool(remote)

    def everyone_remote(self) -> bool:
        """True when there IS at least one player and every one of them is remote.

        Nobody is in the room, so playing to the house speaker is just noise in an
        empty room — and worse than noise if the speaker is somewhere someone is
        asleep. Each remote phone plays its own copy of the clip, so the room
        audio adds nothing.

        Empty is False on purpose: a lobby with no players yet is not "everyone
        remote", and the speaker should still be used for a game that hasn't been
        joined (the host may be about to walk in).
        """
        return bool(self.players) and all(p.get("remote") for p in self.players.values())

    # -- rounds --------------------------------------------------------------
    def start_round(self, conn=None) -> dict:
        if self.phase not in ("lobby", "reveal", "break"):
            raise GameError(f"cannot start a round from {self.phase}")
        if not self.players:
            raise GameError("no players")
        if self.current + 1 >= len(self.rounds):
            raise GameError("no rounds left")
        self.current += 1
        rnd = self.rounds[self.current]
        rnd["started_at"] = self.clock()
        rnd["deadline_at"] = rnd["started_at"] + ANSWER_WINDOW_S
        self.phase = "question"
        # Stamp the play HERE, not in finish(): `abort` throws the game away without ever
        # calling finish (main.py, the 'abort' branch), and an abandoned game's questions
        # were still asked out loud. Recording at start is also the only point that knows
        # a round really happened — self.rounds is built for the whole game up front.
        # conn is optional so the engine stays usable without a DB in tests.
        if conn is not None:
            self.note_played(conn, rnd["track"]["id"])
        return rnd

    def note_played(self, conn, track_id: str) -> None:
        """Remember that a track was asked, for the freshness preference in pick_tracks.

        Never fatal: a failed write here would abort a round that is already playing over
        the speaker, and the cost of losing the row is one possible repeat weeks later.
        """
        try:
            conn.execute("INSERT INTO plays(track_id, played_at) VALUES(?,?)",
                         (track_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        except Exception:  # noqa: BLE001 — a history row is never worth killing a round for
            LOGGER.warning("could not record play of %s", track_id, exc_info=True)

    def extend_clip(self) -> int:
        """Bump the current round to the next clip length (5 -> 10 -> 20).

        The clip replays from the start, so the answer window moves out with
        it — long enough to hear the whole clip plus thinking time. Without
        this, extending to 20s near the deadline cut the clip off mid-play.
        """
        rnd = self._round("question")
        # one extend at a time: the longer clip must play out in full before
        # another press can bump again — a second player mashing the button
        # right behind the first was jumping 5->20 unheard (#27)
        if self.clock() < rnd.get("extend_locked_until", 0):
            raise GameError(f"the {rnd['clip_len']}s clip is still playing — extend again when it finishes")
        for length in (10, 20):
            if rnd["clip_len"] < length:
                rnd["clip_len"] = length
                rnd["extend_locked_until"] = self.clock() + length
                rnd["deadline_at"] = self.clock() + max(ANSWER_WINDOW_S, length + 10)
                return length
        raise GameError("already at the longest clip")

    def window_left(self) -> float:
        """Seconds until the current round's answer window shuts."""
        rnd = self._round("question")
        return max(0.0, rnd["deadline_at"] - self.clock())

    def note_audio_started(self, name: str) -> None:
        """A remote phone reporting that its copy of the clip just began playing.

        Only the first report per round counts, so a replay (or a duplicated
        message) can't push the baseline out and buy a bigger speed bonus.
        """
        if self.phase != "question" or self.current < 0:
            return
        name = self.resolve_name(name)
        if not self.players.get(name, {}).get("remote"):
            return  # locals hear the room; their baseline is the room clock
        rnd = self.rounds[self.current]
        rnd["audio_started"].setdefault(name, self.clock())

    def _speed_baseline(self, name: str, rnd: dict) -> float:
        """When this player's clock started ticking for the speed bonus.

        The room start for everyone in the room; for a remote player, when their
        own stream began — but never more than REMOTE_LATENCY_CREDIT_MAX_S of
        credit, since that timestamp comes from their phone.
        """
        started = rnd["started_at"]
        if not self.players.get(name, {}).get("remote"):
            return started
        own = rnd["audio_started"].get(name)
        if own is None:
            return started  # never reported — no credit, and no crash
        return min(own, started + REMOTE_LATENCY_CREDIT_MAX_S)

    def answer(self, name: str, choice: int) -> dict:
        rnd = self._round("question")
        name = self.resolve_name(name)
        if name not in self.players:
            raise GameError("join first")
        if name in rnd["answers"]:
            raise GameError("already answered")
        # the window still shuts on the ROOM clock — a remote player gets the
        # bonus measured fairly, not a longer round than everyone else
        elapsed = max(0.0, self.clock() - self._speed_baseline(name, rnd))
        if self.clock() > rnd["deadline_at"]:
            raise GameError("too late")
        points = 0
        if choice == rnd["correct"]:
            points = BASE_POINTS + int(SPEED_BONUS_MAX * max(0, 1 - elapsed / ANSWER_WINDOW_S))
            p = self.players[name]
            p["score"] += points
            p["correct"] += 1
            ms = int(elapsed * 1000)
            if p["fastest_ms"] is None or ms < p["fastest_ms"]:
                p["fastest_ms"] = ms
        rnd["answers"][name] = {"choice": choice, "elapsed_ms": int(elapsed * 1000),
                                "points": points}
        return rnd["answers"][name]

    def all_answered(self) -> bool:
        rnd = self._round("question")
        return set(rnd["answers"]) >= set(self.players)

    def flag_current(self, conn) -> str:
        """Ban the current round's track (bad clip — applause, silence, etc.)."""
        if self.current < 0:
            raise GameError("no round to flag")
        rnd = self.rounds[self.current]
        conn.execute("UPDATE tracks SET banned=1, ban_reason='flag' WHERE id=?", (rnd["track"]["id"],))
        conn.commit()
        rnd["flagged"] = True
        return rnd["track"]["id"]

    def reveal(self) -> dict:
        rnd = self._round("question")
        self.phase = "reveal"
        self.revealed_at = self.clock()
        return rnd

    def payoff_wait(self) -> float:
        """Seconds until 'next' is allowed — a brief grace, not the whole song.

        The host may cut the payoff short; see PAYOFF_GRACE_S. Use
        payoff_left() for how much song is actually still playing.
        """
        if self.phase != "reveal" or self.revealed_at is None:
            return 0.0
        return max(0.0, PAYOFF_GRACE_S - (self.clock() - self.revealed_at))

    def payoff_left(self) -> float:
        """Seconds of payoff clip still to play — display only, gates nothing."""
        if self.phase != "reveal" or self.revealed_at is None:
            return 0.0
        return max(0.0, PAYOFF_S - (self.clock() - self.revealed_at))

    # -- half time -------------------------------------------------------------
    def start_break(self, conn) -> None:
        """Half-time show: a fact per player to read aloud, then T/F questions."""
        if self.phase != "reveal":
            raise GameError("half time only follows a reveal")
        facts = trivia.pick(conn, "fact", len(self.players))
        names = list(self.players)
        random.shuffle(names)
        self.break_facts = {n: f["text"] for n, f in zip(names, facts)}
        self.tf_qs = [{"text": q["text"], "answer": bool(q["answer"]),
                       "answers": {}, "revealed": False}
                      for q in trivia.pick(conn, "tf", TF_COUNT) if q["answer"] is not None]
        self.tf_index = -1
        self.phase = "break"

    def _tf_current(self) -> dict:
        if self.phase != "break" or self.tf_index < 0:
            raise GameError("no true/false question live")
        return self.tf_qs[self.tf_index]

    def tf_answer(self, name: str, val: bool) -> None:
        q = self._tf_current()
        name = self.resolve_name(name)
        if name not in self.players:
            raise GameError("join first")
        if q["revealed"]:
            raise GameError("answer's already out")
        if name in q["answers"]:
            raise GameError("already answered")
        q["answers"][name] = {"choice": bool(val), "points": 0}

    def tf_all_answered(self) -> bool:
        return self.tf_index >= 0 and set(self._tf_current()["answers"]) >= set(self.players)

    def _tf_reveal(self) -> None:
        q = self._tf_current()
        q["revealed"] = True
        for n, a in q["answers"].items():
            if a["choice"] == q["answer"]:
                a["points"] = TF_POINTS
                self.players[n]["score"] += TF_POINTS

    def advance_break(self) -> str:
        """Host 'next' during the break: facts -> T/F -> reveal -> ... -> resume."""
        if self.phase != "break":
            raise GameError("not at half time")
        if self.tf_index < 0:
            if not self.tf_qs:
                return "resume"  # bank empty — plain snacks break
            self.tf_index = 0
            return "tf"
        q = self.tf_qs[self.tf_index]
        if not q["revealed"]:
            self._tf_reveal()
            return "tf_reveal"
        if self.tf_index + 1 < len(self.tf_qs):
            self.tf_index += 1
            return "tf"
        return "resume"

    def is_last_round(self) -> bool:
        return self.current + 1 >= len(self.rounds)

    def is_halfway(self) -> bool:
        return len(self.rounds) >= 6 and self.current + 1 == len(self.rounds) // 2

    def finish(self, conn) -> int:
        self.phase = "finished"
        cur = conn.execute("INSERT INTO games(started_at, finished_at, rounds) VALUES(?,?,?)",
                           (self.started_at, datetime.now(timezone.utc).isoformat(),
                            len(self.rounds)))
        game_id = cur.lastrowid
        for name, p in self.players.items():
            conn.execute(
                "INSERT INTO results(game_id, player, score, correct, fastest_ms) VALUES(?,?,?,?,?)",
                (game_id, name, p["score"], p["correct"], p["fastest_ms"]))
        conn.commit()
        return game_id

    # -- snapshots -----------------------------------------------------------
    def filter_label(self) -> str:
        """"Rock · Pop · the 1990s" — what kind of game this is, or "" for the whole library.

        Worth showing everywhere: a filtered game looks identical to a normal one, and a
        player who doesn't know the round is 60s-only reads their four modern-looking
        options as a bug.

        An exclusion is stated too ("no NotForKids"), not left implicit. It's the case most
        worth saying out loud: an exclusion-only game shows the WHOLE library minus a slice,
        so with the label silent the game is indistinguishable from an unfiltered one and
        nobody in the room can tell whether the exclusion was actually applied.
        """
        bits = list(self.genres)
        bits += [f"no {g}" for g in self.exclude_genres]
        lo, hi = self.year_from, self.year_to
        if lo is not None and hi is not None:
            # a single decade reads far better as "the 1990s" than "1990–1999"
            bits.append(f"the {lo}s" if hi == lo + 9 and lo % 10 == 0 else f"{lo}–{hi}")
        elif lo is not None:
            bits.append(f"{lo} onwards")
        elif hi is not None:
            bits.append(f"up to {hi}")
        return " · ".join(bits)

    def snapshot(self) -> dict:
        """State for clients. The correct answer only ships during reveal/finished."""
        s = {
            "phase": self.phase,
            "host": self.host,
            "round": self.current + 1,
            "total_rounds": len(self.rounds),
            "players": [{"name": n, "score": p["score"], "correct": p["correct"],
                         "fastest_ms": p["fastest_ms"], "picked_artists": bool(p.get("artists")),
                         "ready": bool(p.get("ready")), "remote": bool(p.get("remote"))}
                        for n, p in sorted(self.players.items(), key=lambda kv: -kv[1]["score"])],
        }
        if label := self.filter_label():
            # only present on a filtered game, so unfiltered UIs render exactly as before
            s["filter_label"] = label
        if self.current >= 0 and self.phase in ("question", "reveal"):
            rnd = self.rounds[self.current]
            s["options"] = [f'{o["title"]} — {o["artist"]}' for o in rnd["options"]]
            s["clip_len"] = rnd["clip_len"]
            s["replay"] = rnd.get("replay", 0)
            if self.phase == "question":
                s["window_left"] = round(self.window_left(), 1)
                s["extend_wait"] = round(max(0.0, rnd.get("extend_locked_until", 0) - self.clock()), 1)
            s["flagged"] = bool(rnd.get("flagged"))
            s["answered"] = sorted(rnd["answers"])
            if self.phase == "reveal":
                t = rnd["track"]
                s["correct"] = rnd["correct"]
                s["track"] = {"id": t["id"], "title": t["title"], "artist": t["artist"],
                              "album": t["album"], "year": t["year"]}
                s["round_answers"] = rnd["answers"]
                s["payoff_wait"] = round(self.payoff_wait(), 1)
                s["payoff_left"] = round(self.payoff_left(), 1)
        if self.phase == "break":
            s["break_stage"] = "facts" if self.tf_index < 0 else "tf"
            s["facts"] = self.break_facts  # phones show only their own; it's a kitchen
            if self.tf_index >= 0:
                q = self.tf_qs[self.tf_index]
                tf = {"num": self.tf_index + 1, "total": len(self.tf_qs), "text": q["text"],
                      "answered": sorted(q["answers"]), "revealed": q["revealed"],
                      "last": self.tf_index + 1 == len(self.tf_qs)}
                if q["revealed"]:  # the answer only ships once it's out
                    tf["answer"] = q["answer"]
                    tf["results"] = {n: a["points"] for n, a in q["answers"].items()}
                s["tf"] = tf
        return s

    def _round(self, want_phase: str) -> dict:
        if self.phase != want_phase or self.current < 0:
            raise GameError(f"not in {want_phase} phase")
        return self.rounds[self.current]


def all_time_leaderboard(conn, limit: int = 20) -> list[dict]:
    """One row per player across every game — case-folded, so one person is one row.

    `results.player` is plain TEXT and older rows can hold any spelling, so the
    grouping has to fold rather than trust what was stored. GROUP BY ... COLLATE
    NOCASE hands back an ARBITRARY member spelling for the SELECTed column, which
    would make the name on the board depend on which row SQLite happened to pick;
    display_name() over the top makes it deterministic instead.
    """
    return [dict(r, player=display_name(r["player"])) for r in conn.execute(
        "SELECT player, COUNT(*) games, SUM(score) total_score, SUM(correct) total_correct, "
        "MIN(fastest_ms) fastest_ms FROM results GROUP BY player COLLATE NOCASE "
        "ORDER BY total_score DESC LIMIT ?", (limit,))]
