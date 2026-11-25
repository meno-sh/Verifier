"""`vcheck test-check`: run one check on one claim, after the gate, and print its result."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core import cli_command
from core.analysis import analysis_request, findings_analyzer, findings_json
from core.project import watched_project
from core.registry import plugin_checks
from schema import inputs
from schema.primitives import CheckCrashed


class VcheckTestCheckCommand(cli_command.CLICommand):
    """Runs `vcheck test-check` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck test-check`."""
        p.add_argument("name")
        p.add_argument("--claim", required=True)

    def execute(self, args: argparse.Namespace) -> int:
        """Run one check on one claim, after the gate, and print its result.

        The whole register goes through the gate, with every other claim check off, so a
        check that reads the findings of the claims a claim is inferred from sees them.
        """
        root: Path = args.root
        state_dir: Path = args.state
        project = watched_project.WatchedProject.load(root, state_dir)
        reg = project.check_registry()
        name = (args.name if args.name in reg.claim_checks
                else plugin_checks.PLUGIN_CHECK_PREFIX + args.name)
        if name not in reg.claim_checks:
            print(f"error: no check named {args.name!r}", file=sys.stderr)
            return 1
        claim, by_id = project.claim_with_id(reg, args.claim)
        if not reg.claim_checks[name].applies_to_claim(claim):
            print(f"error: {name} does not apply to {claim.id}", file=sys.stderr)
            return 1
        # The check runs on the claim and the claims it rests on, not on the whole register,
        # so a check that calls paid models makes only the calls this claim needs.
        tree = findings_analyzer.FindingsAnalyzer(reg).analyse(analysis_request.AnalysisRequest(
            claim_register_path=str(state_dir / watched_project.WatchedProject.REGISTER_FILE),
            claim_register_base_folder=str(root), switches_on=frozenset({"recompute", name}),
            run_options=project.run_options(reg),
            claims_with_claim_checks=self.ids_it_rests_on(claim, by_id)))
        found = [c for c in tree.claims if c.claim.id == claim.id]
        if not found:
            print(f"error: {claim.id} is superseded, so no check runs on it", file=sys.stderr)
            return 1
        result = found[0].checks[name]
        print(json.dumps(findings_json.FindingsJsonCodec().encode(result), indent=2))
        return 1 if isinstance(result, CheckCrashed) else 0

    @staticmethod
    def ids_it_rests_on(claim: inputs.Claim, by_id: dict[str, inputs.Claim]) -> frozenset[str]:
        """The claim's id and the ids of every claim it is inferred from, directly or not."""
        found, todo = {claim.id}, list(claim.ids_of_claims_it_is_inferred_from())
        while todo:
            cid = todo.pop()
            if cid not in found:
                found.add(cid)
                if cid in by_id:
                    todo.extend(by_id[cid].ids_of_claims_it_is_inferred_from())
        return frozenset(found)
