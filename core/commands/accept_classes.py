"""`vcheck accept-classes`, a claim step that code does before the agent's round.

Make the CLASSES stage from the proposal of `vcheck propose-classes`, as it is.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from utils import alerts, claim_files


class VcheckAcceptClassesCommand(cli_command.CLICommand):
    """Runs `vcheck accept-classes` (runtime/daemon.py runs it when the options say so)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck accept-classes`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Take the one proposal of this round as the classes, then check the CLASSES stage."""
        root: Path = args.root
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():
            files, r = claim_files.loaded(state_dir, root)
            if problems := claim_files.quote_problems(files, r):
                print(f"FAILED. Stage 1 (QUOTES) has {len(problems)} problems. Run this "
                      "command first: vcheck check-quotes")
                return 1
            proposal = claim_files.read_proposal(state_dir, r)
            if proposal is None:
                if claim_files.proposal_items(files):
                    print("FAILED. There is no proposal for this round. Run this command "
                          "first: vcheck propose-classes")
                    return 1
                proposal = {"classes": []}               # no new quote: nothing to group
            before = {cid: len(c.get("quotes") or []) for cid, c in files.classes.items()
                      if isinstance(c, dict)}
            notes: list[str] = []
            files.classes = claim_files.accept_proposal(files, proposal, notes)
            for note in notes:
                alerts.notify("log", "claims.classes_not_merged", note)
            if problems := claim_files.class_problems(files, r):
                print("\n".join(claim_files.failed(problems, "vcheck accept-classes")))
                return 1
            added = claim_files.accept_classes(files, r)
            claim_files.save(files, state_dir)
        new = [cid for cid in files.classes if cid not in before]
        joined = sum(len(files.classes[cid]["quotes"]) - n for cid, n in before.items())
        print("\n".join([f"PASSED. The proposal made {len(new)} new classes, and {joined} "
                         f"quotes joined existing classes ({added} of them classes of earlier "
                         "rounds).", *notes]))
        return 0
