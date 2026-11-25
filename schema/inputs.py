"""The parsed claim register: one typed Claim per entry, checked once when it is read.

schema/claim/claim_register_parser.py reads the file into these records. An entry that is not
valid becomes an UnreadableClaim. The checks do not see it.
"""
from __future__ import annotations

import copy
import dataclasses
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from schema.errors import VerifierError
from schema.primitives import ClaimId, MethodName, TraceReason, members, read_only, record

# What a run looked at: the paper and the claim register.

@record
class Inputs:
    """What one run looked at, so its output is a record that can be checked later."""
    paper_paths: tuple[str, ...]                    # the paper's files; empty for no paper
    paper_sha256: str | None                        # one checksum over all of them
    claim_register_path: str | None
    claim_register_sha256: str | None
    claim_register_base: str | None = None          # the folder the register's paths are read from
    switched_off: frozenset[str]                    # checks turned off with --checks
    tool_commit: str


# The claim register: the claims that could be read, and the entries that could not.

@record
class ClaimRegister:
    """A claim register: its parsed claims for the checks, and its entries as written.

    Any unreadable entry makes the verdict MISSING INPUTS. `entries` is every entry of the
    file in file order, readable or not, with each printed decimal kept (PrintedFloat). A writer
    changes copies of the entries, parses them again, and writes `to_json`, so a write
    loses no key, no unreadable entry and no printed decimal. Nothing changes `entries` in
    place.
    """
    claims: tuple[Claim, ...]
    unreadable: tuple[UnreadableClaim, ...]
    base: str | None = None                         # the folder its relative paths were read from
    entries: tuple[object, ...] = ()                # the JSON of each entry, as json.load gave it

    def entry_copies(self) -> list[object]:
        """A copy of each entry, which a writer can change."""
        return copy.deepcopy(list(self.entries))

    def to_json(self) -> dict[str, object]:
        """The register as the file holds it, with each entry as written."""
        return {"claims": self.entry_copies()}


# Every readable claim of a register, by id.
type ClaimsById = Mapping[ClaimId, Claim]


@record
class UnreadableClaim:
    """One claim register entry that could not be parsed, and why."""
    entry: int                                      # position in the `claims` list, from 1
    id: ClaimId | None                              # None when the entry has no usable id
    reason: str

    def label(self) -> str:
        """The id of the entry, or `entry N` when it has no usable id."""
        return self.id or f"entry {self.entry}"

    def position_label(self) -> str:
        """Where the entry is in the register: `entry N (id)`, or `entry N (no id)`."""
        return f"entry {self.entry} ({self.id or 'no id'})"


@record
class Claim:
    """One entry in the claim register."""
    id: ClaimId
    # the number the paper prints; required for an artifact or document source, and
    # optional for a claim inferred from other claims or with no source
    value: float | None = None
    # the claim in words; required for a claim inferred from other claims, and for
    # every claim another claim names as its support
    statement: str | None = None
    source: Source | None = None
    tol: float | None = None                        # replaces the tolerance from printed decimals
    setup: Mapping[str, Scalar] | None = None       # what the paper says about the run
    # the note or file the claim was written in, resolved like every other path; a
    # source that points back at this same file is reported as a caveat (SelfTraced)
    origin: str | None = None
    # a later claim that replaces this one, for example after the note's number was
    # corrected; a superseded claim is listed in the report and not checked
    superseded_by: ClaimId | None = None
    # the keys the registered claim checks declared and this entry gives; which
    # keys exist, and their types, is decided by the checks
    fields: Mapping[str, object] = dataclasses.field(
        default_factory=lambda: read_only({}))

    def superseded_label(self) -> str:
        """`id -> the id of the claim that replaces it`, for a superseded claim."""
        return f"{self.id} -> {self.superseded_by}"

    def ids_of_claims_it_is_inferred_from(self) -> tuple[ClaimId, ...]:
        """The claims this claim is inferred from: its ClaimsSource ids, or none."""
        return self.source.claims if isinstance(self.source, ClaimsSource) else ()

    def recompute_tolerance(self) -> float:
        """How close a recomputation must be before it counts as matching.

        By default, one unit in the last decimal the paper printed. A paper printing
        0.127 claims three decimals, so the margin is 0.001. A whole unit rather than a
        half allows for rounding slips: a value computed as 0.1247 and printed as 0.124
        (truncated, or rounded from a slightly different run) is not an error worth a
        person's time. The cost is that a last-digit slip of one unit always passes. A
        claim's `tol` replaces
        that margin when it is set, for example when the printed number was rounded
        from a run that can no longer be repeated exactly. A loose `tol` can pass a
        number that is wrong, so the tolerance used is kept in the result.
        """
        if self.tol is not None:
            return self.tol
        return 10.0 ** -PrintedFloat.count_printed_decimals(self.value)


class FieldType(Enum):
    """The type of a key a claim check reads from a claim, as it declares it."""
    INTEGER = "integer"
    NUMBER = "number"                               # a whole number or a decimal
    BOOLEAN = "boolean"
    TEXT = "text"
    TEXT_LIST = "text list"                         # read as a tuple of strings
    OBJECT = "object"                               # read as a read-only mapping, nested too
    PATH = "path"                                   # resolved against the register's folder


# Sources: what a claim rests on. Data its number is recomputed from, a log it is
# traced to, or other claims in the same register it is inferred from. A claim with
# none of these has `source=None`.

type Source = ArtifactSource | DocumentSource | ClaimsSource


@record
class ArtifactSource:
    """The data a claim's number is recomputed from, and the method that recomputes it.

    Provenance checks that the files at `path` exist and match `sha256`. Recompute
    then calls the computational `method` with `path`. How the data is read, filtered and
    combined is the method's business: nothing here knows its format.
    """
    KIND: ClassVar[str] = "artifact"                  # its `kind` in the claim register
    path: str                                       # glob, may span several files
    method: MethodName                              # a computational method from a method file
    sha256: str | Mapping[str, str] | None = None   # the pinned checksum, or one per pin key
    # (utils/claim_files.py pin_key_of_file: the path from the folder before the first wildcard)
    # a checksum a person has been asked to confirm, one per pin key: a file matching
    # it passes provenance as ProvisionallyIntact, which the verdict counts as a caveat
    proposed_sha256: Mapping[str, str] | None = None


@record
class DocumentSource:
    """A log file the claim's number is traced to, for a claim that is not recomputed.

    Provenance checks that the file at `path` exists and still contains
    `must_contain`. Nothing is recomputed, and `why` records which accepted reason
    allows that.
    """
    KIND: ClassVar[str] = "document"                  # its `kind` in the claim register
    path: str
    must_contain: str                               # the exact text that anchors the number
    why: TraceReason


@record
class ClaimsSource:
    """Other claims in the same register that a claim is inferred from.

    A claim with this source is a derived claim. Provenance has no file to check, and
    there is no number to recompute. The claims it names must be readable, must not be
    superseded, and must each give a `statement`. A cycle among derived claims is
    reported by the claim check `acyclic_support`, not refused here.
    """
    KIND: ClassVar[str] = "claims"                    # its `kind` in the claim register
    claims: tuple[ClaimId, ...]                     # at least one, no id twice


# One plain value in the claim register, not a list or an object. Used in `setup`
# ({"model": "Qwen3-8B", "lr": 0.0001}).
type Scalar = str | int | float | bool
SCALAR_TYPES = members(Scalar)
NUMBER = (int, float)    # a number key accepts a whole number or a decimal


# Reading the register (schema/claim/claim_register_parser.py) raises this error, and keeps
# each decimal as it was written.

class ClaimRegisterError(VerifierError, ValueError):
    """A claim register or one of its entries cannot be used as written."""


class PrintedFloat(float):
    """A decimal number that keeps the text it was written as, so "0.50" keeps both decimals.

    JSON reads 0.50 as 0.5, which would halve how strictly recompute checks the
    claim. Every decimal in a claim register that ClaimRegisterParser reads is
    one of these. It is still a float everywhere else.
    """
    __slots__ = ("text",)
    text: str
    # the scales at which two printed numbers can be one value: as is, a percent of a
    # fraction, and a fraction of a percent
    SCALES: ClassVar[tuple[float, ...]] = (1.0, 100.0, 0.01)

    def __new__(cls, text: str) -> PrintedFloat:
        """Make the number that `text` gives, and keep `text`.

        A thousands separator ("1,300") and a minus sign ("−2") are read as in print.
        """
        # some callers give a float already; only text can carry a separator
        number = super().__new__(cls, cls.plain(text) if isinstance(text, str) else text)
        number.text = text
        return number

    @staticmethod
    def plain(text: str) -> str:
        """`text` as Python reads a number: no thousands separator, and "-" for "−"."""
        return text.replace(",", "").replace("−", "-")

    @staticmethod
    def count_printed_decimals(value: float) -> int:
        """How many decimals a number was written with: 3 for 0.127, 2 for 0.50, 0 for 1."""
        text = value.text if isinstance(value, PrintedFloat) else repr(value)
        return max(0, -int(Decimal(PrintedFloat.plain(text)).as_tuple().exponent))

    def interval(self) -> tuple[float, float]:
        """The values that print as this text: plus or minus half a unit of the last digit.

        71.3 is any value from 71.25 to 71.35, and 71 any value from 70.5 to 71.5.
        """
        half = 0.5 * 10.0 ** -self.count_printed_decimals(self)
        return float(self) - half, float(self) + half

    def could_be_same_value(self, other: PrintedFloat) -> bool:
        """Whether the two printed numbers can be one value, at a scale of 1, 100 or 1/100.

        A scale of 100 matches a fraction with its percent: 0.713 and 71.3. The intervals
        of the two (`interval`) must overlap at one of the SCALES. A negative number
        matches only a negative number. Two intervals that only touch do not overlap.
        """
        return bool(self._scales_where_intervals_overlap(other))

    def could_be_same_value_at_similar_precision(self, other: PrintedFloat) -> bool:
        """Whether the two can be one value, printed with at most one digit more or less.

        So 0.7128 matches 71.3, but 0.713 does not match 1: the interval of 1 holds
        almost every fraction, so a match with it tells nothing.
        """
        return any(max(half, other_half) <= 10 * min(half, other_half) * (1 + 1e-9)
                   for half, other_half in self._scales_where_intervals_overlap(other))

    def _scales_where_intervals_overlap(self, other: PrintedFloat
                                        ) -> list[tuple[float, float]]:
        """At each scale where the intervals overlap, the half widths of the two there."""
        if (self < 0) != (other < 0):
            return []
        low, high = self.interval()
        other_low, other_high = other.interval()
        # strictly: 0.200 and 0.201 only touch at 0.2005, and are two different values
        return [((high - low) / 2 * scale, (other_high - other_low) / 2)
                for scale in self.SCALES
                if low * scale < other_high and other_low < high * scale]
