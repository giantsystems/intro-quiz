import logging
import os
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


def test_log_capped():
    def noisy(set_stage):
        for i in range(300):
            logging.getLogger("app.n").info("line %d", i)
        return {}
    jobs.register("noisy", "Noisy", noisy)
    jobs.start("noisy")
    wait_done("noisy")
    assert len(jobs.JOBS["noisy"]["log"]) == jobs.LOG_LINES_MAX + 1
    assert jobs.JOBS["noisy"]["log"][-1] == "… (output capped)"


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
