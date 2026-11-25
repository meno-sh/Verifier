"""The registry of review kinds: one ReviewKind subclass for each kind of question to a person.

To add a kind, add a subclass of ReviewKind (review/kinds/base.py) in its own module, and
one item to the tuple of REVIEW_KINDS.
"""
from __future__ import annotations

from collections.abc import Iterable

from review.kinds.base import ReviewKind
from review.kinds.file_regenerated import FileRegeneratedReviewKind
from review.store import ReviewError


class ReviewKindRegistry:
    """Holds the review kinds by name."""

    def __init__(self, kinds: Iterable[ReviewKind]) -> None:
        """Hold the kinds, in the order given.

        Raises:
            ValueError: two kinds have the same name.
        """
        self.kinds_by_name: dict[str, ReviewKind] = {}
        for kind in kinds:
            if kind.name in self.kinds_by_name:
                raise ValueError(f"two review kinds have the name {kind.name!r}")
            self.kinds_by_name[kind.name] = kind

    def kind_named(self, name: str) -> ReviewKind:
        """The kind registered under `name`.

        Raises:
            ReviewError: no kind has this name.
        """
        if name not in self.kinds_by_name:
            known = ", ".join(sorted(self.kinds_by_name))
            raise ReviewError(f"no review kind {name!r}; known kinds are {known}")
        return self.kinds_by_name[name]

    def all_kinds(self) -> list[ReviewKind]:
        """Every kind, in registration order."""
        return list(self.kinds_by_name.values())

    def agent_guide_text(self) -> list[str]:
        """The guide of each kind for the auditing agent, in registration order."""
        return [kind.agent_guide for kind in self.kinds_by_name.values()]


REVIEW_KINDS = ReviewKindRegistry((FileRegeneratedReviewKind(),))
