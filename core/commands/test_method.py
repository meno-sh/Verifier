"""`vcheck test-method`, a command of the auditing agent.

Recompute one claim with the named method, as `vcheck run` would, and print the result.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

from core import cli_command
from core.analysis import findings_json
from core.gate import artifact_pins
from core.gate import provenance as provenance_check
from core.gate import recompute as recompute_check
from core.project import watched_project
from schema import inputs, recompute


class VcheckTestMethodCommand(cli_command.CLICommand):
    """Runs `vcheck test-method` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck test-method`."""
        p.add_argument("name")
        p.add_argument("--claim", required=True)

    def execute(self, args: argparse.Namespace) -> int:
        """Recompute one claim with the named method, as `vcheck run` does; print the result."""
        root: Path = args.root
        state_dir: Path = args.state
        reg = watched_project.WatchedProject.load(root, state_dir).check_registry()
        if args.name not in reg.computational_methods:
            print(f"error: no computational method named {args.name!r}", file=sys.stderr)
            return 1
        claim, _ = watched_project.WatchedProject.load(root, state_dir).claim_with_id(
            reg, args.claim)
        if not isinstance(claim.source, inputs.ArtifactSource):
            print(f"error: {claim.id} has no artifact source", file=sys.stderr)
            return 1
        claim = dataclasses.replace(claim, source=dataclasses.replace(claim.source,
                                                                      method=args.name))
        start = time.monotonic()
        found = provenance_check.ProvenanceChecker(
            artifact_pins.ArtifactPins()).check_claim_source(claim)
        got = recompute_check.ClaimRecomputer(reg.computational_methods).recompute_claim(
            claim, found)
        seconds = round(time.monotonic() - start, 2)
        if isinstance(got, recompute.MethodFailed):
            print(f"error: {args.name} failed: {got.error}", file=sys.stderr)
            return 1
        print(json.dumps({"claim": claim.id, "claimed": claim.value,
                          "result": findings_json.FindingsJsonCodec().encode(got),
                          "seconds": seconds}, indent=2))
        return 0
