import os
import shutil
import subprocess
import tempfile

import pytest

from app import clips, db

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")


class FakeClient:
    """Stands in for the Subsonic client: 'downloads' a locally generated tone."""
    def __init__(self, src):
        self.src = src

    def download(self, song_id, dest_path):
        if song_id == "broken":
            with open(dest_path, "wb") as f:
                f.write(b"not audio at all")
            return
        shutil.copy(self.src, dest_path)

    def download_transcoded(self, song_id, dest_path):
        # the transcode fallback can't rescue this one either
        self.download(song_id, dest_path)


def probe_duration(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


@pytest.fixture()
def tone(tmp_path):
    src = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=40", "-codec:a", "libmp3lame",
                    str(src)], check=True)
    return str(src)


def make_db(tracks):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = db.connect(path)
    for t in tracks:
        conn.execute(
            "INSERT INTO tracks(id,title,artist,duration,tier,intro_offset,global_listeners,active) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (t["id"], t.get("title", "t"), t.get("artist", "a"), t.get("duration", 40),
             t.get("tier", "easy"), t.get("intro_offset", 0), t.get("global_listeners", 100)))
    conn.commit()
    return conn, path


def test_cut_batch_produces_clips_and_marks(tone, tmp_path):
    conn, p = make_db([{"id": "ok1"}, {"id": "broken"}, {"id": "untiered", "tier": None}])
    conn.execute("UPDATE tracks SET tier=NULL WHERE id='untiered'")
    conn.commit()
    clips_dir = str(tmp_path / "clips")
    try:
        r = clips.cut_batch(conn, FakeClient(tone), clips_dir=clips_dir)
        assert r["cut"] == 1 and r["errors"] == 1
        assert r["remaining"] == 0  # undecodable source = banned, not endlessly retried
        assert conn.execute("SELECT banned FROM tracks WHERE id='broken'").fetchone()["banned"] == 1
        for length in (5, 10, 20):
            f = os.path.join(clips_dir, "ok1", f"{length}.mp3")
            assert abs(probe_duration(f) - length) < 0.6, f
        assert os.path.exists(os.path.join(clips_dir, "ok1", "payoff.mp3"))
        # failed track: no leftover dir, not marked as clipped
        assert not os.path.exists(os.path.join(clips_dir, "broken"))
        marks = {x["id"]: x["clipped_at"] for x in conn.execute("SELECT id, clipped_at FROM tracks")}
        assert marks["ok1"] and not marks["broken"] and not marks["untiered"]
        # second run: nothing left at all (banned track excluded)
        r2 = clips.cut_batch(conn, FakeClient(tone), clips_dir=clips_dir)
        assert r2["cut"] == 0 and r2["errors"] == 0
    finally:
        os.unlink(p)


def test_intro_offset_respected(tone, tmp_path):
    conn, p = make_db([{"id": "off", "intro_offset": 8, "duration": 40}])
    clips_dir = str(tmp_path / "clips")
    try:
        clips.cut_batch(conn, FakeClient(tone), clips_dir=clips_dir)
        # 20s clip starting at 8s of a 40s file → full 20s available
        assert abs(probe_duration(os.path.join(clips_dir, "off", "20.mp3")) - 20) < 0.6
    finally:
        os.unlink(p)


def test_cut_batch_reports_after_every_track_including_the_failures(monkeypatch, tmp_path):
    """A batch is up to an hour of downloading, so a caller watching progress needs
    a tick per track. Failures must tick too: a run where every track errors would
    otherwise look completely silent, which is exactly the "is it wedged?" question.

    cut_track is faked — this is the loop and its reporting, not ffmpeg.
    """
    conn, p = make_db([{"id": "t1"}, {"id": "bad"}, {"id": "t3"}])
    seen = []

    def fake_cut(client, row, clips_dir):
        if row["id"] == "bad":
            raise clips.ClipError("undecodable")
        return 0

    monkeypatch.setattr(clips, "cut_track", fake_cut)
    try:
        r = clips.cut_batch(conn, object(), limit=10, clips_dir=str(tmp_path),
                            on_progress=lambda cut, errors: seen.append((cut, errors)))
    finally:
        os.unlink(p)
    assert r["cut"] == 2 and r["errors"] == 1
    # the running totals as each track lands: the middle one is the banned failure
    assert seen == [(1, 0), (1, 1), (2, 1)]


class DummyConn:
    """Stands in for db.connect() in sweep tests — answers the tiered-count query."""
    tiered = 100

    def execute(self, *a, **k):
        class R:
            def fetchone(_self):
                return {"c": DummyConn.tiered}
        return R()

    def close(self):
        pass


def test_sweep_explains_fresh_install(monkeypatch):
    """First boot with nothing tiered: say WHY nothing was cut, don't claim 'complete'."""
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    monkeypatch.setattr(DummyConn, "tiered", 0)
    try:
        out = clips.sweep(stall_sleep_s=0)
    finally:
        monkeypatch.setattr(DummyConn, "tiered", 100)
    assert out == {"cut": 0, "stopped": "nothing-tiered"}


def test_sweep_runs_to_done_and_survives_stalls(monkeypatch):
    """The CLIP_SWEEP_ON_START bootstrap batches to zero and backs off on stalls."""
    results = [
        Exception("navidrome down"),          # transient failure -> back off
        {"cut": 0, "errors": 5, "remaining": 10},   # all-error batch -> back off
        {"cut": 8, "errors": 2, "remaining": 2},
        {"cut": 2, "errors": 0, "remaining": 0},    # done
    ]

    def fake_batch(conn, client, limit=100, on_progress=None):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0)
    assert out == {"cut": 10, "stopped": "done"}
    assert not results  # consumed everything


def test_sweep_gives_up_after_max_stalls(monkeypatch):
    def always_fails(conn, client, limit=100, on_progress=None):
        raise Exception("still down")

    monkeypatch.setattr(clips, "cut_batch", always_fails)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0, max_stalls=3)
    assert out == {"cut": 0, "stopped": "stalled"}


def test_sweep_stops_when_the_job_is_aborted(monkeypatch):
    """Abort is what makes a multi-hour sweep interruptible. It must stop between
    batches and report what it got, since the sweep resumes where it left off."""
    from app import jobs
    calls = []

    def fake_batch(conn, client, limit=100, on_progress=None):
        calls.append(1)
        if len(calls) == 2:
            jobs._CANCEL.set()      # aborted mid-run, as /api/admin/abort would
        # 'remaining' never reaches 0, so only the abort can end this sweep. The
        # bail-out keeps a broken abort a FAILURE rather than a hung test run.
        assert len(calls) < 50, "sweep ignored the abort and kept batching"
        return {"cut": 5, "errors": 0, "remaining": 9999}

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    try:
        out = clips.sweep(stall_sleep_s=0)
    finally:
        jobs._CANCEL.clear()
    assert out == {"cut": 10, "stopped": "aborted"}
    assert len(calls) == 2      # stopped at the boundary, didn't grind on


def test_sweep_stage_moves_between_batches(monkeypatch):
    """`stage` is the only progress signal /admin has for a multi-hour sweep, and
    it used to read "starting" for the entire run because _job_clips dropped the
    callback. On that evidence a healthy sweep was declared wedged three times and
    aborted, costing ~11 hours of cutting.

    Two batches, so this asserts the stage MOVES rather than merely differing from
    "starting" — a callback fired once at the top would satisfy that weaker check
    while leaving the number frozen for the rest of the session.
    """
    results = [{"cut": 8, "errors": 0, "remaining": 12},
               {"cut": 12, "errors": 0, "remaining": 0}]
    stages = []

    def fake_batch(conn, client, limit=100, on_progress=None):
        stages.append(("batch", None))       # marks where in the sequence we are
        return results.pop(0)

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0,
                      set_stage=lambda text, log=True: stages.append(("stage", text)))
    assert out == {"cut": 20, "stopped": "done"}
    said = [t for kind, t in stages if kind == "stage"]
    assert said, "the sweep reported no progress at all"
    # the counts really advance: 8 cut after the first batch, and the backlog falls
    assert len(set(said)) > 1, f"stage never changed: {said}"
    assert any("0 cut" in s for s in said)       # before any batch ran
    assert any("8 cut" in s for s in said)       # after the first
    # ...and it was said BETWEEN the batches, not all of it at the end
    first_batch = stages.index(("batch", None))
    second_batch = stages.index(("batch", None), first_batch + 1)
    between = [t for kind, t in stages[first_batch + 1:second_batch] if kind == "stage"]
    assert any("8 cut" in t for t in between), f"nothing reported between batches: {between}"


def test_sweep_reports_progress_per_clip_not_just_per_batch(monkeypatch):
    """A batch is 100 downloads — up to an hour. Reporting only per batch leaves
    `stage` frozen for that hour, which is indistinguishable from wedged.

    The per-clip ticks must NOT log: thousands of them would flush the 100-line
    tail (and `docker logs`) of the warnings worth reading.
    """
    def fake_batch(conn, client, limit=100, on_progress=None):
        for i in range(1, 4):
            on_progress(i, 0)
        return {"cut": 3, "errors": 0, "remaining": 0}

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    said = []
    clips.sweep(stall_sleep_s=0,
                set_stage=lambda text, log=True: said.append((text, log)))
    ticks = [t for t, log in said if not log]
    assert len(ticks) == 3, f"expected one tick per clip: {said}"
    assert ticks[0] != ticks[-1], f"ticks never moved: {ticks}"
    # DummyConn reports 100 pending, so the countdown is visible in the text
    assert "1 cut this session, 99 to go" in ticks[0]
    assert "3 cut this session, 97 to go" in ticks[-1]


def test_a_stalled_sweep_says_it_is_backing_off(monkeypatch):
    """The one state that genuinely looks like a stall. Saying so beats a frozen
    "cutting", which is what got read as wedged — the operator needs to know the
    sweep is waiting on Navidrome, not stuck."""
    monkeypatch.setattr(clips, "cut_batch",
                        lambda conn, client, limit=100, on_progress=None:
                        (_ for _ in ()).throw(Exception("down")))
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    said = []
    out = clips.sweep(stall_sleep_s=0, max_stalls=3,
                      set_stage=lambda text, log=True: said.append(text))
    assert out["stopped"] == "stalled"
    backoffs = [t for t in said if "backing off" in t]
    assert len(backoffs) == 2, f"expected one per backoff before giving up: {said}"
    assert "attempt 1 of 3" in backoffs[0] and "attempt 2 of 3" in backoffs[1]


def test_an_aborted_sweep_reports_aborted_and_the_progress_it_made(monkeypatch):
    """Progress plumbing must not swallow the abort path. A cancelled sweep still
    reports "aborted" — never a cheerful "done" — and the stage it leaves behind
    must show the work it did, because that work is kept and resumed."""
    from app import jobs
    calls = []

    def fake_batch(conn, client, limit=100, on_progress=None):
        calls.append(1)
        on_progress(4, 0)
        if len(calls) == 2:
            jobs._CANCEL.set()      # as /api/admin/abort would, mid-session
        assert len(calls) < 50, "sweep ignored the abort and kept batching"
        return {"cut": 4, "errors": 0, "remaining": 9999}

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    said = []
    try:
        out = clips.sweep(stall_sleep_s=0,
                          set_stage=lambda text, log=True: said.append(text))
    finally:
        jobs._CANCEL.clear()
    assert out == {"cut": 8, "stopped": "aborted"}
    assert len(calls) == 2                      # stopped at the boundary
    assert any("4 cut" in t for t in said)      # first batch's progress reported
    assert any("8 cut" in t for t in said)      # and the second's, before stopping


def test_sweep_wakes_early_from_a_stall_backoff_when_aborted(monkeypatch):
    """The backoff is 10 minutes in production. Sleeping through it would leave
    Abort looking broken for exactly as long as the server was already stuck.

    The abort has to land WHILE the sweep is waiting: setting the flag up front
    means the top-of-loop check returns first and _abort_wait is never reached, so
    the test would pass with a plain time.sleep in there. That's the version of
    this test that failed to catch a deliberately reverted fix.
    """
    from app import jobs
    monkeypatch.setattr(clips, "cut_batch",
                        lambda conn, client, limit=100, on_progress=None: (_ for _ in ()).throw(Exception("down")))
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    # Stands in for the operator hitting Abort a moment into the backoff. Also
    # bounds the test: a non-interruptible wait would sleep 6 × 300s for real.
    slept = []

    def fake_sleep(s):
        slept.append(s)
        jobs._CANCEL.set()

    monkeypatch.setattr(clips.time, "sleep", fake_sleep)
    try:
        out = clips.sweep(stall_sleep_s=300, max_stalls=6)
    finally:
        jobs._CANCEL.clear()
    assert out["stopped"] == "aborted"
    # One 0.5s tick, not a 300s block: the wait polls and bails out. A plain
    # time.sleep(stall_sleep_s) shows up here as a single 300.
    assert slept and max(slept) <= 1, f"slept in one long block: {slept}"


def test_cut_batch_stops_between_tracks_not_mid_track(monkeypatch, tmp_path):
    """Granularity matters: a clip dir half-written but marked clipped_at would be
    an unplayable round forever. So the check sits between tracks.

    cut_track is faked, so this needs no ffmpeg — it's the loop under test.
    """
    from app import jobs
    conn, _ = make_db([{"id": "t1"}, {"id": "t2"}, {"id": "t3"}])
    cut = []

    def fake_cut(client, row, clips_dir):
        cut.append(row["id"])
        jobs._CANCEL.set()      # abort lands while the FIRST track is in flight
        return 0

    monkeypatch.setattr(clips, "cut_track", fake_cut)
    try:
        r = clips.cut_batch(conn, object(), limit=10, clips_dir=str(tmp_path))
    finally:
        jobs._CANCEL.clear()
    assert len(cut) == 1                      # the in-flight track finished
    assert r["cut"] == 1
    # and it was recorded as done — an interrupted batch must not lose the work
    assert conn.execute("SELECT COUNT(*) c FROM tracks WHERE clipped_at IS NOT NULL"
                        ).fetchone()["c"] == 1


def test_sweep_respects_time_limit(monkeypatch):
    """CLIP_SWEEP_MAX_HOURS: stops cleanly at the deadline, resumes next start."""
    ticker = iter(range(0, 100000, 1800))  # each clock() call advances 30 min

    def fake_batch(conn, client, limit=100, on_progress=None):
        return {"cut": 100, "errors": 0, "remaining": 5000}  # never finishes

    monkeypatch.setattr(clips, "cut_batch", fake_batch)
    monkeypatch.setattr(clips.db, "connect", lambda *a, **k: DummyConn())
    monkeypatch.setattr(clips.subsonic, "Client", lambda: object())
    out = clips.sweep(stall_sleep_s=0, max_hours=1, clock=lambda: next(ticker))
    assert out["stopped"] == "time-limit"
    assert out["cut"] > 0  # got at least one batch in before the cap


@pytest.fixture()
def quiet_intro_tone(tmp_path):
    """8 seconds of near-silence, then tone — the metal/ambient intro problem."""
    src = tmp_path / "quiet.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=60",
                    "-af", "adelay=8000|8000",
                    "-codec:a", "libmp3lame", str(src)], check=True)
    return str(src)


def test_detect_intro_offset(tone, quiet_intro_tone):
    assert clips.detect_intro_offset(tone) == 0.0          # audible from the start
    off = clips.detect_intro_offset(quiet_intro_tone)
    assert 6.5 <= off <= 9.5, off                          # skips the quiet opening


def test_cut_batch_persists_detected_offset(quiet_intro_tone, tmp_path):
    conn, p = make_db([{"id": "q1", "duration": 68}])
    clips_dir = tmp_path / "clips"
    try:
        r = clips.cut_batch(conn, FakeClient(quiet_intro_tone), limit=5, clips_dir=str(clips_dir))
        assert r["cut"] == 1
        row = conn.execute("SELECT intro_offset, clipped_at FROM tracks WHERE id='q1'").fetchone()
        assert 6.5 <= row["intro_offset"] <= 9.5
        assert row["clipped_at"] is not None
        # the 5s clip must contain audio, not the quiet opening
        assert probe_duration(str(clips_dir / "q1" / "5.mp3")) >= 4.5
    finally:
        os.unlink(p)
