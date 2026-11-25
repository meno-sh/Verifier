"""Recompute results: does the claim's number come back from the data?

One result per claim. The claim's computational method reads the data and
returns a number; recompute compares it with the claim. Matches is the only result
carrying numbers, and claim checks registered with needs_recompute read them.
"""
from __future__ import annotations

from schema.inputs import PrintedFloat
from schema.primitives import MethodName, RecomputeResult, record

# What recompute found for one claim: a subclass of RecomputeResult (schema/primitives.py).
# Skipped means provenance did not find the files intact, or the claim is Traced. Off
# means recompute was switched off. The verdict reads `is_broken`, never the classes.


# Fine: the number came back.

@record
class Matches(RecomputeResult):
    """Recomputed within tolerance of what the paper prints."""
    gate_status_word = "PASS"
    value: float
    n: int
    interval: tuple[float, float] | None     # as Computed.interval gave it
    tolerance: float

    def describe_for_reader(self) -> str:
        """The recomputed number and its count."""
        return f"{self.value:.6g} from n={self.n}"

    def recomputed_number(self) -> Matches:
        """This result, which holds the recomputed number."""
        return self


# Broken: the number did not come back, and the verdict is REJECT.

@record
class Disagrees(RecomputeResult):
    """The data gives a different number from the paper."""
    is_broken = True
    board_code = "number-disagrees"
    gate_status_word = "FAIL"
    paper_value: float
    value: float
    n: int
    tolerance: float

    def describe_for_reader(self) -> str:
        """The number of the claim, as printed, and the number of the data."""
        printed = self.paper_value
        written = printed.text if isinstance(printed, PrintedFloat) else f"{printed:g}"
        return (f"the claim says {written}, the data gives {self.value:.6g} "
                f"(tolerance {self.tolerance:g})")


@record
class NotANumber(RecomputeResult):
    """The computational method produced NaN."""
    is_broken = True
    board_code = "not-a-number"
    method: MethodName

    def describe_for_reader(self) -> str:
        """That the method gave no number."""
        return f"recomputing with {self.method} gave no number (no usable records?)"


@record
class MethodFailed(RecomputeResult):
    """The computational method raised instead of returning a number."""
    is_broken = True
    board_code = "method-failed"
    method: MethodName
    error: str

    def describe_for_reader(self) -> str:
        """The method and its error."""
        return f"the computational method {self.method} failed: {self.error}"


@record
class SetupDisagrees(RecomputeResult):
    """The method reported a setup that differs from the setup the claim states."""
    is_broken = True
    board_code = "setup-mismatch"
    field: str
    paper_says: str
    record_says: str

    def describe_for_reader(self) -> str:
        """The setup field, as the claim and the data record give it."""
        return (f"the claim says {self.field}={self.paper_says!r}, the data's own record says "
                f"{self.record_says!r}")


@record
class SetupUnrecorded(RecomputeResult):
    """The claim states a setup, but the method reported none, or not this field."""
    is_broken = True
    board_code = "setup-mismatch"
    field: str | None           # None when the method reported no setup at all

    def describe_for_reader(self) -> str:
        """That the method reported no setup, or not this field."""
        if self.field is None:
            return "the claim states a setup, but its method reported none"
        return (f"the method's report does not state {self.field}, so the claim's setup "
                "cannot be checked")
