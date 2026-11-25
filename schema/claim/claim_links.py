"""The links between claims: which ones hold, and the order that follows them.

A claim can be superseded by another claim, or inferred from other claims. A link to a
claim that cannot be read makes the claim unreadable too. The claims are checked in
dependency order, so a claim check can read the findings of the claims it rests on.
"""
from __future__ import annotations

from collections.abc import Sequence

from schema.inputs import Claim, ClaimsById, UnreadableClaim
from schema.primitives import ClaimId, record


@record
class ClaimsInDependencyOrder:
    """The claims with each one after the claims it is inferred from, as far as cycles allow."""
    claims: tuple[Claim, ...]
    # the claims that come before at least one claim they are inferred from, because they
    # are on a cycle; each claim gets the findings only of the claims that come before it
    ids_before_a_claim_they_rest_on: frozenset[ClaimId]


class ClaimLinks:
    """The rules of the links between claims.

    It moves each claim whose links do not hold to the unreadable entries, and it orders
    the claims so that each one comes after the claims it is inferred from.
    """

    def keep_claims_with_valid_links(self, claims: Sequence[Claim],
                                     unreadable: Sequence[UnreadableClaim],
                                     entries: Sequence[object]
                                     ) -> tuple[tuple[Claim, ...], tuple[UnreadableClaim, ...]]:
        """The claims whose links hold, and the unreadable entries with the others added.

        A claim superseded by a claim that is missing, unreadable or itself is unreadable,
        so a replacement can never hide the claim it replaces. A derived claim is
        unreadable when it names a claim that is missing, unreadable or superseded, or a
        claim with no `statement`. Moving one claim can break the links of another, so
        this repeats until no claim moves.

        Args:
            claims: the claims that parsed, in file order.
            unreadable: the entries that did not parse.
            entries: the raw `claims` list, to give each moved claim its entry number.

        Returns:
            The claims kept, and every unreadable entry in file order.
        """
        kept = list(claims)
        moved = list(unreadable)
        while True:
            by_id = {c.id: c for c in kept}
            broken = {c.id: reason for c in kept
                      if (reason := self.broken_link_reason(c, by_id)) is not None}
            if not broken:
                break
            for cid, reason in broken.items():
                n = next(i for i, e in enumerate(entries, start=1)
                         if isinstance(e, dict) and e.get("id") == cid)
                moved.append(UnreadableClaim(entry=n, id=cid, reason=reason))
            kept = [c for c in kept if c.id not in broken]
        return tuple(kept), tuple(sorted(moved, key=lambda u: u.entry))

    @staticmethod
    def broken_link_reason(claim: Claim, by_id: ClaimsById) -> str | None:
        """Why one of the links of the claim to other readable claims does not hold, or None."""
        at = f"claim {claim.id!r}"
        if claim.superseded_by is not None and (claim.superseded_by not in by_id
                                                or claim.superseded_by == claim.id):
            return (f"{at} is superseded by {claim.superseded_by!r}, which is not another "
                    f"readable claim")
        for dep in claim.ids_of_claims_it_is_inferred_from():
            other = by_id.get(dep)
            if other is None:
                return f"{at} is inferred from {dep!r}, which is not a readable claim"
            if other.superseded_by is not None:
                return (f"{at} is inferred from {dep!r}, which is superseded by "
                        f"{other.superseded_by!r}; name that claim instead")
            if other.statement is None:
                return (f"{at} is inferred from {dep!r}, which gives no `statement`; a claim "
                        f"other claims are inferred from must say what it claims")
        return None

    @staticmethod
    def order_by_dependency(claims: Sequence[Claim]) -> ClaimsInDependencyOrder:
        """The claims in an approximate dependency order, found greedily.

        Ids a claim names that are not in `claims` are ignored. Each claim whose claims all
        come before it is placed next, in the order given. When none is left and claims
        remain, they are on a cycle or depend on one: the claim with the fewest claims not
        yet placed comes next (the first in the order given, on a tie), and the greedy
        order goes on. So every claim is placed, and a cycle breaks at one claim.
        """
        ids = {c.id for c in claims}
        waiting = {c.id: {d for d in c.ids_of_claims_it_is_inferred_from() if d in ids}
                   for c in claims}
        placed: list[Claim] = []
        done: set[ClaimId] = set()
        early: set[ClaimId] = set()
        while len(done) < len(claims):
            ready = [c for c in claims if c.id not in done and waiting[c.id] <= done]
            if not ready:
                c = min((c for c in claims if c.id not in done),
                        key=lambda c: len(waiting[c.id] - done))
                early.add(c.id)
                ready = [c]
            for c in ready:
                placed.append(c)
                done.add(c.id)
        return ClaimsInDependencyOrder(claims=tuple(placed),
                                       ids_before_a_claim_they_rest_on=frozenset(early))
