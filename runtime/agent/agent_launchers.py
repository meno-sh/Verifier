"""The commands the auditing agent runs (vcheck, vjob, vwake), as launchers the daemon writes.

The agent's shell gets a folder holding only these three launchers, not the folder of
the Python the verifier runs on, so the agent's `python` is still the project's. Each
launcher runs the verifier's Python with the verifier's code on PYTHONPATH for that
command alone, and with -P, so a folder in the worker's project (one named `checks/`,
say) cannot shadow the verifier's modules. This works whether the verifier was
installed with uv or runs from its checkout.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from core.report_output import report_store
from runtime.project import git
from utils import file_storage, version

# What each launcher runs after `python -P`.
CALLS = {
    "vcheck": "-m core.cli",
    "vjob": "-c 'import sys; from runtime.agent import jobs; sys.exit(jobs.vjob_main())'",
    "vwake": "-c 'import sys; from runtime.agent import jobs; sys.exit(jobs.vwake_main())'",
}


def write_launchers(folder: Path) -> Path:
    """Write one launcher per command into `folder`, and return it for the agent's PATH.

    A launcher is replaced only when its text changed, and by renaming a new file over
    it, so a command already running from it never reads a half-written script.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for name, call in CALLS.items():
        launcher = folder / name
        text = (f"#!/bin/sh\n# {name}: written by the verifier's daemon; "
                "see runtime/agent/agent_launchers.py.\n"
                f'PYTHONPATH={shlex.quote(str(version.VERIFIER_HOME))}'
                '"${PYTHONPATH:+:$PYTHONPATH}" '
                f'exec {shlex.quote(sys.executable)} -P {call} "$@"\n')
        if launcher.is_file() and launcher.read_text() == text:
            continue
        file_storage.AtomicallyWrittenFile(launcher, mode=0o755).write_text(text)
    return folder


TIMEOUT_EXIT = 124                    # the exit code of a vcheck stopped at its timeout


def run_vcheck(args: list[str], env: Mapping[str, str], timeout: float | None = None
               ) -> subprocess.CompletedProcess:
    """Run vcheck with these arguments, as its launcher would, and capture its output.

    With `timeout` (seconds), a vcheck still running then is killed, and the result has
    exit code TIMEOUT_EXIT and says so on stderr.
    """
    path = env.get("PYTHONPATH")
    cmd = [sys.executable, "-P", "-m", "core.cli", *args]
    try:
        return subprocess.run(cmd, env={**env, "PYTHONPATH": str(version.VERIFIER_HOME)
                                        + (os.pathsep + path if path else "")},
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        def as_text(v: object) -> str:
            return v.decode(errors="replace") if isinstance(v, bytes) else str(v or "")
        return subprocess.CompletedProcess(
            cmd, TIMEOUT_EXIT, as_text(e.output),
            as_text(e.stderr) + f"\nvcheck {' '.join(args)} timed out after {timeout:g} s\n")


class ReportRefresher:
    """Runs vcheck again for the commit of a checkout, so the report shows a change at once.

    A command of a person, such as an answer to a review or a dispute, calls it. It prints
    the verdict, and the verdict without the disputed flags when there is one.
    """

    def __init__(self, root: Path, state_dir: Path) -> None:
        """Refresh the report of the checkout `root`, with its state folder."""
        self.root = root
        self.state_dir = state_dir

    def refresh_report_and_print_verdicts(self) -> None:
        """Run vcheck for the commit of the checkout, then print the verdicts it saved."""
        commit = git.head(self.root)
        out = run_vcheck(["--state", str(self.state_dir), "--root", str(self.root), "run",
                          "--commit", commit], os.environ)
        saved = report_store.ReportStore(self.state_dir).load_saved_report(commit)
        first = (out.stdout.splitlines() or [""])[0]
        # a crash also exits with 1, so the report is new only when vcheck printed its verdict
        if out.returncode not in (0, 1, 2) or not first.startswith("Verdict: ") or saved is None:
            print("error: the report could not be updated. vcheck said:\n"
                  + (out.stderr.strip() or out.stdout.strip()), file=sys.stderr)
            return
        second = saved.verdict_without_disputed_flags
        print(f"The report is updated. Verdict: {saved.verdict.value}"
              + (f"; without the disputed flags: {second.value}" if second else ""))
