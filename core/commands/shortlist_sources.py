"""`vcheck shortlist-sources`, a command of the auditing agent.

Write the likely source files of each claim with no source to source_shortlists.json.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core import cli_command
from core.methods import source_index
from core.project import watched_project
from schema import inputs
from utils import extract, file_storage, structure, text_search

SHORTLISTS = "source_shortlists.json"      # in the state folder, beside the claim files
# the text search of rule 4 of the short list; any class of the TextSearch protocol
TEXT_SEARCH: type[text_search.TextSearch] = text_search.Bm25TextSearch


class VcheckShortlistSourcesCommand(cli_command.CLICommand):
    """Runs `vcheck shortlist-sources` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck shortlist-sources`."""
        p.add_argument("--commit", default=None,
                       help="the commit whose changed files rank higher; by default the "
                            "commit the daemon checks")

    def execute(self, args: argparse.Namespace) -> int:
        """Update the number index, and write the short list of each claim with no source.

        The claims with no source are those of the claim register that have no `source`
        and are not superseded. The numbers of a claim are its value and the numbers of
        its statement. The file holds {"commit": SHA, "claims": {claim id: [candidate]}},
        each candidate as SourceCandidate.to_json gives it.
        """
        root: Path = args.root
        state_dir: Path = args.state
        project = watched_project.WatchedProject.load(root, state_dir)
        register = project.claim_register(project.check_registry())
        commit = args.commit or structure.checked_commit(state_dir)
        changed = structure.changed_files(str(root), commit) if commit else []
        index = source_index.SourceIndex(root, state_dir)
        read, unreadable = index.update()
        search = TEXT_SEARCH.for_project(root, index.documents())
        lists: dict[str, list[dict[str, object]]] = {}
        for claim in register.claims:
            if claim.source is not None or claim.superseded_by is not None:
                continue
            numbers = extract.numbers(claim.statement or "")
            if claim.value is not None:
                numbers.append(claim.value.text if isinstance(claim.value, inputs.PrintedFloat)
                               else repr(claim.value))
            lists[claim.id] = [c.to_json() for c in index.shortlist(
                numbers, claim.statement or claim.id, changed, search)]
        file_storage.JsonFile(state_dir / SHORTLISTS, indent=2).write(
            {"commit": commit, "claims": lists})
        found = sum(1 for candidates in lists.values() if candidates)
        print(f"index: {read} files read again; {len(unreadable)} files could not be read")
        for line in unreadable:
            print(f"  {line}")
        print(f"{found} of {len(lists)} claims with no source have a short list in {SHORTLISTS}")
        for claim_id, candidates in lists.items():
            for c in candidates:
                where = f" row {c['row']}" if c["row"] is not None else ""
                print(f"  {claim_id}: {c['rank']}. {c['path']}{where} ({c['rule']})")
        return 0
