"""The state of a stored request now, computed from what is stored and never saved.

A review request and a dispute each keep only append-only rows. Their state comes from
those rows, and from the files of the project, on each read. Each state is a record
that extends ComputedState, so every command reads a state in the same way.
"""
from __future__ import annotations

from typing import ClassVar

from schema.primitives import record


@record
class ComputedState:
    """The state of a stored request now. It is computed on each read, never saved."""
    # the word for the state in a message to a person
    state_word: ClassVar[str]
    # someone must still act on the request: a person answers it, or the agent writes for it
    is_waiting: ClassVar[bool] = False
