"""Interval: is the uncertainty interval strong enough for what the text says?

Flags an interval that excludes zero only barely (fragile to one more seed), and a
claim of significance contradicted by an interval that spans zero.

It reads the interval the claim's computational method returns (`Computed.interval`,
kept in `Matches.interval`); a claim whose method gives none is reported as not checked. Reads
`claimed_significant` from the claim.
"""
from checks import interface
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    Problem,
    ProblemKind,
    Severity,
    Skipped,
    passed_or_flagged,
    read_only,
)

# The problem kinds this check reports, each with its severity.
# the interval only barely excludes zero
FRAGILE_INTERVAL = ProblemKind(code="fragile-interval", severity=Severity.STRENGTH)
# significance claimed while the interval spans zero
SIGNIFICANCE_CONTRADICTED = ProblemKind(code="significance-contradicted",
                                        severity=Severity.STRENGTH)


class IntervalCheck(interface.ClaimCheck):
    """Flag a fragile interval, and significance claimed over an interval spanning zero."""
    name = "interval"
    title = "Interval supports the claim"
    why = ("Flags intervals that only barely exclude zero, and significance "
           "claimed while the interval spans zero.")
    problem_kinds = (FRAGILE_INTERVAL, SIGNIFICANCE_CONTRADICTED,)
    fields = read_only({"claimed_significant": inputs.FieldType.BOOLEAN})
    needs_recompute = True
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a fragile interval, and significance claimed over an interval spanning zero."""
        assert recomputed is not None
        if recomputed.interval is None:
            return Skipped(because="the computational method gives no interval")
        lo, hi = recomputed.interval
        problems = []
        if lo * hi > 0 and min(abs(lo), abs(hi)) < 0.02:
            problems.append(Problem(kind=FRAGILE_INTERVAL, detail=(
                f"CI excludes zero by only {min(abs(lo), abs(hi)):.3f} -- fragile to one "
                "more seed")))
        if lo < 0 < hi and claim.fields.get("claimed_significant"):
            problems.append(Problem(kind=SIGNIFICANCE_CONTRADICTED, detail=(
                "the text claims significance but the interval spans zero")))
        return passed_or_flagged(problems)
