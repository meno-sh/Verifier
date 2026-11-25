"""The one place that changes reviews: it checks, records, and then calls the hook of the kind.

Each change holds the store lock from the first read to the change of the kind, so two
commands at once cannot give the same id or record two answers to one request.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from review.kinds import ReviewKindRegistry
from review.states import ReviewStateResolver
from review.store import ReviewError, ReviewLedger, ReviewStore
from review.types import ReviewDecision, ReviewRequest, ReviewWithdrawal
from schema.inputs import Scalar
from schema.primitives import read_only
from utils import timestamps


class ReviewRequestRecorder:
    """Raises, decides and withdraws review requests of one working directory."""

    def __init__(self, store: ReviewStore, kinds: ReviewKindRegistry, root: Path,
                 state_dir: Path) -> None:
        """Record in `store`; the kinds change the project in `root` and `state_dir`."""
        self.store = store
        self.kinds = kinds
        self.root = root
        self.state_dir = state_dir
        self.states = ReviewStateResolver(kinds, root)

    def raise_request(self, kind_name: str, facts: Mapping[str, Scalar], question: str,
                      reason: str, raised_by: str, raised_in: str) -> tuple[ReviewRequest, bool]:
        """Record a new request, or return the open one with the same binding.

        A new request then gets the change of its kind (after_request_raised).

        Returns:
            The request, and whether it is new.

        Raises:
            ReviewError: the kind is unknown, was raised by the wrong party, or its facts
                do not hold now.
        """
        kind = self.kinds.kind_named(kind_name)
        if raised_by != kind.raised_by:
            raise ReviewError(f"{kind_name} requests are raised by the {kind.raised_by}, "
                              f"not the {raised_by}")
        if not kind.facts_still_hold(facts, self.root):
            raise ReviewError(f"the facts of this {kind_name} request do not hold now")
        with self.store.holding_lock():
            key = ReviewRequest.binding_for(kind_name, facts)
            ledger = self.store.read_ledger()
            self.repair_failed_kind_changes_while_locked(ledger)
            for existing in ledger.requests:
                if (existing.binding == key and self.states.state_of_request(
                        existing, ledger).blocks_a_new_request_with_same_facts):
                    return existing, False
            request = ReviewRequest(
                id=f"R-{len(ledger.requests) + 1}", kind=kind_name, binding=key,
                facts=read_only(dict(facts)), question=question, reason=reason,
                raised_by=raised_by,
                raised_in=raised_in, raised_at=timestamps.utc_now_iso())
            self.store.append_request(request)
            kind.after_request_raised(request, self.root, self.state_dir)
            return request, True

    def record_decision(self, request_id: str, option: str, note: str,
                        via: str) -> ReviewDecision:
        """Record a person's answer, then apply the change of the kind (after_decision_recorded).

        Raises:
            ReviewError: the request or the option is not valid, or the request does not
                wait for an answer.
        """
        with self.store.holding_lock():
            ledger = self.store.read_ledger()
            self.repair_failed_kind_changes_while_locked(ledger)
            request = ledger.request_with_id(request_id)
            kind = self.kinds.kind_named(request.kind)
            if option not in kind.options:
                raise ReviewError(f"{option!r} is not an answer to {request_id}; "
                                  f"answers are {', '.join(kind.options)}")
            state = self.states.state_of_request(request, ledger)
            if not state.is_waiting:
                raise ReviewError(f"{request_id} is {state.state_word}, not waiting "
                                  "for an answer")
            decision = ReviewDecision(request=request_id, option=option, note=note,
                                      decided_at=timestamps.utc_now_iso(), via=via)
            self.store.append_decision(decision)
            kind.after_decision_recorded(request, decision, self.root, self.state_dir)
        return decision

    def withdraw_request(self, request_id: str, reason: str) -> ReviewRequest:
        """Withdraw a request nobody has answered yet, and undo what raising it changed.

        Raises:
            ReviewError: there is no such request, or it does not wait for an answer.
        """
        with self.store.holding_lock():
            ledger = self.store.read_ledger()
            self.repair_failed_kind_changes_while_locked(ledger)
            request = ledger.request_with_id(request_id)
            state = self.states.state_of_request(request, ledger)
            if not state.is_waiting:
                raise ReviewError(f"{request_id} is {state.state_word}; only a "
                                  "waiting request can be withdrawn")
            self.store.append_withdrawal(ReviewWithdrawal(request=request_id, reason=reason,
                                                          at=timestamps.utc_now_iso()))
            self.kinds.kind_named(request.kind).after_request_withdrawn(request, self.root,
                                                                        self.state_dir)
        return request

    def apply_kind_effects_again(self, request: ReviewRequest, ledger: ReviewLedger,
                                 open_bindings: set[str]) -> None:
        """Apply again the changes of the kind that the recorded history of a request asks for.

        Each change of the kind gives the same result when it runs again.

        - A withdrawn request gets the change of its withdrawal, unless a later request
          with the same facts is open (its binding is in `open_bindings`); that one owns
          the change now.
        - A request whose facts no longer hold gets nothing, so an old answer cannot undo
          the change of a newer request on the same file.
        - Any other request gets the change of its raise, then that of its decision.
        """
        if ledger.withdrawal_for(request.id) is not None:
            if request.binding not in open_bindings:
                self.kinds.kind_named(request.kind).after_request_withdrawn(
                    request, self.root, self.state_dir)
            return
        kind = self.kinds.kind_named(request.kind)
        if not kind.facts_still_hold(request.facts, self.root):
            return
        kind.after_request_raised(request, self.root, self.state_dir)
        decision = ledger.decision_for(request.id)
        if decision is not None:
            kind.after_decision_recorded(request, decision, self.root, self.state_dir)

    def repair_failed_kind_changes(self) -> None:
        """Apply again the changes of the kind of every request, under the store lock."""
        with self.store.holding_lock():
            self.repair_failed_kind_changes_while_locked(self.store.read_ledger())

    def repair_failed_kind_changes_while_locked(self, ledger: ReviewLedger) -> None:
        """Apply again the changes of the kind of every request, oldest first.

        The store records a change before the kind changes the project. So when the change
        of the kind failed, the record and the project disagree. Each change of the
        recorder, and `verifier review`, first runs this, so the next one repairs them.
        The caller holds the store lock.
        """
        open_bindings = {r.binding for r in ledger.requests
                         if self.states.state_of_request(
                             r, ledger).blocks_a_new_request_with_same_facts}
        for request in ledger.requests:
            self.apply_kind_effects_again(request, ledger, open_bindings)
