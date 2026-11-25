"""The numbers that the claim checks measured, such as the uncertainty of a claim."""
from __future__ import annotations

from schema import findings
from schema.primitives import Flagged, Passed, record


@record
class MeasuredNumber:
    """One number a claim check measured on a claim."""
    claim_id: str
    check_name: str
    value: float


class MeasuredNumberCollector:
    """Lists the numbers that the claim checks measured."""

    @staticmethod
    def collect_measured_numbers(tree: findings.Findings) -> list[MeasuredNumber]:
        """Each number a claim check measured, on a claim whose gate did not fail."""
        return [MeasuredNumber(claim_id=c.claim.id, check_name=name, value=result.measured)
                for c in tree.claims if not c.gate_failed
                for name, result in c.checks.items()
                if isinstance(result, Passed | Flagged) and result.measured is not None]
