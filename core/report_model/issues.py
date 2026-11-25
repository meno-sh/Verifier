"""The issues of a report: one record for each thing a reader must see, in groups and sections."""
from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from core.report_model.axis import Axis
from schema.primitives import record

# The issue types that the reports show as a table of their facts, each with the head of
# the subject column.
TABLE_CODES = {"number-not-in-any-table": "Document"}
# A group of at least this many issues that all say the same thing gives that sentence
# once, then only what each issue is about.
SAME_DETAIL_MIN = 4
# The longest text of a fact that fits a narrow column of a table.
NARROW_SUBJECT = 30
# The label of the fact that lists the claims a claim is inferred from.
INFERRED_FROM = "Inferred from"
# The label of the fact that gives the statement of a claim.
CLAIM_STATEMENT = "Claim"


@record
class IssueIdentity:
    """The code, subject and detail of an issue.

    Two flags with the same identity are the same flag in two reports. The flag id, the
    disputes and the saved report all use this one rule. Every record of a flag extends
    this class: Issue, DisputedFlag (core/report_model/disputed_flags.py), and SavedFlag
    and SavedIssue (core/report_output/saved_report.py).
    """
    code: str
    subject: str
    detail: str

    def identity(self) -> IssueIdentity:
        """The code, subject and detail alone, which name the flag in every report."""
        return IssueIdentity(code=self.code, subject=self.subject, detail=self.detail)


@record
class FlagDispute:
    """The dispute that says a flag is false: its id and its reason."""
    dispute_id: str
    reason: str

    def to_json(self) -> dict[str, object]:
        """The dispute as the saved report writes it."""
        return {"dispute_id": self.dispute_id, "reason": self.reason}


@record
class Issue(IssueIdentity):
    """One issue of the report: its type, the board axis of that type, what it is about, and what.

    `subject` is the path of the document or the claim, and `detail` says what is wrong with
    it. `facts` are the parts of the detail as (label, text) lines, when the problem gives
    them. Then `subject` is the id of the claim alone, and the first facts are the statement
    of the claim and the claims it is inferred from.
    """
    axis: Axis
    facts: tuple[tuple[str, str], ...] = ()
    # the number the check measured on the claim, such as its uncertainty, or None
    measured: float | None = None
    # the flag id (FlagIdAssigner), which a reader uses to name this issue; set by IssueSectioner
    flag_id: str = ""
    # whether a dispute can name this flag (the issue has a place); set by IssueSectioner
    disputable: bool = False
    # the dispute that says this flag is false, or None
    dispute: FlagDispute | None = None

    @property
    def is_disputed(self) -> bool:
        """True when a dispute names this issue."""
        return self.dispute is not None


@record
class FlagPlaceInTree:
    """Where the flag of an issue is in the Findings tree, so a copy of the tree can leave it out.

    `paper` is the path of the document, or `claim` the index of the claim in tree.claims.
    `check` and `index` name one problem of a Flagged result. Both are None for a claim whose
    gate failed: the flag is the whole claim.
    """
    paper: str | None = None
    claim: int | None = None
    check: str | None = None
    index: int | None = None


@record
class LocatedIssue:
    """An issue and the place of its flag, or no place when nobody can dispute it."""
    issue: Issue
    place: FlagPlaceInTree | None


@record
class ClaimIssueGroup:
    """The issues of one claim, most severe first, under the claim's id and statement.

    The reports give the statement and the claims it is inferred from once, above the
    issues, so they leave those facts out of each issue (facts_without_claim).
    """
    # The facts that the claim gives above its issues.
    CLAIM_FACTS: ClassVar[frozenset[str]] = frozenset({CLAIM_STATEMENT, INFERRED_FROM})
    claim_id: str
    statement: str | None
    # the place of the claim in the register order, from 1, and the number of claims
    register_place: int
    register_size: int
    # the register places of the claims of a ClaimsSource, else none
    inferred_from_places: tuple[int, ...]
    issues: tuple[Issue, ...]

    @property
    def place_words(self) -> str:
        """The place of the claim in the register order, as words: "Claim 56 of 112"."""
        return f"Claim {self.register_place} of {self.register_size}"

    @property
    def inferred_from_words(self) -> str | None:
        """The register places of the claims it is inferred from, as words, or None."""
        places = [str(p) for p in self.inferred_from_places]
        if not places:
            return None
        listed = places[0] if len(places) == 1 else ", ".join(places[:-1]) + " and " + places[-1]
        return f"Inferred from claim{'s' if len(places) > 1 else ''} {listed}"

    @classmethod
    def facts_without_claim(cls, issue: Issue) -> tuple[tuple[str, str], ...]:
        """The facts of an issue, less the statement and the claims it is inferred from.

        An issue with facts shows only them, as a card; an issue with none shows its detail.
        """
        return tuple((label, text) for label, text in issue.facts
                     if label not in cls.CLAIM_FACTS)

    def count_words(self) -> str:
        """How many issues the claim has, and how many of them are disputed, as words."""
        return issue_and_disputed_count_words(self.issues)


@record
class IssueGroup:
    """The issues of one type, in report order."""
    code: str
    issues: tuple[Issue, ...]

    @property
    def axis(self) -> Axis:
        """The board axis of the type."""
        return self.issues[0].axis

    @property
    def reader_warning(self) -> str | None:
        """The first sentence of the type: the warning of its axis when its issues are flags.

        A caveat flag comes from a cautious check, so its type starts with a warning; a
        caveat that is not a flag (a check that did not run) does not.
        """
        return self.axis.reader_warning if any(i.disputable for i in self.issues) else None

    def disputed_count(self) -> int:
        """How many issues of the group are disputed."""
        return sum(i.is_disputed for i in self.issues)

    def count_words(self) -> str:
        """How many issues the type has, and how many of them are disputed, as words."""
        return issue_and_disputed_count_words(self.issues)

    def count_table_cell(self) -> str:
        """How many issues the type has, and how many of them are disputed, for a table cell."""
        n = self.disputed_count()
        return str(len(self.issues)) + (f" ({n} disputed)" if n else "")

    def layout(self) -> IssueGroupLayout:
        """How both renderers show this group."""
        return IssueGroupLayout.of_group(self)


def issue_count_words(n: int) -> str:
    """`n` issues, as words."""
    return f"{n} issue" + ("" if n == 1 else "s")


def issue_and_disputed_count_words(issues: tuple[Issue, ...]) -> str:
    """How many `issues` there are, and how many of them are disputed, as words."""
    disputed = sum(i.is_disputed for i in issues)
    return issue_count_words(len(issues)) + (f" ({disputed} disputed)" if disputed else "")


@record
class IssueGroupLayout:
    """How both renderers show one group: as a table, as one shared detail, with one subject.

    Computed once for each group, so the Markdown and the PDF never disagree.
    """
    # True when the type is in TABLE_CODES and every issue has facts with the same labels;
    # then the reports show the issues as a table with one column for each label
    is_shown_as_table: bool
    # the one detail of a large group whose issues all say the same thing, else None
    shared_detail: str | None
    # the one subject of two or more issues, which the reports then give once, else None
    shared_subject: str | None
    # for each fact of a table, True when a text is longer than NARROW_SUBJECT; a wide
    # column takes the free width of the page and is aligned left
    wide_fact_columns: tuple[bool, ...]

    @classmethod
    def of_group(cls, group: IssueGroup) -> IssueGroupLayout:
        """The layout of a group."""
        issues = group.issues
        labels = [label for label, _ in issues[0].facts]
        table = (group.code in TABLE_CODES and bool(labels)
                 and all([label for label, _ in i.facts] == labels for i in issues))
        same = (issues[0].detail if len(issues) >= SAME_DETAIL_MIN
                and len({i.detail for i in issues}) == 1 else None)
        subjects = {i.subject for i in issues}
        shared = next(iter(subjects)) if len(issues) > 1 and len(subjects) == 1 else None
        wide = tuple(any(len(i.facts[k][1]) > NARROW_SUBJECT for i in issues)
                     for k in range(len(issues[0].facts))) if table else ()
        return cls(is_shown_as_table=table, shared_detail=same, shared_subject=shared,
                   wide_fact_columns=wide)


class ReportSectionKind(StrEnum):
    """One section of the report, in report order.

    The caveats are the issues on the caveat axis. Of the other issues, those of a claim
    are in the claims section, and all others (the documents, the inputs and the claim
    register entries that cannot be read) are in the setup section.
    """
    SETUP = "setup"
    CLAIMS = "claims"
    CAVEATS = "caveats"

    @property
    def heading(self) -> str:
        """The heading of the section."""
        return _SECTION_HEADINGS[self]

    @property
    def note(self) -> str | None:
        """The sentence under the heading of the section, or None when it has none."""
        return _SECTION_NOTES.get(self)


_SECTION_HEADINGS = {
    ReportSectionKind.SETUP: "Setup Issues Needing Attention",
    ReportSectionKind.CLAIMS: "Claims Needing Attention",
    ReportSectionKind.CAVEATS: "Caveats",
}
# Only the caveats section has a sentence under its heading.
_SECTION_NOTES = {
    ReportSectionKind.CAVEATS: "These do not block: a check did not run, or a cautious check "
                               "flagged a sentence.",
}
if set(_SECTION_HEADINGS) != set(ReportSectionKind):
    raise RuntimeError("each section of the report needs a heading")


@record
class ReportSection:
    """One section of the report: its groups of issues, in report order.

    The setup and caveats sections group the issues by type (IssueGroup). The claims
    section groups them by claim (ClaimIssueGroup).
    """
    kind: ReportSectionKind
    groups: tuple[IssueGroup | ClaimIssueGroup, ...]

    @property
    def issues(self) -> tuple[Issue, ...]:
        """The issues of the section, in report order."""
        return tuple(i for g in self.groups for i in g.issues)

    @property
    def reader_warning(self) -> str | None:
        """The warning of the caveat flags, once for the section, or None when it has none."""
        return next((g.reader_warning for g in self.groups
                     if isinstance(g, IssueGroup) and g.reader_warning is not None), None)

    def count_words(self) -> str:
        """How many issues the section has, and how many of them are disputed, as words."""
        n = sum(i.is_disputed for i in self.issues)
        return issue_count_words(len(self.issues)) + (f" ({n} disputed)" if n else "")

    def count_table_cell(self) -> str:
        """How many issues the section has, and how many of them are disputed, for a table cell."""
        n = sum(i.is_disputed for i in self.issues)
        return str(len(self.issues)) + (f" ({n} disputed)" if n else "")
