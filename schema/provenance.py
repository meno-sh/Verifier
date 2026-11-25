"""Provenance results: is the file behind a claim's number there, and unchanged?

One result per claim, in one of four groups. Fine results let the claim go on.
A provisional result also lets it go on, so its number is still recomputed, but the
verdict counts it as a caveat until a person confirms it. A caveat means there was
nothing to check, which the verdict reports as PASS WITH CAVEATS. A broken result
means the claim cannot be verified, and the verdict is REJECT.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import ClassVar

from schema.primitives import GateResult, TraceReason, record

# What provenance found for one claim. The verdict reads the four categories, never
# the individual classes, so a new result only needs its category.


class ProvenanceCategory(Enum):
    """The four groups of provenance results, which the verdict reads."""
    FINE = "fine"                   # checked further, traced on purpose, or inferred
    PROVISIONAL = "provisional"     # matches a checksum a person has been asked to confirm
    CAVEAT = "caveat"               # nothing to check the claim against
    BROKEN = "broken"               # the claim cannot be verified: REJECT


@record
class ProvenanceResult(GateResult):
    """The result of the provenance check of one claim. Each subclass sets its category."""
    category: ClassVar[ProvenanceCategory]
    # how the summary line of the report names this result
    report_word: ClassVar[str]
    # True when recompute runs after this result
    allows_recompute: ClassVar[bool] = False
    # True when the gate note of the claim describes the recompute result, not this one
    gate_note_describes_recompute: ClassVar[bool] = False
    # board_code (GateResult) is on the integrity axis for a broken result, and on the
    # coverage axis for a result that leaves the claim unverified

    @property
    def is_broken(self) -> bool:
        """True when the claim cannot be verified, so the verdict is REJECT."""
        return self.category is ProvenanceCategory.BROKEN

    @property
    def counts_as_caveat(self) -> bool:
        """True when the verdict counts the claim as a caveat: nothing to check, or provisional."""
        return self.category in (ProvenanceCategory.CAVEAT, ProvenanceCategory.PROVISIONAL)

    def reason_recompute_did_not_run(self) -> str:
        """The reason recompute did not run after this result, for the Skipped result."""
        return "not recomputed: provenance failed"



# Fine: the claim can be checked further, is traced on purpose, or is inferred from other claims.

@record
class Intact(ProvenanceResult):
    """The artifact exists and matches its pinned checksum."""
    category = ProvenanceCategory.FINE
    report_word = "recomputed from data"
    allows_recompute = True
    gate_note_describes_recompute = True
    path: str

    def describe_for_reader(self) -> str:
        """That the artifact is there and unchanged."""
        return "artifact present and matches its pinned checksum"


@record
class Traced(ProvenanceResult):
    """Traced to a log on purpose, and the anchor text was found there."""
    category = ProvenanceCategory.FINE
    report_word = "traced to a log"
    board_code = "traced"
    gate_status_word = "TRACED"
    path: str
    anchor: str
    reason: TraceReason

    def reason_recompute_did_not_run(self) -> str:
        """That the claim is traced on purpose."""
        return "not recomputed: traced to a log on purpose"

    def describe_for_reader(self) -> str:
        """Why the claim is traced, and the log it is traced to."""
        return (f"not recomputed on purpose -- {self.reason.meaning_for_reader}. "
                f"Traced instead to {os.path.basename(self.path)}.")


@record
class Derived(ProvenanceResult):
    """Inferred from other claims in the register, so there is no file to check.

    How far the claims it names support it is a question for the claim checks that
    read dependencies, such as `claim_uncertainty`.
    """
    category = ProvenanceCategory.FINE
    report_word = "inferred from other claims"
    gate_status_word = "DERIVED"
    claims: tuple[str, ...]         # the ids of the claims it is inferred from

    def reason_recompute_did_not_run(self) -> str:
        """That a derived claim has no number to recompute."""
        return "not recomputed: inferred from other claims, so there is no number to recompute"

    def describe_for_reader(self) -> str:
        """The claims it is inferred from."""
        return f"inferred from other claims: {', '.join(self.claims)}"


# Provisional: the file matches a checksum a person has been asked to confirm.

@record
class ProvisionallyIntact(ProvenanceResult):
    """The artifact matches a proposed checksum (`proposed_sha256`), not its pin.

    Recompute still runs, so a number the new file moves is still rejected. The
    verdict counts the claim as a caveat until the proposal is confirmed into the pin.
    """
    category = ProvenanceCategory.PROVISIONAL
    report_word = "waiting for a pin to be confirmed"
    board_code = "pin-proposed"
    allows_recompute = True
    path: str
    files: tuple[str, ...]          # the file names that match only their proposed checksum

    def describe_for_reader(self) -> str:
        """Which files match only a proposed checksum."""
        return (f"{', '.join(self.files)} changed, and match(es) a proposed checksum nobody has "
                f"confirmed; recomputed, and counted as a caveat")


# Caveat: there was nothing to check the claim against.

@record
class NoSource(ProvenanceResult):
    """The claim register gives nothing to check the claim against."""
    category = ProvenanceCategory.CAVEAT
    report_word = "no source"
    board_code = "no-source"
    gate_status_word = "SKIP"

    def reason_recompute_did_not_run(self) -> str:
        """That the claim has no source."""
        return "not recomputed: the claim register gives no source"

    def describe_for_reader(self) -> str:
        """That no source is declared."""
        return "no source declared"


@record
class SelfTraced(ProvenanceResult):
    """Traced to the same note the claim was written in, which checks nothing.

    A trace must point at a log or output the number was copied from. Pointing at the
    claim's own note only shows the note contains its own words.
    """
    category = ProvenanceCategory.CAVEAT
    report_word = "traced only to their own note"
    board_code = "self-traced"
    gate_status_word = "SKIP"
    path: str

    def reason_recompute_did_not_run(self) -> str:
        """That the claim is traced only to its own note."""
        return "not recomputed: traced only to the note the claim was written in"

    def describe_for_reader(self) -> str:
        """The note the claim is traced to, which checks nothing."""
        return (f"traced only to {os.path.basename(self.path)}, the note the claim was "
                f"written in, which checks nothing")


# Broken: the claim cannot be verified, and the verdict is REJECT.

@record
class Missing(ProvenanceResult):
    """The artifact or document is gone."""
    category = ProvenanceCategory.BROKEN
    report_word = "missing file"
    board_code = "artifact-missing"
    path: str

    def describe_for_reader(self) -> str:
        """The file that is gone."""
        return f"file missing: {self.path}"


@record
class Changed(ProvenanceResult):
    """The file changed since its checksum was recorded."""
    category = ProvenanceCategory.BROKEN
    report_word = "changed file"
    board_code = "hash-mismatch"
    file: str
    expected_sha256: str
    actual_sha256: str

    def describe_for_reader(self) -> str:
        """The file whose checksum changed."""
        return f"checksum mismatch for {self.file}: file changed since it was recorded"


@record
class AnchorMissing(ProvenanceResult):
    """The log exists but no longer contains the anchor text."""
    category = ProvenanceCategory.BROKEN
    report_word = "anchor gone"
    board_code = "anchor-missing"
    path: str
    anchor: str

    def describe_for_reader(self) -> str:
        """The log and the anchor text that is no longer in it."""
        return f"anchor not found in {os.path.basename(self.path)}: {self.anchor!r}"
