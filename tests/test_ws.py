"""The websocket protocol.

Until the dispatch table landed this was a 260-line if/elif inside
`ws_endpoint`, reachable only through a live socket — which is why none of the
game-control rules (who may start a round, who may abandon a game, whether a
message between games kills the connection) had a single test. These call the
handlers directly against a throwaway Hub and a fake socket.

Nothing here touches `main.hub`: a module-level singleton shared between tests
is how one test's abandoned game becomes another's mystery failure.
"""
import asyncio
import os
import tempfile

import pytest

from app import db, game, main


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class FakeWS:
    """Records what the server sent it. `sent` is every payload, in order."""

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    def kinds(self):
        return [p.get("type") for p in self.sent]

    def errors(self):
        return [p["message"] for p in self.sent if p.get("type") == "error"]


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make_db(n_tracks=30):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    for i in range(n_tracks):
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,year,tier,clipped_at,global_listeners,active) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (f"t{i}", f"Song {i}", f"Artist {i}", "Album", 1990 + (i % 4) * 10,
             "easy" if i % 2 == 0 else "medium", "2026-07-06T00:00:00", 1000 + i))
    conn.commit()
    return conn, path


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    """Stop the handlers reaching for real speakers, TVs, disks and executors.

    Every one of these is a network call in production. `run_in_executor` is
    stubbed to a no-op rather than allowed through, so a test never waits on
    a cast attempt that will time out.
    """
    # main.db IS app.db — capture the real connect before shadowing it, or the
    # replacement calls itself forever.
    real_connect = db.connect
    monkeypatch.setattr(main.db, "connect",
                        lambda *a, **k: real_connect(str(tmp_path / "ws.db")))
    monkeypatch.setattr(main.board_cast, "display_names", lambda: ["TV"])
    monkeypatch.setattr(main.board_cast, "hide_board", lambda *a, **k: None)
    monkeypatch.setattr(main.board_cast, "show_board", lambda *a, **k: None)
    monkeypatch.setattr(main.ha, "available", lambda: False)
    monkeypatch.setattr(main.ha, "house_is_sleeping", lambda: False)
    monkeypatch.setattr(main.ha, "play_clip", lambda *a, **k: None)
    monkeypatch.setattr(main.ha, "play_url", lambda *a, **k: None)

    class Loop:
        def run_in_executor(self, ex, fn, *a):
            return None

        def call_later(self, *a, **k):
            return None
    monkeypatch.setattr(main.asyncio, "get_event_loop", lambda: Loop())


@pytest.fixture
def hub(quiet):
    """A fresh Hub with a running game and two players. Alice is master."""
    conn, path = make_db()
    h = main.Hub()
    h.display = "TV"
    g = game.Game(conn, rounds=2, tiers=["easy", "medium"], clock=Clock())
    g.join("Alice")
    g.join("Bob")
    g.host = "Alice"
    g.build_rounds(conn)
    h.game = g
    try:
        yield h
    finally:
        conn.close()
        os.unlink(path)


def session(hub, name=None):
    s = main.WSSession(FakeWS(), hub)
    s.name = name
    return s


def send(s, **msg):
    """One message through the real dispatch path, lock and all."""
    asyncio.run(main.dispatch(s, msg))


# --------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------

def test_every_kind_the_clients_send_has_a_handler():
    """The phone and board speak these; a missing entry is a dead button.

    Hard-coded rather than derived from HANDLERS, or the assertion is just
    "the dict equals itself" and a deleted handler passes.
    """
    spoken = {
        "ping", "join", "set_remote", "audio_started", "set_artists", "ready",
        "start_round", "extend_clip", "answer", "flag_clip", "abort",
        "tf_answer", "next", "set_display", "stop_board", "board_hello",
    }
    assert spoken <= set(main.HANDLERS), f"unhandled: {spoken - set(main.HANDLERS)}"


def test_an_unknown_kind_is_ignored_not_an_error(hub):
    """Phones cache JS across deploys, so a retired message type outliving its
    handler is normal traffic — it must not error or drop the socket."""
    s = session(hub, "Alice")
    send(s, type="cast_a_spell")
    assert s.ws.sent == [], f"replied to an unknown kind: {s.ws.sent}"


def test_a_message_between_games_is_a_game_error_not_a_dropped_socket(quiet):
    """This is the bug the if/elif chain had: `answer` with no game ran
    `hub.game.answer(...)` on None, and the AttributeError escaped the handler
    to kill the connection. The phone got no error and no state — it just went
    dead. Every handler that dereferences hub.game must be guarded."""
    h = main.Hub()
    h.game = None
    for kind in sorted(main.NEEDS_GAME):
        s = session(h, "Alice")
        send(s, type=kind, name="Alice", choice=0, answer=True)
        assert s.ws.errors() == ["no game — start one first"], \
            f"{kind} with no game: {s.ws.sent}"


def test_the_guarded_set_is_actually_the_handlers_that_need_a_game(quiet):
    """A handler added later that touches hub.game but forgets requires_game
    would crash the socket exactly as before. Prove the guard set is right by
    running EVERY handler against a gameless hub: anything not in NEEDS_GAME
    must survive it."""
    for kind in sorted(set(main.HANDLERS) - main.NEEDS_GAME):
        h = main.Hub()
        h.game = None
        h.display = "TV"
        s = session(h)
        send(s, type=kind, display="none")   # must not raise
        assert "no game — start one first" not in s.ws.errors()


# --------------------------------------------------------------------------
# activity accounting (#26, #50)
# --------------------------------------------------------------------------

def test_a_ping_does_not_keep_a_stale_game_alive(hub):
    """#50: a phone left on the page pings forever. If that counted as
    activity the stale-game reaper could never fire."""
    hub.last_activity = 0.0
    send(session(hub, "Alice"), type="ping")
    assert hub.last_activity == 0.0, "ping bumped the stale-game clock"


def test_ping_still_pongs(hub):
    s = session(hub, "Alice")
    send(s, type="ping")
    assert s.ws.kinds() == ["pong"]


def test_the_display_picker_is_not_activity_either(hub):
    """set_display/board_hello are plumbing — someone fiddling with the TV
    dropdown between games isn't a game in progress."""
    for kind in ("set_display", "board_hello"):
        hub.last_activity = 0.0
        send(session(hub), type=kind, display="none")
        assert hub.last_activity == 0.0, f"{kind} bumped the stale-game clock"


def test_playing_the_game_is_activity_and_resets_the_recast_budget(hub):
    """The other half of #26: real play resets the watchdog's cast attempts,
    so an outage during a game gets a fresh budget of retries."""
    hub.last_activity = 0.0
    hub.cast_attempts = 5
    send(session(hub, "Alice"), type="ready")
    assert hub.last_activity > 0.0
    assert hub.cast_attempts == 0


# --------------------------------------------------------------------------
# who is allowed to do what
# --------------------------------------------------------------------------

def test_only_the_master_starts_rounds(hub):
    s = session(hub, "Bob")
    send(s, type="start_round")
    assert s.ws.errors() == ["only Alice controls the rounds"]
    assert hub.game.phase == "lobby", "Bob started the round anyway"


def test_a_nonplaying_master_loses_the_wheel_to_whoever_presses(hub):
    """The master rotates to whoever has waited longest — who may not be in
    this game at all. If a crowned absentee kept the controls the game would
    be unstartable from every phone in the room."""
    hub.game.host = "Ghost"          # rotated to someone who never joined
    send(session(hub, "Alice"), type="ready")
    send(session(hub, "Bob"), type="ready")
    s = session(hub, "Bob")
    send(s, type="start_round")
    assert s.ws.errors() == [], f"Bob was refused: {s.ws.errors()}"
    assert hub.game.host == "Bob"
    assert hub.game.phase == "question"


def test_a_playing_master_keeps_the_wheel(hub):
    """The take-over above must not become "anyone can grab the controls" —
    Alice IS playing, so Bob is refused."""
    hub.game.phase = "lobby"
    s = session(hub, "Bob")
    send(s, type="start_round")
    assert s.ws.errors() == ["only Alice controls the rounds"]


def test_a_round_will_not_start_until_everyone_is_ready(hub):
    s = session(hub, "Alice")
    send(s, type="start_round")
    assert s.ws.errors() == ["not everyone is ready: Alice, Bob"]
    assert hub.game.phase == "lobby"


def test_ready_players_can_start(hub):
    send(session(hub, "Alice"), type="ready")
    send(session(hub, "Bob"), type="ready")
    s = session(hub, "Alice")
    send(s, type="start_round")
    assert s.ws.errors() == []
    assert hub.game.phase == "question"


def test_only_the_master_flags_a_clip(hub):
    s = session(hub, "Bob")
    send(s, type="flag_clip")
    assert s.ws.errors() == ["only Alice can flag clips"]


def test_with_no_master_crowned_anyone_may_drive(hub):
    """A board that pressed Start without joining leaves `host` as None — it
    has no name to be crowned with. Refusing every socket then would leave the
    game with no way to advance at all, so an uncrowned game is open to all.
    """
    hub.game.host = None
    hub.game.start_round()
    # Bob, not the nameless board: with host None a nameless socket compares
    # None != None and is allowed however the check is written, so it can't
    # tell a working guard from a broken one.
    bob = session(hub, "Bob")
    send(bob, type="flag_clip")
    assert bob.ws.errors() == [], f"an uncrowned game refused Bob: {bob.ws.errors()}"
    assert hub.game.phase == "reveal"

    hub.game.clock.t += 60
    s = session(hub, "Bob")
    send(s, type="next")
    assert s.ws.errors() == []


def test_only_a_present_master_owns_the_abandon(hub):
    """#46: Alice is here, so Bob may not abandon her game."""
    s = session(hub, "Bob")
    send(s, type="abort")
    assert s.ws.errors() == ["only Alice can abandon the game"]
    assert hub.game is not None, "the game was abandoned anyway"


def test_an_orphaned_game_can_be_abandoned_by_anyone(hub):
    """#46, the other way: if the crowned master isn't in the game, refusing
    everyone leaves it stuck unabandonable from every phone."""
    hub.game.host = "Ghost"
    s = session(hub, "Bob")
    send(s, type="abort")
    assert s.ws.errors() == []
    assert hub.game is None


def test_a_second_game_cannot_start_over_a_live_one(hub):
    hub.game.phase = "question"
    s = session(hub, "Alice")
    send(s, type="new_game")
    assert s.ws.errors() == ["a game is already running"]


def test_new_game_carries_the_round_filters_including_the_exclusion(hub):
    """The handler is the seam the picker's choices cross. `genres` was already threaded;
    `exclude_genres` reaching Game is what makes "everything except this tag" more than a
    checkbox — a dropped key here plays the excluded genre with the UI insisting otherwise.
    """
    # the handler opens its OWN connection (pinned by `quiet`), so the pool has to exist
    # there rather than in the fixture's game db
    seeded = main.db.connect()
    for i in range(12):
        seeded.execute(
            "INSERT INTO tracks(id,title,artist,album,genre,year,duration,tier,clipped_at,"
            "global_listeners,active) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (f"x{i}", f"Song {i}", f"Rock Band {i}", "Album", "Rock", 1995, 200, "easy",
             "2026-07-06T00:00:00", 5000))
    seeded.commit()
    seeded.close()

    hub.game.phase = "finished"
    s = session(hub, "Alice")
    send(s, type="new_game", genres=["Rock"], exclude_genres=["NotForKids"],
         year_from=1990, year_to=1999)
    assert s.ws.errors() == [], s.ws.errors()
    assert hub.game.exclude_genres == ["NotForKids"]
    assert "no NotForKids" in hub.game.filter_label(), \
        "every phone and the board must say what was left out"
    assert "IS NULL" in hub.game.filters[0], \
        "an untagged track was never named for exclusion — see game.exclusion_sql"
    # ...and a plain new_game stays unfiltered rather than picking up an empty exclusion
    hub.game.phase = "finished"
    send(session(hub, "Alice"), type="new_game")
    assert hub.game.exclude_genres == []
    assert hub.game.filter_label() == ""


def _seed_pool(n=30, tier="easy"):
    """Fill the handler's OWN db (pinned by `quiet`) so new_game has a pool to preflight.

    The `hub` fixture's game lives in a separate temp db, and on_new_game calls db.connect()
    for itself — without this every new_game fails on the pool check instead of on the rule
    under test.
    """
    seeded = main.db.connect()
    for i in range(n):
        seeded.execute(
            "INSERT INTO tracks(id,title,artist,album,year,duration,tier,clipped_at,"
            "global_listeners,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (f"n{i}", f"Song {i}", f"Band {i}", "Album", 1995, 200, tier,
             "2026-07-06T00:00:00", 5000))
    seeded.commit()
    seeded.close()


def test_new_game_builds_the_number_of_rounds_the_master_picked(hub):
    """The count crosses the socket as a plain payload key, and it was hardcoded at 10 on the
    far side. A handler that dropped it would build ten rounds and look entirely healthy."""
    _seed_pool()
    hub.game.phase = "finished"
    s = session(hub, "Alice")
    send(s, type="new_game", rounds=5)
    assert s.ws.errors() == [], s.ws.errors()
    assert hub.game.n_rounds == 5
    hub.game.join("Alice")
    conn = main.db.connect()
    try:
        hub.game.build_rounds(conn)
    finally:
        conn.close()
    assert len(hub.game.rounds) == 5
    assert hub.game.snapshot()["total_rounds"] == 5
    # ...and no count at all is still the ten-round game every existing phone asks for
    hub.game.phase = "finished"
    send(session(hub, "Alice"), type="new_game")
    assert hub.game.n_rounds == game.DEFAULT_ROUNDS


def test_a_junk_round_count_is_refused_and_leaves_no_game_started(hub):
    """The count is a number off a phone, so it's checked rather than trusted. Unclamped, 0
    reached Game() as a game that finishes on the first tap, 51 asked the picker for rounds
    the library cannot fill, and a non-numeric value raised ValueError straight out of the
    handler — which kills the socket instead of telling the phone why.

    Each case must also leave the finished game in place: a rejected new_game that had already
    replaced hub.game would strand the room with a half-built one.
    """
    _seed_pool()
    for bad, want in ((0, "between"), (51, "between"), (game.MAX_ROUNDS + 1, "between"),
                      ("lots", "number"), (None, "number"), ([5], "number")):
        hub.game.phase = "finished"
        before = hub.game
        s = session(hub, "Alice")
        send(s, type="new_game", rounds=bad)
        assert s.ws.errors() and want in s.ws.errors()[0], f"rounds={bad!r}: {s.ws.sent}"
        assert hub.game is before, f"rounds={bad!r} started a game anyway"
    # the edges of the accepted range are not junk
    for good in (game.MIN_ROUNDS, game.DEFAULT_ROUNDS):
        hub.game.phase = "finished"
        s = session(hub, "Alice")
        send(s, type="new_game", rounds=good)
        assert s.ws.errors() == [], f"rounds={good} refused: {s.ws.errors()}"
        assert hub.game.n_rounds == good


def test_a_sleeping_house_refuses_a_new_game_unless_forced(hub, monkeypatch):
    monkeypatch.setattr(main.ha, "house_is_sleeping", lambda: True)
    hub.game.phase = "finished"
    s = session(hub, "Alice")
    send(s, type="new_game")
    assert s.ws.errors() == ["house is Sleeping — start from the board to override"]


def test_only_a_joined_nonmaster_is_refused_the_board_off(hub):
    """The board's own socket never joined, so it has no name — it must still
    be able to turn itself off (#31). Only a *player* who isn't the master is
    refused, which is why this can't just be require_host."""
    board = session(hub)                    # name is None: the board itself
    send(board, type="stop_board")
    assert board.ws.errors() == []
    assert hub.display is None

    hub.display = "TV"
    bob = session(hub, "Bob")
    send(bob, type="stop_board")
    assert bob.ws.errors() == ["only Alice can turn off the TV board"]
    assert hub.display == "TV", "Bob turned the board off anyway"


# --------------------------------------------------------------------------
# the name a socket acts as
# --------------------------------------------------------------------------

def test_join_claims_the_name_for_every_later_message(hub):
    """The reason a plain dict of functions wasn't enough: `join` WRITES the
    per-connection name, and every later handler reads it."""
    s = session(hub)
    assert s.name is None
    send(s, type="join", name="Carol")
    assert s.name == "Carol"
    assert "Carol" in hub.game.players


def test_a_joined_socket_cannot_act_as_someone_else(hub):
    """`who()` prefers the claimed name, so a phone that joined as Bob can't
    answer as Alice by putting her name in the payload."""
    hub.game.start_round()
    send(session(hub, "Bob"), type="answer", name="Alice", choice=0)
    answers = hub.game.rounds[hub.game.current]["answers"]
    assert "Bob" in answers, "the claimed name was ignored"
    assert "Alice" not in answers, "Bob answered as Alice"


def test_the_board_may_act_on_a_named_players_behalf(hub):
    """A socket with no claimed name falls back to the payload — that's how
    the board submits for a player."""
    hub.game.start_round()
    send(session(hub), type="answer", name="Alice", choice=0)
    assert "Alice" in hub.game.rounds[hub.game.current]["answers"]


def test_a_rejected_join_still_claims_the_socket(hub):
    """A name that the game refuses (duplicate, blank) must still stick to
    this socket. If it didn't, the phone's next message would arrive nameless
    and get attributed to whoever the payload said."""
    s = session(hub)
    send(s, type="join", name="")
    assert s.name == "", "a failed join left the socket nameless"


def test_a_case_variant_join_claims_the_socket_under_the_name_the_game_seated(hub):
    """Players are folded by case, so joining as `alice` seats you as Alice. The
    socket has to claim THAT spelling: every host and abandon check is an equality
    test against g.host, so a master whose phone autocorrected their own name would
    be handed their own seat and then refused control of their own rounds."""
    s = session(hub)
    send(s, type="join", name="alice")
    assert s.name == "Alice"
    assert list(hub.game.players) == ["Alice", "Bob"], "a second Alice joined"
    for who in hub.game.players.values():   # clear the lobby's everyone-ready gate
        who["ready"] = True
    send(s, type="start_round")
    assert s.ws.errors() == [], "the master was locked out of their own game"
    assert hub.game.phase == "question"


# --------------------------------------------------------------------------
# round flow
# --------------------------------------------------------------------------

def test_next_will_not_skip_an_unread_reveal(hub):
    hub.game.start_round()
    hub.game.answer("Alice", hub.game.rounds[hub.game.current]["correct"])
    hub.game.answer("Bob", 0)
    hub.game.reveal()
    s = session(hub, "Alice")
    send(s, type="next")
    assert s.ws.errors() and "hold on" in s.ws.errors()[0]


def test_next_advances_once_the_payoff_has_been_seen(hub):
    hub.game.start_round()
    hub.game.reveal()
    hub.game.clock.t += 60          # long enough that payoff_wait is done
    s = session(hub, "Alice")
    send(s, type="next")
    assert s.ws.errors() == []
    assert hub.game.current == 1 and hub.game.phase == "question"


def test_an_answer_from_everyone_reveals_early(hub):
    hub.game.start_round()
    send(session(hub, "Alice"), type="answer", choice=0)
    assert hub.game.phase == "question", "revealed before Bob answered"
    send(session(hub, "Bob"), type="answer", choice=0)
    assert hub.game.phase == "reveal"


def test_extend_clip_moves_the_deadline_out_with_the_longer_clip(hub):
    """A longer clip with the old deadline reveals the answer while the music
    is still playing."""
    hub.game.start_round()
    before = hub.game.rounds[hub.game.current]["clip_len"]
    s = session(hub, "Alice")

    # capture the rescheduled deadline instead of letting it run: a real task
    # would sit sleeping out the answer window for the length of the test
    started = []
    orig = main.asyncio.create_task
    main.asyncio.create_task = lambda coro: (started.append(coro), coro.close())[0]
    try:
        send(s, type="extend_clip")
    finally:
        main.asyncio.create_task = orig
    assert s.ws.errors() == []
    assert hub.game.rounds[hub.game.current]["clip_len"] > before
    assert started, "the clip got longer but the reveal deadline did not move"


def test_a_flagged_clip_ends_the_round_immediately(hub):
    """A clip that's wrong isn't worth guessing — flagging mid-question must
    reveal rather than leave the table listening to a broken cut."""
    hub.game.start_round()
    send(session(hub, "Alice"), type="flag_clip")
    assert hub.game.phase == "reveal"


# --------------------------------------------------------------------------
# remote players
# --------------------------------------------------------------------------

def test_join_carries_the_remote_flag(hub):
    s = session(hub)
    send(s, type="join", name="Carol", remote=True)
    assert hub.game.players["Carol"]["remote"] is True


def test_a_player_can_move_between_the_room_and_remote(hub):
    send(session(hub, "Bob"), type="set_remote", remote=True)
    assert hub.game.players["Bob"]["remote"] is True
    send(session(hub, "Bob"), type="set_remote", remote=False)
    assert hub.game.players["Bob"]["remote"] is False


def test_audio_started_is_silent(hub):
    """It arrives once per remote player per round and changes nothing anyone
    else can see. Broadcasting a full state snapshot for it would put an extra
    fan-out on every round for no visible effect."""
    hub.game.start_round()
    send(session(hub, "Bob"), type="set_remote", remote=True)
    s = session(hub, "Bob")
    # REGISTERED, or nothing reaches it and the silence assertion is vacuous —
    # broadcast() fans out over hub.sockets, not over the sending session.
    hub.sockets = [s.ws]
    send(s, type="audio_started")
    assert s.ws.sent == [], f"audio_started broadcast: {s.ws.sent}"
    # recorded per ROUND, not per player: the baseline is only meaningful for
    # the clip currently playing
    assert "Bob" in hub.game.rounds[hub.game.current]["audio_started"]


# --------------------------------------------------------------------------
# broadcast contract
# --------------------------------------------------------------------------

def test_a_state_broadcast_reaches_every_socket_not_just_the_sender(hub):
    a, b = FakeWS(), FakeWS()
    hub.sockets = [a, b]
    send(session(hub, "Alice"), type="ready")
    assert a.kinds() == ["state"] and b.kinds() == ["state"]


def test_a_dead_socket_is_dropped_and_does_not_block_the_others(hub):
    """One phone that has gone away must not stop the rest of the room
    getting the new state."""
    class Dead(FakeWS):
        async def send_json(self, payload):
            raise RuntimeError("gone")

    dead, live = Dead(), FakeWS()
    hub.sockets = [dead, live]
    send(session(hub, "Alice"), type="ready")
    assert live.kinds() == ["state"]
    assert dead not in hub.sockets
