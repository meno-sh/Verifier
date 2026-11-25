#!/usr/bin/env python3
"""Check the commands a tool uses to run the verifier for many checkouts of one repo.

Covers: one hook serves two checkouts on two branches, and uninstalling one keeps the
other; `start-if-pending` starts a daemon only where work waits, and a daemon is idle
with no round and no event; `status --json`, `report --json` and `diff`; the change
between two reports; and the rules that keep the agent's code inside `.verifier/`.
Returns 1 if any case fails.
"""
import argparse
import contextlib
import copy
import io
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_daemon_checkout as dc  # noqa: E402
import test_runtime as rt  # noqa: E402

from core.report_output import report_comparer, saved_report  # noqa: E402
from runtime import daemon  # noqa: E402
from runtime.agent import jobs, opencode  # noqa: E402
from runtime.commands import diff as diff_command  # noqa: E402
from runtime.commands import report as report_command  # noqa: E402
from runtime.commands import start_if_pending  # noqa: E402
from runtime.commands import status as status_command  # noqa: E402
from runtime.project import daemon_process, hook  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import alerts  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def output(command, *args) -> str:
    """What `command(*args)` prints."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        command(*args)
    return out.getvalue()


def test_two_checkouts(tmp: Path) -> None:
    """Each checkout gets the events of its own branch only."""
    a = rt.repo(tmp)
    rt.git(a.root, "branch", "other")
    b = StatePaths(root=tmp / "b")
    rt.git(a.root, "worktree", "add", "-q", str(b.root), "other")
    b.ensure()
    hook.install(a.root, "main")
    hook.install(b.root, "other")
    rt.commit(a, "on main")
    rt.commit(a, "on other", tree=b.root)
    expect("two checkouts: each inbox gets one event, for its own branch",
           len(rt.events_now(a)) == 1 and len(rt.events_now(b)) == 1,
           f"{rt.events_now(a)} {rt.events_now(b)}")
    hook.uninstall(b.root)
    rt.commit(a, "on main again")
    expect("two checkouts: after one uninstalls, the other still gets its events",
           len(rt.events_now(a)) == 2)
    hook.uninstall(a.root)
    hooks = runtime_git.git_path(a.root, f"hooks/{hook.HOOK_NAME}")
    expect("two checkouts: the last uninstall removes the hook", not hooks.exists())
    # the lines an earlier version wrote into the hook for its one checkout
    hooks.write_text(f"#!/bin/sh\n{hook.MARKER}\nd={shlex.quote(str(a.inbox))}\n")
    hook.install(b.root, "other")
    rt.commit(a, "after the new hook")
    expect("old hook: a checkout set up by an earlier version keeps its events",
           len(rt.events_now(a)) == 3)
    rt.git(b.root, "checkout", "-q", "--detach")
    expect("init: a detached HEAD has no branch to track",
           runtime_git.output(b.root, "symbolic-ref", "--short", "HEAD") == "")


def test_start_if_pending(tmp: Path) -> None:
    """A daemon starts only where work waits, and a daemon with no work is idle."""
    paths, _, d, _ = dc.started(tmp)
    started: list[Path] = []
    daemon_process.spawn_daemon = lambda p, *a, **k: started.append(p.root)
    # start-if-pending checks the key of the agent's command models, which no test calls
    os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
    args = argparse.Namespace(roots=[paths.root], model="stub/none", round_timeout=10)
    output(start_if_pending.VerifierStartIfPendingCommand().execute, args)
    expect("start-if-pending: nothing waits, so no daemon starts", started == [])
    expect("daemon: with no round and no event, the daemon is idle", d.idle())
    rt.commit(paths, "two")
    output(start_if_pending.VerifierStartIfPendingCommand().execute, args)
    expect("start-if-pending: a commit waits, so the daemon starts", started == [paths.root])
    expect("daemon: with an event waiting, the daemon is not idle", not d.idle())
    d.tick(now=2000)                    # round 2 is sent, then its daemon dies
    started.clear()
    output(start_if_pending.VerifierStartIfPendingCommand().execute, args)
    expect("start-if-pending: a round no daemon saw end is resumed", started == [paths.root])
    alerts.configure(paths.alerts_log, "daemon", paths.root)
    alerts.notify("fatal", "daemon.test_stop", "stopped for the test")
    started.clear()
    with contextlib.redirect_stderr(io.StringIO()):
        output(start_if_pending.VerifierStartIfPendingCommand().execute, args)
    expect("start-if-pending: a daemon that stopped on an error is not started again",
           started == [])


def test_json_commands(tmp: Path) -> None:
    """`status --json`, `report --json` and `diff` print what the plugin reads."""
    paths, _, _, first = dc.started(tmp)
    status = json.loads(output(status_command.VerifierStatusCommand().execute,
                               argparse.Namespace(json=True, root=paths.root)))
    expect("status --json: the version, the branch and the checked commit",
           status["schema_version"] == report_comparer.SavedReportComparer.SCHEMA_VERSION
           and status["branch"] == "main"
           and status["checked"] == first and status["waiting_events"] == 0, repr(status))
    report = json.loads(output(report_command.VerifierReportCommand().execute,
                               argparse.Namespace(json=True, root=paths.root)))["report"]
    expect("report --json: the latest report, with its commit, verdict and text",
           report["commit"] == first and report["verdict"]
           and report["text"].startswith("Verdict: "), repr(report)[:200])
    diff = json.loads(output(diff_command.VerifierDiffCommand().execute,
                             argparse.Namespace(since=first, root=paths.root)))
    expect("diff: the latest report against its own commit is no change",
           diff["change"] == "none", repr(diff))


def test_startup(tmp: Path) -> None:
    """A first `init` puts one startup event in the inbox, and only one."""
    paths = rt.repo(tmp)
    daemon.write_startup_event(paths)
    daemon.write_startup_event(paths)
    expect("init: one startup event waits for the first round",
           [e["kind"] for e in rt.events_now(paths)] == ["startup"])


def compare(old: dict, new: dict) -> dict:
    """The change from one saved report to another, as `verifier diff` gives it."""
    return report_comparer.SavedReportComparer().compare_reports(
        saved_report.SavedReport.from_json(old), saved_report.SavedReport.from_json(new))


def test_changes() -> None:
    """The change between two reports is none, minor or major."""
    claim = {"claim": {"id": "acc"}, "provenance": {"type": "Intact"},
             "recompute": {"type": "Matches"},
             "checks": {"seeds": {"type": "Passed", "measured": 1.0}}}
    old = {"commit": "a", "verdict": "PASS", "run_error": None, "methods_ran_in": None,
           "findings": {"papers": {}, "claims": [claim], "unreadable_claims": []},
           "verdict_without_disputed": None, "issues": [], "report_id": "0" * 12}
    new = copy.deepcopy(old)
    new["commit"] = "b"
    expect("changes: only the commit differs, so no change",
           compare(old, new)["change"] == "none")
    new["findings"]["claims"][0]["checks"]["seeds"]["measured"] = 2.0
    expect("changes: a measured value differs, so a minor change",
           compare(old, new)["change"] == "minor")
    new["findings"]["claims"][0]["checks"]["seeds"] = {
        "type": "Flagged", "problems": [{"kind": "single-seed", "detail": "one seed"}]}
    new["verdict"] = "PASS WITH DISCLOSURE"
    diff = compare(old, new)
    expect("changes: a new flagged check is a major change, named and with its state",
           diff["change"] == "major" and diff["added"] == [
               {"finding": "claim acc: seeds", "state": "Flagged single-seed"}], repr(diff))


def test_outside_rules(tmp: Path) -> None:
    """The agent's shell and its jobs cannot run code outside `.verifier/`."""
    rules = opencode.bash_rules()
    expect("rules: interpreters on ../ and absolute paths, and inline code, are denied",
           all(rules.get(p) == "deny" for p in
               ("python ../*", "node /*", "python -c *", "bash -c *", "../*")))
    paths = rt.repo(tmp)
    tree = paths.jobs / "x.tree"
    expect("jobs: a script in .verifier/ may run",
           jobs.runs_outside(paths, tree, ["python", "methods/m.py"]) is None)
    expect("jobs: a script outside .verifier/ may not run",
           jobs.runs_outside(paths, tree, ["uv", "run", "python", "../../../run.py"]) is not None)
    expect("jobs: inline code may not run",
           jobs.runs_outside(paths, tree, ["python", "-c", "print(1)"]) is not None)
    expect("jobs: options and a versioned python do not hide a script outside .verifier/",
           jobs.runs_outside(paths, tree, ["python3.12", "-u", "../../../run.py"]) is not None)


def main() -> int:
    """Run every case; return 1 if any failed."""
    for group in (test_two_checkouts, test_start_if_pending, test_json_commands,
                  test_outside_rules, test_startup):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    print("test_changes")
    test_changes()
    print("\n" + (f"{len(FAILED)} case(s) failed." if FAILED
                  else "All checkout-watch cases passed."))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
