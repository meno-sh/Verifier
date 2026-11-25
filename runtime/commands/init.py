"""`verifier init`: prepare a working directory as `start` does, but start no daemon."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.registry import extensions
from runtime import daemon
from runtime.project import daemon_process, git, project_preparation
from runtime.project.paths import StatePaths
from utils import alerts, models


class VerifierInitCommand(cli_command.CLICommand):
    """Runs `verifier init`, for a tool that later starts the daemon with `start-if-pending`."""
    HELP: ClassVar[str] = "prepare a working directory; start nothing"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier init`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        p.add_argument("--branch", help="the branch to check (default: the checked-out one)")
        p.add_argument("--model", default=models.agent_preset.KIMI,
                       help="the model of the auditing agent, as openrouter/<vendor>/<model> "
                            "(default: %(default)s)")
        self.add_preparation_arguments(p)

    def execute(self, args: argparse.Namespace) -> int:
        """Install the hook and record the branch, as `start` does, but start no daemon.

        A first `init` also puts the startup event in the inbox, so that `start-if-pending`
        starts the first round, which reads the whole history.
        """
        paths = StatePaths(root=args.root)
        branch = args.branch or git.output(paths.root, "symbolic-ref", "--short", "HEAD")
        problem = (project_preparation.prepare(paths, branch, args.model, args.claims_from,
                                               options=args.option,
                                               extension_names=args.extensions)
                   if branch
                   else f"{paths.root} has no branch checked out; give one with --branch")
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 1
        daemon.write_startup_event(paths)
        alerts.notify("log", daemon_process.INIT_KEY, f"ready to check branch {branch}",
                      branch=branch)
        print(f"verifier ready for {paths.root}, checking branch {branch}")
        print(project_preparation.worker_guide_message(paths))
        return 0

    @staticmethod
    def add_preparation_arguments(p: argparse.ArgumentParser) -> None:
        """Add the arguments that `init` and `start` give to project_preparation.prepare."""
        p.add_argument("--extensions", type=extensions.extension_names_from_argument,
                       metavar="MODULE,...",
                       help="the extension modules to load, in .verifier/extensions.json; "
                            "empty for none (default: the saved ones, else "
                            f"{','.join(extensions.DEFAULT_EXTENSIONS)})")
        p.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                       help="save a run option in .verifier/options.json, such as "
                            "want_model_review=true; give it once for each option")
        p.add_argument("--claims-from", nargs="+", metavar="PATH",
                       help="the files and folders to read claims from (`.` for every note); "
                            "asked at the terminal when none is given or saved")
