"""`verifier diff`: what changed from the report of one commit to the latest report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.report_output import report_comparer, report_store
from runtime.project.paths import StatePaths


class VerifierDiffCommand(cli_command.CLICommand):
    """Runs `verifier diff`, which prints JSON for a tool."""
    HELP: ClassVar[str] = "what changed since the report of a commit (JSON)"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier diff`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        p.add_argument("--from", dest="since", required=True, metavar="COMMIT",
                       help="the commit whose report to compare with the latest")

    def execute(self, args: argparse.Namespace) -> int:
        """Print, as JSON, what changed from the report of commit `--from` to the latest one."""
        paths = StatePaths(root=args.root)
        store = report_store.ReportStore(paths.state)
        old, new = (store.load_saved_report(name) for name in (args.since, "latest"))
        if old is None or new is None:
            print(f"error: no report for {'the latest commit' if new is None else args.since}",
                  file=sys.stderr)
            return 1
        print(json.dumps(report_comparer.SavedReportComparer().compare_reports(old, new)))
        return 0
