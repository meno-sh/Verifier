"""`verifier report`: the latest report, as text or as JSON."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.report_output import report_comparer
from runtime.project import project_status
from runtime.project.paths import StatePaths


class VerifierReportCommand(cli_command.CLICommand):
    """Runs `verifier report`."""
    HELP: ClassVar[str] = "the latest report"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier report`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        p.add_argument("--json", action="store_true", help="print one JSON object")

    def execute(self, args: argparse.Namespace) -> int:
        """Print the latest report: its text, or with --json the saved report and its text."""
        paths = StatePaths(root=args.root)
        latest = project_status.latest_report(paths)
        if args.json:
            print(json.dumps({"schema_version": report_comparer.SavedReportComparer.SCHEMA_VERSION,
                              "report": latest and latest.to_json()}))
        elif latest:
            print(latest.text, end="")
        else:
            print("no report yet")
        return 0
