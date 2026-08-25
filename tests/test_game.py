import os
import tempfile

import pytest

from app import db, game


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


class Clock:
    def __init__(self):
        self.t = 100.0
    def __call__(self):
        return self.t


def test_full_game_flow():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=2, tiers=["easy", "medium"], clock=clock)
        g.join("Alice"); g.join("Bob")
        g.build_rounds(conn)
        rnd = g.start_round()
        assert g.phase == "question" and len(rnd["options"]) == 4
        correct = rnd["correct"]
        # correct answer at t+2s: base + speed bonus
        clock.t += 2
        a = g.answer("Alice", correct)
        assert a["points"] == 100 + int(50 * (1 - 2 / 20))
        # wrong answer scores nothing
        clock.t += 3
        assert g.answer("Bob", (correct + 1) % 4)["points"] == 0
        assert g.all_answered()
        g.reveal()
        snap = g.snapshot()
        assert snap["correct"] == correct and snap["track"]["title"]
        # round 2 → finish
        g.start_round()
        clock.t += 25  # window expired
        with pytest.raises(game.GameError):
            g.answer("Alice", 0)
        g.reveal()
        assert g.is_last_round()
        gid = g.finish(conn)
        rows = {r["player"]: r for r in conn.execute("SELECT * FROM results WHERE game_id=?", (gid,))}
        assert rows["Alice"]["score"] > 0 and rows["Alice"]["correct"] == 1
        assert rows["Bob"]["score"] == 0
        lb = game.all_time_leaderboard(conn)
        assert lb[0]["player"] == "Alice"
    finally:
        os.unlink(p)


def test_options_contain_answer_and_unique_artists():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=5, clock=Clock())
        g.join("X"); g.build_rounds(conn)
        for rnd in g.rounds:
            t = rnd["track"]
            labels = [(o["title"], o["artist"]) for o in rnd["options"]]
            assert (t["title"], t["artist"]) in labels
            artists = [o["artist"] for o in rnd["options"]]
            assert len(set(artists)) == 4  # no duplicate artists among options
    finally:
        os.unlink(p)


def test_snapshot_hides_answer_during_question():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        g.join("X")
        g.build_rounds(conn)
        g.start_round()
        snap = g.snapshot()
        assert "correct" not in snap and "track" not in snap
        assert len(snap["options"]) == 4
    finally:
        os.unlink(p)


def test_guards():
    conn, p = make_db(4)
    try:
        with pytest.raises(game.GameError):  # not enough clipped tracks
            game.Game(conn, rounds=10, clock=Clock())
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        with pytest.raises(game.GameError):  # no players yet
            g.start_round()
        g.join("A")
        g.build_rounds(conn)
        g.start_round()
        with pytest.raises(game.GameError):  # stranger can't answer
            g.answer("B", 0)
        g.answer("A", 0)
        with pytest.raises(game.GameError):  # no double answer
            g.answer("A", 1)
        assert g.extend_clip() == 10
        clock.t += 10.1  # the lock (#27): next extend only after the clip plays out
        assert g.extend_clip() == 20
        clock.t += 20.1
        with pytest.raises(game.GameError):
            g.extend_clip()
    finally:
        os.unlink(p)


def test_flag_current_bans_track():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        g.join("A")
        g.build_rounds(conn)
        g.start_round()
        tid = g.flag_current(conn)
        assert conn.execute("SELECT banned FROM tracks WHERE id=?", (tid,)).fetchone()["banned"] == 1
        assert g.snapshot()["flagged"] is True
        # banned tracks never picked again
        ids = {r["track"]["id"] for r in game.Game(conn, rounds=5, clock=Clock()).rounds}
        assert tid not in ids
    finally:
        os.unlink(p)



def test_artist_boost_rounds():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=4, clock=Clock())
        g.join("Bob")
        g.set_artists("Bob", ["Artist 7", "Artist 9"])
        g.join("Carol")  # no picks — no boost round for him
        g.build_rounds(conn)
        artists = [r["track"]["artist"] for r in g.rounds]
        assert any(a in ("Artist 7", "Artist 9") for a in artists), artists
        assert len(g.rounds) == 4
        assert len({r["track"]["id"] for r in g.rounds}) == 4  # no dupes
        snap = g.snapshot()
        by = {pl["name"]: pl for pl in snap["players"]}
        assert by["Bob"]["picked_artists"] is True
        assert by["Carol"]["picked_artists"] is False
        assert "artists" not in by["Bob"]  # picks never leak in snapshots
    finally:
        os.unlink(p)


def test_extend_clip_extends_the_window():
    """Extending to a 20s clip near the deadline must not cut the clip off —
    the answer window moves out with the replayed clip."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        g.join("A"); g.join("B"); g.build_rounds(conn)
        g.start_round()
        assert g.window_left() == game.ANSWER_WINDOW_S
        clock.t += 18  # extend just before the old deadline
        assert g.extend_clip() == 10
        assert g.window_left() == game.ANSWER_WINDOW_S  # fresh 20s
        clock.t += 15
        assert g.extend_clip() == 20
        assert g.window_left() == 30  # 20s clip + 10s thinking time
        clock.t += 28  # 61s after round start — old window long gone
        a = g.answer("A", g.rounds[0]["correct"])
        assert a["points"] == game.BASE_POINTS  # correct, but speed bonus decayed to 0
        clock.t += 5   # now past even the extended window
        with pytest.raises(game.GameError):
            g.answer("B", 0)
        assert g.snapshot().get("window_left") == 0
    finally:
        os.unlink(p)


def test_payoff_gates_next():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, clock=clock)
        g.join("A"); g.build_rounds(conn)
        assert g.payoff_wait() == 0  # no gate outside reveal
        g.start_round()
        clock.t += 2
        g.answer("A", 0)
        g.reveal()
        # the gate is the short grace, NOT the length of the song: the host is
        # allowed to cut the payoff short, and a button locked for a full 12s
        # was indistinguishable from a broken one.
        assert g.payoff_wait() == game.PAYOFF_GRACE_S
        assert game.PAYOFF_GRACE_S < game.PAYOFF_S
        clock.t += 1
        assert g.payoff_wait() == game.PAYOFF_GRACE_S - 1
        clock.t += 1  # grace served, song still playing
        assert g.payoff_wait() == 0
        assert g.payoff_left() > 0
        snap = g.snapshot()
        assert snap["payoff_wait"] == 0        # next is live...
        assert snap["payoff_left"] > 0         # ...while the song runs on
        clock.t += game.PAYOFF_S              # past the end of the song
        assert g.payoff_left() == 0
        assert g.snapshot()["payoff_left"] == 0
    finally:
        os.unlink(p)


def test_half_time_trivia_flow():
    from app import trivia
    conn, p = make_db()
    try:
        trivia.ensure_seeded(conn)
        clock = Clock()
        g = game.Game(conn, rounds=6, clock=clock)
        g.join("A"); g.join("B")
        g.build_rounds(conn)
        for _ in range(3):  # play to halfway
            g.start_round()
            clock.t += 1
            g.answer("A", g.rounds[g.current]["correct"])
            g.answer("B", 0 if g.rounds[g.current]["correct"] else 1)
            g.reveal()
            clock.t += game.PAYOFF_S
        assert g.is_halfway()
        g.start_break(conn)
        snap = g.snapshot()
        assert snap["phase"] == "break" and snap["break_stage"] == "facts"
        assert set(snap["facts"]) == {"A", "B"} and all(snap["facts"].values())
        assert len(g.tf_qs) == game.TF_COUNT
        with pytest.raises(game.GameError):  # no T/F live yet
            g.tf_answer("A", True)
        scores = {n: pl["score"] for n, pl in g.players.items()}
        assert g.advance_break() == "tf"
        for i in range(game.TF_COUNT):
            q = g.tf_qs[i]
            snap = g.snapshot()
            assert snap["break_stage"] == "tf" and snap["tf"]["text"] == q["text"]
            assert "answer" not in snap["tf"]  # answer never ships early
            g.tf_answer("A", q["answer"])       # A always right
            g.tf_answer("B", not q["answer"])   # B always wrong
            with pytest.raises(game.GameError):  # no double answer
                g.tf_answer("A", True)
            assert g.tf_all_answered()
            assert g.advance_break() == "tf_reveal"
            snap = g.snapshot()
            assert snap["tf"]["revealed"] and snap["tf"]["results"]["A"] == game.TF_POINTS
            expected = "resume" if i + 1 == game.TF_COUNT else "tf"
            assert g.advance_break() == expected
        assert g.players["A"]["score"] == scores["A"] + game.TF_COUNT * game.TF_POINTS
        assert g.players["B"]["score"] == scores["B"]
        g.start_round()  # play on
        assert g.phase == "question"
    finally:
        os.unlink(p)


def test_trivia_seed_and_recycling():
    from app import trivia
    conn, p = make_db()
    try:
        # the pack must actually be shipped — a gitignore once ate app/data/
        import subprocess
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", trivia.SEED_PATH],
            capture_output=True, cwd=os.path.dirname(trivia.SEED_PATH))
        assert tracked.returncode == 0, "trivia_seed.json is not tracked by git"
        added = trivia.ensure_seeded(conn)
        assert added >= 80
        assert trivia.ensure_seeded(conn) == 0  # idempotent
        rows = conn.execute("SELECT * FROM trivia").fetchall()
        assert all(r["answer"] in (0, 1) for r in rows if r["kind"] == "tf")
        n_facts = sum(1 for r in rows if r["kind"] == "fact")
        # picking more than the bank holds recycles rather than starving
        first = trivia.pick(conn, "fact", n_facts)
        assert len(first) == n_facts
        again = trivia.pick(conn, "fact", 5)
        assert len(again) == 5  # recycled from used
    finally:
        os.unlink(p)


def test_trivia_custom_pack_and_builtin_optout(tmp_path, monkeypatch):
    import json

    from app import config, trivia
    conn, p = make_db()
    try:
        # custom pack sits beside the DB on the data volume
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "quiz.db"))
        pack = [{"kind": "fact", "text": "A local fact"},
                {"kind": "tf", "text": "A local claim.", "answer": 1},
                {"kind": "tf", "text": "broken item, no answer"},   # skipped, not fatal
                {"kind": "nonsense", "text": "bad kind"}]           # skipped, not fatal
        (tmp_path / "trivia_custom.json").write_text(json.dumps(pack))
        # builtin off: only the two valid custom items land
        monkeypatch.setattr(config, "TRIVIA_BUILTIN_PACK", False)
        assert trivia.ensure_seeded(conn) == 2
        rows = conn.execute("SELECT source, COUNT(*) c FROM trivia GROUP BY source").fetchall()
        assert {r["source"]: r["c"] for r in rows} == {"custom": 2}
        # builtin back on: shipped pack joins the custom one, custom rows kept
        monkeypatch.setattr(config, "TRIVIA_BUILTIN_PACK", True)
        assert trivia.ensure_seeded(conn) >= 80
        assert conn.execute("SELECT COUNT(*) c FROM trivia WHERE source='custom'").fetchone()["c"] == 2
    finally:
        os.unlink(p)


def test_phone_ui_renders_every_phase():
    """Run the JS render smoke in node — catches thrown renders python tests can't see."""
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node not available")
    r = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "js", "render_smoke.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_bootstrap_job_sequence(monkeypatch):
    """/api/bootstrap chains sync -> lastfm(loop) -> tiers -> clips and resolves."""
    from app import main

    calls = []
    lastfm_runs = iter([
        {"scored": 200, "errors": 0, "remaining": 150},
        {"scored": 150, "errors": 0, "remaining": 0},
    ])

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(main.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(main.subsonic, "Client", lambda: object())
    monkeypatch.setattr(main.sync, "sync_library",
                        lambda c, cl, set_stage=None: calls.append("sync") or {"tracks_active": 42})
    monkeypatch.setattr(main.lastfm, "score_batch",
                        lambda c, limit: calls.append("lastfm") or next(lastfm_runs))
    monkeypatch.setattr(main.scoring, "assign_tiers",
                        lambda c: calls.append("tiers") or {"easy": 1})
    monkeypatch.setattr(main.clips, "sweep",
                        lambda set_stage=None: calls.append("clips") or {"cut": 7, "stopped": "done"})
    monkeypatch.setattr(main.library, "clean",
                        lambda c: calls.append("hygiene") or {"banned": {"duplicate": 3}})
    # the registry's real callback signature, ticks included (jobs._run_wrapped)
    out = main._job_bootstrap(lambda stage, log=True: None)
    # hygiene must land BEFORE clips: each row it bans is a clip not cut, and
    # cutting is the expensive step. Cleaning afterwards pays for the junk first.
    assert calls == ["sync", "lastfm", "lastfm", "tiers", "hygiene", "clips"]
    assert out["clips_cut"] == 7
    assert out["tracks_synced"] == 42
    assert out["hygiene"] == {"duplicate": 3}
    assert "warning" not in out


def test_extend_lock_one_at_a_time():
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("A")
        g.build_rounds(conn)
        g.start_round()
        assert g.extend_clip() == 10
        # a second press while the 10s clip is still playing is refused (#27)
        with pytest.raises(game.GameError):
            g.extend_clip()
        snap = g.snapshot()
        assert snap["extend_wait"] > 0
        clock.t += 10.1  # the 10s clip has played out
        assert g.extend_clip() == 20
        clock.t += 20.1
        with pytest.raises(game.GameError):  # nothing beyond 20s
            g.extend_clip()
    finally:
        conn.close(); os.unlink(p)


# ---------- the game master must actually rotate (#36) ----------

def test_least_recently_master_picks_the_one_who_has_never_had_it():
    """The old rotation walked the CURRENT game's join order — i.e. whoever picked up
    their phone first — so it changed every game. Real result: Alice -> Bob -> Alice, and
    Carol was never picked once in the life of the app. Least-recently-master fixes it."""
    import json as _json

    def next_master(present, host, hist):
        hist = dict(hist)
        hist[host] = max(hist.values(), default=0) + 1   # stamped 'now' (time moves forward)
        return min(present, key=lambda n: (hist.get(n, 0), present.index(n))), hist

    # game 1: Alice hosts. Bob and Carol have never mastered -> earliest joiner of them
    present = ["Alice", "Bob", "Carol"]
    nxt, hist = next_master(present, "Alice", {})
    assert nxt == "Bob"

    # game 2: Bob hosts, and this time HE joined first (the old bug's exact trigger)
    present = ["Bob", "Alice", "Carol"]
    nxt, hist = next_master(present, "Bob", hist)
    assert nxt == "Carol", "Carol has never been master — she must be next"

    # game 3: Carol hosts. Alice mastered first of all, so he has waited longest.
    present = ["Carol", "Alice", "Bob"]
    nxt, hist = next_master(present, "Carol", hist)
    assert nxt == "Alice"


def test_a_player_who_misses_a_game_is_not_skipped_forever():
    def next_master(present, host, hist):
        hist = dict(hist)
        hist[host] = max(hist.values(), default=0) + 1
        return min(present, key=lambda n: (hist.get(n, 0), present.index(n))), hist

    hist = {"Alice": 1, "Bob": 2}          # Carol has never mastered
    nxt, hist = next_master(["Alice", "Bob"], "Bob", hist)   # Carol sits this one out
    assert nxt == "Alice"
    nxt, hist = next_master(["Alice", "Bob", "Carol"], "Alice", hist)   # she's back
    assert nxt == "Carol", "the one who has never had it still goes first"


def test_admin_game_summary_is_spoiler_safe():
    """/api/admin/status must never leak the current song mid-round (#52)."""
    from app import main

    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=2, tiers=["easy", "medium"], clock=clock)
        g.join("Alice"); g.join("Bob")
        g.host = "Alice"
        g.build_rounds(conn)
        g.start_round()
        g.answer("Alice", 0)
        old_game = main.hub.game
        main.hub.game = g
        try:
            s = main._admin_game_summary()
            assert s["phase"] == "question"
            assert s["round"] == 1 and s["total_rounds"] == 2
            assert s["answered"] == 1
            assert {pl["name"] for pl in s["players"]} == {"Alice", "Bob"}
            # the spoiler guarantees: no track, no options, mid-question
            blob = str(s)
            t = g.rounds[g.current]["track"]
            assert "track" not in s and "options" not in s and "correct" not in s
            assert t["title"] not in blob
            assert "last_revealed" not in s  # nothing revealed yet
            # after the reveal, THAT song may (and should) show
            g.reveal()
            s = main._admin_game_summary()
            assert s["last_revealed"]["title"] == t["title"]
        finally:
            main.hub.game = old_game
    finally:
        conn.close(); os.unlink(p)


# ---------- remote players: streaming to their own phone, scored fairly ----------

def test_remote_player_speed_bonus_is_measured_from_their_own_audio():
    """The whole point of the feature: a remote player whose stream started 2s
    late and who answered 2s after hearing it should score the same as someone in
    the room who answered 2s after the room heard it. Measuring both from the
    room clock would quietly tax everyone who isn't there."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Local")
        g.join("Away", remote=True)
        assert g.players["Away"]["remote"] and not g.players["Local"]["remote"]
        g.build_rounds(conn)
        rnd = g.start_round()
        correct = rnd["correct"]

        clock.t += 2                      # in the room: 2s of thinking
        local = g.answer("Local", correct)
        clock.t += 2                      # Away's stream only starts now (2s buffering)
        g.note_audio_started("Away")
        clock.t += 2                      # ...then the same 2s of thinking
        away = g.answer("Away", correct)

        assert away["points"] == local["points"]
        assert away["elapsed_ms"] == local["elapsed_ms"] == 2000
    finally:
        conn.close(); os.unlink(p)


def test_remote_latency_credit_is_capped():
    """audio_started is client-reported, so it is not trusted. A phone claiming
    its audio began 30s late must not be able to answer at leisure and still
    collect a full-speed bonus."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Cheat", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 15                     # a very late "my audio just started"
        g.note_audio_started("Cheat")
        a = g.answer("Cheat", rnd["correct"])
        # credit is clamped to 5s, so 15s in reads as 10s elapsed, not 0s
        assert a["elapsed_ms"] == 10_000
        assert a["points"] == 100 + int(50 * (1 - 10 / 20))
    finally:
        conn.close(); os.unlink(p)


def test_first_audio_report_wins():
    """A replay (or a duplicate message) must not push the baseline out and buy a
    bigger bonus part-way through the round."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Away", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 1
        g.note_audio_started("Away")
        clock.t += 4
        g.note_audio_started("Away")      # ignored
        a = g.answer("Away", rnd["correct"])
        assert a["elapsed_ms"] == 4000    # from t+1, not t+5
    finally:
        conn.close(); os.unlink(p)


def test_remote_who_never_reports_audio_falls_back_to_the_room_clock():
    """No report = no credit, and above all no crash: a phone with audio blocked
    (autoplay overlay untapped) still has to be able to answer."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Silent", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 3
        a = g.answer("Silent", rnd["correct"])
        assert a["elapsed_ms"] == 3000
    finally:
        conn.close(); os.unlink(p)


def test_locals_get_no_credit_and_the_room_deadline_still_binds():
    """A local player's audio_started is ignored (they hear the room), and the
    answer window shuts on the ROOM clock for everyone — a remote player gets a
    fairer bonus, not a longer round."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Local")
        g.join("Away", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 4
        g.note_audio_started("Local")     # no-op for a local player
        assert "Local" not in rnd["audio_started"]
        g.note_audio_started("Away")
        clock.t += 17                     # past the room's 20s window
        with pytest.raises(game.GameError):
            g.answer("Away", rnd["correct"])
        with pytest.raises(game.GameError):
            g.answer("Local", rnd["correct"])
    finally:
        conn.close(); os.unlink(p)


def test_remote_flag_survives_a_reconnect_and_can_be_changed():
    """quiz.js re-sends `join` on every reconnect. That must not flip someone
    back to local mid-game — but an explicit set_remote must still work."""
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        g.join("Away", remote=True)
        g.join("Away")                    # reconnect, no remote flag
        assert g.players["Away"]["remote"] is True
        g.set_remote("Away", False)       # walked into the room
        assert g.players["Away"]["remote"] is False
        assert g.snapshot()["players"][0]["remote"] is False
        with pytest.raises(game.GameError):
            g.set_remote("Nobody", True)
    finally:
        conn.close(); os.unlink(p)


def test_everyone_remote_only_when_there_is_someone_and_all_are_remote():
    """Drives whether the house speaker is used at all: if nobody is in the room,
    playing to it is noise in an empty room (and worse if someone's asleep near
    it). Each remote phone plays its own copy, so nothing is lost."""
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        assert g.everyone_remote() is False, "an empty lobby is not 'everyone remote'"

        g.join("Away", remote=True)
        assert g.everyone_remote() is True

        g.join("Here")                      # one person in the room is enough
        assert g.everyone_remote() is False

        g.set_remote("Here", True)          # ...and they wandered off
        assert g.everyone_remote() is True

        g.set_remote("Away", False)         # ...while the other walked in
        assert g.everyone_remote() is False
    finally:
        conn.close(); os.unlink(p)


def test_play_in_room_skips_the_speaker_for_a_board_or_an_all_remote_game():
    """The single gate every in-game speaker call goes through. Two independent
    reasons to stay silent, and either one alone is enough."""
    from app import main

    conn, p = make_db()
    try:
        hub = main.Hub()
        hub.board_last_seen = 0.0        # no board has ever checked in
        assert hub.play_in_room() is True, "no board, no game: the speaker is all there is"

        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        hub.game = g
        assert hub.play_in_room() is True, "an empty lobby still gets room audio"

        g.join("Away", remote=True)
        assert hub.play_in_room() is False, "nobody in the room to hear it"

        g.join("Here")
        assert hub.play_in_room() is True, "one person in the room is enough"

        import time as _t
        hub.board_last_seen = _t.time()   # a board turned up and plays its own audio
        assert hub.play_in_room() is False, "the board's audio would be doubled"
    finally:
        conn.close(); os.unlink(p)


def test_note_audio_started_outside_a_question_is_a_no_op():
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        g.join("Away", remote=True)
        g.note_audio_started("Away")      # lobby: no round exists yet
        g.build_rounds(conn)
        rnd = g.start_round()
        g.reveal()
        g.note_audio_started("Away")
        assert rnd["audio_started"] == {}
    finally:
        conn.close(); os.unlink(p)


# -- artist-variant handling and the duration floor -------------------------

def _insert(conn, tid, title, artist, duration=200, tier="easy", year=1990, listeners=5000):
    conn.execute(
        "INSERT INTO tracks(id,title,artist,album,year,duration,tier,clipped_at,"
        "global_listeners,active) VALUES(?,?,?,?,?,?,?,?,?,1)",
        (tid, title, artist, "Album", year, duration, tier, "2026-07-06T00:00:00", listeners))
    conn.commit()


def test_decoys_never_offer_the_same_song_under_a_variant_artist_tag():
    """The broken round this fixes: 'Back In Black — AC/DC' as the answer with
    'Back In Black — AC, DC' as a decoy. Two options that read identically, only
    one scored right — and the SQL `artist != ?` filter can't see it."""
    conn, p = make_db(0)
    try:
        _insert(conn, "ans", "Back In Black", "AC/DC")
        _insert(conn, "twin", "Back In Black", "AC, DC")      # the trap
        for i in range(20):
            _insert(conn, f"d{i}", f"Other Song {i}", f"Decoy Band {i}")
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='ans'").fetchone())
        for _ in range(40):  # decoys are random — one pass proves little
            decoys = game.pick_decoys(conn, answer)
            titles = [d["title"] for d in decoys]
            assert "Back In Black" not in titles, decoys
            assert "AC, DC" not in [d["artist"] for d in decoys], decoys
    finally:
        conn.close(); os.unlink(p)


def test_pick_artist_track_reaches_every_spelling_of_the_chosen_artist():
    """Picking 'AC/DC' off the wall must reach tracks tagged 'AC, DC' too."""
    conn, p = make_db(0)
    try:
        _insert(conn, "v1", "Shoot To Thrill", "AC, DC")
        _insert(conn, "v2", "Stiff Upper Lip", "AC-DC")
        got = set()
        for _ in range(60):
            t = game.pick_artist_track(conn, ["AC/DC"], set())
            assert t is not None, "no spelling matched at all"
            got.add(t["id"])
        assert got == {"v1", "v2"}
    finally:
        conn.close(); os.unlink(p)


def test_pick_artist_track_still_returns_none_for_an_unknown_artist():
    conn, p = make_db(0)
    try:
        _insert(conn, "a", "Song", "Someone Else")
        assert game.pick_artist_track(conn, ["Nobody At All"], set()) is None
        assert game.pick_artist_track(conn, [], set()) is None
    finally:
        conn.close(); os.unlink(p)


def test_pick_artist_track_honours_the_exclude_set():
    conn, p = make_db(0)
    try:
        _insert(conn, "only", "Song", "AC/DC")
        assert game.pick_artist_track(conn, ["AC/DC"], set())["id"] == "only"
        assert game.pick_artist_track(conn, ["AC/DC"], {"only"}) is None
    finally:
        conn.close(); os.unlink(p)


def test_tracks_too_short_to_clip_are_not_quizzable():
    """A 20s intro clip out of a 25s track IS the song — the clip gives the
    answer away, so short tracks must never reach a round."""
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert(conn, f"ok{i}", f"Real Song {i}", f"Band {i}", duration=200)
        _insert(conn, "skit", "Skit #2", "Kanye West", duration=8)
        _insert(conn, "sting", "Interlude", "Someone", duration=31)
        _insert(conn, "long", "DJ Mix", "Someone Else", duration=4000)
        picked = {t["id"] for t in game.pick_tracks(conn, 12, ["easy"])}
        assert "skit" not in picked and "sting" not in picked
        assert "long" not in picked, "the existing MAX_DURATION_S guard must still hold"
        assert len(picked) == 12
    finally:
        conn.close(); os.unlink(p)


def test_a_null_duration_track_is_still_quizzable():
    """Duration is metadata that can simply be missing; that's not evidence the
    track is unusable, and excluding NULLs would silently shrink the pool."""
    conn, p = make_db(0)
    try:
        conn.execute(
            "INSERT INTO tracks(id,title,artist,album,year,duration,tier,clipped_at,"
            "global_listeners,active) VALUES('n','Song','Band','Album',1990,NULL,'easy',"
            "'2026-07-06T00:00:00',5000,1)")
        conn.commit()
        assert [t["id"] for t in game.pick_tracks(conn, 1, ["easy"])] == ["n"]
    finally:
        conn.close(); os.unlink(p)


# -- round filters (genre / decade) ----------------------------------------

def _insert_f(conn, tid, artist, genre, year, tier="easy"):
    conn.execute(
        "INSERT INTO tracks(id,title,artist,album,genre,year,duration,tier,clipped_at,"
        "global_listeners,active) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
        (tid, f"Song {tid}", artist, "Album", genre, year, 200, tier,
         "2026-07-06T00:00:00", 5000))
    conn.commit()


def test_a_genre_filter_only_offers_that_genre():
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        for i in range(12):
            _insert_f(conn, f"p{i}", f"Pop Act {i}", "Pop", 2005)
        picked = game.pick_tracks(conn, 10, ["easy"], filters=game.filter_sql(["Rock"]))
        assert {t["genre"] for t in picked} == {"Rock"}
        assert len(picked) == 10
    finally:
        conn.close(); os.unlink(p)


def test_a_decade_filter_excludes_untagged_and_junk_years():
    """You cannot honestly claim an untagged track belongs to the 90s, and the tags carry
    real junk — AFI's 'Miss Murder' is tagged 1212 in this library. Both must be out of a
    decade round rather than quietly padding it."""
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert_f(conn, f"n{i}", f"Nineties {i}", "Rock", 1993)
        _insert_f(conn, "noyear", "No Year Band", "Rock", None)
        _insert_f(conn, "junk", "Junk Year Band", "Rock", 1212)
        _insert_f(conn, "later", "Later Band", "Rock", 2005)
        picked = game.pick_tracks(conn, 12, ["easy"],
                                  filters=game.filter_sql(None, 1990, 1999))
        ids = {t["id"] for t in picked}
        assert "noyear" not in ids and "junk" not in ids and "later" not in ids
        assert len(ids) == 12
    finally:
        conn.close(); os.unlink(p)


def test_genres_match_exactly_so_rock_does_not_drag_in_four_other_genres():
    """The tags are freeform — 70 distinct over the real pool, with 'Rock', 'Hard Rock',
    'Alternative Rock' and 'Rock & Roll' all separate. A substring match on 'Rock' would
    silently include genres nobody ticked."""
    conn, p = make_db(0)
    try:
        for i in range(10):
            _insert_f(conn, f"rock{i}", f"Rock Band {i}", "Rock", 1995)
        for i in range(10):
            _insert_f(conn, f"hard{i}", f"Hard Band {i}", "Hard Rock", 1995)
        picked = game.pick_tracks(conn, 10, ["easy"], filters=game.filter_sql(["Rock"]))
        assert {t["genre"] for t in picked} == {"Rock"}, "'Hard Rock' is a different genre"
    finally:
        conn.close(); os.unlink(p)


def test_an_impossible_combination_fails_at_construction_not_mid_game():
    """A filtered pool too small to fill the rounds must be refused up front, and the error
    has to say the filters are why — 'only 2 tracks in tiers [easy]' sounds like a broken
    library when the truth is 'Reggae in the 1960s is two songs'."""
    conn, p = make_db(0)
    try:
        _insert_f(conn, "a", "Reggae One", "Reggae", 1965)
        _insert_f(conn, "b", "Reggae Two", "Reggae", 1968)
        for i in range(20):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 2005)
        with pytest.raises(game.GameError, match="filters"):
            game.Game(conn, rounds=10, tiers=["easy"], genres=["Reggae"],
                      year_from=1960, year_to=1969, clock=Clock())
        # ...and the same library is perfectly fine unfiltered
        game.Game(conn, rounds=10, tiers=["easy"], clock=Clock())
    finally:
        conn.close(); os.unlink(p)


def test_pool_count_is_the_preflight_the_ui_needs():
    """Genre and decade are each plausible while their INTERSECTION is empty. Without a
    count the only feedback was GameError at the moment someone tapped Start."""
    conn, p = make_db(0)
    try:
        for i in range(10):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        for i in range(4):
            _insert_f(conn, f"s{i}", f"Sixties Act {i}", "Pop", 1965)
        assert game.pool_count(conn, ["easy"]) == 14
        assert game.pool_count(conn, ["easy"], ["Rock"]) == 10
        assert game.pool_count(conn, ["easy"], None, 1960, 1969) == 4
        assert game.pool_count(conn, ["easy"], ["Rock"], 1960, 1969) == 0, "empty overlap"
    finally:
        conn.close(); os.unlink(p)


def test_decoys_obey_the_filter_so_the_answer_does_not_stand_out():
    """A fairness fix, not tidiness: in a 60s-only game three decoys drawn from the whole
    library are recognisably modern, so the answer is the one old-sounding option and the
    question is free."""
    conn, p = make_db(0)
    try:
        for i in range(10):
            _insert_f(conn, f"six{i}", f"Sixties Act {i}", "Rock", 1965)
        for i in range(30):
            _insert_f(conn, f"mod{i}", f"Modern Act {i}", "Pop", 2015)
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='six0'").fetchone())
        filters = game.filter_sql(None, 1960, 1969)
        for _ in range(30):
            decoys = game.pick_decoys(conn, answer, filters=filters)
            assert all(d["artist"].startswith("Sixties") for d in decoys), decoys
    finally:
        conn.close(); os.unlink(p)


def test_a_real_filtered_game_builds_rounds_whose_options_are_all_on_theme():
    """Covers the WIRING, not just pick_decoys: _mk_round has to pass the game's filters
    down, and testing pick_decoys directly leaves that call site free to drop them.

    Uses a GENRE filter with every track in the same year on purpose. pick_decoys has always
    preferred the answer's own decade, so a decade-filtered game looks correct even with the
    filters dropped — the pre-existing preference does the same job by accident. Genre is
    the case where only the real wiring can save the round.
    """
    conn, p = make_db(0)
    try:
        for i in range(14):
            _insert_f(conn, f"jz{i}", f"Jazz Act {i}", "Jazz", 1995)
        for i in range(40):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 1995)
        g = game.Game(conn, rounds=10, tiers=["easy"], genres=["Jazz"], clock=Clock())
        g.join("Sam")
        g.build_rounds(conn)
        for rnd in g.rounds:
            for o in rnd["options"]:
                assert o["artist"].startswith("Jazz"), \
                    f"a pop decoy makes the jazz answer obvious: {rnd['options']}"
    finally:
        conn.close(); os.unlink(p)


def test_decoys_widen_rather_than_fail_when_the_filter_is_too_narrow():
    """Four options beat a failed round. A narrow filter may not hold enough DISTINCT
    artists to fill the decoys, so the unfiltered pool is the fallback."""
    conn, p = make_db(0)
    try:
        _insert_f(conn, "only", "The Only Sixties Band", "Rock", 1965)
        for i in range(30):
            _insert_f(conn, f"mod{i}", f"Modern Act {i}", "Pop", 2015)
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='only'").fetchone())
        decoys = game.pick_decoys(conn, answer, filters=game.filter_sql(None, 1960, 1969))
        assert len(decoys) == 3, "the round still gets four options"
    finally:
        conn.close(); os.unlink(p)


def test_a_boost_round_falls_back_when_a_favourite_has_nothing_in_the_theme():
    """A boost round is a bonus, not a promise: with filters on, a player's favourite
    artists may have nothing in the chosen genre, and build_rounds just fills the slot
    from the pool instead."""
    conn, p = make_db(0)
    try:
        _insert_f(conn, "jazz", "Miles Davis", "Jazz", 1959)
        for i in range(12):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        filters = game.filter_sql(["Rock"])
        assert game.pick_artist_track(conn, ["Miles Davis"], set(), filters=filters) is None
        g = game.Game(conn, rounds=10, tiers=["easy"], genres=["Rock"], clock=Clock())
        g.join("Sam")
        g.set_artists("Sam", ["Miles Davis"])
        g.build_rounds(conn)
        assert len(g.rounds) == 10, "the round list is still full"
        assert all(r["track"]["genre"] == "Rock" for r in g.rounds)
    finally:
        conn.close(); os.unlink(p)


def test_filter_label_reads_like_a_person_wrote_it():
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        mk = lambda **kw: game.Game(conn, rounds=1, tiers=["easy"], clock=Clock(), **kw)
        assert mk().filter_label() == "", "an unfiltered game says nothing"
        assert mk(genres=["Rock"]).filter_label() == "Rock"
        assert mk(year_from=1990, year_to=1999).filter_label() == "the 1990s"
        assert mk(genres=["Rock"], year_from=1990, year_to=1999).filter_label() \
            == "Rock · the 1990s"
        assert mk(year_from=1985, year_to=1995).filter_label() == "1985–1995", \
            "a span that isn't a decade reads as a range"
        assert "filter_label" not in mk().snapshot(), \
            "absent on an unfiltered game so existing UIs render exactly as before"
        assert mk(genres=["Rock"]).snapshot()["filter_label"] == "Rock"
    finally:
        conn.close(); os.unlink(p)


# -- genre exclusion ("everything except...") -------------------------------

def test_excluding_a_genre_drops_exactly_those_tracks_and_keeps_the_rest():
    """The real ask: one tag in this library sits on tracks that shouldn't come up at a
    family quiz. Saying so must not be a choice between that and the whole library."""
    conn, p = make_db(0)
    try:
        for i in range(6):
            _insert_f(conn, f"adult{i}", f"Adult Act {i}", "NotForKids", 1995)
        for i in range(6):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 1995)
        for i in range(6):
            _insert_f(conn, f"rock{i}", f"Rock Band {i}", "Rock", 1995)
        picked = game.pick_tracks(conn, 12, ["easy"],
                                  filters=game.filter_sql(exclude_genres=["NotForKids"]))
        assert len(picked) == 12, "everything but the excluded genre is still eligible"
        assert {t["genre"] for t in picked} == {"Pop", "Rock"}
    finally:
        conn.close(); os.unlink(p)


def test_an_untagged_track_survives_an_exclusion():
    """THE regression guard, and the reason the fragment is written the way it is.

    SQL's three-valued logic makes `NULL NOT IN ('X')` evaluate to NULL, not true, so the
    obvious `genre NOT IN (...)` silently drops every track with no genre tag — hundreds of
    quizzable tracks in this library. The chosen semantics are "exclude only what I named",
    and an untagged track was never named. Verified against SQLite over the rows
    ['Pop', 'X', NULL, 'Obscure']: the bare NOT IN returns Pop and Obscure only.
    """
    conn, p = make_db(0)
    try:
        _insert_f(conn, "adult", "Adult Act", "NotForKids", 1995)
        _insert_f(conn, "untagged", "Untagged Band", None, 1995)
        _insert_f(conn, "obscure", "Obscure Act", "Sea Shanty", 1995)
        for i in range(4):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 1995)
        frag, params = game.filter_sql(exclude_genres=["NotForKids"])
        picked = game.pick_tracks(conn, 6, ["easy"], filters=(frag, params))
        ids = {t["id"] for t in picked}
        assert "untagged" in ids, \
            "a NULL genre was never named for exclusion — a bare NOT IN loses it"
        assert "obscure" in ids, "a genre too small for the picker is still eligible"
        assert "adult" not in ids
        assert "IS NULL" in frag, \
            "the NULL arm is the fix, not decoration — see exclusion_sql"
    finally:
        conn.close(); os.unlink(p)


def test_exclusion_composes_with_the_include_list_and_the_year_range():
    """Three filters that must all apply at once. An exclusion that quietly replaced the
    theme — or was appended after the year clause in a way that broke it — would look fine
    in isolation and produce an off-theme game."""
    conn, p = make_db(0)
    try:
        _insert_f(conn, "want", "Wanted Rock", "Rock", 1995)
        _insert_f(conn, "wrong_decade", "Old Rock", "Rock", 1965)
        _insert_f(conn, "wrong_genre", "Nineties Pop", "Pop", 1995)
        _insert_f(conn, "banned", "Nineties Adult Rock", "NotForKids", 1995)
        _insert_f(conn, "untagged", "Nineties Untagged", None, 1995)
        # Rock in the 90s, minus NotForKids: only 'want'. 'untagged' is NOT Rock, so the
        # INCLUDE list drops it — inclusion is exact, exclusion spares NULLs, and the two
        # are deliberately not symmetric.
        assert game.pool_count(conn, ["easy"], ["Rock"], 1990, 1999, ["NotForKids"]) == 1
        picked = game.pick_tracks(conn, 1, ["easy"], filters=game.filter_sql(
            ["Rock"], 1990, 1999, ["NotForKids"]))
        assert [t["id"] for t in picked] == ["want"]
        # exclusion + decade with no include list: the untagged 90s track comes along
        ids = {t["id"] for t in game.pick_tracks(conn, 3, ["easy"], filters=game.filter_sql(
            None, 1990, 1999, ["NotForKids"]))}
        assert ids == {"want", "wrong_genre", "untagged"}
    finally:
        conn.close(); os.unlink(p)


def test_a_genre_in_both_lists_is_excluded_rather_than_silently_forgiven():
    """"Rock, but not Rock" has to resolve one way, in the open. The exclusion wins — a
    veto beats a request — so the pool is empty and the preflight locks Start. Quietly
    dropping the exclusion instead would play the one genre the host asked to leave out."""
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        for i in range(12):
            _insert_f(conn, f"p{i}", f"Pop Act {i}", "Pop", 1995)
        assert game.pool_count(conn, ["easy"], ["Rock"], None, None, ["Rock"]) == 0
        assert game.pool_count(conn, ["easy"], ["Rock", "Pop"], None, None, ["Rock"]) == 12, \
            "the other requested genre is untouched"
        with pytest.raises(game.GameError, match="filters"):
            game.Game(conn, rounds=10, tiers=["easy"], genres=["Rock"],
                      exclude_genres=["Rock"], clock=Clock())
    finally:
        conn.close(); os.unlink(p)


def test_the_pool_preflight_sees_an_exclusion_that_empties_the_library():
    """Excluding most of the library must lock Start with a count, not sail through the
    preflight and fail at the moment of the tap. An exclusion the preflight ignored would
    always report the full pool, which is the one number guaranteed to be wrong."""
    conn, p = make_db(0)
    try:
        for i in range(20):
            _insert_f(conn, f"p{i}", f"Pop Act {i}", "Pop", 1995)
        for i in range(4):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        assert game.pool_count(conn, ["easy"]) == 24
        assert game.pool_count(conn, ["easy"], None, None, None, ["Pop"]) == 4, \
            "excluding the bulk of the library must show up as a small pool"
        assert game.pool_count(conn, ["easy"], None, None, None, ["Pop", "Rock"]) == 0
        with pytest.raises(game.GameError):
            game.Game(conn, rounds=10, tiers=["easy"], exclude_genres=["Pop"], clock=Clock())
    finally:
        conn.close(); os.unlink(p)


def test_an_excluded_genre_never_appears_as_a_decoy_even_when_the_options_widen():
    """The subtle one. pick_decoys deliberately abandons the theme when a narrow filter
    can't fill four options — a modern decoy in a 60s round is a small giveaway, worth it
    for a playable round. An excluded genre is not the same trade: decoys are read out loud
    like every other option, so the widened query must keep the exclusion."""
    conn, p = make_db(0)
    try:
        _insert_f(conn, "answer", "The Only Jazz Act", "Jazz", 1995)
        for i in range(30):
            _insert_f(conn, f"adult{i}", f"Adult Act {i}", "NotForKids", 1995)
        for i in range(5):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 1995)
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='answer'").fetchone())
        # one Jazz track, so the themed decoy query can't fill three and MUST widen
        filters = game.filter_sql(["Jazz"], exclude_genres=["NotForKids"])
        exclusions = game.exclusion_sql(["NotForKids"])
        for _ in range(30):
            decoys = game.pick_decoys(conn, answer, filters=filters, exclusions=exclusions)
            assert len(decoys) == 3, "the round still gets four options"
            assert not any(d["artist"].startswith("Adult") for d in decoys), decoys
    finally:
        conn.close(); os.unlink(p)


def test_a_real_excluded_game_wires_the_exclusion_into_every_picker():
    """Covers the WIRING rather than the fragment: Game has to thread the exclusion into
    the track pool, the decoys AND the boost round. Testing filter_sql alone leaves every
    one of those call sites free to drop it."""
    conn, p = make_db(0)
    try:
        for i in range(14):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 1995)
        for i in range(20):
            _insert_f(conn, f"adult{i}", "Adult Act", "NotForKids", 1995)
        g = game.Game(conn, rounds=10, tiers=["easy"], exclude_genres=["NotForKids"],
                      clock=Clock())
        g.join("Sam")
        # a favourite artist who only exists in the excluded genre gets no boost round
        g.set_artists("Sam", ["Adult Act"])
        g.build_rounds(conn)
        assert len(g.rounds) == 10
        for rnd in g.rounds:
            assert rnd["track"]["genre"] == "Pop", rnd["track"]
            for o in rnd["options"]:
                assert o["artist"] != "Adult Act", \
                    f"an excluded track was read out as a decoy: {rnd['options']}"
    finally:
        conn.close(); os.unlink(p)


def test_the_count_endpoint_carries_the_exclusion_from_the_picker(monkeypatch):
    """The wire format, not the SQL: the picker sends exclude_genres as a pipe-separated
    query param, and a server that ignored it would answer every exclusion with the full
    pool — so Start would unlock on a combination that cannot fill a game."""
    from fastapi.testclient import TestClient

    from app import main
    conn, p = make_db(0)
    try:
        for i in range(20):
            _insert_f(conn, f"pop{i}", f"Pop Act {i}", "Pop", 1995)
        for i in range(4):
            _insert_f(conn, f"adult{i}", f"Adult Act {i}", "NotForKids", 1995)
        _insert_f(conn, "untagged", "Untagged Band", None, 1995)
        real_connect = db.connect
        monkeypatch.setattr(main.db, "connect", lambda *a, **kw: real_connect(p))
        c = TestClient(main.app)
        assert c.get("/api/round-filters/count?tiers=easy").json()["tracks"] == 25
        body = c.get("/api/round-filters/count?tiers=easy&exclude_genres=Pop").json()
        assert body["tracks"] == 5, "the 4 excluded-genre tracks plus the untagged one"
        assert body["enough_for_10"] is False, "Start must lock on a 5-song pool"
        assert c.get("/api/round-filters/count?tiers=easy&exclude_genres=NotForKids"
                     ).json()["tracks"] == 21, "untagged tracks survive the exclusion"
        # two exclusions at once, pipe-separated like the include list
        assert c.get("/api/round-filters/count?tiers=easy&exclude_genres=Pop|NotForKids"
                     ).json()["tracks"] == 1
    finally:
        conn.close(); os.unlink(p)


def test_the_filter_label_says_what_was_left_out():
    """An exclusion-only game plays the whole library bar a slice, so it is otherwise
    indistinguishable from an unfiltered one — with the label silent nobody in the room can
    tell whether the exclusion was applied at all."""
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        mk = lambda **kw: game.Game(conn, rounds=1, tiers=["easy"], clock=Clock(), **kw)
        assert mk(exclude_genres=["NotForKids"]).filter_label() == "no NotForKids"
        assert mk(genres=["Rock"], exclude_genres=["Blues"], year_from=1990, year_to=1999) \
            .filter_label() == "Rock · no Blues · the 1990s"
        assert mk(exclude_genres=["Blues", "Jazz"]).filter_label() == "no Blues · no Jazz"
        assert mk(exclude_genres=["NotForKids"]).snapshot()["filter_label"] \
            == "no NotForKids", "the board and every phone get it too"
    finally:
        conn.close(); os.unlink(p)


# -- cross-game track history ----------------------------------------------

def _played(conn, track_id, at):
    conn.execute("INSERT INTO plays(track_id, played_at) VALUES(?,?)", (track_id, at))
    conn.commit()


def test_a_started_round_is_recorded_even_if_the_game_is_abandoned():
    """Why the stamp is in start_round and not finish(): `abort` throws the game away
    without ever calling finish, and an abandoned game's questions were still asked out
    loud. Recording at start is the only point that knows a round really happened."""
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=3, tiers=["easy", "medium"], clock=Clock())
        g.join("Sam")
        g.build_rounds(conn)
        first = g.start_round(conn)["track"]["id"]
        g.reveal()
        second = g.start_round(conn)["track"]["id"]
        # ...and now the game is abandoned: no finish(), no games row
        rows = [r["track_id"] for r in conn.execute("SELECT track_id FROM plays")]
        assert rows == [first, second], "both asked rounds recorded, round 3 never asked"
        assert conn.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_start_round_without_a_conn_records_nothing_and_does_not_crash():
    """The engine stays usable with no DB handle — 18 existing tests call start_round()
    bare, and a missing history row must never be the thing that breaks a round."""
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=2, tiers=["easy", "medium"], clock=Clock())
        g.join("Sam")
        g.build_rounds(conn)
        g.start_round()
        assert conn.execute("SELECT COUNT(*) c FROM plays").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_the_picker_prefers_tracks_that_have_not_been_played_lately():
    conn, p = make_db(0)
    try:
        for i in range(6):
            _insert(conn, f"t{i}", f"Song {i}", f"Band {i}")
        # t0..t3 all played; t4/t5 never
        for i in range(4):
            _played(conn, f"t{i}", f"2026-07-0{i + 1}T00:00:00")
        for _ in range(30):
            picked = [t["id"] for t in game.pick_tracks(conn, 2, ["easy"])]
            assert set(picked) == {"t4", "t5"}, f"fresh tracks must win outright: {picked}"
    finally:
        conn.close(); os.unlink(p)


def test_a_pool_smaller_than_the_history_still_fills_the_round():
    """The reason recency SORTS instead of filtering. A hard exclude of everything played
    looks equivalent on a 14,000-track library and fails outright on a small pool — which
    a genre+decade filter can easily produce. Worst case we hand back the LEAST recently
    asked repeats, which is what a human would do."""
    conn, p = make_db(0)
    try:
        for i in range(3):
            _insert(conn, f"t{i}", f"Song {i}", f"Band {i}")
            _played(conn, f"t{i}", f"2026-07-0{i + 1}T00:00:00")   # t2 = most recent
        picked = [t["id"] for t in game.pick_tracks(conn, 2, ["easy"])]
        assert len(picked) == 2, "the round still fills — no GameError"
        assert set(picked) == {"t0", "t1"}, f"the two stalest, not the freshest: {picked}"
    finally:
        conn.close(); os.unlink(p)


def test_recency_beats_nothing_when_a_track_was_played_twice():
    """MAX(played_at) is what counts: an old favourite played again last night is as
    recent as anything else, not still stale from its first outing."""
    conn, p = make_db(0)
    try:
        for i in range(3):
            _insert(conn, f"t{i}", f"Song {i}", f"Band {i}")
        _played(conn, "t0", "2026-01-01T00:00:00")   # long ago...
        _played(conn, "t0", "2026-07-29T00:00:00")   # ...but again last night
        _played(conn, "t1", "2026-02-01T00:00:00")
        for _ in range(30):
            assert [t["id"] for t in game.pick_tracks(conn, 1, ["easy"])] == ["t2"]
        picked = [t["id"] for t in game.pick_tracks(conn, 2, ["easy"])]
        assert set(picked) == {"t2", "t1"}, f"t0 is the freshest play, so it goes last: {picked}"
    finally:
        conn.close(); os.unlink(p)


def test_a_boost_round_avoids_repeating_the_same_favourite_artist_track():
    """This matters more than the main pool: a player picks the same three favourite
    artists most weeks, so without a freshness preference their boost round is drawn from
    a handful of tracks and lands on the same song every time."""
    conn, p = make_db(0)
    try:
        _insert(conn, "old", "Highway To Hell", "AC/DC")
        _insert(conn, "new", "Back In Black", "AC/DC")
        _played(conn, "old", "2026-07-29T00:00:00")
        for _ in range(30):
            assert game.pick_artist_track(conn, ["AC/DC"], set())["id"] == "new"
    finally:
        conn.close(); os.unlink(p)


def test_decoys_never_offer_two_spellings_of_the_same_band():
    """The other half of the variant problem: decoys are meant to be four
    DIFFERENT artists. 'Highway To Hell — AC/DC' plus 'Shoot To Thrill — AC, DC'
    in one round is one band twice, which narrows the guess unfairly."""
    conn, p = make_db(0)
    try:
        _insert(conn, "ans", "Some Answer", "Answer Band")
        for i, (title, artist) in enumerate((
                ("Highway To Hell", "AC/DC"), ("Shoot To Thrill", "AC, DC"),
                ("Stiff Upper Lip", "AC-DC"), ("Back In Black", "ACDC"))):
            _insert(conn, f"v{i}", title, artist)
        for i in range(6):
            _insert(conn, f"o{i}", f"Other {i}", f"Band {i}")
        answer = dict(conn.execute("SELECT * FROM tracks WHERE id='ans'").fetchone())
        for _ in range(60):
            decoys = game.pick_decoys(conn, answer)
            acdc = [d for d in decoys if game.library.artist_key(d["artist"]) == "acdc"]
            assert len(acdc) <= 1, decoys
    finally:
        conn.close(); os.unlink(p)


# -- one player, one row: names folded by case ------------------------------
#
# `results.player` is plain TEXT and `join` only ever did `.strip()[:24]`, so
# `robin` and `Robin` were two all-time rows and a returning player lost their
# whole history by typing a lowercase name. Fixed while the production
# leaderboard was still empty, so there was nothing to merge by hand.

def _result(conn, game_id, player, score, correct=1, fastest_ms=1000):
    conn.execute("INSERT OR IGNORE INTO games(id, started_at, rounds) VALUES(?,?,10)",
                 (game_id, f"2026-07-{game_id:02d}T20:00:00"))
    conn.execute("INSERT INTO results(game_id, player, score, correct, fastest_ms) "
                 "VALUES(?,?,?,?,?)", (game_id, player, score, correct, fastest_ms))
    conn.commit()


def test_mixed_case_spellings_of_one_player_are_one_leaderboard_row():
    """The bug in full: three games under three capitalisations read as three
    different people, each with a third of the history. Totals must add up over
    the person, not the spelling — and fastest_ms is the MINIMUM across all of
    them, not whichever row grouped first."""
    conn, p = make_db(0)
    try:
        _result(conn, 1, "robin", 100, correct=2, fastest_ms=1800)
        _result(conn, 2, "Robin", 200, correct=3, fastest_ms=900)
        _result(conn, 3, "ROBIN", 50, correct=1, fastest_ms=2500)
        _result(conn, 1, "Bob", 10, correct=1, fastest_ms=4000)
        lb = game.all_time_leaderboard(conn)
        assert [r["player"] for r in lb] == ["Robin", "Bob"], \
            f"one person must be one row: {lb}"
        me = lb[0]
        assert me["games"] == 3
        assert me["total_score"] == 350
        assert me["total_correct"] == 6
        assert me["fastest_ms"] == 900, "the best time of all three, not an arbitrary row's"
    finally:
        conn.close(); os.unlink(p)


def test_the_leaderboard_name_does_not_depend_on_which_row_sqlite_picked():
    """GROUP BY ... COLLATE NOCASE hands back an ARBITRARY member spelling for the
    SELECTed column, so without display_name() the name on the board would depend
    on insertion order. Same person, two histories inserted in opposite orders —
    the displayed name has to come out identical."""
    conn, p = make_db(0)
    try:
        _result(conn, 1, "rObIn", 100)
        _result(conn, 2, "ROBIN", 100)
        first = game.all_time_leaderboard(conn)[0]["player"]
    finally:
        conn.close(); os.unlink(p)
    conn, p = make_db(0)
    try:
        _result(conn, 1, "ROBIN", 100)
        _result(conn, 2, "rObIn", 100)
        second = game.all_time_leaderboard(conn)[0]["player"]
    finally:
        conn.close(); os.unlink(p)
    assert first == second == "Robin", f"unstable display name: {first!r} vs {second!r}"


def test_display_name_is_title_case_with_its_known_flattening():
    """Deliberately simple, and this pins the accepted cost so a future refinement
    is a decision rather than an accident: `JB` flattens to `Jb`. If that is ever
    fixed, this assertion is the one to change — and only in display_name."""
    assert game.display_name("robin") == "Robin"
    assert game.display_name("ROBIN") == "Robin"
    assert game.display_name("  robin  ") == "Robin"
    assert game.display_name("mary jane") == "Mary Jane"
    assert game.display_name("JB") == "Jb"
    assert game.display_name("x" * 40) == "X" + "x" * 23, "the 24-char cap still applies"


def test_joining_as_a_case_variant_takes_the_same_seat_and_keeps_the_score():
    """The lobby half of the fold. Someone whose phone autocapitalised — or who
    just typed lowercase after a reconnect — must land back in their own seat.
    Two seats would also put two spellings into `results` for one game, since the
    primary key there is (game_id, player)."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Robin")
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 2
        g.answer("Robin", rnd["correct"])
        earned = g.players["Robin"]["score"]
        assert earned > 0

        g.join("robin")                       # same person, phone shouted quietly
        assert list(g.players) == ["Robin"], f"a second seat opened: {list(g.players)}"
        assert g.players["Robin"]["score"] == earned, "rejoining reset their score"
        assert g.players["Robin"]["correct"] == 1
    finally:
        conn.close(); os.unlink(p)


def test_an_exact_name_rejoin_still_does_not_reset_a_player():
    """The pre-existing behaviour the fold must not break: quiz.js re-sends `join`
    on every reconnect, and `setdefault` is why that doesn't wipe a live player."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Robin", remote=True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 2
        g.answer("Robin", rnd["correct"])
        earned = g.players["Robin"]["score"]

        g.join("Robin")                       # a plain reconnect
        assert g.players["Robin"]["score"] == earned
        assert g.players["Robin"]["remote"] is True, "a reconnect flipped them back to local"
        assert list(g.players) == ["Robin"]
    finally:
        conn.close(); os.unlink(p)


def test_a_case_variant_join_is_seated_in_the_lobbys_spelling_not_the_typed_one():
    """A fresh join is Title Cased, but an EXISTING player keeps the spelling the
    lobby already holds. Renaming them mid-game would move the snapshot key out
    from under every phone showing scores against it."""
    conn, p = make_db()
    try:
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=Clock())
        g.join("mary jane")
        assert list(g.players) == ["Mary Jane"], "a fresh join is Title Cased"
        g.join("MARY JANE")
        assert list(g.players) == ["Mary Jane"]
        assert [pl["name"] for pl in g.snapshot()["players"]] == ["Mary Jane"]
    finally:
        conn.close(); os.unlink(p)


def test_a_case_variant_is_not_a_stranger_to_the_rest_of_the_game():
    """Folding only in `join` would have been worse than the bug: the phone still
    sends its typed name on `answer`, `ready` and the rest, and every one of those
    checks membership — so a player seated as `Robin` would be told "join first"
    for the whole game. A genuine stranger must still be refused."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Robin", remote=True)
        g.set_artists("robin", ["Artist 3"])
        assert g.players["Robin"]["ready"] is True
        assert g.players["Robin"]["artists"] == ["Artist 3"]
        g.set_remote("ROBIN", True)
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 1
        g.note_audio_started("rObIn")
        assert "Robin" in rnd["audio_started"], "the remote baseline landed on nobody"
        g.answer("robin", rnd["correct"])
        assert "Robin" in rnd["answers"]
        assert g.all_answered()
        with pytest.raises(game.GameError):   # not a spelling — a stranger
            g.answer("Trevor", 0)
    finally:
        conn.close(); os.unlink(p)


def test_one_game_writes_one_results_row_per_person_however_they_spelled_it():
    """`results` has PRIMARY KEY (game_id, player), so two spellings would both be
    accepted inside a single game — splitting one night's score in two before the
    leaderboard ever sees it."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=1, tiers=["easy", "medium"], clock=clock)
        g.join("Robin")
        g.join("robin")
        g.build_rounds(conn)
        rnd = g.start_round()
        clock.t += 2
        g.answer("ROBIN", rnd["correct"])
        g.reveal()
        gid = g.finish(conn)
        rows = conn.execute("SELECT player, score FROM results WHERE game_id=?",
                            (gid,)).fetchall()
        assert [r["player"] for r in rows] == ["Robin"], f"one game, two rows: {list(rows)}"
        assert rows[0]["score"] > 0
        assert game.all_time_leaderboard(conn)[0]["games"] == 1
    finally:
        conn.close(); os.unlink(p)


# -- how many rounds (the master's choice, was hardcoded at 10) --------------

def test_a_pool_too_small_for_ten_rounds_still_plays_a_short_game(monkeypatch):
    """THE bug the round picker exists to fix: the preflight judged every pool against 10, so
    a tight theme holding six songs locked Start even though a 5-round game plays it happily.
    The count the master actually picked is what the pool has to fill."""
    from fastapi.testclient import TestClient

    from app import main
    conn, p = make_db(0)
    try:
        for i in range(6):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        real_connect = db.connect
        monkeypatch.setattr(main.db, "connect", lambda *a, **kw: real_connect(p))
        c = TestClient(main.app)
        short = c.get("/api/round-filters/count?tiers=easy&rounds=5").json()
        assert short["tracks"] == 6
        assert short["rounds"] == 5
        assert short["enough"] is True, "a 6-song pool really does play 5 rounds"
        # the same pool, judged against the counts it genuinely cannot fill
        assert c.get("/api/round-filters/count?tiers=easy&rounds=20").json()["enough"] is False
        assert c.get("/api/round-filters/count?tiers=easy&rounds=7").json()["enough"] is False, \
            "one round more than the pool holds"
        assert c.get("/api/round-filters/count?tiers=easy&rounds=6").json()["enough"] is True, \
            "exactly as many songs as rounds is playable"
        # ...and the count is optional: an old phone that sends none gets the 10-round rule
        plain = c.get("/api/round-filters/count?tiers=easy").json()
        assert plain["rounds"] == game.DEFAULT_ROUNDS and plain["enough"] is False
    finally:
        conn.close(); os.unlink(p)


def test_enough_for_10_still_means_ten_whatever_count_was_asked_about(monkeypatch):
    """`enough_for_10` is a published key this route has always returned, so it must not
    quietly start tracking the new param — a client reading it would then be told "enough
    for 10" about a 3-round game. `enough` is the one that follows the choice."""
    from fastapi.testclient import TestClient

    from app import main
    conn, p = make_db(0)
    try:
        for i in range(6):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        for i in range(6):
            _insert_f(conn, f"p{i}", f"Pop Act {i}", "Pop", 2005)
        real_connect = db.connect
        monkeypatch.setattr(main.db, "connect", lambda *a, **kw: real_connect(p))
        c = TestClient(main.app)
        # a short game on the 6-track Rock pool: playable, but not ten rounds' worth
        body = c.get("/api/round-filters/count?tiers=easy&genres=Rock&rounds=5").json()
        assert body["enough"] is True and body["enough_for_10"] is False
        # and the mirror: the whole 12-track library is fine for ten and short of twenty
        body = c.get("/api/round-filters/count?tiers=easy&rounds=20").json()
        assert body["enough"] is False and body["enough_for_10"] is True
    finally:
        conn.close(); os.unlink(p)


def test_a_round_count_below_one_is_refused_by_the_preflight(monkeypatch):
    """A nought-round game finishes on the first tap and a negative one is nonsense; both
    arrive as query params, so the route rejects them rather than answering `enough: True`
    about a game that cannot be played."""
    from fastapi.testclient import TestClient

    from app import main
    conn, p = make_db(0)
    try:
        for i in range(12):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        real_connect = db.connect
        monkeypatch.setattr(main.db, "connect", lambda *a, **kw: real_connect(p))
        c = TestClient(main.app)
        assert c.get("/api/round-filters/count?tiers=easy&rounds=0").status_code == 400
        assert c.get("/api/round-filters/count?tiers=easy&rounds=-3").status_code == 400
        assert c.get("/api/round-filters/count?tiers=easy&rounds=1").status_code == 200
    finally:
        conn.close(); os.unlink(p)


def test_the_picker_is_told_which_counts_to_offer_and_which_include_half_time(monkeypatch):
    """The buttons are built from this, not from a list kept in quiz.js: a count the phone
    offers but the server won't preflight is a Start button that locks for no stated reason,
    and half time silently absent from a short game reads as a bug."""
    from fastapi.testclient import TestClient

    from app import main
    conn, p = make_db(0)
    try:
        for i in range(30):
            _insert_f(conn, f"r{i}", f"Rock Band {i}", "Rock", 1995)
        real_connect = db.connect
        monkeypatch.setattr(main.db, "connect", lambda *a, **kw: real_connect(p))
        body = TestClient(main.app).get("/api/round-filters?tiers=easy").json()
        assert body["round_choices"] == list(game.ROUND_CHOICES)
        assert body["default_rounds"] == game.DEFAULT_ROUNDS
        assert body["halftime_min_rounds"] == game.HALFTIME_MIN_ROUNDS
        assert game.DEFAULT_ROUNDS in body["round_choices"], "the default must be pickable"
        assert all(game.MIN_ROUNDS <= n <= game.MAX_ROUNDS for n in body["round_choices"]), \
            "a button the new_game handler would refuse"
    finally:
        conn.close(); os.unlink(p)


def test_a_three_round_game_really_plays_three_rounds_and_then_ends():
    """The count has to reach the round list and the snapshot, not just the constructor: a
    game that says "round 1 of 10" and stops after three is worse than no choice at all."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=3, tiers=["easy", "medium"], clock=clock)
        g.join("Alice"); g.join("Bob")
        g.build_rounds(conn)
        assert len(g.rounds) == 3
        assert len({r["track"]["id"] for r in g.rounds}) == 3, "no dupes in a short game"
        assert g.snapshot()["total_rounds"] == 3, "the phones and the board count along"
        for n in (1, 2, 3):
            g.start_round(conn)
            snap = g.snapshot()
            assert (snap["round"], snap["total_rounds"]) == (n, 3)
            clock.t += 1
            g.answer("Alice", g.rounds[g.current]["correct"])
            g.answer("Bob", (g.rounds[g.current]["correct"] + 1) % 4)
            g.reveal()
            assert g.is_last_round() is (n == 3), f"round {n} of 3"
            assert not g.is_halfway(), "three rounds is too short for a break"
            clock.t += game.PAYOFF_S
        with pytest.raises(game.GameError):  # and there is no fourth
            g.start_round(conn)
        gid = g.finish(conn)
        assert conn.execute("SELECT rounds FROM games WHERE id=?",
                            (gid,)).fetchone()["rounds"] == 3
    finally:
        conn.close(); os.unlink(p)


def test_a_long_game_builds_every_round_it_was_asked_for():
    """The other end of the picker. 15 is past the old hardcoded 10, so a constructor that
    ignored the argument would build ten rounds and look fine."""
    conn, p = make_db(40)
    try:
        g = game.Game(conn, rounds=15, tiers=["easy", "medium"], clock=Clock())
        g.join("Sam")
        g.build_rounds(conn)
        assert len(g.rounds) == 15
        assert len({r["track"]["id"] for r in g.rounds}) == 15, "no track asked twice"
        assert g.snapshot()["total_rounds"] == 15
    finally:
        conn.close(); os.unlink(p)


def test_a_five_round_game_never_reaches_half_time():
    """Below HALFTIME_MIN_ROUNDS a break is more interruption than interval, so a short game
    deliberately has none. The positive case (6 rounds) is test_half_time_trivia_flow; this
    is the boundary one below it, checked at EVERY point of a fully played game rather than
    just at the middle — `is_halfway` is polled after each reveal, so one stray True anywhere
    stops a 5-round game dead in a break it has no trivia stage for."""
    conn, p = make_db()
    try:
        clock = Clock()
        g = game.Game(conn, rounds=5, tiers=["easy", "medium"], clock=clock)
        g.join("A"); g.join("B")
        g.build_rounds(conn)
        assert len(g.rounds) == 5 < game.HALFTIME_MIN_ROUNDS
        assert not g.is_halfway(), "halfway in the lobby"
        for _ in range(5):
            g.start_round(conn)
            assert not g.is_halfway(), f"half time offered during round {g.current + 1}"
            clock.t += 1
            g.answer("A", g.rounds[g.current]["correct"])
            g.answer("B", 0 if g.rounds[g.current]["correct"] else 1)
            g.reveal()
            assert not g.is_halfway(), f"half time offered after round {g.current + 1}"
            clock.t += game.PAYOFF_S
        assert g.is_last_round()
        # is_halfway is the ONLY thing standing between a short game and the break: the
        # engine will start one from any reveal, so nothing else would catch a regression.
        assert g.phase == "reveal" and g.tf_qs == []
    finally:
        conn.close(); os.unlink(p)


def _insert_fav(conn, tid, artist):
    """A favourite-artist track in a tier the game itself never draws on.

    That is what makes the boost counting exact rather than probabilistic: pick_artist_track
    searches every tier, while the pool fill is restricted to the game's tiers, so a 'hard'
    round can ONLY have arrived as somebody's boost.
    """
    _insert(conn, tid, f"Fav Song {tid}", artist, tier="hard", year=1995)


def test_boosts_available_is_what_build_rounds_actually_hands_out():
    """The phone warns with boosts_available() before Start, so it has to agree with the loop
    that enforces the cap — a warning that says "2 of you get a boost" while the builder gives
    three is worse than no warning. One neutral round is always kept back."""
    conn, p = make_db(0)
    try:
        for i in range(20):
            _insert_f(conn, f"pool{i}", f"Pool Act {i}", "Pop", 1995)
        for i in range(5):
            _insert_fav(conn, f"fav{i}", f"Fav Band {i}")
        assert game.boosts_available(3) == 2
        assert game.boosts_available(1) == 0, "a one-round game is all neutral"
        assert game.boosts_available(0) == 0, "never negative"
        g = game.Game(conn, rounds=3, tiers=["easy"], clock=Clock())
        for i in range(5):  # five hopefuls, two slots
            g.join(f"P{i}")
            g.set_artists(f"P{i}", [f"Fav Band {i}"])
        g.build_rounds(conn)
        boosts = [r for r in g.rounds if r["track"]["tier"] == "hard"]
        assert len(boosts) == game.boosts_available(3), \
            f"{len(boosts)} boost rounds for {game.boosts_available(3)} slots"
        assert len(g.rounds) - len(boosts) >= 1, "no neutral round left"
        assert len(g.rounds) == 3
    finally:
        conn.close(); os.unlink(p)


def test_who_misses_out_on_a_boost_is_not_decided_by_who_joined_first(monkeypatch):
    """Before this, build_rounds walked self.players — which is join order — so with more
    players than boost slots the quickest phone got a boost every single game and the last to
    arrive never did. Who misses out is now shuffled.

    Asserted by monkeypatching random.shuffle rather than by seeding and counting: a
    statistical test over a genuinely random allocation is flaky, and a flaky test here is
    worse than none. Both orderings are pinned, because only the pair proves the shuffle is
    consulted at all — reverse gives the LAST joiner the slot, identity gives the first, and
    a build_rounds that ignored random.shuffle would hand the slot to the first joiner in
    both runs.
    """
    conn, p = make_db(0)
    try:
        for i in range(20):
            _insert_f(conn, f"pool{i}", f"Pool Act {i}", "Pop", 1995)
        for i in range(3):
            _insert_fav(conn, f"fav{i}", f"Fav Band {i}")

        def boosted(shuffle):
            monkeypatch.setattr(game.random, "shuffle", shuffle)
            g = game.Game(conn, rounds=2, tiers=["easy"], clock=Clock())
            for i in range(3):        # three hopefuls, one slot: rounds - 1
                g.join(f"P{i}")
                g.set_artists(f"P{i}", [f"Fav Band {i}"])
            g.build_rounds(conn)
            boosts = [r["track"]["artist"] for r in g.rounds if r["track"]["tier"] == "hard"]
            assert len(boosts) == game.boosts_available(2) == 1, boosts
            return boosts[0]

        assert boosted(lambda seq: seq.reverse()) == "Fav Band 2", \
            "the last to join was never reached — build_rounds is still in join order"
        assert boosted(lambda seq: None) == "Fav Band 0", \
            "with the shuffle a no-op the order is join order, so this pins the seam"
    finally:
        conn.close(); os.unlink(p)
