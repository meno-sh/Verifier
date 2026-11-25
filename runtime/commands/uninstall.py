"""`verifier uninstall`: stop the verifier and remove its hook, and keep `.verifier/`."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import ClassVar

from core import cli_command
from runtime.commands import stop
from runtime.project import hook
from runtime.project.paths import StatePaths


class VerifierUninstallCommand(cli_command.CLICommand):
    """Runs `verifier uninstall`."""
    HELP: ClassVar[str] = "stop the verifier and remove its hook; keep .verifier/"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier uninstall`."""
        p.add_argument("root", type=Path, help="the project folder, a git repository")

    def execute(self, args: argparse.Namespace) -> int:
        """Stop the verifier and remove its hook. Keep `.verifier/`."""
        paths = StatePaths(root=args.root)
        code = stop.VerifierStopCommand().execute(args)
        hook.uninstall(paths.root)
        print(f"hook removed; {paths.state} is kept")
        return code
