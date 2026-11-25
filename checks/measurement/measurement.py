"""Measurement: did the result survive with its sample intact?

A metric that improves while its sample collapses is usually the measurement being
destroyed rather than the model improving, especially when the dropped episodes are
the ones carrying the signal. The check fires when the scored episodes fall more than
10% below the baseline's count. It compares counts only, so it cannot tell which
episodes were dropped.

Reads `baseline_n` from the claim, and the episode count recompute found.
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
# the scored sample shrank against its baseline
SAMPLE_SHRANK = ProblemKind(code="sample-shrank", severity=Severity.STRENGTH)


class MeasurementCheck(interface.ClaimCheck):
    """Flag a scored sample more than 10% smaller than the baseline's."""
    name = "measurement"
    title = "Measurement survived"
    why = ("Flags a result scored on far fewer episodes than its baseline, "
           "a sign the measurement was destroyed.")
    problem_kinds = (SAMPLE_SHRANK,)
    fields = read_only({"baseline_n": inputs.FieldType.INTEGER})
    needs = ("baseline_n",)
    needs_recompute = True
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a scored sample more than 10% smaller than the baseline's."""
        assert recomputed is not None
        baseline_n = claim.fields["baseline_n"]
        problems = []
        if recomputed.n < 0.9 * baseline_n:
            problems.append(Problem(kind=SAMPLE_SHRANK, detail=(
                f"scored episodes dropped to {recomputed.n} from {baseline_n} "
                f"-- measurement may be destroyed, not improved")))
        return passed_or_flagged(problems)
