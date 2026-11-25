"""Human review: requests the verifier raises, and the decisions a person records.

The agent raises a ReviewRequest when something should be decided by a person. A
person records a ReviewDecision. A request's state is computed from the stored requests,
decisions and withdrawals, and from whether its facts still hold in the working
directory; it is never stored. What a state means for the verdict is decided by the
request's kind (see review/kinds/), which reports it as an ordinary claim check result.
The schema and the core know nothing of reviews.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from typing import ClassVar

from schema.inputs import Scalar
from schema.primitives import read_only, record
from utils.computed_state import ComputedState


@record
class ReviewRequest:
    """One question for a person, bound to the exact facts it is about."""
    id: str                             # "R-12"
    kind: str                           # a registered kind's name, such as "file-regenerated"
    binding: str                        # sha256 of the facts; one open request per kind and binding
    facts: Mapping[str, Scalar]         # e.g. {"file": ..., "old_sha256": ..., "new_sha256": ...}
    question: str                       # one sentence a person can answer
    reason: str                         # why it was asked
    raised_by: str                      # who raised it: the kind's raised_by
    raised_in: str                      # the commit being checked when it was raised
    raised_at: str                      # ISO time

    @staticmethod
    def binding_for(kind_name: str, facts: Mapping[str, Scalar]) -> str:
        """The checksum of a kind and its facts. One open request per binding."""
        return hashlib.sha256(json.dumps([kind_name, dict(facts)],
                                         sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_json(cls, row: dict[str, object]) -> ReviewRequest:
        """Read one row of requests.jsonl."""
        facts = row["facts"]
        assert isinstance(facts, dict), f"the facts of {row['id']} are not an object"
        return cls(id=str(row["id"]), kind=str(row["kind"]), binding=str(row["binding"]),
                   facts=read_only(facts), question=str(row["question"]),
                   reason=str(row["reason"]), raised_by=str(row["raised_by"]),
                   raised_in=str(row["raised_in"]), raised_at=str(row["raised_at"]))

    def to_json(self) -> dict[str, object]:
        """The row of requests.jsonl."""
        row = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        return {**row, "facts": dict(self.facts)}


@record
class ReviewDecision:
    """A person's answer to one request. The saved keys are the field names."""
    request: str                        # ReviewRequest.id
    option: str                         # one of the kind's options
    note: str
    decided_at: str                     # ISO time
    via: str                            # how it was given: "cli", later "slack"

    @classmethod
    def from_json(cls, row: dict[str, object]) -> ReviewDecision:
        """Read one row of decisions.jsonl."""
        return cls(request=str(row["request"]), option=str(row["option"]),
                   note=str(row["note"]), decided_at=str(row["decided_at"]),
                   via=str(row["via"]))

    def to_json(self) -> dict[str, object]:
        """The row of decisions.jsonl."""
        return dataclasses.asdict(self)


@record
class ReviewWithdrawal:
    """The withdrawal of one request, with its reason and time."""
    request: str
    reason: str
    at: str

    @classmethod
    def from_json(cls, row: dict[str, object]) -> ReviewWithdrawal:
        """Read one row of withdrawals.jsonl."""
        return cls(request=str(row["request"]), reason=str(row["reason"]), at=str(row["at"]))

    def to_json(self) -> dict[str, object]:
        """The row of withdrawals.jsonl."""
        return dataclasses.asdict(self)


# A request's state, computed on every run.

@record
class ReviewState(ComputedState):
    """The state of a request now. It waits when no person has answered it."""
    # a request in this state is the open one for its facts, so asking again returns it
    blocks_a_new_request_with_same_facts: ClassVar[bool] = False

    @property
    def decision(self) -> ReviewDecision | None:
        """The answer of the person, when the state comes from one."""
        return None


@record
class ReviewWaitingForAnswer(ReviewState):
    """No answer yet, and the facts still hold."""
    state_word = "pending"
    is_waiting = True
    blocks_a_new_request_with_same_facts = True


@record
class ReviewConfirmed(ReviewState):
    """A person gave a positive answer, and the facts still hold."""
    state_word = "confirmed"
    blocks_a_new_request_with_same_facts = True
    answer: ReviewDecision

    @property
    def decision(self) -> ReviewDecision | None:
        """The positive answer."""
        return self.answer


@record
class ReviewDeclined(ReviewState):
    """A person gave a negative answer, and the facts still hold."""
    state_word = "declined"
    blocks_a_new_request_with_same_facts = True
    answer: ReviewDecision

    @property
    def decision(self) -> ReviewDecision | None:
        """The negative answer."""
        return self.answer


@record
class ReviewSuperseded(ReviewState):
    """The facts no longer hold in the working directory, so any answer no longer applies."""
    state_word = "superseded"


@record
class ReviewWithdrawn(ReviewState):
    """Whoever raised it withdrew it before anyone answered."""
    state_word = "withdrawn"
    reason: str
