"""The state of each review request now, from the ledger, its kind and the files of the project."""
from __future__ import annotations

from pathlib import Path

from review.kinds import ReviewKindRegistry
from review.store import ReviewLedger
from review.types import (
    ReviewConfirmed,
    ReviewDeclined,
    ReviewRequest,
    ReviewState,
    ReviewSuperseded,
    ReviewWaitingForAnswer,
    ReviewWithdrawn,
)


class ReviewStateResolver:
    """Gives the state of each request from the ledger, the kind, and the project files now."""

    def __init__(self, kinds: ReviewKindRegistry, root: Path) -> None:
        """Resolve states with these kinds, in the working directory `root`."""
        self.kinds = kinds
        self.root = root

    def state_of_request(self, request: ReviewRequest, ledger: ReviewLedger) -> ReviewState:
        """The request's state now: withdrawn, superseded, answered, or waiting for an answer."""
        withdrawal = ledger.withdrawal_for(request.id)
        if withdrawal is not None:
            return ReviewWithdrawn(reason=withdrawal.reason)
        kind = self.kinds.kind_named(request.kind)
        if not kind.facts_still_hold(request.facts, self.root):
            return ReviewSuperseded()
        decision = ledger.decision_for(request.id)
        if decision is None:
            return ReviewWaitingForAnswer()
        if kind.option_is_positive(decision.option):
            return ReviewConfirmed(answer=decision)
        return ReviewDeclined(answer=decision)

    def requests_waiting_for_answer(self, ledger: ReviewLedger) -> list[ReviewRequest]:
        """Every request waiting for an answer, oldest first."""
        return [r for r in ledger.requests
                if self.state_of_request(r, ledger).is_waiting]
