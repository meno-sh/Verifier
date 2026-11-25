"""`vcheck reorder-claims`, the claim step that sorts the register, most valuable claim first.

The daemon runs it after the agent's round, before `vcheck link-claims` (runtime/daemon.py).
A model sorts the claims that have a statement (core/claim_steps/claim_reordering.py). The
claim files lock is not held while the model answers.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.claim_steps import claim_reordering
from core.project import watched_project
from utils import claim_files


class VcheckReorderClaimsCommand(cli_command.CLICommand):
    """Runs `vcheck reorder-claims` (runtime/daemon.py runs it after each agent round)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck reorder-claims`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Sort the claims of the register, then write the register in the new order.

        The register is read under the lock, the model sorts it without the lock, and the
        register is read again under the lock before the order is written. When the claims
        changed in between, nothing is written and the command fails, so the daemon does
        not link claims in an old order. When the register is in the order of the last sort,
        no model is asked. When a claim file is broken, nothing is sorted: `vcheck run` then
        reports the broken file.
        """
        root: Path = args.root
        state_dir: Path = args.state
        try:
            with claim_files.lock(state_dir).held():     # loading can begin a round, which writes
                files, _ = claim_files.loaded(state_dir, root)
        except claim_files.ClaimFilesBroken as e:
            print(f"No claim is reordered, because a claim file is broken: {e}")
            return 0
        claims = claim_reordering.sortable_claims(files.register)
        if claim_reordering.is_sorted_already(state_dir, claims):
            print(f"No claim needs reordering: the {len(claims)} claims are in the order of the "
                  "last sort.")
            return 0
        project = watched_project.WatchedProject.load(root, state_dir)
        settings = claim_reordering.ClaimReorderingSettings.from_options(
            project.run_options(project.check_registry()))
        sorted_claims = claim_reordering.ClaimQuicksort(settings).sort_claims(claims)
        with claim_files.lock(state_dir).held():
            files, _ = claim_files.loaded(state_dir, root)
            claim_reordering.apply_order(files.register, sorted_claims)
            claim_files.save(files, state_dir)
            claim_reordering.save_sorted_checksum(state_dir, sorted_claims)
        print(f"{settings.model} reordered {len(sorted_claims)} claims; the first is "
              f"{sorted_claims[0]['id'] if sorted_claims else 'none'}")
        return 0
