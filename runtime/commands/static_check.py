"""`verifier static-check`: check a paper and its claim register once, and print the result.

Usage: verifier static-check --paper paper.tex [more.tex notes.md ...] [--claims claims.json]

The check itself is core.project.check_runs.StaticCheckRun; this file parses the arguments and
prints. It runs the built-in checks only, and needs no watched project and no daemon.
"""
import argparse
import json
import os
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.project import check_runs
from core.registry import checks, extensions, run_options
from core.report_output import terminal_printer
from schema import inputs
from utils import alerts, extract, structure

COMMAND = "verifier static-check"


class VerifierStaticCheckCommand(cli_command.CLICommand):
    """Runs `verifier static-check`: check a paper and its claim register once.

    It gives each run option of the built-in checks a flag. It prints the terminal
    report, or the JSON with --json, and gives the exit code of the verdict.
    """
    # The prefix of the argparse dest of each declared run option, so an option cannot
    # replace one of the arguments of the command.
    OPTION_DEST: ClassVar[str] = "run_option:"
    HELP: ClassVar[str] = "check a paper and its claim register once; start nothing"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of the command, and a flag for each run option of the checks."""
        p.add_argument("--paper", required=True, nargs="+", metavar="FILE",
                       help="the paper: its main .tex, or one or more .tex and .md files, "
                            "read as one paper in the order given")
        p.add_argument("--claims", help="optional claim register (.json); "
                                        "unlocks provenance and the claim checks")
        p.add_argument("--plain", action="store_true", help="no ANSI colour")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument("--paper-only", action="store_true",
                       help="ignore the claim register; check the paper on its own")
        built_in = extensions.builtin_check_registry()
        p.add_argument("--checks", help="comma-separated claim checks to run, from: "
                                        + ", ".join(built_in.claim_checks) + " (default: all)")
        self.add_run_option_flags(p, built_in)

    def execute(self, args: argparse.Namespace) -> int:
        """Check the paper, print the terminal report or the JSON, and give the exit code."""
        here = Path.cwd().resolve()       # the command has no state folder; its log is the cwd's
        alerts.configure(alerts.project_log(here / structure.STATE_DIR_NAME), "static-check", here)
        if self._refuse_missing_papers(args.paper):
            return 1
        chosen = (frozenset(k.strip() for k in args.checks.split(",") if k.strip())
                  if args.checks else None)
        try:
            outcome = check_runs.StaticCheckRun().run(
                args.paper, args.claims, args.paper_only, chosen,
                {key: getattr(args, self.OPTION_DEST + key)
                 for key in extensions.builtin_check_registry().run_option_catalog.options})
        except (check_runs.UnknownCheckError, inputs.ClaimRegisterError,
                extract.UnknownPaperFormat, run_options.OptionsError) as e:
            # Exit 1, as for missing inputs: 2 would read as REJECT to a gate.
            print(f"{COMMAND}: {e}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(outcome.to_json(), indent=2))
        else:
            names = ", ".join(os.path.basename(p) for p in args.paper)
            print(f"\nVERIFIER --- {names}")
            terminal_printer.TerminalReportPrinter(colour=not args.plain).print_report(outcome)
        return outcome.verdict.exit_code

    @classmethod
    def add_run_option_flags(cls, ap: argparse.ArgumentParser,
                             reg: checks.CheckRegistry) -> None:
        """Give each run option of the registry its flag.

        Raises:
            argparse.ArgumentError: the flag of an option clashes with another flag.
        """
        group = ap.add_argument_group("run options of the checks")
        for o in reg.run_option_catalog.options.values():
            default = "" if o.default is None else f" (default: {o.default})"
            dest = cls.OPTION_DEST + o.key
            if o.kind is bool:
                # None when not given, so the option keeps its own default
                group.add_argument(o.flag, dest=dest, action=argparse.BooleanOptionalAction,
                                   default=None, help=o.help + default)
            else:
                group.add_argument(o.flag, dest=dest, type=o.parse_text_value,
                                   metavar=o.kind.__name__.upper(), help=o.help + default)

    @staticmethod
    def _refuse_missing_papers(papers: list[str]) -> bool:
        """True, after a message, when a paper file is missing.

        The message names the main files a folder holds, since `--paper .` or a mistyped
        name is the common way to get here.
        """
        missing = [p for p in papers if not os.path.isfile(p)]
        for path in missing:
            print(f"{COMMAND}: --paper: not a file: {path}", file=sys.stderr)
            folder = path if os.path.isdir(path) else os.path.dirname(path) or "."
            found = structure.paper_candidates(folder) if os.path.isdir(folder) else []
            if found:
                print(f"main files in {folder}:", file=sys.stderr)
                for main_file, _rel, _size in found:
                    print(f"  --paper {main_file}", file=sys.stderr)
        return bool(missing)

