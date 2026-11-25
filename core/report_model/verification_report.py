"""One object for one run: everything the report of `vcheck run` shows, computed once."""
from __future__ import annotations

import collections
from typing import ClassVar

from core.analysis import findings_json, verdict_policy
from core.report_model.axis import Axis
from core.report_model.checks_not_run import ChecksNotRunTable
from core.report_model.issues import Issue, ReportSection, ReportSectionKind, issue_count_words
from core.report_model.measured_numbers import MeasuredNumber
from core.report_model.report_header import METHODS_RAN_IN, ReportHeaderRow
from core.report_model.triage_board import TriageBoard, TriageBoardBuilder
from core.report_model.verdict_explainer import VerdictExplainer
from schema import findings as schema_findings
from schema.primitives import record
from schema.verdict import Verdict


@record
class VerdictSummary:
    """The verdict of the findings, their triage board, and the sentence that explains it.

    Every report shows these three: the report of `vcheck run` (VerificationReport
    extends this class), and `verifier static-check` in the terminal and as JSON.
    """
    findings: schema_findings.Findings
    verdict: Verdict
    triage_board: TriageBoard
    reason_for_verdict: str                          # the sentence that explains the verdict

    @classmethod
    def of_findings(cls, tree: schema_findings.Findings) -> VerdictSummary:
        """The verdict of `tree` by the verdict policy, its board, and the reason."""
        verdict = verdict_policy.VerdictPolicy().verdict_for(tree)
        board = TriageBoardBuilder().build_board(tree)
        return cls(findings=tree, verdict=verdict, triage_board=board,
                   reason_for_verdict=VerdictExplainer().explain_verdict(verdict, board, tree))

    def to_json(self) -> dict[str, object]:
        """The verdict, why, the triage board and the findings, as one JSON object."""
        return {"verdict": self.verdict.value, "why": self.reason_for_verdict,
                "board": self.triage_board.to_json(),
                "findings": findings_json.FindingsJsonCodec().encode(self.findings)}


@record
class VerificationReport(VerdictSummary):
    """Everything one run of `vcheck run` shows, computed once.

    The Markdown, the Typst markup and the saved JSON all read this object, so nothing
    computes the issues or the verdict twice.
    """
    commit: str | None
    # the verdict without the disputed flags; None when no flag of the run is disputed
    verdict_without_disputed_flags: Verdict | None
    # the setup, claims and caveats sections, in this order; a section can be empty
    report_sections: tuple[ReportSection, ...]
    # the "Methods ran in" row first, then the rows of the extensions
    rows_under_verdict: tuple[ReportHeaderRow, ...]
    header_rows: tuple[ReportHeaderRow, ...]
    measured_numbers: tuple[MeasuredNumber, ...]
    checks_not_run: ChecksNotRunTable

    # The section on how to dispute a flag and share the result, in a report with a flag that
    # can be disputed. The commands are those of the diagnosis extension, each with its comment.
    DISPUTE_HEADING: ClassVar[str] = "How to Dispute False Issues"
    DISPUTE_COMMANDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ('verifier dispute . --flag F-3a9c1e --why "your reason"',
         "mark one flag as false, with your reason"),
        ("verifier diagnosis . --share", "write the diagnosis of your disputes to one file"),
    )
    DISPUTE_SEND: ClassVar[str] = ("Please read the file to double-check its anonymity, then "
                                   "send it to verifier@meno.sh.")
    # The heading of the tables of the issue counts, by section and by severity.
    OVERVIEW_HEADING: ClassVar[str] = "Overview of Issues"

    @property
    def sections_to_show(self) -> list[ReportSection]:
        """The sections with an issue, and the caveats section when a check did not run.

        The caveats section holds the table of the checks that did not run.
        """
        return [s for s in self.report_sections
                if s.issues or (s.kind is ReportSectionKind.CAVEATS and self.checks_not_run.rows)]

    @property
    def shows_dispute_help(self) -> bool:
        """True when a flag of the report can be disputed, so the report says how."""
        return any(i.disputable for i in self.issues)

    def issue_counts_by_severity(self) -> tuple[tuple[Axis, int], ...]:
        """How many issues each severity (board axis) has, most severe first; none with 0."""
        counts = collections.Counter(i.axis for i in self.issues)
        return tuple((axis, counts[axis]) for axis in Axis if counts[axis])

    @property
    def issues(self) -> tuple[Issue, ...]:
        """Every issue of the report, in report order: the sections, then their groups."""
        return tuple(i for section in self.report_sections for i in section.issues)

    @property
    def second_verdict_to_show(self) -> Verdict | None:
        """The verdict without the disputed flags, when it differs from the verdict."""
        second = self.verdict_without_disputed_flags
        return None if second is self.verdict else second

    def disputed_flags_removed_words(self) -> str:
        """How many disputed flags the second verdict leaves out, as words."""
        n = sum(i.is_disputed for i in self.issues)
        return f"{n} disputed flag{'' if n == 1 else 's'} removed"

    def issue_count_words(self) -> str:
        """How many issues the report has, as words."""
        return issue_count_words(len(self.issues))

    @property
    def methods_ran_in(self) -> str | None:
        """The text of the "Methods ran in" row, or None when the run gave none."""
        return next((r.text for r in self.rows_under_verdict if r.name == METHODS_RAN_IN),
                    None)


@record
class RunStartFailure:
    """A run of `vcheck run` that could not start: its commit and the error."""
    # what the reader does next, in both reports of a run that could not start
    ACTION: ClassVar[str] = "Fix this in the project, then commit again."
    # the verdict of a run that could not start: the project must change first
    VERDICT: ClassVar[Verdict] = Verdict.BLOCK_UNTIL_FIXED
    commit: str | None
    error: str
