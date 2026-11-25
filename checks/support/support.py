"""Support: is a claim inferred from other claims free of circular support?

A claim may name other claims in the register as its source. If following those
names leads back to the claim, the claim supports itself, which is circular
reasoning. This check names the cycle. It reads only the register, so it is
deterministic.
"""
from collections.abc import Mapping

from checks import interface
from schema import inputs, recompute
from schema.primitives import (
    CheckResult,
    ClaimId,
    Problem,
    ProblemKind,
    Severity,
    passed_or_flagged,
)

# The problem kinds this check reports, each with its severity.
# the claim is inferred, through other claims, from itself
CIRCULAR_SUPPORT = ProblemKind(code="circular-support", severity=Severity.SOUNDNESS)


class AcyclicSupportCheck(interface.ClaimCheck):
    """Flag a claim that lies on a cycle of claims sources, naming the cycle in order."""
    name = "acyclic_support"
    title = "No circular support"
    why = ("Flags a claim inferred from other claims that lead back to the "
           "claim itself.")
    problem_kinds = (CIRCULAR_SUPPORT,)
    needs_dependencies = True

    # the whole support graph, which is cheap to check
    input_scope = interface.CheckInputScope(claims=interface.WHOLE_REGISTER)

    def reads_findings_of_claims_it_rests_on(self, options: Mapping[str, object]) -> bool:
        """Never: the check reads only the register, not the findings of other claims."""
        return False

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: interface.ClaimContext) -> CheckResult:
        """Flag a claim that lies on a cycle of claims sources, naming the cycle in order."""
        cycle = cycle_through(claim.id, context.register)
        problems = []
        if cycle is not None:
            problems.append(Problem(kind=CIRCULAR_SUPPORT, detail=(
                f"{claim.id} is inferred from itself: {' -> '.join(cycle)}")))
        return passed_or_flagged(problems)


def cycle_through(start: ClaimId,
                  register: inputs.ClaimsById) -> tuple[ClaimId, ...] | None:
    """The shortest path of claims sources from `start` back to itself, or None.

    The path starts and ends with `start`. Ids that are not in the register are not
    followed; the parser makes such a register unreadable anyway.
    """
    paths: dict[ClaimId, tuple[ClaimId, ...]] = {start: (start,)}
    frontier = [start]
    while frontier:
        following = []
        for cid in frontier:
            for dep in register[cid].ids_of_claims_it_is_inferred_from():
                if dep == start:
                    return (*paths[cid], start)
                if dep in register and dep not in paths:
                    paths[dep] = (*paths[cid], dep)
                    following.append(dep)
        frontier = following
    return None
