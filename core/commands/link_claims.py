"""`vcheck link-claims`, the claim step that writes the links between claims.

The daemon runs it after the agent's round, after `vcheck reorder-claims` and before `vcheck
run` (runtime/daemon.py). A model reads the full paper and links each claim that has no source
yet (core/claim_steps/full_paper_linker.py); a link to a claim before it in the register order
is dropped. The claim files lock is not held while the model
answers.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import cli_command
from core.claim_steps import full_paper_linker
from core.project import watched_project
from utils import claim_files, shared_paper_prefix


class VcheckLinkClaimsCommand(cli_command.CLICommand):
    """Runs `vcheck link-claims` (runtime/daemon.py runs it after each agent round)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck link-claims`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Link the claims with no source, then write the links into the register.

        The register is read under the lock, the model is asked without it, and the register
        is read again under the lock before the links are written. Only the answers to the
        register as it is now are written. The paper is watched_project.full_paper_text. When a
        claim file is broken, nothing is linked: `vcheck run` then reports the broken file.
        """
        root: Path = args.root
        state_dir: Path = args.state
        try:
            with claim_files.lock(state_dir).held():     # loading can begin a round, which writes
                files, _ = claim_files.loaded(state_dir, root)
        except claim_files.ClaimFilesBroken as e:
            print(f"No claim is linked, because a claim file is broken: {e}")
            return 0
        claims = register_claims(files)
        project = watched_project.WatchedProject.load(root, state_dir)
        settings = full_paper_linker.FullPaperLinkerSettings.from_options(
            project.run_options(project.check_registry()))
        done = frozenset((row["claim"], row["register_sha256"])
                         for row in full_paper_linker.read_results(state_dir))
        todo = full_paper_linker.claims_to_link(claims, settings.tables, done,
                                                full_paper_linker.register_checksum(claims))
        if not todo:
            print("No claim needs links: each claim has a source, or was asked with this "
                  "register.")
            return 0
        paper_text = project.full_paper_text()
        if paper_text is None:
            print("error: vcheck link-claims reads the full paper, but the project has no "
                  "document and no note file", file=sys.stderr)
            return 1
        try:
            counts = full_paper_linker.link_claims(
                paper_text, claims, todo, settings,
                save=lambda row: full_paper_linker.append_result(state_dir, row))
        except shared_paper_prefix.PaperTooLongForModelContext as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        with claim_files.lock(state_dir).held():
            files, _ = claim_files.loaded(state_dir, root)
            checksum = full_paper_linker.register_checksum(register_claims(files))
            rows = [row for row in full_paper_linker.read_results(state_dir)
                    if row["register_sha256"] == checksum]
            changed = full_paper_linker.apply_links(files.register, rows)
            claim_files.save(files, state_dir)
        print(f"{settings.model} linked {counts.linked} claims ({counts.failed} failed, asked "
              f"again in the next round); {changed} claims got links; "
              f"{counts.links_against_order} links against the claim order were dropped")
        return 0


def register_claims(files: claim_files.ClaimFiles) -> list[dict]:
    """The claims of the register that have a statement, in register order.

    A claim with no statement, such as an artifact claim with only a value, cannot be shown
    to the model, so it is neither linked nor a link.
    """
    return [e for e in files.register
            if isinstance(e, dict) and "id" in e and isinstance(e.get("statement"), str)]
