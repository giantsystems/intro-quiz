import logging
import os
import re
import subprocess
import time

import pytest

from app import config, jobs


@pytest.fixture(autouse=True)
def clean_registry():
    jobs.ACTIONS.clear()
    jobs.JOBS.clear()
    jobs._CURRENT[0] = None
    jobs._CANCEL.clear()   # a leaked flag would abort the next test's job instantly
    yield
    jobs.ACTIONS.clear()
    jobs.JOBS.clear()
    jobs._CURRENT[0] = None
    jobs._CANCEL.clear()   # a leaked flag would abort the next test's job instantly


def wait_done(name, timeout=5.0):
    t0 = time.time()
    while jobs.JOBS[name]["running"]:
        assert time.time() - t0 < timeout, f"{name} never finished"
        time.sleep(0.01)


def test_run_records_summary_and_times():
    jobs.register("ok", "OK", lambda set_stage: {"n": 3})
    started, st = jobs.start("ok")
    assert started
    wait_done("ok")
    j = jobs.JOBS["ok"]
    assert j["summary"] == {"n": 3}
    assert j["error"] is None
    assert j["stage"] == "done"
    assert j["finished_at"] >= j["started_at"]


def test_error_recorded_and_guard_released():
    def boom(set_stage):
        raise RuntimeError("kaput")
    jobs.register("bad", "Bad", boom)
    jobs.register("ok", "OK", lambda set_stage: {})
    started, _ = jobs.start("bad")
    assert started
    wait_done("bad")
    assert jobs.JOBS["bad"]["error"] == "kaput"
    assert jobs.JOBS["bad"]["stage"] == "failed"
    # the failure must release the global guard
    started, _ = jobs.start("ok")
    assert started
    wait_done("ok")


def test_global_one_at_a_time():
    import threading
    gate = threading.Event()
    jobs.register("slow", "Slow", lambda set_stage: (gate.wait(3), {})[1])
    jobs.register("other", "Other", lambda set_stage: {})
    started, _ = jobs.start("slow")
    assert started
    started, st = jobs.start("other")
    assert not started
    assert st["busy_with"] == "slow"
    gate.set()
    wait_done("slow")
    started, _ = jobs.start("other")
    assert started
    wait_done("other")


def test_abort_asks_the_running_job_to_stop_and_it_reports_aborted():
    """The job cooperates by returning early — the common case, since most jobs
    have partial progress worth reporting."""
    import threading
    seen, released = [], threading.Event()

    def loops(set_stage):
        for i in range(1000):
            if jobs.cancelled():
                return {"did": i}          # partial summary, not an exception
            seen.append(i)
            released.set()
            time.sleep(0.01)
        return {"did": "all"}

    jobs.register("loops", "Loops", loops)
    jobs.start("loops")
    released.wait(2)
    asked, name = jobs.abort()
    assert (asked, name) == (True, "loops")
    assert jobs.JOBS["loops"]["abort_requested"] is True
    wait_done("loops")
    j = jobs.JOBS["loops"]
    # returning early must NOT be mistaken for success — that's the whole point
    assert j["stage"] == "aborted"
    assert j["aborted"] is True
    assert j["error"] is None            # stopped on purpose isn't a failure
    assert j["summary"]["did"] < 1000     # it really stopped short


def test_a_job_that_raises_jobaborted_is_stopped_not_failed():
    def raiser(set_stage):
        raise jobs.JobAborted("aborted while scanning")
    jobs.register("raiser", "Raiser", raiser)
    jobs.start("raiser")
    wait_done("raiser")
    j = jobs.JOBS["raiser"]
    assert j["stage"] == "aborted" and j["aborted"] is True
    # must not land in the red "failed" bucket, or /admin cries wolf
    assert j["error"] is None


def test_abort_with_nothing_running_is_refused():
    assert jobs.abort() == (False, None)


def test_the_cancel_flag_does_not_leak_into_the_next_job():
    """Regression guard: clearing on completion instead of on start would let an
    aborted job's flag kill the very next one before it did any work.

    The flag is set directly rather than by aborting a real job — the previous
    run's thread can still be unwinding when the next start() lands, and racing
    to reproduce that would make this test flaky about the thing it's asserting.
    """
    import threading
    ran = threading.Event()
    jobs.register("second", "Second", lambda set_stage: (ran.set(), {"n": 1})[1])
    jobs._CANCEL.set()                     # the state an aborted run leaves behind
    assert jobs.cancelled() is True
    started, _ = jobs.start("second")
    assert started
    wait_done("second")
    assert ran.is_set()
    assert jobs.JOBS["second"]["stage"] == "done"    # NOT "aborted"
    assert jobs.JOBS["second"]["aborted"] is False


def test_a_refused_start_is_409_not_a_cheerful_200():
    """/api/bootstrap and /api/library/retag used to answer HTTP 200 with
    started:false in the body, so `curl -f` in a cron and every casual glance said
    the job had been queued when nothing had. /api/admin/run always got this right."""
    import threading
    from fastapi.testclient import TestClient
    from app import main
    gate = threading.Event()
    # replace the real registrations — no network, no library, no ffmpeg
    jobs.register("bootstrap", "Full pipeline", lambda set_stage: (gate.wait(3), {})[1])
    jobs.register("retag", "Artist tags: preview", lambda set_stage: {})
    jobs.register("clips", "Clip cutting", lambda set_stage: {})
    c = TestClient(main.app)
    try:
        assert c.post("/api/bootstrap").status_code == 200      # first one starts
        for path in ("/api/bootstrap", "/api/admin/run/clips"):
            r = c.post(path)
            assert r.status_code == 409, f"{path} answered {r.status_code}"
            assert "bootstrap" in r.json()["detail"]
    finally:
        gate.set()
        wait_done("bootstrap")


def test_the_abort_endpoint_is_409_when_there_is_nothing_to_stop():
    """So a script can tell "stopped it" from "there was nothing running"."""
    import threading
    from fastapi.testclient import TestClient
    from app import main
    gate = threading.Event()
    jobs.register("clips", "Clip cutting", lambda set_stage: (gate.wait(3), {})[1])
    c = TestClient(main.app)
    r = c.post("/api/admin/abort")
    assert r.status_code == 409 and "no job" in r.json()["detail"]
    jobs.start("clips")
    try:
        r = c.post("/api/admin/abort")
        assert r.status_code == 200 and r.json() == {"aborting": "clips"}
    finally:
        gate.set()
        wait_done("clips")


def test_bootstrap_stops_its_stage_chain_instead_of_grinding_on(monkeypatch, tmp_path):
    """The inner loops stop themselves, but without the between-stage guards an
    abort during sync would still be followed by lastfm, tiers, hygiene and hours
    of clip cutting — the exact complaint that made abort necessary."""
    from app import db as adb, main
    # Capture the real connect FIRST: main.db is the same module object as adb, so
    # patching it and then calling adb.connect would recurse into the patch.
    real_connect = adb.connect
    monkeypatch.setattr(main.db, "connect", lambda *a, **k: real_connect(str(tmp_path / "b.db")))
    stages = []

    def fake_sync(conn, client):
        stages.append("sync")
        jobs._CANCEL.set()          # abort lands during the very first stage
        return {"tracks_active": 3}

    monkeypatch.setattr(main.sync, "sync_library", fake_sync)
    monkeypatch.setattr(main.subsonic, "Client", lambda: object())
    monkeypatch.setattr(main.lastfm, "score_batch",
                        lambda *a, **k: stages.append("lastfm") or {"scored": 0, "remaining": 1})
    monkeypatch.setattr(main.scoring, "assign_tiers", lambda conn: stages.append("tiers") or {})
    monkeypatch.setattr(main.library, "clean", lambda conn: stages.append("hygiene") or {"banned": {}})
    monkeypatch.setattr(main.clips, "sweep", lambda *a, **k: stages.append("clips") or {})
    jobs.register("bootstrap", "Full pipeline", main._job_bootstrap)
    jobs.start("bootstrap")
    wait_done("bootstrap")
    assert stages == ["sync"], f"kept going after the abort: {stages}"
    assert jobs.JOBS["bootstrap"]["stage"] == "aborted"
    assert jobs.JOBS["bootstrap"]["error"] is None


def test_unknown_action_raises():
    with pytest.raises(KeyError):
        jobs.start("nope")


def test_log_capture_attach_and_detach():
    def chatty(set_stage):
        set_stage("working")
        logging.getLogger("app.something").info("hello from the job")
        return {}
    jobs.register("chatty", "Chatty", chatty)
    jobs.start("chatty")
    wait_done("chatty")
    log = "\n".join(jobs.JOBS["chatty"]["log"])
    assert "hello from the job" in log
    assert "working" in log
    # handler detached: nothing new lands after the run
    n = len(jobs.JOBS["chatty"]["log"])
    logging.getLogger("app.something").info("after the run")
    assert len(jobs.JOBS["chatty"]["log"]) == n


def test_a_long_log_keeps_the_LAST_lines_not_the_first():
    """The sink used to keep the FIRST 100 lines and then stop, so the admin page's
    "tail" was the head. On a multi-hour clip sweep the newest line shown was hours
    old the moment the log filled — read as "the job has stalled", and a healthy
    sweep was aborted on that evidence three times, losing ~11 hours of cutting.

    Lines are numbered so first and last are distinguishable: a length-only check
    would pass just as happily with the old first-N sink in place.
    """
    def noisy(set_stage):
        for i in range(200):
            logging.getLogger("app.n").info("line %d", i)
        return {}
    jobs.register("noisy", "Noisy", noisy)
    jobs.start("noisy")
    wait_done("noisy")
    log = jobs.JOBS["noisy"]["log"]
    assert len(log) == jobs.LOG_LINES_MAX
    # Match whole numbers, not substrings: "line 1" is inside "line 100", so an
    # `in` check would call the head the tail and pass with the old sink in place.
    kept = {int(n) for n in re.findall(r"line (\d+)$", "\n".join(log), re.M)}
    assert kept == set(range(100, 200)), f"kept the wrong lines: {sorted(kept)[:5]}…"


def test_a_truncated_log_says_so_when_the_api_serves_it():
    """Bounded FIFO means old lines vanish silently. Without a marker a reader
    can't tell a truncated log from a short one — and the whole point of this
    change is that the log stops implying things it doesn't know."""
    def noisy(set_stage):
        for i in range(150):
            logging.getLogger("app.n").info("line %d", i)
        return {}
    jobs.register("noisy", "Noisy", noisy)
    jobs.start("noisy")
    wait_done("noisy")
    served = jobs.status()["jobs"]["noisy"]["log"]
    assert "dropped" in served[0] and "50" in served[0], served[0]
    # leading, not trailing: a note after the last line reads as "it stopped here"
    assert "line 199" not in served[0]
    assert "line 149" in served[-1]


def test_a_short_log_is_served_clean_with_no_truncation_marker():
    """The marker must not appear on every run, or it stops meaning anything."""
    jobs.register("quiet", "Quiet",
                  lambda set_stage: logging.getLogger("app.n").info("just the one") or {})
    jobs.start("quiet")
    wait_done("quiet")
    served = jobs.status()["jobs"]["quiet"]["log"]
    assert not any("dropped" in line for line in served), served


def test_the_served_log_is_a_json_array_not_a_deque():
    """`log` is a deque on the job record and /admin renders it with .join().
    Handing FastAPI's encoder the deque itself worked by luck; a plain list at
    the boundary is the contract, and TestClient proves the JSON shape."""
    import threading
    from fastapi.testclient import TestClient
    from app import main
    gate = threading.Event()

    def chatty(set_stage):
        logging.getLogger("app.n").info("hello from the running job")
        gate.wait(3)
        return {}

    jobs.register("clips", "Clip cutting", chatty)
    c = TestClient(main.app)
    jobs.start("clips")
    try:
        # polled MID-RUN, as /admin does every couple of seconds
        while not jobs.JOBS["clips"]["log"]:
            time.sleep(0.01)
        body = c.get("/api/admin/status").json()
        log = body["jobs"]["clips"]["log"]
        assert isinstance(log, list)
        assert any("hello from the running job" in line for line in log)
        assert "log_dropped" in body["jobs"]["clips"]   # discoverable, not hidden
    finally:
        gate.set()
        wait_done("clips")


def test_the_clips_job_passes_set_stage_to_the_sweep(monkeypatch):
    """_job_clips accepted the callback and dropped it, so `stage` stayed on
    "starting" for the sweep's whole multi-hour run. That frozen field is what a
    healthy sweep was judged wedged on, three times.

    The sweep itself is faked: this asserts the wiring, which is the bit that was
    missing — tests/test_clips.py covers what the sweep says.
    """
    from app import main
    got = {}

    def fake_sweep(max_hours=0, set_stage=None):
        got["callback"] = set_stage
        set_stage("cutting — 7 cut this session, 3 to go")
        return {"cut": 7, "stopped": "done"}

    monkeypatch.setattr(main.clips, "sweep", fake_sweep)
    jobs.register("clips", "Clip cutting", main._job_clips)
    jobs.start("clips")
    wait_done("clips")
    assert got["callback"] is not None, "the sweep was handed no progress callback"
    # and what it said actually reached the record the admin page reads
    j = jobs.JOBS["clips"]
    assert j["stage"] == "done"          # resolved by _run_wrapped, as before
    assert any("7 cut this session" in line for line in j["log"]), list(j["log"])


def test_a_progress_tick_moves_the_stage_without_logging_it():
    """The sweep ticks once per clip — thousands per session. Logging each would
    flush the 100-line tail of the warnings that matter, so a tick moves `stage`
    and stays out of the log; the per-batch lines carry the story there.

    `stage` is read MID-RUN, because _run_wrapped overwrites it with done/aborted
    the moment the job returns — checking afterwards would prove nothing.
    """
    import threading
    ticked, release = threading.Event(), threading.Event()

    def ticker(set_stage):
        set_stage("batch done")                                  # logged
        set_stage("cutting — 2 cut this session, 98 to go", log=False)   # not
        ticked.set()
        release.wait(3)
        return {}

    jobs.register("ticks", "Ticks", ticker)
    jobs.start("ticks")
    try:
        assert ticked.wait(3)
        # the quiet tick still won the field /admin displays
        assert jobs.JOBS["ticks"]["stage"] == "cutting — 2 cut this session, 98 to go"
    finally:
        release.set()
        wait_done("ticks")
    log = "\n".join(jobs.JOBS["ticks"]["log"])
    assert "batch done" in log
    assert "2 cut this session" not in log, "a per-clip tick must not spend a log line"


def _health_db(path):
    """A library mid-sweep: every tier represented, some clipped, some not.

    Tier counts are all DIFFERENT and none is the sum of the others, so a field
    wired to the wrong tier can't accidentally read correct. The three rows at the
    end are the exclusions QUIZZABLE applies, so tracks_playable_all_tiers is not
    simply "every row with a tier".
    """
    from app import db as adb
    conn = adb.connect(path)
    rows = []
    # clipped and playable: easy 2, medium 3, hard 4, tiebreak 5
    for tier, n in (("easy", 2), ("medium", 3), ("hard", 4), ("tiebreak", 5)):
        for i in range(n):
            rows.append((f"{tier}{i}", tier, 200, "2026-01-01", 0))
    # tiered but not yet cut — the sweep's backlog (2 easy, 1 hard)
    for tier, n in (("easy", 2), ("hard", 1)):
        for i in range(n):
            rows.append((f"{tier}-pending{i}", tier, 200, None, 0))
    # excluded from BOTH counts, for three different reasons
    rows.append(("banned", "easy", 200, "2026-01-01", 1))    # banned
    rows.append(("tooshort", "easy", 9, "2026-01-01", 0))    # under MIN_DURATION_S
    rows.append(("untiered", None, 200, "2026-01-01", 0))    # never scored
    conn.executemany(
        "INSERT INTO tracks(id,title,artist,duration,tier,clipped_at,banned,active) "
        "VALUES(?,'t','a',?,?,?,?,1)",
        [(r[0], r[2], r[1], r[3], r[4]) for r in rows])
    conn.commit()
    conn.close()


def test_health_reports_every_tier_because_the_sweep_cuts_every_tier(monkeypatch, tmp_path):
    """tracks_playable counts easy+medium only, so it can sit still through hours of
    real cutting — hard and tiebreak are most of a real library. A watcher reading
    only that key concluded the sweep was wedged and it was killed.

    So /health now also answers per tier, and clips_remaining answers the question
    directly. tracks_playable's own meaning is asserted UNCHANGED below: external
    watchers read it, and ready_to_play is the same population.
    """
    from fastapi.testclient import TestClient
    from app import db as adb, main
    path = str(tmp_path / "h.db")
    _health_db(path)
    real_connect = adb.connect     # main.db IS this module — grab it before patching
    monkeypatch.setattr(main.db, "connect", lambda *a, **k: real_connect(path))
    body = TestClient(main.app).get("/health").json()

    assert body["playable_by_tier"] == {"easy": 2, "medium": 3, "hard": 4, "tiebreak": 5}
    assert body["tracks_playable_all_tiers"] == 14
    # the backlog the cutter's next batch will draw from: 2 easy + 1 hard, and
    # NOT the banned/too-short/untiered rows, which no batch will ever pick up
    assert body["clips_remaining"] == 3

    # --- the promise to external watchers: still easy+medium, still nothing else
    assert body["tracks_playable"] == 5
    assert body["ready_to_play"] is False        # 5 < 10, same threshold as before
    assert body["tracks_synced"] == 20           # every active row: 14 + 3 + 3
    assert body["tracks_tiered"] == 19           # ...all but the untiered one


def test_health_survives_an_unreadable_db_without_the_new_fields(monkeypatch):
    """health must never 500 — it's the liveness probe. The per-tier queries are
    three more chances to throw, so the guard is worth re-asserting around them."""
    from fastapi.testclient import TestClient
    from app import main

    def boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(main.db, "connect", boom)
    r = TestClient(main.app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ready_to_play"] is False and "disk gone" in body["message"]
    # partial data is worse than none: a watcher must not read a missing tier as 0
    assert "playable_by_tier" not in body


def test_check_token(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "")
    assert jobs.check_token("")
    assert jobs.check_token("anything")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "s3cret")
    assert jobs.check_token("s3cret")
    assert not jobs.check_token("")
    assert not jobs.check_token("wrong")


def test_bootstrap_compat_shape():
    jobs.register("bootstrap", "Full pipeline",
                  lambda set_stage: {"tracks_synced": 5, "clips_cut": 2})
    assert jobs.bootstrap_compat() == {}  # no run yet -> /health omits the key
    jobs.start("bootstrap")
    wait_done("bootstrap")
    compat = jobs.bootstrap_compat()
    assert compat["running"] is False
    assert compat["stage"] == "done"
    assert compat["tracks_synced"] == 5
    assert compat["clips_cut"] == 2
    assert "error" not in compat


def test_admin_smoke_js():
    """Drive admin.js in the stub DOM against idle/running/failed snapshots."""
    if not __import__("shutil").which("node"):
        if os.environ.get("CI"):
            pytest.fail("node required in CI — the admin render smoke must not silently skip")
        pytest.skip("node not installed")
    root = os.path.dirname(os.path.dirname(__file__))
    r = subprocess.run(["node", os.path.join(root, "tests", "js", "admin_smoke.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"admin smoke failed:\n{r.stdout}\n{r.stderr}"


def test_trivia_stats_and_reset_semantics(tmp_path):
    from app import db as adb, main
    conn = adb.connect(str(tmp_path / "t.db"))
    conn.executemany(
        "INSERT INTO trivia(kind, text, answer, source, used_at) VALUES(?,?,?,'seed',?)",
        [("fact", "f1", None, "2026-07-01"), ("fact", "f2", None, None),
         ("tf", "q1", 1, "2026-07-02"), ("tf", "q2", 0, None), ("tf", "q3", 1, None)])
    conn.commit()
    s = main._trivia_stats(conn)
    assert s["fact"] == {"total": 2, "played": 1, "left": 1}
    assert s["tf"] == {"total": 3, "played": 1, "left": 2}
    # the /admin reset: everything fresh, nothing deleted
    n = conn.execute("UPDATE trivia SET used_at=NULL WHERE used_at IS NOT NULL").rowcount
    conn.commit()
    assert n == 2
    s = main._trivia_stats(conn)
    assert s["fact"]["left"] == 2 and s["tf"]["left"] == 3
    assert s["fact"]["total"] == 2 and s["tf"]["total"] == 3


def test_llm_prompt_parameterised():
    from app import trivia
    p = trivia.llm_prompt("Canada", facts=10, tf=20)
    assert "10 facts and 20 true/false" in p
    assert "Canada" in p
    assert "<YOUR REGION" in trivia.llm_prompt("")  # blank keeps the placeholder
    assert "500 facts" in trivia.llm_prompt("X", facts=9999)  # clamped


def test_parse_pack_tolerates_llm_wrapping():
    from app import trivia
    items = [{"kind": "fact", "text": "A."}, {"kind": "tf", "text": "B?", "answer": 0}]
    import json as j
    raw = j.dumps(items)
    assert trivia.parse_pack(raw) == items
    assert trivia.parse_pack(f"Sure! Here you go:\n```json\n{raw}\n```\nEnjoy!") == items
    with pytest.raises(ValueError):
        trivia.parse_pack("no json here")
    with pytest.raises(ValueError):
        trivia.parse_pack('{"kind": "fact"}')  # object, not a list
    with pytest.raises(ValueError):
        trivia.parse_pack("")


def test_insert_items_reports_and_is_idempotent(tmp_path):
    from app import db as adb, trivia
    conn = adb.connect(str(tmp_path / "t.db"))
    pack = [{"kind": "fact", "text": "A."},
            {"kind": "tf", "text": "B?", "answer": 1},
            {"kind": "tf", "text": "no answer"},          # reject
            {"kind": "nope", "text": "bad kind"},          # reject
            "not an object"]                               # reject
    r = trivia.insert_items(conn, pack, "import")
    assert r["added"] == 2 and r["duplicates"] == 0 and len(r["rejects"]) == 3
    # imported items are never-played and source=import
    rows = conn.execute("SELECT source, used_at FROM trivia").fetchall()
    assert all(x["source"] == "import" and x["used_at"] is None for x in rows)
    # re-import: everything is a duplicate, nothing added
    r = trivia.insert_items(conn, pack[:2], "import")
    assert r["added"] == 0 and r["duplicates"] == 2
