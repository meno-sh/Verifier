"""The records of the diagnosis extension: a dispute, its withdrawal, and its state now."""
from __future__ import annotations

from core.report_model.disputed_flags import DisputedFlag
from core.report_model.issues import FlagDispute
from core.report_output.saved_report import SavedFlag
from diagnosis.diagnosis_document import DiagnosisDocument
from schema.errors import VerifierError
from schema.primitives import record
from utils.computed_state import ComputedState


class DisputeError(VerifierError, ValueError):
    """A dispute or withdrawal the store refuses, with the reason."""


@record
class Dispute:
    """One saved dispute: its id D-N, the commit, the reason, the time, and its flags."""
    dispute_id: str
    commit: str
    reason: str
    raised_at: str
    flags: tuple[SavedFlag, ...]

    def disputed_flags_for_report(self) -> list[DisputedFlag]:
        """Each flag of the dispute, as the report reads it."""
        dispute = FlagDispute(dispute_id=self.dispute_id, reason=self.reason)
        return [DisputedFlag(code=f.code, subject=f.subject, detail=f.detail, dispute=dispute)
                for f in self.flags]

    @classmethod
    def from_json(cls, row: dict[str, object]) -> Dispute:
        """Read one row of disputes.jsonl."""
        flags = row["flags"]
        assert isinstance(flags, list)
        return cls(dispute_id=str(row["id"]), commit=str(row["commit"]),
                   reason=str(row["reason"]), raised_at=str(row["at"]),
                   flags=tuple(SavedFlag.from_json(f) for f in flags))

    def to_json(self) -> dict[str, object]:
        """The row of disputes.jsonl."""
        return {"id": self.dispute_id, "commit": self.commit, "reason": self.reason,
                "at": self.raised_at, "flags": [f.to_json() for f in self.flags]}


@record
class DisputeWithdrawal:
    """One withdrawal of a dispute: its id and the time. A row of withdrawals.jsonl."""
    dispute_id: str
    at: str

    @classmethod
    def from_json(cls, row: dict[str, object]) -> DisputeWithdrawal:
        """Read one row of withdrawals.jsonl."""
        return cls(dispute_id=str(row["id"]), at=str(row["at"]))

    def to_json(self) -> dict[str, object]:
        """The row of withdrawals.jsonl."""
        return {"id": self.dispute_id, "at": self.at}


# A dispute's state, computed on every read.

@record
class DisputeState(ComputedState):
    """The state of a dispute now. It waits while its diagnosis is not complete."""


@record
class DisputeWithdrawn(DisputeState):
    """A person or the worker's agent withdrew the dispute. Its flags count again."""
    state_word = "withdrawn"


@record
class DisputeWaitingForDiagnosis(DisputeState):
    """The auditing agent has not written a complete diagnosis yet."""
    state_word = "waiting for a diagnosis"
    is_waiting = True


@record
class DisputeDiagnosisWritten(DisputeState):
    """The auditing agent wrote a diagnosis with no problem."""
    state_word = "diagnosis written"
    diagnosis: DiagnosisDocument
