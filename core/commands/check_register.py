"""`vcheck check-register`, a command of the auditing agent.

Check the REGISTER stage, then pin the files of each new artifact source.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.project import watched_project
from utils import claim_files


class VcheckCheckRegisterCommand(cli_command.CLICommand):
    """Runs `vcheck check-register` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck check-register`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Check the REGISTER stage, then pin the files of each new artifact source."""
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():     # from the read to the last write
            return self.check_and_accept(args.root, state_dir)

    def check_and_accept(self, root: Path, state_dir: Path) -> int:
        """Check the register, and accept it. The caller holds the lock of the claim files."""
        files, r = claim_files.loaded(state_dir, root)
        if earlier_stage_failed(files, r):
            return 1
        pinned = pass_register(files, r, root, state_dir)
        if pinned is None:
            return 1
        new = sum(e["created"] == claim_files.NEW for e in files.register)
        count = f"{len(files.register)} claims" if r.first else f"{new} new claims"
        print("\n".join([f"PASSED. {count}. The core pinned the files of {pinned} artifact claims.",
                         *claim_files.next_steps(files)]))
        return 0


def earlier_stage_failed(files: claim_files.ClaimFiles, r: claim_files.ClaimRound) -> bool:
    """Print why and return True if the QUOTES or CLASSES stage fails.

    Code does these stages before the agent's round. A failed step leaves them open, and
    the next round runs them again (runtime/daemon.py, do_claim_steps).
    """
    for name, problems in (("1 (QUOTES)", claim_files.quote_problems(files, r)),
                           ("2 (CLASSES)", claim_files.class_problems(files, r))):
        if problems:
            print(f"FAILED. Stage {name} has {len(problems)} problems, because a claim step "
                  "of code did not finish in this round. The next round runs it again.")
            return True
    return False


def pass_register(files: claim_files.ClaimFiles, r: claim_files.ClaimRound, root: Path,
                  state_dir: Path) -> int | None:
    """Check the REGISTER stage; if it passes, pin new sources and save the files and round.

    Prints the problems when the check fails. The caller holds the lock of the claim files.

    Returns:
        The number of claims pinned now, or None if the check failed.
    """
    reg = watched_project.WatchedProject.load(root, state_dir).check_registry()
    if problems := claim_files.register_problems(files, r, reg.computational_methods,
                                                 reg.claim_field_types(), root):
        print("\n".join(claim_files.failed(problems, "vcheck check-register")))
        return None
    pinned = claim_files.accept_register(files, r, root)
    claim_files.save(files, state_dir)
    claim_files.save_round(r, state_dir)
    return pinned
