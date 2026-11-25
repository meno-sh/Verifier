"""The base class of a review kind: one kind of question to a person."""
from __future__ import annotations

import abc
import inspect
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from review.types import ReviewDecision, ReviewRequest, ReviewState
from schema import inputs
from schema.inputs import Scalar
from schema.primitives import ClaimId, Problem, ProblemKind

POLARITIES = frozenset({"positive", "negative"})
# The first letter of an option: `verifier review` reads the answer in lower case, and
# [s] is skip.
OPTION_FIRST_LETTERS = frozenset("abcdefghijklmnopqrtuvwxyz")


class ReviewKind(abc.ABC):
    """One kind of question to a person, and what each answer means for the claims.

    A subclass gives the name, the title, the options with their polarity and effect,
    the problem kinds its claim check reports, who raises it, and the guide text for
    the auditing agent. A concrete subclass with a missing name, an option with no
    polarity or effect, two options with the same first letter, or no problem kind is
    refused when the class is made.
    """
    name: ClassVar[str]
    title: ClassVar[str]
    # each option and its polarity, "positive" or "negative"
    options: ClassVar[Mapping[str, str]]
    # each option and what it does, in words for the person
    option_effects: ClassVar[Mapping[str, str]]
    problem_kinds: ClassVar[tuple[ProblemKind, ...]]
    raised_by: ClassVar[str]
    agent_guide: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuse a concrete kind that misses a class attribute or has malformed options."""
        super().__init_subclass__(**kwargs)
        if inspect.isabstract(cls):
            return
        required = ("name", "title", "options", "option_effects", "problem_kinds",
                    "raised_by", "agent_guide")
        missing = [n for n in required if not hasattr(cls, n)]
        if missing:
            raise TypeError(f"review kind {cls.__name__} is missing {', '.join(missing)}")
        if set(cls.options.values()) - POLARITIES or set(cls.options) != set(cls.option_effects):
            raise TypeError(f"review kind {cls.name}: every option needs a polarity and an "
                            "effect")
        first_letters = [option[:1] for option in cls.options]
        usable = all(letter in OPTION_FIRST_LETTERS for letter in first_letters)
        if not usable or len(set(first_letters)) != len(first_letters):
            raise TypeError(f"review kind {cls.name}: `verifier review` answers an option by "
                            "its first letter in lower case, and [s] is skip, so each option "
                            "needs its own first letter, a to z other than s: "
                            f"{sorted(cls.options)}")
        if not cls.problem_kinds:
            raise TypeError(f"review kind {cls.name}: problem_kinds must declare at least one "
                            "problem kind")

    def option_is_positive(self, option: str) -> bool:
        """Whether an option of this kind is a positive answer."""
        return self.options[option] == "positive"

    @abc.abstractmethod
    def build_request_facts(self, root: Path, claims: Iterable[inputs.Claim],
                            given: Mapping[str, str]) -> tuple[dict[str, Scalar], str]:
        """The facts of a new request and its question, from the key=value pairs given.

        Raises:
            ReviewError: the pairs do not name facts that a person can be asked
                about. Any other error is a bug of the kind, and the command does not
                catch it.
        """

    @abc.abstractmethod
    def facts_still_hold(self, facts: Mapping[str, Scalar], root: Path) -> bool:
        """Whether the facts a request was raised about still hold in the working directory."""

    @abc.abstractmethod
    def ids_of_claims_it_bears_on(self, facts: Mapping[str, Scalar],
                                  claims: Iterable[inputs.Claim],
                                  root: Path) -> tuple[ClaimId, ...]:
        """The claims a request with these facts bears on."""

    @abc.abstractmethod
    def problems_for_claim(self, request: ReviewRequest,
                           state: ReviewState) -> tuple[Problem, ...]:
        """The problems a request in this state gives each claim it bears on; none for none."""

    @abc.abstractmethod
    def evidence_for_person(self, request: ReviewRequest, root: Path) -> str:
        """What the person should see before answering, one fact per line."""

    def after_request_raised(self, request: ReviewRequest, root: Path, state_dir: Path) -> None:
        """Change the project after a new request is recorded. By default, nothing."""
        return None

    def after_decision_recorded(self, request: ReviewRequest, decision: ReviewDecision,
                                root: Path, state_dir: Path) -> None:
        """Change the project after a decision is recorded. By default, nothing."""
        return None

    def after_request_withdrawn(self, request: ReviewRequest, root: Path,
                                state_dir: Path) -> None:
        """Change the project after a request is withdrawn. By default, nothing."""
        return None
