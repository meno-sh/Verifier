"""`verifier start`: prepare a working directory, start its daemon, and open the terminal view."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from runtime import daemon
from runtime.commands import attach, init
from runtime.project import daemon_process, project_preparation
from runtime.project.paths import StatePaths
from utils import models


class VerifierStartCommand(cli_command.CLICommand):
    """Runs `verifier start`: what `init` does, then the daemon and the terminal view."""
    HELP: ClassVar[str] = "prepare a working directory, start its daemon and open the view"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier start`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        p.add_argument("--branch", default=project_preparation.DEFAULT_BRANCH,
                       help=f"the branch to check (default: {project_preparation.DEFAULT_BRANCH})")
        p.add_argument("--model", default=models.agent_preset.KIMI,
                       help="the model of the auditing agent, as openrouter/<vendor>/<model> "
                            "(default: %(default)s)")
        p.add_argument("--round-timeout", type=float, default=daemon.ROUND_TIMEOUT_SECONDS,
                       help="the most seconds one round may take (default: %(default)s)")
        p.add_argument("--no-attach", action="store_true",
                       help="do not open the auditing agent's view")
        init.VerifierInitCommand.add_preparation_arguments(p)

    def execute(self, args: argparse.Namespace) -> int:
        """Install the hook, start the daemon in the background, and open the terminal view."""
        paths = StatePaths(root=args.root)
        problem = project_preparation.prepare(
            paths, args.branch, args.model, args.claims_from,
            input if sys.stdin.isatty() else None, args.option, args.extensions)
        if problem:
            print(f"error: {problem}", file=sys.stderr)
            return 1
        # a running daemon that stops when idle stays up from now on
        (paths.runtime / daemon.ON_DEMAND).unlink(missing_ok=True)
        if daemon_process.daemon_pid(paths) is None:
            daemon_process.spawn_daemon(paths, args.model, args.round_timeout)
            if not daemon_process.wait_for_server(paths):
                print(f"error: the daemon did not start; see {paths.daemon_log}",
                      file=sys.stderr)
                return 1
        print(f"verifier running for {paths.root}, checking branch {args.branch}")
        print(project_preparation.worker_guide_message(paths))
        return 0 if args.no_attach else attach.VerifierAttachCommand().execute(args)
