"""Review requests and decisions, kept as append-only JSON lines in `.verifier/reviews/`.

    requests.jsonl      one ReviewRequest per line
    decisions.jsonl     one ReviewDecision per line
    withdrawals.jsonl   one ReviewWithdrawal per line

Nothing is ever rewritten. A request's state is computed from the three files and
from whether its facts still hold in the working directory (review/states.py).
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from review.types import ReviewDecision, ReviewRequest, ReviewWithdrawal
from schema.errors import VerifierError
from schema.primitives import record
from utils import file_storage, structure

REVIEWS_DIR = "reviews"
REQUESTS, DECISIONS, WITHDRAWALS = "requests.jsonl", "decisions.jsonl", "withdrawals.jsonl"
# Held while a command reads the store and appends to it. It is in runtime/, which the
# state's git history leaves out.
LOCK_FILE = Path(structure.RUNTIME_DIR) / "reviews.lock"


class ReviewError(VerifierError, ValueError):
    """A request or decision the store refuses, with the reason."""


@record
class ReviewLedger:
    """All requests, decisions and withdrawals, read once, oldest first."""
    requests: tuple[ReviewRequest, ...]
    decisions: tuple[ReviewDecision, ...]
    withdrawals: tuple[ReviewWithdrawal, ...]

    def request_with_id(self, request_id: str) -> ReviewRequest:
        """One request by id.

        Raises:
            ReviewError: there is no request with this id.
        """
        for r in self.requests:
            if r.id == request_id:
                return r
        raise ReviewError(f"no review request {request_id!r}")

    def decision_for(self, request_id: str) -> ReviewDecision | None:
        """The first decision on a request, or None."""
        return next((d for d in self.decisions if d.request == request_id), None)

    def withdrawal_for(self, request_id: str) -> ReviewWithdrawal | None:
        """The first withdrawal of a request, or None."""
        return next((w for w in self.withdrawals if w.request == request_id), None)

    def requests_of_kind(self, kind_name: str) -> list[ReviewRequest]:
        """The requests of one kind, oldest first."""
        return [r for r in self.requests if r.kind == kind_name]


class ReviewStore:
    """Reads and appends the three files of `.verifier/reviews/`.

    It knows no rule about states or kinds.
    """

    def __init__(self, state_dir: Path) -> None:
        """Open the reviews in `state_dir`."""
        self.folder = file_storage.AppendOnlyJsonLinesFolder(
            state_dir / REVIEWS_DIR, file_storage.FileLock(state_dir / LOCK_FILE))
        self.requests_file = self.folder.file(REQUESTS)
        self.decisions_file = self.folder.file(DECISIONS)
        self.withdrawals_file = self.folder.file(WITHDRAWALS)

    def read_ledger(self) -> ReviewLedger:
        """Every request, decision and withdrawal."""
        return ReviewLedger(
            requests=tuple(ReviewRequest.from_json(r) for r in self.requests_file.read_rows()),
            decisions=tuple(ReviewDecision.from_json(d)
                            for d in self.decisions_file.read_rows()),
            withdrawals=tuple(ReviewWithdrawal.from_json(w)
                              for w in self.withdrawals_file.read_rows()))

    @contextlib.contextmanager
    def holding_lock(self) -> Iterator[None]:
        """Hold the store lock for the block."""
        with self.folder.holding_lock():
            yield

    def append_request(self, request: ReviewRequest) -> None:
        """Save a new request."""
        self.requests_file.append_row(request.to_json())

    def append_decision(self, decision: ReviewDecision) -> None:
        """Save a decision."""
        self.decisions_file.append_row(decision.to_json())

    def append_withdrawal(self, withdrawal: ReviewWithdrawal) -> None:
        """Save a withdrawal."""
        self.withdrawals_file.append_row(withdrawal.to_json())
