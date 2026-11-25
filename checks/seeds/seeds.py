"""Seeds: does a result rest on enough seeds to support a reliability claim?

One or two seeds cannot separate an effect from luck, so the text must not claim
reliability on them. Reads `seeds` from the claim.
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
# fewer than three seeds
TOO_FEW_SEEDS = ProblemKind(code="too-few-seeds", severity=Severity.STRENGTH)


class SeedsCheck(interface.ClaimCheck):
    """Flag a claim resting on fewer than three seeds."""
    name = "seeds"
    title = "Enough seeds"
    why = ("Flags results resting on one or two seeds, which cannot support "
           "a reliability claim.")
    problem_kinds = (TOO_FEW_SEEDS,)
    fields = read_only({"seeds": inputs.FieldType.INTEGER})
    needs = ("seeds",)
    input_scope = interface.CheckInputScope(claims=interface.OWN_CLAIM)

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a claim resting on fewer than three seeds."""
        n = claim.fields["seeds"]
        problems = []
        if n < 3:
            problems.append(Problem(kind=TOO_FEW_SEEDS, detail=(
                f"only {n} seed(s); the text must not claim reliability")))
        return passed_or_flagged(problems)
