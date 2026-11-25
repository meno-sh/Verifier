"""The verdict computed from a Findings tree.

Integrity failures (REJECT) come only from the gate (provenance and recompute). An input
file or a claim register entry that cannot be read is a missing input (MISSING INPUTS).
Every problem a check reports is soundness or presentation (blocks until fixed), strength
(must be disclosed) or a caveat, as its ProblemKind says. Keeping these apart stops a
strong result from buying off a fabricated one.
"""
from __future__ import annotations

from schema import findings
from schema.primitives import Severity, Skipped
from schema.verdict import Verdict


class VerdictPolicy:
    """Gives the one verdict of a Findings: the worst condition wins."""

    def verdict_for(self, tree: findings.Findings) -> Verdict:
        """Worst first. Counts never matter: one integrity failure is enough to reject.

        A check that was asked for and must run (tree.must_run, such as one that calls a
        paid model) but was Skipped is checked after REJECT and BLOCK UNTIL FIXED, so a
        provider error never hides a broken claim, and before the passing verdicts, so
        the run cannot pass without it. Claim checks that did not run at all
        (tree.claim_checks_not_run_because) are checked at the same place.
        """
        if (any(paper.unreadable_inputs for paper in tree.papers.values())
                or tree.unreadable_claims):
            return Verdict.MISSING_INPUTS
        if any(c.gate_failed for c in tree.claims):
            return Verdict.REJECT
        severities = {p.kind.severity for p in tree.all_problems()}
        if {Severity.SOUNDNESS, Severity.PRESENTATION} & severities:
            return Verdict.BLOCK_UNTIL_FIXED
        if self.a_required_check_was_skipped(tree) or tree.claim_checks_not_run_because:
            return Verdict.MISSING_INPUTS
        if Severity.STRENGTH in severities:
            return Verdict.PASS_WITH_DISCLOSURE
        if Severity.CAVEAT in severities or self.some_check_did_not_run(tree):
            return Verdict.PASS_WITH_CAVEATS
        return Verdict.PASS

    @staticmethod
    def a_required_check_was_skipped(tree: findings.Findings) -> bool:
        """True when a check that was asked for and must run was Skipped instead.

        Which checks must run is decided when the run is set up, from the registration of
        each check (`must_run_if_requested`), and recorded in tree.must_run. A run must
        not pass without a check it asked for, whatever kept it from running.
        """
        return any(isinstance(part.checks.get(name), Skipped)
                   for part in tree.document_and_claim_findings() for name in tree.must_run)

    @staticmethod
    def some_check_did_not_run(tree: findings.Findings) -> bool:
        """True when some check did not run, or some claim had nothing to check it against.

        A check that did not run is NotChecked (a key was missing) or Skipped (something
        it needs did not run, such as recompute). A claim whose file only matches a
        checksum still waiting for the confirmation of a person (provisional) is a caveat
        too. A Skipped check after a broken gate never reaches here, because the verdict
        is already REJECT.
        """
        return (any(result.counts_as_caveat for result in tree.all_check_results())
                or any(c.provenance.counts_as_caveat for c in tree.claims))
