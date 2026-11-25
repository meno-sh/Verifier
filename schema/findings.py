"""The tree one run produces: every paper check per document, and every claim check per claim.

Results are kept by check name, in `checks`, in the order the checks ran. The tree
does not know which checks exist: the checks registered for a run (see
core/registry/checks.py) decide that, and the same tree holds built-in checks, checks
written for one working directory and checks an extension adds alike. Each check's problems
carry their own kinds and severities, so nothing here needs to change when a check
is added or removed.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping

from schema import inputs, provenance
from schema.primitives import (
    CheckResult,
    Problem,
    RecomputeResult,
    record,
)

# The tree: one Findings per run, holding each document's results and each claim's.

@record
class Findings:
    """Everything one run found. The verdict is computed from this, never stored in it."""
    inputs: inputs.Inputs
    # each document's paper findings, by its path; empty when no document was read
    papers: Mapping[str, PaperFindings]
    claims: tuple[ClaimFindings, ...]
    unreadable_claims: tuple[inputs.UnreadableClaim, ...]   # entries the parser could not read
    # claims replaced by a later claim (Claim.superseded_by): listed, never checked
    superseded: tuple[inputs.Claim, ...] = ()
    # checks that were asked for and must not be skipped: if one of them is
    # Skipped, the verdict is MISSING INPUTS
    must_run: frozenset[str] = frozenset()
    # why no claim was checked, when the claim register could not be built; the claims are
    # then left out, and the verdict is MISSING INPUTS
    claim_checks_not_run_because: str | None = None

    def document_and_claim_findings(self) -> tuple[CheckResults, ...]:
        """Each document's findings, then each claim's, in order."""
        return (*self.papers.values(), *self.claims)

    def all_check_results(self) -> Iterator[CheckResult]:
        """Every check's result in the tree, documents first, then claims in order."""
        for part in self.document_and_claim_findings():
            yield from part.checks.values()

    def all_problems(self) -> list[Problem]:
        """Every Problem inside every Flagged result, documents first, then claims in order."""
        return [problem for part in self.document_and_claim_findings()
                for problem in part.problems()]


@record
class CheckResults:
    """The result of each check on one thing, a document or a claim, by check name."""
    checks: Mapping[str, CheckResult]

    def problems(self) -> list[Problem]:
        """Every Problem in the Flagged results."""
        return [p for result in self.checks.values() for p in result.problems_found()]


@record
class PaperFindings(CheckResults):
    """Every paper check's result on one document, by check name, and counts of what was read."""
    unreadable_inputs: tuple[str, ...]          # \input files that could not be read
    # counts the paper check groups report about what they read, by name
    facts: Mapping[str, int]
    # `checks` (CheckResults): by paper check name, e.g. {"dangling_ref": Passed(), ...}


@record
class ClaimFindings(CheckResults):
    """The gate's results and every claim check's result, for one claim in the claim register."""
    claim: inputs.Claim
    provenance: provenance.ProvenanceResult
    recompute: RecomputeResult
    # `checks` (CheckResults): by claim check name, in the order they ran

    @property
    def gate_failed(self) -> bool:
        """True when provenance or recompute is broken, so the claim cannot be verified."""
        return self.provenance.is_broken or self.recompute.is_broken

    def gate_status_word(self) -> str:
        """The one-word gate status shown in the terminal table."""
        if self.provenance.gate_status_word is not None:
            return self.provenance.gate_status_word
        if self.gate_failed:
            return self.recompute.gate_status_word or "BLOCK"
        return self.recompute.gate_status_word or "INTACT"

    def gate_note(self, register_folder: str | None = None) -> str:
        """The sentence shown next to the gate status: the recompute result once it ran.

        Paths are shown relative to `register_folder`, as the claim register wrote them.
        """
        shown = (self.recompute
                 if self.provenance.gate_note_describes_recompute
                 and not self.recompute.is_switched_off else self.provenance)
        return shown.describe_with_paths_relative_to(register_folder)
