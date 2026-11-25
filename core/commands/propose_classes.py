"""`vcheck propose-classes`, a claim step that code does before the agent's round.

Ask models to put the quotes in no class into classes; write the proposal to a file, which
`vcheck accept-classes` then takes.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.claim_steps import duplicates
from core.project import watched_project
from utils import claim_files


class VcheckProposeClassesCommand(cli_command.CLICommand):
    """Runs `vcheck propose-classes`, a claim step of code (runtime/daemon.py)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck propose-classes`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Ask models to put the quotes in no class into classes; write the proposal."""
        root: Path = args.root
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():     # loading can begin a round, which writes
            files, r = claim_files.loaded(state_dir, root)
        if problems := claim_files.quote_problems(files, r):
            print(f"FAILED. Stage 1 (QUOTES) has {len(problems)} problems.")
            return 1
        items = claim_files.proposal_items(files)
        if not items:
            new = claim_files.new_quotes(files)
            if not new:
                print("No quote is new. There is nothing to propose.")
                return 0
            # Every new quote is a whole table, a claim of its own: a proposal with no group,
            # so the CLASSES stage can pass, with each table in a class of its own.
            path = claim_files.write_proposal(state_dir, r, {}, [])
            print("\n".join([f"None of the {len(new)} new quotes needs grouping: each is a "
                             "whole table, which is a claim of its own. "
                             "Each gets a class of its own. The proposal has no group:",
                             f"  {path.relative_to(state_dir.parent)}"]))
            return 0
        project = watched_project.WatchedProject.load(root, state_dir)
        options = project.run_options(project.check_registry())
        settings = duplicates.Settings.from_options(options)
        count = duplicates.CallCount()
        # the classes of earlier rounds are only compared with the new quotes
        settled = frozenset(k for k in items if k.startswith("class:"))
        partition = duplicates.propose(items, settings, call_count=count, settled=settled,
                                       values=claim_files.item_values(files, items))
        path = claim_files.write_proposal(state_dir, r, items, partition)
        quotes = sum(k.startswith("quote:") for k in items)
        what = f"the {quotes} quotes" if quotes == len(items) else \
            f"the {quotes} new quotes and the {len(items) - quotes} existing classes"
        calls = f"{count.calls} model calls" + (
            f", {count.failed} of them failed and were left out" if count.failed else "")
        print("\n".join([f"The search put {what} into {len(partition)} equivalence "
                         f"classes ({calls}). The proposal:",
                         f"  {path.relative_to(state_dir.parent)}"]))
        return 0
