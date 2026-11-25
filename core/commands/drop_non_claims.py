"""`vcheck drop-non-claims`, a claim step that code does before the agent's round.

Drop the unanswered candidates that code rules find are not claims (utils/candidate_rules.py).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from utils import candidate_rules, claim_files


class VcheckDropNonClaimsCommand(cli_command.CLICommand):
    """Runs `vcheck drop-non-claims` (runtime/daemon.py runs it when the options say so)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck drop-non-claims`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Write a drop for each unanswered candidate that a rule finds is not a claim."""
        root: Path = args.root
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():
            files, _ = claim_files.loaded(state_dir, root)
            quoted = claim_files.quoted_candidates(files)
            found = candidate_rules.non_claim_reasons(
                files.candidates, claim_files.quote_words(files), files.dropped)
            for cid, why in found.items():
                files.dropped[cid] = claim_files.new_entry(why=why)
            claim_files.save(files, state_dir)
        counts: dict[str, int] = {}
        for why in found.values():
            rule = why.removeprefix(claim_files.AUTO).split()[0]
            counts[rule] = counts.get(rule, 0) + 1
        unanswered = len(files.candidates) - len(quoted | set(files.dropped)) + len(found)
        per_rule = ", ".join(f"{rule} {n}" for rule, n in sorted(
            counts.items(), key=lambda kv: int(kv[0][1:])))
        print("\n".join([f"The rules dropped {len(found)} of the {unanswered} candidates with "
                         "no answer" + (f" ({per_rule})." if per_rule else ".")]))
        return 0
