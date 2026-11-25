"""`verifier stop`: stop the daemon, which stops the OpenCode server."""
from __future__ import annotations

import argparse
import os
import signal
import time
from pathlib import Path
from typing import ClassVar

from core import cli_command
from runtime.project import daemon_process
from runtime.project.paths import StatePaths


class VerifierStopCommand(cli_command.CLICommand):
    """Runs `verifier stop`; `verifier uninstall` runs it too."""
    HELP: ClassVar[str] = "stop the verifier; jobs keep running"
    STOP_WAIT_SECONDS: ClassVar[float] = 30

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier stop`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")

    def execute(self, args: argparse.Namespace) -> int:
        """Stop the daemon, which stops the OpenCode server. Jobs keep running."""
        paths = StatePaths(root=args.root)
        pid = daemon_process.daemon_pid(paths)
        if pid is None:
            print("the verifier is not running")
            return 0
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + self.STOP_WAIT_SECONDS
        while time.time() < deadline and daemon_process.daemon_pid(paths) is not None:
            time.sleep(0.5)
        stopped = daemon_process.daemon_pid(paths) is None
        print("stopped" if stopped else f"daemon {pid} did not stop")
        return 0 if stopped else 1
