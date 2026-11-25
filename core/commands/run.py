"""`vcheck run`: check a watched project, save its report, print it, and give the exit code."""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.project import check_runs
from core.registry import extensions


class VcheckRunCommand(cli_command.CLICommand):
    """Runs `vcheck run` for the auditing agent and the daemon."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck run`."""
        p.add_argument("--commit", help="the commit this run checks, used to name the report")
        p.add_argument("--no-cache", action="store_true",
                       help="run every claim check again, and give no stored result")
        p.add_argument("--claim-checks-not-run", metavar="REASON",
                       help="run only the paper checks, and flag that no claim was checked "
                            "for this reason")

    def execute(self, args: argparse.Namespace) -> int:
        """Check every claim, save the report files, print the Markdown, and give the exit code.

        The exit code follows the verdict with all flags, also when some are disputed.
        """
        root: Path = args.root
        state_dir: Path = args.state
        outcome = check_runs.WatchedProjectCheckRun(
            root, state_dir, extensions.ExtensionSet.of_project(state_dir)).run(
                args.commit, use_cache=not args.no_cache,
                claim_checks_not_run_because=args.claim_checks_not_run)
        print(outcome.markdown, end="")
        return outcome.verdict.exit_code
