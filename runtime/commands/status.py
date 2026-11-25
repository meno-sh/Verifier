"""`verifier status`: the daemon's state, the round in flight, the jobs and the latest verdict."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.registry import extensions
from core.report_output import report_comparer
from runtime.agent import inbox, jobs, rounds
from runtime.project import daemon_process, project_status
from runtime.project.paths import StatePaths


class VerifierStatusCommand(cli_command.CLICommand):
    """Runs `verifier status`, as text for a person or as JSON for a tool."""
    HELP: ClassVar[str] = "the daemon, the round in flight, the jobs and the latest verdict"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier status`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        p.add_argument("--json", action="store_true", help="print one JSON object")

    def execute(self, args: argparse.Namespace) -> int:
        """Print the daemon's state, the round in flight, the jobs and the latest verdict."""
        paths = StatePaths(root=args.root)
        pid = daemon_process.daemon_pid(paths)
        stop = daemon_process.last_fatal(paths) if pid is None else None
        last = rounds.RoundHistory(paths).last_round()
        waiting = len(inbox.AgentInbox(paths.inbox).pending_files())
        latest = project_status.latest_report(paths)
        if args.json:
            print(json.dumps({
                "schema_version": report_comparer.SavedReportComparer.SCHEMA_VERSION,
                "running": pid is not None,
                "stopped_because": stop and stop["message"],
                "branch": paths.branch() if paths.branch_file.exists() else None,
                "checked": paths.checked() or None, "waiting_events": waiting,
                "round": last and {"n": last.n, "ended": last.ended_at is not None,
                                   "error": last.error,
                                   "verdict": last.verdict and last.verdict.value},
                "verdict": latest and latest.saved.verdict.value,
                "spending": project_status.Spending.of_project(paths).to_json()}))
            return 0
        print(f"daemon: {'running, pid ' + str(pid) if pid else 'not running'}")
        if stop is not None:
            print(f"daemon stopped at {stop['time']}: {stop['message']}")
        if paths.branch_file.exists():
            print(f"branch: {paths.branch()}, "
                  f"checked up to {paths.checked()[:12] or 'nothing yet'}")
        if last:
            state = ("in progress" if last.ended_at is None
                     else f"FAILED: {last.error}" if last.error
                     else f"ended, verdict {last.verdict and last.verdict.value}")
            print(f"round {last.n}: {len(last.events)} event(s), {state}")
        print(f"waiting events: {waiting}")
        if paths.jobs.exists():
            for r in jobs.records(paths):
                print(f"job {r.name}: {'running' if r.is_running() else 'ended'}")
        if paths.checkout.exists():          # the extensions read the checkout
            loaded = extensions.ExtensionSet.of_project(paths.state)
            for line in loaded.status_lines(paths.checkout, paths.state):
                print(line)
        print(project_status.Spending.of_project(paths).status_line())
        if latest:
            print(f"verdict: {latest.saved.verdict.value}")
        return 0
