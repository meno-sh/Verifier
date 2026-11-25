"""The lowest-level types, which every other schema module builds on.

Other modules import names from here directly (`from schema.primitives import
CheckResult`), the one module imported that way, because these names are the
vocabulary every module shares.
"""
from __future__ import annotations

import dataclasses
import math
import os
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    ClassVar,
    Self,
    TypeAliasType,
    TypeGuard,
    dataclass_transform,
    get_args,
)

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    from schema.recompute import Matches

# Records and sum types: how every schema type is made. Defined first, because the
# classes below use them.

def read_only[K, V](values: Mapping[K, V]) -> Mapping[K, V]:
    """A read-only copy of a dict: reading works, adding or changing a key raises TypeError.

    A record cannot be changed once made, but a dict inside it still could. Every
    mapping a record holds is made with this instead. (It is Python's
    MappingProxyType over a private copy, so nothing else holds the dict behind it.)
    """
    return MappingProxyType(dict(values))


@dataclass_transform(frozen_default=True, kw_only_default=True,
                     field_specifiers=(dataclasses.field,))
def record[T](cls: type[T]) -> type[T]:
    """Make a class a record: named fields of data that never change once made.

    Every schema type is a record, so no step can alter what an earlier step produced.
    """
    return dataclasses.dataclass(frozen=True, slots=True, kw_only=True)(cls)


def members(alias: TypeAliasType) -> tuple[type, ...]:
    """The classes a sum type alias such as Scalar is made of, at any depth.

    A `type` alias is evaluated lazily, so its union sits in `__value__`, and
    isinstance cannot take the alias itself. An alias built from other aliases is
    expanded to its classes.
    """
    value = alias.__value__
    parts = get_args(value) or (value,)       # an alias of one class has no union to unpack
    out: list[type] = []
    for part in parts:
        out.extend(members(part) if isinstance(part, TypeAliasType) else [part])
    return tuple(out)


# The result bases. Every paper check and every claim check ends with a CheckResult.
# The two steps of the gate end with a GateResult: provenance with a ProvenanceResult
# (schema/provenance.py), and recompute with a RecomputeResult. Skipped and Off are a
# CheckResult and a RecomputeResult, so these bases are here, beside them.

@record
class CheckResult:
    """The result of one check: Passed, Flagged, NotChecked, Skipped or Off.

    The name `problems` is the field of Flagged. So the base gives the problems with
    `problems_found()`, which a dataclass field of a subclass cannot hide.
    """
    # True for a result that did not run (NotChecked, Skipped): the verdict counts it
    # as a caveat
    counts_as_caveat: ClassVar[bool] = False

    def problems_found(self) -> tuple[Problem, ...]:
        """The problems the check found; none unless the result is Flagged."""
        return ()

    @staticmethod
    def from_problems(problems: Iterable[Problem]) -> Passed | Flagged:
        """Passed when a check found no problems, otherwise Flagged with all of them."""
        found = tuple(problems)
        return Flagged(problems=found) if found else Passed()


@record
class GateResult:
    """The result of one step of the gate of a claim: provenance or recompute."""
    # the one word of the gate status in the terminal table, when this result gives it
    gate_status_word: ClassVar[str | None] = None
    # the code of the result on the triage board
    board_code: ClassVar[str | None] = None

    def describe_for_reader(self) -> str:
        """One sentence that says what the result means; each subclass gives its own."""
        raise NotImplementedError(f"{type(self).__name__} gives no description")

    def with_paths_relative_to(self, folder: str | None) -> Self:
        """A copy whose absolute `path` is relative to `folder`, for display only."""
        return paths_relative_to(self, folder)

    def describe_with_paths_relative_to(self, folder: str | None) -> str:
        """The description, with paths as the claim register in `folder` wrote them."""
        return self.with_paths_relative_to(folder).describe_for_reader()


@record
class RecomputeResult(GateResult):
    """The result of recomputing the number of one claim.

    The subclasses are in schema/recompute.py, and Skipped and Off below.
    """
    # True when the number did not come back, so the claim cannot be verified: REJECT
    is_broken: ClassVar[bool] = False
    # True for Off: recompute was switched off for the run
    is_switched_off: ClassVar[bool] = False

    def recomputed_number(self) -> Matches | None:
        """The result that holds the recomputed number (Matches), or None for every other."""
        return None

    def reason_there_is_no_recomputed_number(self) -> str:
        """Why a claim check that reads the recomputed number cannot run after this result."""
        return self.describe_for_reader()


def paths_relative_to[R: DataclassInstance](result: R, folder: str | None) -> R:
    """A copy of a result record whose absolute `path` field is relative to `folder`.

    The report shows paths as the claim register wrote them, relative to its folder.
    """
    path = getattr(result, "path", None)
    if folder is None or not isinstance(path, str) or not os.path.isabs(path):
        return result
    return dataclasses.replace(result, path=os.path.relpath(path, folder))


@record
class Passed(CheckResult):
    """Ran, and found nothing wrong."""
    measured: float | None = None       # a number the check measured, such as an uncertainty

    def __post_init__(self) -> None:
        """Refuse a measured value that is not a finite number."""
        _refuse_bad_measured_number(self.measured)


@record
class Flagged(CheckResult):
    """Ran, and found at least one problem."""
    problems: tuple[Problem, ...]
    measured: float | None = None       # a number the check measured, such as an uncertainty

    def __post_init__(self) -> None:
        """Refuse an empty list (a check that found nothing has Passed), or a bad measured value."""
        if not self.problems:
            raise ValueError("Flagged needs at least one problem; use Passed")
        _refuse_bad_measured_number(self.measured)

    def problems_found(self) -> tuple[Problem, ...]:
        """The problems the check found."""
        return self.problems


def scalars_are_equal(a: object, b: object) -> bool:
    """Equal as numbers when both are numbers (so 1 equals 1.0), and as text otherwise."""
    if is_real_number(a) and is_real_number(b):
        return float(a) == float(b)
    return str(a) == str(b)


def path_in_folder(folder: str, path: str) -> str | None:
    """`path` resolved against `folder`, or None when `path` is absolute.

    A file the Verifier reads names its paths relative to its own folder, because an
    absolute path names a file on one machine only. The caller refuses an absolute path
    with its own error.
    """
    if os.path.isabs(os.path.expanduser(path)):
        return None
    return os.path.normpath(os.path.join(folder, path))


def is_real_number(value: object) -> TypeGuard[int | float]:
    """True for an int or float that is not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _refuse_bad_measured_number(value: object) -> None:
    """Raise unless `value` is None or a finite int or float that is not a bool."""
    if value is None:
        return
    if (isinstance(value, bool) or not isinstance(value, int | float)
            or not math.isfinite(value)):
        raise ValueError(f"a measured value must be a finite number, got {value!r}")


@record
class NotChecked(CheckResult):
    """Could not run, because the claim register lacks a field it needs."""
    counts_as_caveat = True
    missing: str                        # the claim key that was not given

    def describe_for_reader(self) -> str:
        """Why it did not run: the key the claim does not give."""
        return f"no `{self.missing}` given"


@record
class Skipped(CheckResult, RecomputeResult):
    """Did not run, because something before it failed."""
    counts_as_caveat = True
    because: str                        # "provenance failed", "an \\input file could not be read"

    def describe_for_reader(self) -> str:
        """Why it did not run."""
        return self.because


@record
class CheckCrashed(Skipped):
    """Did not give a result, because a plug-in check raised an error. `because` names it."""


@record
class ModelFailedAfterRetries(Skipped):
    """Did not give a result, because a model failed after its retries. `because` names it.

    The result cache never stores it, so the next run asks the model again.
    """


@record
class Off(CheckResult, RecomputeResult):
    """Switched off with --checks."""
    is_switched_off = True

    def describe_for_reader(self) -> str:
        """That it was switched off."""
        return "switched off"

    def reason_there_is_no_recomputed_number(self) -> str:
        """That recompute was switched off."""
        return "recompute was switched off"


def passed_or_flagged(problems: Iterable[Problem]) -> Passed | Flagged:
    """Passed when a check found no problems, otherwise Flagged with all of them.

    Part of the API of plug-in checks, which saved plug-in checks import by this name.
    """
    return CheckResult.from_problems(problems)


# Problems: what a Flagged result actually holds.

@record
class Problem:
    """One specific thing that is wrong."""
    kind: ProblemKind
    detail: str                         # the sentence shown to the reader
    where: Location | None = None       # set for problems in the paper text
    # the parts of the detail as (label, text) lines, for a report that lays them out;
    # empty when the detail is one fact
    facts: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """Refuse a fact that is not a pair of a label and a text."""
        if not isinstance(self.facts, tuple) or not all(
                isinstance(f, tuple) and len(f) == 2 and all(isinstance(x, str) and x for x in f)
                for f in self.facts):
            raise ValueError(f"Problem.facts must be a tuple of (label, text) pairs of "
                             f"non-empty text, got {self.facts!r}")

    def text_for_reader(self) -> str:
        """The problem as the reader sees it, with where it is when that is known."""
        if self.where is None:
            return self.detail
        return f"{self.where.section or '?'}: {self.detail} -- \"{self.where.excerpt}\""


@record
class Location:
    """Where in the paper a problem is."""
    section: str | None                 # "abstract", "Section 4.1"; None when unknown
    excerpt: str                        # the sentence or table cell


class Severity(Enum):
    """How bad a problem is. Integrity failures have no severity: they come only from the gate."""
    SOUNDNESS = "soundness"           # the number may not measure the claim: BLOCK UNTIL FIXED
    PRESENTATION = "presentation"     # the paper misdescribes itself: BLOCK UNTIL FIXED
    STRENGTH = "strength"             # a weak result: PASS WITH DISCLOSURE
    CAVEAT = "caveat"                 # wording the evidence may not carry: PASS WITH CAVEATS


@record
class ProblemKind:
    """One kind of problem a check can report, with its severity.

    There is no central list of kinds. Each check declares the kinds it reports when
    it registers (see core/registry/checks.py), and the registry refuses a code that two
    checks both claim. A check can only report kinds it declared.

    `code` is what reports and JSON show: lowercase words joined by dashes.
    """
    code: str
    severity: Severity

    def __post_init__(self) -> None:
        """Refuse a kind without a real severity, or a code that is not lowercase-with-dashes."""
        if not isinstance(self.severity, Severity):
            raise TypeError(f"{self.code}: severity must be a Severity, got {self.severity!r}")
        if not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", self.code):
            raise ValueError(f"problem kind code {self.code!r} must be lowercase words "
                             "joined by dashes")


# Claim register values: the ids and fixed choices a claim register uses.

type ClaimId = str


type MethodName = str     # a computational method, by the name it registered under


class TraceReason(Enum):
    """Why a claim is not recomputed, but only found in a log file instead.

    Most claims are recomputed from their data. Some cannot be, so the tool only
    checks that the number appears in a named log. The claim register must give one
    of these reasons for that. A fixed list, because free text would let any
    awkward claim skip recomputation with a plausible excuse.
    """
    PARSER_SUPERSEDED = "parser-superseded"   # the stored results were parsed by a since-fixed bug
    PIPELINE_RETIRED = "pipeline-retired"     # the code that produced the number no longer exists
    EXTERNAL_SOURCE = "external-source"       # the number comes from published work, not our runs
    COMPUTED_OFFLINE = "computed-offline"     # worked out by hand and recorded, cannot be rerun

    @property
    def meaning_for_reader(self) -> str:
        """What the reason means, as the report shows it for a traced claim."""
        return TRACE_REASON_MEANINGS[self]


TRACE_REASON_MEANINGS = {
    TraceReason.PARSER_SUPERSEDED: "the stored evaluation used a parser that has since been fixed",
    TraceReason.PIPELINE_RETIRED: "the pipeline that produced it no longer exists",
    TraceReason.EXTERNAL_SOURCE: "the number comes from published work, not from our runs",
    TraceReason.COMPUTED_OFFLINE: "recomputed by hand and recorded, not reproducible in place",
}
