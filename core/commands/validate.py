"""`vcheck validate`: load every plug-in check and method, and parse the claim register."""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.project import watched_project
from core.registry import plugin_checks


class VcheckValidateCommand(cli_command.CLICommand):
    """Runs `vcheck validate` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck validate`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Load every plug-in check and method, and parse the claim register."""
        root: Path = args.root
        state_dir: Path = args.state
        project = watched_project.WatchedProject.load(root, state_dir)
        reg = project.check_registry()
        register = project.claim_register(reg)
        names_of_plugin_checks = [n for n in reg.claim_checks
                                  if n.startswith(plugin_checks.PLUGIN_CHECK_PREFIX)]
        print(f"{len(register.claims)} claim(s), {len(names_of_plugin_checks)} plug-in check(s), "
              f"{len(reg.computational_methods)} method(s)")
        for u in register.unreadable:
            print(f"unreadable {u.position_label()}: {u.reason}")
        return 1 if register.unreadable else 0
