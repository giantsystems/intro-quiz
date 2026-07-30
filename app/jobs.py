"""Maintenance job registry for the admin page (#52).

One background job at a time, globally: every action contends for the same
sqlite DB (and clips for ffmpeg/CPU), and the full pipeline already chains
most of them — parallel runs would just fight. Status is in-memory only;
after a restart the page honestly says "no runs since restart".
"""
import logging
import threading
import time
from typing import Callable

from . import config

LOGGER = logging.getLogger(__name__)

LOG_LINES_MAX = 100

ACTIONS: dict[str, dict] = {}   # name -> {"label": str, "run": callable}
JOBS: dict[str, dict] = {}      # name -> status dict (see _fresh_status)
_LOCK = threading.Lock()
_CURRENT: list = [None]         # [name] of the running job, or [None]
_CANCEL = threading.Event()     # set by abort(); cleared when a job starts


class JobAborted(Exception):
    """Raised by job code that notices cancelled() mid-run.

    A job may also just RETURN early with a partial summary — both count as
    aborted. Raising is for the deep call sites where returning something
    summary-shaped would be a lie.
    """


def abort() -> tuple[bool, str | None]:
    """Ask the running job to stop. Returns (asked, name).

    COOPERATIVE, and it cannot be otherwise: the work is ffmpeg subprocesses,
    HTTP fetches and sqlite writes on a daemon thread, and killing that thread
    mid-write is how you get a half-cut clip recorded as done or a corrupt DB.
    So this sets a flag the loops check between items, and the job stops at the
    next safe boundary — usually under a second, but a clip cut in flight
    finishes first (a ~10s download is the worst case).

    Every job is resumable by design (each step only processes what's missing),
    so an abort loses no completed work.
    """
    with _LOCK:
        name = _CURRENT[0]
        if name is None:
            return False, None
        _CANCEL.set()
        JOBS[name]["abort_requested"] = True
    LOGGER.warning("%s: abort requested", name)
    return True, name


def cancelled() -> bool:
    """For job code to poll between items. Cheap — an Event flag read."""
    return _CANCEL.is_set()


def register(name: str, label: str, run: Callable[[Callable[[str], None]], dict]) -> None:
    """`run` receives a set_stage(str) callback and returns a summary dict."""
    ACTIONS[name] = {"label": label, "run": run}


def check_token(header_value: str) -> bool:
    """Unset ADMIN_PASSWORD = open (existing cron curls keep working)."""
    return not config.ADMIN_PASSWORD or header_value == config.ADMIN_PASSWORD


class RunLogHandler(logging.Handler):
    """Captures the run's log lines for the admin page. Attached to the root
    logger only while a job runs — safe because jobs never overlap."""

    def __init__(self, sink: list):
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record):
        if len(self.sink) < LOG_LINES_MAX:
            try:
                self.sink.append(self.format(record))
            except Exception:  # noqa: BLE001 — logging must never break the job
                pass
        elif len(self.sink) == LOG_LINES_MAX:
            self.sink.append("… (output capped)")


def _fresh_status() -> dict:
    return {"running": True, "stage": "starting", "started_at": time.time(),
            "finished_at": None, "summary": None, "error": None, "log": [],
            "abort_requested": False, "aborted": False}


def start(name: str) -> tuple[bool, dict]:
    """Start a job. Returns (started, status) — status is the RUNNING job's
    record when refused, so the caller can say who's hogging the server."""
    if name not in ACTIONS:
        raise KeyError(name)
    with _LOCK:
        if _CURRENT[0] is not None:
            return False, {"busy_with": _CURRENT[0], **JOBS.get(_CURRENT[0], {})}
        # Cleared HERE rather than at the end of the aborted job: a stale flag
        # would abort the next job instantly, and the previous run's thread may
        # still be unwinding when this one starts.
        _CANCEL.clear()
        _CURRENT[0] = name
        JOBS[name] = _fresh_status()
    threading.Thread(target=_run_wrapped, args=(name,), daemon=True).start()
    return True, JOBS[name]


def _run_wrapped(name: str) -> None:
    job = JOBS[name]

    def set_stage(stage: str) -> None:
        job["stage"] = stage
        LOGGER.info("%s: %s", name, stage)

    handler = RunLogHandler(job["log"])
    root = logging.getLogger()
    old_level = root.level
    if root.getEffectiveLevel() > logging.INFO:
        root.setLevel(logging.INFO)  # capture INFO progress lines even if root is quieter
    root.addHandler(handler)
    try:
        job["summary"] = ACTIONS[name]["run"](set_stage)
        # An aborted job usually RETURNS its partial summary rather than raising,
        # so "did we stop early" is decided by the flag, not by the exception path.
        # Otherwise a cancelled sweep would report a cheerful "done".
        job["stage"] = "aborted" if cancelled() else "done"
        job["aborted"] = cancelled()
    except JobAborted as e:
        LOGGER.warning("%s aborted: %s", name, e)
        job["stage"] = "aborted"
        job["aborted"] = True
    except Exception as e:  # noqa: BLE001 — status must always resolve
        LOGGER.exception("%s failed", name)
        job["error"] = str(e)
        job["stage"] = "failed"
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        job["running"] = False
        job["finished_at"] = time.time()
        with _LOCK:
            _CURRENT[0] = None


def status() -> dict:
    return {"current": _CURRENT[0],
            "actions": {n: {"label": a["label"]} for n, a in ACTIONS.items()},
            "jobs": {n: dict(j) for n, j in JOBS.items()}}


def bootstrap_compat() -> dict:
    """The old BOOTSTRAP dict shape for /health — external watchers rely on it."""
    j = JOBS.get("bootstrap")
    if not j:
        return {}
    out = {"running": j["running"], "stage": j["stage"]}
    if j["error"]:
        out["error"] = j["error"]
    for k, v in (j["summary"] or {}).items():
        out[k] = v
    return out
