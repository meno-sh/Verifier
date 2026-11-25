"""`vcheck fill-register`, a claim step that code does before the agent's round.

Write a claim for each class with none, its statement taken from its quotes
(claim_files.fill_register). `vcheck link-claims` writes the links after the agent's round.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.commands import check_register
from utils import claim_files


class VcheckFillRegisterCommand(cli_command.CLICommand):
    """Runs `vcheck fill-register`, a claim step of code (runtime/daemon.py)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """`vcheck fill-register` has no argument."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Write the claims of the new classes, then check and accept the register.

        The claims are written even when the register then fails its check (because of a
        source that the agent wrote), so the agent fixes only that source.
        """
        root: Path = args.root
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():     # from the read to the last write
            files, r = claim_files.loaded(state_dir, root)
            if check_register.earlier_stage_failed(files, r):
                return 1
            added = claim_files.fill_register(files)
            for cid in added:
                entry = files.claim(cid)
                assert entry is not None, cid
                r.written[cid] = entry["statement"]
            claim_files.save(files, state_dir)
            claim_files.save_round(r, state_dir)
            pinned = check_register.pass_register(files, r, root, state_dir)
        if pinned is None:
            return 1
        print("\n".join([f"PASSED. Code wrote {len(added)} new claims, each with a statement "
                         f"from its quotes and no source. The core pinned the files of {pinned} "
                         "artifact claims."]))
        return 0
