"""Start the daemon of a working directory, and read whether it runs and why it stopped."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from runtime.agent import opencode
from runtime.project.paths import StatePaths
from utils import alerts, file_storage, version

START_WAIT_SECONDS = 90
INIT_KEY = "verifier.init"          # the alert record of `init`, which clears a fatal stop


def spawn_daemon(paths: StatePaths, model: str, round_timeout: float,
                 exit_when_idle: bool = False) -> None:
    """Start the daemon in the background, in its own session."""
    log = open(paths.daemon_log, "ab")  # noqa: SIM115 - the daemon writes to this file
    subprocess.Popen([sys.executable, "-m", "runtime.daemon", str(paths.root),
                      "--model", model, "--round-timeout", str(round_timeout),
                      *(["--exit-when-idle"] if exit_when_idle else [])],
                     cwd=version.VERIFIER_HOME,
                     env={**os.environ, "PYTHONPATH": str(version.VERIFIER_HOME)},
                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                     start_new_session=True)


def daemon_pid(paths: StatePaths) -> int | None:
    """The running daemon's pid, read from its lock file, or None if no daemon holds the lock.

    Raises:
        RuntimeError: a daemon holds the lock but has not written its pid yet.
    """
    return file_storage.ProcessLock(paths.lock).holder_pid()


def last_fatal(paths: StatePaths) -> dict | None:
    """The daemon's last fatal record, if it came after the daemon last started; else None.

    A daemon that stops on an error writes a fatal record, so `verifier status` can say why.
    A later start of OpenCode, or a later `verifier init`, clears it.
    """
    records = alerts.read(paths.alerts_log)
    for r in reversed(records):
        if r["key"] in ("daemon.opencode_started", INIT_KEY):
            return None
        if r["process"] != "daemon":
            continue
        if r["level"] == "fatal":
            return r
    return None


def wait_for_server(paths: StatePaths) -> bool:
    """Wait until the daemon is running and its OpenCode server answers."""
    deadline = time.time() + START_WAIT_SECONDS
    while time.time() < deadline:
        try:
            info = opencode.ServerInfo.read(paths.server_file)
        except opencode.ServerFileError:    # an older daemon's file, until the new one writes
            info = None
        try:
            running = daemon_pid(paths) is not None
        except RuntimeError:           # the daemon has the lock but has not written its pid yet
            running = False
        if running and info and info.session_id and info.client().healthy():
            return True
        time.sleep(1)
    return False
