"""The six axes of the triage board, and what a finding on each axis means.

The six axes are kept apart on purpose. A single composite score would let a strong
result offset a fabricated one, which is the failure the tool is built to prevent.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from schema.primitives import Severity, read_only


class Axis(StrEnum):
    """One axis of the triage board.

    The members are in report order: when two issue types have the same number of
    issues, the type on the earlier axis comes first. The board prints the axes in
    another order (`in_board_order`). A problem's axis is its severity's value, so
    "soundness", "presentation", "strength" and "caveat" must stay Severity values.
    """
    INTEGRITY = "integrity"
    MISSING_INPUTS = "missing_inputs"
    SOUNDNESS = "soundness"
    PRESENTATION = "presentation"
    STRENGTH = "strength"
    CAVEAT = "caveat"

    @classmethod
    def of_severity(cls, severity: Severity) -> Axis:
        """The axis of a problem with this severity."""
        return cls(severity.value)

    @classmethod
    def in_board_order(cls) -> tuple[Axis, ...]:
        """The axes in the order the triage board prints them."""
        return tuple(_TITLES)

    @property
    def rank(self) -> int:
        """The place of the axis in report order, from 0."""
        return list(Axis).index(self)

    @property
    def board_heading(self) -> str:
        """The heading of the axis on the triage board."""
        return _TITLES[self]

    def impact(self, disputable: bool) -> str:
        """How much an issue on this axis matters to the reader.

        A high-impact flag stops a pass, and a low-impact flag needs only a disclosure or a
        caveat. A missing input, or a caveat that is not a flag (a check that did not run),
        says nothing wrong about a claim, so nobody can dispute it.
        """
        return _IMPACTS[self] if disputable else NOT_DISPUTABLE

    @property
    def effect(self) -> str:
        """What the axis means for the verdict, shown next to each issue type."""
        return _EFFECTS[self]

    @property
    def reader_warning(self) -> str | None:
        """The first sentence of each issue type of flags on this axis, or None."""
        return _READER_WARNINGS.get(self)


# The impact of an issue that is not a flag: a missing input, or a check that did not run.
NOT_DISPUTABLE = "cannot be disputed"
# In the order the triage board prints the axes.
_TITLES: Mapping[Axis, str] = read_only({
    Axis.MISSING_INPUTS: "MISSING INPUTS (checks could not run)",
    Axis.INTEGRITY: "INTEGRITY    (blocks -- cannot be evaluated)",
    Axis.SOUNDNESS: "SOUNDNESS    (blocks until fixed -- the number may not measure the claim)",
    Axis.PRESENTATION: "PRESENTATION (blocks until fixed -- science is fine)",
    Axis.STRENGTH: "STRENGTH     (does not block -- must be disclosed)",
    Axis.CAVEAT: "CAVEAT       (does not block -- a check did not run, or a cautious check flagged "
                 "a sentence)",
})
_IMPACTS: Mapping[Axis, str] = read_only({  # of a flag
    Axis.INTEGRITY: "high", Axis.SOUNDNESS: "high", Axis.PRESENTATION: "high",
    Axis.STRENGTH: "low", Axis.CAVEAT: "low",
    Axis.MISSING_INPUTS: "cannot be disputed"})
_EFFECTS: Mapping[Axis, str] = read_only({
    Axis.INTEGRITY: "integrity issue (rejecting)",
    Axis.MISSING_INPUTS: "missing input (cannot pass)",
    Axis.SOUNDNESS: "soundness issue (blocking)",
    Axis.PRESENTATION: "presentation issue (blocking)",
    Axis.STRENGTH: "strength issue (disclosure)",
    Axis.CAVEAT: "caveat (pass with caveats)",
})
# The flags of a caveat come from the words of the paper alone, so they lean to caution. A
# check that did not run is a caveat too, but not a flag, so it has no such sentence.
_READER_WARNINGS: Mapping[Axis, str] = read_only({
    Axis.CAVEAT: "These flags lean to caution: a check finds them from the words of the paper "
                 "alone, so some of them are false alarms. Read the sentence before you act.",
})

for _table in (_TITLES, _IMPACTS, _EFFECTS):
    if set(_table) != set(Axis):
        raise RuntimeError("each table of the axes must name every axis of the triage board")
if not {s.value for s in Severity} <= set(Axis):
    raise RuntimeError("every severity must be an axis of the triage board")
