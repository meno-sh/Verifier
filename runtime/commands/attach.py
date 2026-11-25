"""`verifier attach`: open OpenCode's terminal view on the verifier's session."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from runtime.agent import opencode
from runtime.project import daemon_process
from runtime.project.paths import StatePaths


class VerifierAttachCommand(cli_command.CLICommand):
    """Runs `verifier attach`; `verifier start` runs it too, unless --no-attach."""
    HELP: ClassVar[str] = "open the terminal view of the running verifier"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier attach`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")

    def execute(self, args: argparse.Namespace) -> int:
        """Open OpenCode's terminal view on the verifier's session."""
        paths = StatePaths(root=args.root)
        try:
            info = opencode.ServerInfo.read(paths.server_file)
        except opencode.ServerFileError as e:
            print(f"error: {e}; restart the verifier with `verifier start`", file=sys.stderr)
            return 1
        if info is None or daemon_process.daemon_pid(paths) is None:
            print("error: the verifier is not running; use `verifier start`", file=sys.stderr)
            return 1
        return subprocess.call([opencode.binary(), "attach", f"http://127.0.0.1:{info.port}",
                                "--session", info.session_id, "--password", info.password])
