"""`verifier start-if-pending`: start the daemon of each working directory where work waits."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from runtime import daemon
from runtime.agent import inbox, jobs, rounds
from runtime.project import daemon_process, project_preparation
from runtime.project.paths import StatePaths
from utils import file_storage, models


class VerifierStartIfPendingCommand(cli_command.CLICommand):
    """Runs `verifier start-if-pending`, which a tool calls often after `verifier init`."""
    HELP: ClassVar[str] = "start the daemon of each directory where work waits"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier start-if-pending`."""
        p.add_argument("roots", type=Path, nargs="+",
                       help="the project folders, each prepared by `verifier init`")
        p.add_argument("--model", default=models.agent_preset.KIMI,
                       help="the model of the auditing agent, as openrouter/<vendor>/<model> "
                            "(default: %(default)s)")
        p.add_argument("--round-timeout", type=float, default=daemon.ROUND_TIMEOUT_SECONDS,
                       help="the most seconds one round may take (default: %(default)s)")

    def execute(self, args: argparse.Namespace) -> int:
        """Start the daemon of each working directory where work waits, and return at once.

        Work is an event in the inbox, a due job end, check-in or timer, or a round no daemon
        saw end. The daemon stops when no work is left. A directory whose daemon stopped on an
        error is reported and skipped, until `verifier init` runs again.
        """
        code = 0
        for root in args.roots:
            paths = StatePaths(root=root.resolve())
            if not paths.branch_file.exists():
                print(f"error: {paths.root} has no `verifier init`", file=sys.stderr)
                code = 1
                continue
            with file_storage.FileLock(paths.runtime / "start.lock").held_if_free() as held:
                if not held:
                    continue
                try:
                    if daemon_process.daemon_pid(paths) is not None:
                        continue
                except RuntimeError:    # a daemon is starting: it has the lock, not yet its pid
                    continue
                jobs.collect(paths)
                if (not inbox.AgentInbox(paths.inbox).pending_files()
                        and not rounds.RoundHistory(paths).open_round()):
                    continue
                stop = daemon_process.last_fatal(paths)
                problem = (None if stop is not None
                           else project_preparation.saved_project_problem(paths, args.model))
                if stop is not None:
                    print(f"error: {paths.root}: the daemon stopped at {stop['time']}: "
                          f"{stop['message']}", file=sys.stderr)
                    code = 1
                elif problem is not None:
                    print(f"error: {paths.root}: {problem}", file=sys.stderr)
                    code = 1
                else:
                    daemon_process.spawn_daemon(paths, args.model, args.round_timeout,
                                                exit_when_idle=True)
                    print(f"started for {paths.root}")
        return code
