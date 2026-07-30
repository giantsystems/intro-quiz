"""Maintenance job registry for the admin page (#52).

One background job at a time, globally: every action contends for the same
sqlite DB (and clips for ffmpeg/CPU), and the full pipeline already chains
most of them — parallel runs would just fight. Status is in-memory only;
after a restart the page honestly says "no runs since restart".
"""
import collections
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
    logger only while a job runs — safe because jobs never overlap.

    The sink is a bounded FIFO, so what survives is the TAIL. It used to keep the
    first LOG_LINES_MAX lines and then stop, which made the admin page's "last
    line" an hours-old line from the start of the run — a healthy multi-hour clip
    sweep was declared wedged and aborted three times on that evidence.
    """

    def __init__(self, job: dict):
        super().__init__(level=logging.INFO)
        self.job = job
        self.sink = job["log"]
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            if len(self.sink) == self.sink.maxlen:
                # the deque is about to push the oldest line out — count it, so a
                # reader can tell a truncated log from a short one (see status())
                self.job["log_dropped"] += 1
            self.sink.append(self.format(record))
        except Exception:  # noqa: BLE001 — logging must never break the job
            pass


def _fresh_status() -> dict:
    return {"running": True, "stage": "starting", "started_at": time.time(),
            "finished_at": None, "summary": None, "error": None,
            "log": collections.deque(maxlen=LOG_LINES_MAX), "log_dropped": 0,
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

    def set_stage(stage: str, log: bool = True) -> None:
        """`log=False` for high-frequency progress ticks.

        The clip sweep ticks once per clip — thousands of them over a session. At
        one log line each they would flush the 100-line tail (and `docker logs`)
        of the warnings that actually matter, so a tick moves `stage` and stays
        out of the log; the per-batch lines carry the story there.
        """
        job["stage"] = stage
        if log:
            LOGGER.info("%s: %s", name, stage)

    handler = RunLogHandler(job)
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


def public_status(j: dict) -> dict:
    """One job's record as the API serves it: `log` a plain JSON array, tail last.

    The deque -> list conversion belongs here rather than in the record: the job
    thread wants a bounded sink, the API wants a JSON array, and only the API
    needs the "some lines were dropped" note spelled out for a human reader.
    """
    out = dict(j)
    dropped = j.get("log_dropped", 0)
    # deque.copy() rather than list(...): /admin polls this while the job is still
    # logging, and iterating a deque another thread appends to raises RuntimeError.
    lines = list(j["log"].copy())
    if dropped:
        # Leading, not trailing: the dropped lines are the OLD ones now, and a
        # note at the end would read as "the job stopped talking here".
        lines = [f"… ({dropped} earlier line(s) dropped — showing the last "
                 f"{LOG_LINES_MAX})"] + lines
    out["log"] = lines
    return out


def status() -> dict:
    return {"current": _CURRENT[0],
            "actions": {n: {"label": a["label"]} for n, a in ACTIONS.items()},
            "jobs": {n: public_status(j) for n, j in JOBS.items()}}


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
