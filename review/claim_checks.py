"""The claim check of each review kind: what the requests of the kind mean for each claim."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from core.registry import checks
from review.kinds import ReviewKindRegistry
from review.kinds.base import ReviewKind
from review.states import ReviewStateResolver
from review.store import ReviewStore
from review.types import ReviewRequest, ReviewState
from schema import inputs, recompute
from schema.primitives import CheckResult, ClaimId, Flagged, Passed

CHECK_PREFIX = "review:"


class ReviewClaimCheck(checks.ClaimCheck):
    """The claim check of one review kind: every problem its requests give a claim, together.

    The states of the requests are computed once, when the run starts. The claims each
    request bears on are computed again when the register is a different object. The
    check keeps the register object it computed them for and compares it with `is`, so a
    new register at the address of a freed one is never taken for it.
    """

    def __init__(self, kind: ReviewKind,
                 requests_with_states: Sequence[tuple[ReviewRequest, ReviewState]],
                 root: Path) -> None:
        """The check of `kind`, for its requests and their states now."""
        self.name = CHECK_PREFIX + kind.name
        self.title = kind.title
        self.why = f"Reports what each '{kind.name}' review means for the claims it bears on."
        self.problem_kinds = tuple(kind.problem_kinds)
        self.kind = kind
        self.requests_with_states = tuple(requests_with_states)
        self.root = root
        self.register_of_claim_ids: Mapping[ClaimId, inputs.Claim] | None = None
        self.claim_ids_by_request: dict[str, set[ClaimId]] = {}

    @classmethod
    def add_one_check_for_each_kind(cls, check_registry: checks.CheckRegistry,
                                    store: ReviewStore, kinds: ReviewKindRegistry,
                                    root: Path) -> None:
        """Add the check of each kind, with the requests of the kind and their states now.

        The requests come from `store`, and their states are computed in `root`.
        """
        ledger = store.read_ledger()
        states = ReviewStateResolver(kinds, root)
        for kind in kinds.all_kinds():
            requests = [(r, states.state_of_request(r, ledger))
                        for r in ledger.requests_of_kind(kind.name)]
            check_registry.add_claim_check(cls(kind, requests, root))

    def check(self, claim: inputs.Claim, recomputed: recompute.Matches | None,
              context: checks.ClaimContext) -> CheckResult:
        """Flagged with the problems of every request that bears on the claim, or Passed."""
        register = context.register
        if register is not self.register_of_claim_ids:
            self.claim_ids_by_request = {
                r.id: set(self.kind.ids_of_claims_it_bears_on(r.facts, register.values(),
                                                              self.root))
                for r, _ in self.requests_with_states}
            self.register_of_claim_ids = register
        claim_ids = self.claim_ids_by_request
        problems = tuple(p for r, s in self.requests_with_states if claim.id in claim_ids[r.id]
                         for p in self.kind.problems_for_claim(r, s))
        return Flagged(problems=problems) if problems else Passed()
