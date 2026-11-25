"""Baseline: does a result beat the trivial baseline it declares?

A skill score that never beats always-predicting-the-base-rate is a real finding,
but it belongs on the strength axis: it may be the honest result, and blocking it
would teach authors to hide weak numbers.

Reads `baseline` and `lower_is_better` from the claim, and the recomputed value.
A lower-is-better result must score below its baseline, and any other must score
above it.
"""
from checks import interface
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    Problem,
    ProblemKind,
    Severity,
    passed_or_flagged,
    read_only,
)

# The problem kinds this check reports, each with its severity.
# the result does not beat the baseline it declares
BELOW_BASELINE = ProblemKind(code="below-baseline", severity=Severity.STRENGTH)


class BaselineCheck(interface.ClaimCheck):
    """Flag a result that does not beat its declared baseline, in the metric's direction."""
    name = "baseline"
    title = "Beats a trivial baseline"
    why = ("Does the result beat always predicting the base rate, "
           "the baseline it declares?")
    problem_kinds = (BELOW_BASELINE,)
    fields = (read_only({"baseline": inputs.FieldType.NUMBER,
              "lower_is_better": inputs.FieldType.BOOLEAN}))
    needs = ("baseline", "lower_is_better")
    needs_recompute = True
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a result that does not beat its declared baseline, in the metric's direction."""
        assert recomputed is not None
        trivial = claim.fields["baseline"]
        if claim.fields["lower_is_better"]:
            beats, direction = recomputed.value < trivial, "lower"
        else:
            beats, direction = recomputed.value > trivial, "higher"
        problems = []
        if not beats:
            problems.append(Problem(kind=BELOW_BASELINE, detail=(
                f"does not beat the trivial baseline: scored {recomputed.value:.3f} "
                f"against a baseline of {trivial:.3f}, and {direction} is better here, "
                f"so this result is no better than always predicting the base rate")))
        return passed_or_flagged(problems)
