"""The state of each dispute now: withdrawn, waiting for a diagnosis, or diagnosis written."""
from __future__ import annotations

from pathlib import Path

from diagnosis.diagnosis_file_check import DiagnosisFileChecker
from diagnosis.dispute_store import DisputeLedger, DisputeStore
from diagnosis.records import (
    Dispute,
    DisputeDiagnosisWritten,
    DisputeState,
    DisputeWaitingForDiagnosis,
    DisputeWithdrawn,
)
from schema.primitives import record


@record
class DisputeWithState:
    """One dispute and its state now."""
    dispute: Dispute
    state: DisputeState


class DisputeStateResolver:
    """Gives the state of each dispute from the ledger and its diagnosis file now."""

    def __init__(self, state_dir: Path) -> None:
        """The disputes of `state_dir`."""
        self.store = DisputeStore(state_dir)
        self.checker = DiagnosisFileChecker()

    def state_of_dispute(self, dispute: Dispute, ledger: DisputeLedger) -> DisputeState:
        """The state of one dispute. A diagnosis with a problem leaves it waiting."""
        if ledger.withdrawal_for(dispute.dispute_id) is not None:
            return DisputeWithdrawn()
        document = self.store.read_diagnosis(dispute.dispute_id)
        if document is None or self.checker.problems_of_diagnosis(document, dispute.flags):
            return DisputeWaitingForDiagnosis()
        return DisputeDiagnosisWritten(diagnosis=document)

    def states_of_all_disputes(self) -> list[DisputeWithState]:
        """Every dispute ever raised, oldest first, with its state."""
        ledger = self.store.read_ledger()
        return [DisputeWithState(dispute=d, state=self.state_of_dispute(d, ledger))
                for d in ledger.disputes]

    def state_of_dispute_with_id(self, dispute_id: str) -> DisputeWithState | None:
        """The dispute with this id and its state, or None when there is no such dispute."""
        ledger = self.store.read_ledger()
        dispute = ledger.dispute_with_id(dispute_id)
        return (None if dispute is None
                else DisputeWithState(dispute=dispute,
                                      state=self.state_of_dispute(dispute, ledger)))

    def count_waiting_for_diagnosis(self) -> int:
        """How many disputes wait for a diagnosis."""
        return sum(d.state.is_waiting for d in self.states_of_all_disputes())
