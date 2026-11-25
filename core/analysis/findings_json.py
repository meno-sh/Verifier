"""The findings as plain JSON values, for --json and the saved report."""
from __future__ import annotations

import dataclasses
import enum
import json
from collections.abc import Mapping, Sequence
from typing import ClassVar

from schema import inputs
from schema.primitives import (
    CheckResult,
    Flagged,
    GateResult,
    Location,
    NotChecked,
    Off,
    Passed,
    Problem,
    ProblemKind,
    Skipped,
)
from utils import text


class FindingsJsonCodec:
    """Turns a Findings, or any part of it, into plain JSON values, and reads a result back.

    A result is written with a "type" tag, so a reader can tell Passed from Off, or
    Missing from Changed, without guessing from the fields.

    In the fixed form (`fixed_form=True`) one value always gives the same JSON, so a
    checksum of it names the value (core/analysis/check_result_cache.py): each record
    gets a type tag, mappings and sets are sorted, a PrintedFloat keeps its text (0.50 and
    0.5 differ, as they do for recompute), and a value with no JSON form raises TypeError.
    """
    TAGGED_RESULT_BASES: ClassVar[tuple[type, ...]] = (CheckResult, GateResult)

    def __init__(self, fixed_form: bool = False) -> None:
        """Encode in the fixed form or in the form of the saved report."""
        self.fixed_form = fixed_form

    def encode(self, value: object) -> object:
        """The value as plain JSON values.

        Raises:
            TypeError: in the fixed form, a value that has no JSON form.
        """
        if self.fixed_form and isinstance(value, inputs.PrintedFloat):
            return {"printed": value.text}
        if isinstance(value, ProblemKind):         # a kind is shown by its code
            return value.code
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            out: dict[str, object] = (
                {"type": type(value).__name__}
                if self.fixed_form or isinstance(value, self.TAGGED_RESULT_BASES) else {})
            for f in dataclasses.fields(value):
                out[f.name] = self.encode(getattr(value, f.name))
            return out
        if isinstance(value, enum.Enum):
            return value.value
        if self.fixed_form:
            return self._encode_fixed_container(value)
        if isinstance(value, (tuple, list, frozenset)):
            items = sorted(value) if isinstance(value, frozenset) else value
            return [self.encode(v) for v in items]
        if isinstance(value, Mapping):
            return {k: self.encode(v) for k, v in value.items()}
        return value

    def _encode_fixed_container(self, value: object) -> object:
        """A mapping, set, sequence or scalar in the fixed form.

        Raises:
            TypeError: a value that has no JSON form.
        """
        if isinstance(value, Mapping):
            return {str(k): self.encode(v)
                    for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, frozenset | set):
            return sorted((self.encode(v) for v in value),
                          key=lambda c: json.dumps(c, sort_keys=True))
        if isinstance(value, tuple | list):
            return [self.encode(v) for v in value]
        if value is None or isinstance(value, str | int | float | bool):
            return value
        raise TypeError(f"no fixed JSON form for {type(value).__name__}: "
                        f"{text.first_part(repr(value), 200)}")

    # the results that a check gives, and that the result cache stores; not CheckCrashed and
    # not ModelFailedAfterRetries
    STORED_RESULTS: ClassVar[tuple[type[CheckResult], ...]] = (Passed, Flagged, Skipped, Off,
                                                                NotChecked)

    def decode_check_result(self, data: object, kinds: Sequence[ProblemKind]) -> CheckResult:
        """The result of STORED_RESULTS that `encode` gave as `data`.

        Each problem kind is one of `kinds`.

        Raises:
            ValueError: `data` is not the JSON of such a result with these kinds.
        """
        names = [t.__name__ for t in self.STORED_RESULTS]
        if not isinstance(data, dict) or data.get("type") not in names:
            raise ValueError(f"not a stored {' or '.join(names)}: "
                             f"{text.first_part(repr(data), 200)}")
        try:
            return self._check_result(data, kinds)
        except (KeyError, TypeError) as e:
            raise ValueError(f"a stored result without {e}: "
                             f"{text.first_part(repr(data), 200)}") from None

    def _check_result(self, data: dict[str, object], kinds: Sequence[ProblemKind]
                      ) -> CheckResult:
        """The result in `data`, whose type decode_check_result has checked.

        Raises:
            KeyError, TypeError: a key is missing, or a value has the wrong type.
            ValueError: a value is not one the encoder gives.
        """
        if data["type"] == "Skipped":
            return Skipped(because=str(data["because"]))
        if data["type"] == "Off":
            return Off()
        if data["type"] == "NotChecked":
            return NotChecked(missing=str(data["missing"]))
        measured = data["measured"]
        if measured is not None and not isinstance(measured, int | float):
            raise ValueError(f"`measured` is not a number: {measured!r}")
        if data["type"] == "Passed":
            return Passed(measured=measured)
        by_code = {k.code: k for k in kinds}
        problems = data["problems"]
        if not isinstance(problems, list):
            raise ValueError(f"`problems` is not a list: {text.first_part(repr(problems), 200)}")
        return Flagged(problems=tuple(self._problem(p, by_code) for p in problems),
                       measured=measured)

    @staticmethod
    def _problem(data: object, kinds: Mapping[str, ProblemKind]) -> Problem:
        """One problem of a stored Flagged.

        Raises:
            ValueError: the problem is not in the form the encoder gives, or its kind is
                not one of `kinds`.
        """
        if not isinstance(data, dict) or data.get("kind") not in kinds:
            raise ValueError("not a stored problem of a known kind: "
                             f"{text.first_part(repr(data), 200)}")
        where = data["where"]
        return Problem(kind=kinds[data["kind"]], detail=str(data["detail"]),
                       where=None if where is None else Location(section=where["section"],
                                                                 excerpt=where["excerpt"]),
                       facts=tuple((str(a), str(b)) for a, b in data["facts"]))
