"""The alert log: the one log of the verifier's own processes, for its developers.

A problem the verifier is made to find (a missing file) goes in the report, never here.
Each record has one level. `log`: a routine event, such as a round sent. `warn`: one
failed attempt that is retried. `error`: a failure that a long-running part survives by
design. `fatal`: a bug; the process stops. Each process calls configure(); then notify()
appends one JSON line to the project's log, under a lock, and prints one line to
stderr. Nothing is cut short. A record that cannot be written raises AlertError.
"""
from __future__ import annotations

import dataclasses
import datetime
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import types
from pathlib import Path

from utils import file_storage, structure, version

LEVELS = ("log", "warn", "error", "fatal")         # least severe first
KEY = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")      # such as models.retry
PROCESS = re.compile(r"[a-z][a-z0-9_-]*")                      # such as vcheck
FIELDS = ("ts", "time", "level", "key", "process", "pid", "project", "verifier_commit",
          "message", "error_type", "traceback", "details")


class AlertError(Exception):
    """The alert log cannot be written or read, or notify() was called before configure()."""


class Fatal(Exception):
    """A fatal error that is already in the log, so the process's last handler skips it."""


@dataclasses.dataclass(frozen=True)
class Log:
    """Where this process writes its records, and what each record says about the process."""
    path: Path                      # the project's alerts.jsonl
    process: str                    # the command, such as "vcheck" or "daemon"
    project: Path                   # the working directory the process serves
    verifier_commit: str | None     # None when it is not a git checkout, or git cannot name it


_LOG: Log | None = None
_LOCK = threading.Lock()            # one change of the settings at a time


def configure(path: Path, process: str, project: Path) -> Log:
    """Send this process's records to `path`, and record any exception that ends it.

    Call it at the start of each command. A later call (a test that runs several
    commands in one process) replaces the settings. The file is made at the first record.
    When git cannot name the commit of the verifier's code, the commit is None, and the
    first record is a `warn` that gives git's error.

    Raises:
        ValueError: `path` is not absolute, or `process` is not a plain name.
    """
    global _LOG
    if not path.is_absolute() or not project.is_absolute():
        raise ValueError(f"the log and the project must be absolute paths: {path}, {project}")
    if not PROCESS.fullmatch(process):
        raise ValueError(f"process {process!r} must be a plain lower-case name")
    commit, problem = _verifier_commit()
    log = Log(path=path, process=process, project=project, verifier_commit=commit)
    with _LOCK:
        if _LOG is None:
            _install_hooks()
        _LOG = log
    if problem is not None:
        notify("warn", "alerts.commit_unknown", problem, home=str(version.VERIFIER_HOME))
    return log


def project_log(state_dir: Path) -> Path:
    """The alert log of the project whose state directory is `state_dir`; never committed."""
    return state_dir / structure.RUNTIME_DIR / "alerts.jsonl"


def configured() -> Log:
    """The settings configure() gave. Raises AlertError before configure()."""
    if _LOG is None:
        raise AlertError("alerts.configure() was not called, so there is no alert log")
    return _LOG


def notify(level: str, key: str, message: str, *, error: BaseException | None = None,
           **details: object) -> dict:
    """Append one record to the log and print it on stderr, and return the record.

    Args:
        level: "log", "warn", "error" or "fatal" (see the module text).
        key: a dotted name that is the same for each case, such as "models.retry".
        message: one sentence about what happened, not empty.
        error: the exception, if there is one; its whole traceback is kept.
        details: more about the case, as JSON values; each is kept whole.

    Raises:
        ValueError: the level, key or message is not valid.
        TypeError: a detail is not a JSON value.
        AlertError: there is no log, or the record cannot be written.
    """
    if level not in LEVELS:
        raise ValueError(f"level {level!r} is not one of {LEVELS}")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError(f"key {key!r} must be a dotted lower-case name, such as models.retry")
    if not isinstance(message, str) or not message.strip():
        raise ValueError(f"the message of {key} must be non-empty text, got {message!r}")
    if error is not None and not isinstance(error, BaseException):
        raise TypeError(f"error must be an exception, got {type(error).__name__}")
    log = configured()
    now = time.time()
    record = {
        "ts": int(now * 1000),
        "time": datetime.datetime.fromtimestamp(now, datetime.UTC).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "level": level, "key": key, "process": log.process, "pid": os.getpid(),
        "project": str(log.project), "verifier_commit": log.verifier_commit,
        "message": message,
        "error_type": None if error is None else type(error).__name__,
        "traceback": None if error is None else "".join(traceback.format_exception(error)),
        "details": details,
    }
    # a value json cannot write raises here, before anything is written: no str() fallback
    try:
        file_storage.AppendOnlyJsonLinesFile(log.path, sync=True).append_row(record)
    except OSError as e:
        raise AlertError(f"cannot write the alert log {log.path}: {e}") from e
    print(f"[{level.upper()}] {key}: {message} (see {log.path})", file=sys.stderr, flush=True)
    return record


def fail(key: str, message: str, *, error: BaseException | None = None,
         **details: object) -> Fatal:
    """Write a fatal record, and return the Fatal error that the caller must raise."""
    notify("fatal", key, message, error=error, **details)
    return Fatal(f"{key}: {message}")


def read(path: Path, bad: list[str] | None = None) -> list[dict]:
    """Every record in the log, oldest first; none when the file does not exist.

    The last line can be partial while a writer appends it, so it is left out. Any
    other line that is not a whole record raises, unless `bad` is a list.

    Args:
        path: the alert log.
        bad: when given, each line that is not a whole record is added to it as a
            problem, and the other records are still read.

    Raises:
        AlertError: a line other than the last is not JSON or not a record, and `bad`
            is None.
    """
    def damaged(problem: str) -> None:
        """Add `problem` to `bad`, or raise it when there is no `bad`."""
        if bad is None:
            raise AlertError(problem)
        bad.append(problem)

    rows = file_storage.AppendOnlyJsonLinesFile(path).read_rows_with_line_numbers(
        lambda e: damaged(str(e)))
    records = []
    for i, row in rows:
        try:
            records.append(_record(row))
        except AlertError as e:
            damaged(f"{path}:{i}: {e}")
    return records


def _record(record: dict[str, object]) -> dict:
    """One row of the log as a record.

    Raises:
        AlertError: the row is not a record.
    """
    if tuple(record) != FIELDS:
        raise AlertError(f"not a record with exactly the fields {FIELDS}")
    if record["level"] not in LEVELS or not KEY.fullmatch(str(record["key"])):
        raise AlertError(f"level {record['level']!r} or key {record['key']!r} is not valid")
    return record

def _verifier_commit() -> tuple[str | None, str | None]:
    """The commit of the verifier's code, and why git cannot name it.

    Returns (None, None) when the code is not a git checkout. Returns (None, the reason)
    when git is not installed, or refuses the checkout (such as "dubious ownership").
    """
    if not (version.VERIFIER_HOME / ".git").exists():
        return None, None
    try:
        out = subprocess.run(["git", "-C", str(version.VERIFIER_HOME), "rev-parse", "HEAD"],
                             capture_output=True, text=True)
    except OSError as e:
        return None, f"git cannot run, so the verifier's commit is not known: {e}"
    sha = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None, (f"git cannot name the verifier's commit in {version.VERIFIER_HOME}: "
                      f"exit {out.returncode}, {out.stderr.strip()!r}")
    return sha, None


def _install_hooks() -> None:
    """Record each exception that ends the process or a thread, then report it as before."""
    previous, previous_thread = sys.excepthook, threading.excepthook

    def on_exit(kind: type[BaseException], value: BaseException,
                tb: types.TracebackType | None) -> None:
        if not isinstance(value, Fatal | KeyboardInterrupt):
            notify("fatal", "process.crashed", f"{kind.__name__}: {value}", error=value)
        previous(kind, value, tb)

    def on_thread_exit(args: threading.ExceptHookArgs) -> None:
        value = args.exc_value
        if value is not None and not isinstance(value, Fatal | KeyboardInterrupt):
            notify("fatal", "process.thread_crashed", f"{args.exc_type.__name__}: {value}",
                   error=value, thread=None if args.thread is None else args.thread.name)
        previous_thread(args)

    sys.excepthook = on_exit
    threading.excepthook = on_thread_exit
