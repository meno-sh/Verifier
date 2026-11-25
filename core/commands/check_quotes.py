"""`vcheck check-quotes`, a claim step that code does before the agent's round.

Check the QUOTES stage: every new candidate has a quote or a drop, and old entries stay.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from utils import claim_files


class VcheckCheckQuotesCommand(cli_command.CLICommand):
    """Runs `vcheck check-quotes`, a claim step of code (runtime/daemon.py)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck check-quotes`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Check the QUOTES stage: each new candidate has a quote or a drop; old entries stay."""
        root: Path = args.root
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():     # loading can begin a round, which writes
            files, r = claim_files.loaded(state_dir, root)
        if problems := claim_files.quote_problems(files, r):
            print("\n".join(claim_files.failed(problems, "vcheck check-quotes")))
            return 1
        new = [q for q in files.quotes.values() if q["created"] == claim_files.NEW]
        drops = [d for d in files.dropped.values() if d["created"] == claim_files.NEW]
        fresh = sum(c["created"] == claim_files.NEW for c in files.candidates.values())
        if r.first:
            line = (f"PASSED. {len(new)} quotes and {len(drops)} drops answer the {fresh} new "
                    "candidates.")
        else:
            line = (f"PASSED. {len(new)} new quotes and {len(drops)} new drops answer the "
                    f"{fresh} new "
                    "candidates.")
        print(line)
        return 0
