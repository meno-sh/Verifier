"""Building the VerificationReport of one run, once, from its Findings."""
from __future__ import annotations

from collections.abc import Iterable

from core.analysis import verdict_policy
from core.report_model.checks_not_run import ChecksNotRunCollector
from core.report_model.disputed_flags import DisputedFlag, DisputedFlagSet
from core.report_model.issue_collector import IssueCollector
from core.report_model.issue_grouping import FlagIdAssigner, IssueSectioner
from core.report_model.measured_numbers import MeasuredNumberCollector
from core.report_model.report_header import ReportHeaderBuilder, ReportHeaderRow
from core.report_model.verification_report import VerdictSummary, VerificationReport
from schema import findings


class VerificationReportBuilder:
    """Builds a VerificationReport once from a Findings."""

    def __init__(self) -> None:
        """Build reports with the collectors of this package and the verdict policy."""
        self.policy = verdict_policy.VerdictPolicy()

    def build_report(self, tree: findings.Findings, commit: str | None,
                     rows_under_verdict: Iterable[ReportHeaderRow] = (),
                     disputed_flags: Iterable[DisputedFlag] = ()) -> VerificationReport:
        """The report of one run.

        Args:
            tree: what the run found.
            commit: the commit the run checked, or None.
            rows_under_verdict: the "Methods ran in" row first, then the rows of the
                extensions, such as what is waiting for a person.
            disputed_flags: the flags that a dispute says are false.
        """
        disputes = DisputedFlagSet(disputed_flags, self.policy)
        summary = VerdictSummary.of_findings(tree)
        second = disputes.verdict_without_disputed_flags(tree)
        rows = tuple(rows_under_verdict)
        return VerificationReport(
            commit=commit, findings=tree, verdict=summary.verdict,
            verdict_without_disputed_flags=second,
            report_sections=IssueSectioner(FlagIdAssigner()).sort_into_sections(
                IssueCollector().collect_issues(tree), [c.claim for c in tree.claims],
                disputes.disputed_flags),
            rows_under_verdict=rows,
            header_rows=ReportHeaderBuilder().build_header_rows(tree, rows),
            measured_numbers=tuple(MeasuredNumberCollector().collect_measured_numbers(tree)),
            checks_not_run=ChecksNotRunCollector().table(tree),
            triage_board=summary.triage_board,
            reason_for_verdict=summary.reason_for_verdict)
