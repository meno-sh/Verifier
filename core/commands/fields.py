"""`vcheck fields`, a command of the auditing agent.

Print each claim key a registered check reads: its type, and the checks reading it.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.project import watched_project


class VcheckFieldsCommand(cli_command.CLICommand):
    """Runs `vcheck fields` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck fields`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Print each claim key a registered check reads: its type, and the checks reading it."""
        root: Path = args.root
        state_dir: Path = args.state
        reg = watched_project.WatchedProject.load(root, state_dir).check_registry()
        for key, ftype in sorted(reg.claim_field_types().items()):
            print(f"{key:<22} {ftype.value:<16} {', '.join(reg.claim_checks_that_read_field(key))}")
        return 0
