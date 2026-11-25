"""`verifier alerts`: the alert log, newest first, for developers."""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from runtime.project.paths import StatePaths
from utils import alerts


class VerifierAlertsCommand(cli_command.CLICommand):
    """Runs `verifier alerts`, with filters on the level, the key and the time."""
    HELP: ClassVar[str] = "the alert log, newest first (for developers)"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier alerts`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        p.add_argument("--level", choices=alerts.LEVELS, default="warn",
                       help="only records of this level or worse (default: warn; "
                            "`log` shows every record)")
        p.add_argument("--key", help="only records whose key is this or starts with it")
        p.add_argument("--since", type=self.utc_time,
                       help="only records at or after this UTC time, such as 2026-10-28T21:00")

    def execute(self, args: argparse.Namespace) -> int:
        """Print the alert log's records, newest first, each whole.

        A damaged line does not stop the command. It is printed to stderr, and the command
        exits with 1 after it prints the other records.
        """
        paths = StatePaths(root=args.root)
        bad: list[str] = []
        records = alerts.read(paths.alerts_log, bad)
        least = alerts.LEVELS.index(args.level)
        records = [r for r in records if alerts.LEVELS.index(r["level"]) >= least]
        if args.key:
            records = [r for r in records
                       if r["key"] == args.key or r["key"].startswith(args.key + ".")]
        if args.since is not None:
            records = [r for r in records if r["ts"] >= args.since]
        print(f"{len(records)} record(s) in {paths.alerts_log}")
        for r in reversed(records):
            print(f"\n{r['time']}  {r['level'].upper()}  {r['key']}  ({r['process']}, "
                  f"pid {r['pid']})\n  {r['message']}")
            if r["details"]:
                print("  details: " + json.dumps(r["details"], indent=2).replace("\n", "\n  "))
            if r["traceback"]:
                print("  " + r["traceback"].rstrip("\n").replace("\n", "\n  "))
        for problem in bad:
            print(f"error: a damaged line was not read: {problem}", file=sys.stderr)
        return 1 if bad else 0

    @staticmethod
    def utc_time(text: str) -> int:
        """An ISO time with no zone, read as UTC, in epoch milliseconds."""
        when = datetime.datetime.fromisoformat(text)
        if when.tzinfo is not None:
            raise argparse.ArgumentTypeError(f"give the time in UTC with no zone, not {text!r}")
        return int(when.replace(tzinfo=datetime.UTC).timestamp() * 1000)
