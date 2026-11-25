"""The report of one run as Markdown, with the layout of the PDF."""
from __future__ import annotations

from typing import ClassVar, Protocol

from core.report_model.issues import (
    INFERRED_FROM,
    TABLE_CODES,
    ClaimIssueGroup,
    Issue,
    IssueGroup,
    ReportSection,
    ReportSectionKind,
)
from core.report_model.verification_report import RunStartFailure, VerificationReport
from utils.text import (
    markdown_code_span,
    markdown_table_cell,
    on_one_line,
    paragraphs_and_bullets,
)


class ReportRenderer(Protocol):
    """Writes the report of one run, or of a run that could not start, as text."""

    def render_report(self, report: VerificationReport) -> str:
        """The text of the report."""
        ...

    def render_start_failure(self, failure: RunStartFailure) -> str:
        """The text of the report of a run that could not start."""
        ...


class MarkdownReportRenderer:
    """Writes a VerificationReport as Markdown, with the layout of the PDF.

    The first line is the verdict, which the daemon checks is there. Each disputed flag is
    struck out, with the reason under it. When the verdict without the disputed flags
    differs, the first line strikes out the verdict, and the second line gives the verdict
    without them.
    """
    # The head of the column that gives the id of each flag (FlagIdAssigner).
    FLAG_ID_COLUMN_HEAD: ClassVar[str] = "Flag"
    # The words before the reason of a dispute, under each disputed flag.
    DISPUTED_BECAUSE: ClassVar[str] = "Disputed because:"
    # The facts whose text is a list of claim ids, shown in a fixed-width font.
    MONO_FACTS: ClassVar[frozenset[str]] = frozenset({INFERRED_FROM})

    def render_report(self, report: VerificationReport) -> str:
        """The Markdown of the report."""
        v, second = report.verdict, report.second_verdict_to_show
        sections = report.sections_to_show
        code_span = markdown_code_span
        at = f"  (commit {report.commit[:12]})" if report.commit else ""
        lines = ([f"Verdict: ~~{v.value}~~{at}",
                  f"Verdict: {second.value}  ({report.disputed_flags_removed_words()})"]
                 if second is not None else [f"Verdict: {v.value}{at}"])
        lines += ["", f"**{v.style.report_action}**", ""]
        for row in report.header_rows:
            lines.append(f"- **{row.name}:** {row.text}")
            lines += [f"{'  ' * depth}- {on_one_line(line)}" for depth, line in row.details]
        if report.shows_dispute_help:
            lines += ["", f"## {report.DISPUTE_HEADING}", "", "```bash"]
            lines += [f"{command}  # {comment}" for command, comment in report.DISPUTE_COMMANDS]
            lines += ["```", "", report.DISPUTE_SEND]
        if report.issues:
            lines += ["", f"## {report.OVERVIEW_HEADING}", "",
                      "| Section | Issues |", "|---|--:|"]
            lines += [f"| [{s.kind.heading}](#{self.heading_anchor(s.kind.heading)}) | "
                      f"{s.count_table_cell()} |" for s in sections if s.issues]
            lines += ["", "| Severity | Issues |", "|---|--:|"]
            lines += [f"| {axis.effect} | {n} |" for axis, n in report.issue_counts_by_severity()]
        for section in sections:
            lines += self.section_lines(section)
            if section.kind is ReportSectionKind.CAVEATS:
                lines += self.checks_not_run_lines(report)
        measured = sorted(report.measured_numbers, key=lambda m: -m.value)
        if measured:
            lines += ["", "## Numbers the claim checks measured", "",
                      "| Claim | Check | Number |", "|---|---|--:|"]
            lines += [f"| {markdown_table_cell(code_span(m.claim_id))} | "
                      f"{markdown_table_cell(code_span(m.check_name))} | {m.value:.3g} |"
                      for m in measured]
        if not sections and not measured:
            lines += ["", "Nothing to report."]
        return "\n".join(lines) + "\n"

    @staticmethod
    def heading_anchor(heading: str) -> str:
        """The link target of a Markdown heading, as GitHub makes it: lower case, dashes."""
        return "-".join(heading.lower().split())

    def checks_not_run_lines(self, report: VerificationReport) -> list[str]:
        """The checks that did not run, in the caveats section: a table, and the claims apart."""
        code_span = markdown_code_span
        skipped = report.checks_not_run.rows
        lines: list[str] = []
        if skipped:
            lines += ["", "### Checks that did not run", "",
                      "| Check | Why it did not run | Claims |", "|---|---|--:|"]
            lines += [f"| {markdown_table_cell(code_span(r.check_name))} | "
                      f"{markdown_table_cell(r.reason)} | {r.claim_count} |" for r in skipped]
        for name, rows in report.checks_not_run.claims_of_checks_with_many_reasons.items():
            lines += ["", f"### Why {code_span(name)} did not run", "",
                      "| Claim | Why it did not run |", "|---|---|"]
            lines += [f"| {markdown_table_cell(code_span(n.claim_id))} | "
                      f"{markdown_table_cell(n.reason)} |" for n in rows]
        return lines

    def render_start_failure(self, failure: RunStartFailure) -> str:
        """The Markdown report of a run that could not start. The first line is the verdict."""
        return (f"Verdict: {failure.VERDICT.value}"
                + (f"  (commit {failure.commit[:12]})" if failure.commit else "")
                + f"\n\n**{RunStartFailure.ACTION}** The run could not start: "
                  f"{on_one_line(failure.error)}\n")

    def section_lines(self, section: ReportSection) -> list[str]:
        """The lines of one section: its heading, its note and warning, then each group.

        The warning of the caveat flags is given once for the section, not for each type.
        """
        lines = ["", f"## {section.kind.heading}"]
        if section.kind.note is not None:
            lines += ["", section.kind.note]
        warning = section.reader_warning
        lines += ["", f"*{warning}*"] if warning is not None else []
        for group in section.groups:
            if isinstance(group, ClaimIssueGroup):
                lines += self.claim_group_lines(group)
                continue
            lines += ["", f"### {markdown_code_span(group.code)}", ""]
            lines += [f"{group.count_words()}, {group.axis.effect}", ""]
            lines += self._group_lines(group)
        return lines

    def claim_group_lines(self, group: ClaimIssueGroup) -> list[str]:
        """The lines of one claim: its place, id, statement and sources, then each issue.

        An issue with facts gives its type and its facts, as a card; one with none gives its
        detail.
        """
        code_span = markdown_code_span
        lines = ["", f"### {group.place_words}: {code_span(group.claim_id)}", ""]
        if group.statement:
            lines += [f"*{on_one_line(group.statement)}*", ""]
        if group.inferred_from_words is not None:
            lines += [f"{group.inferred_from_words}.", ""]
        for issue in group.issues:
            head = f"{code_span(issue.flag_id)} {code_span(issue.code)}, {issue.axis.effect}"
            if not group.facts_without_claim(issue):
                head += f": {on_one_line(issue.detail)}"
            lines.append("- " + self.struck_out_if_disputed(head, issue))
            lines += [line for label, text in group.facts_without_claim(issue)
                      for line in self.fact_lines(label, text, issue, "  ")]
            lines += [f"  - {self.dispute_reason_line(issue)}"] if issue.is_disputed else []
        return lines

    def _group_lines(self, group: IssueGroup) -> list[str]:
        """The lines of one issue type: a table, one shared detail, one subject, or a list."""
        issues, layout, code_span = group.issues, group.layout(), markdown_code_span
        if layout.is_shown_as_table:
            return self.table_lines(group)
        same = layout.shared_detail
        if same is not None:
            kept = [i for i in issues if not i.is_disputed]
            lines = [f"All {len(issues)} say: {on_one_line(same)}"]
            if kept:
                lines += ["", ", ".join(f"{code_span(i.flag_id)} {code_span(i.subject)}"
                                        for i in kept)]
            for i in issues:
                if i.is_disputed:
                    lines += ["", f"~~{code_span(i.flag_id)} {code_span(i.subject)}~~", "",
                              self.dispute_reason_line(i)]
            return lines
        shared = layout.shared_subject
        if shared is not None and not any(i.facts for i in issues):
            lines = [f"In {code_span(shared)}:", ""]
            for i in issues:
                item = f"{code_span(i.flag_id)} {on_one_line(i.detail)}"
                lines.append(f"- {self.struck_out_if_disputed(item, i)}")
                lines += [f"  - {self.dispute_reason_line(i)}"] if i.is_disputed else []
            return lines
        return [line for i in issues for line in self.issue_list_item(i)]

    def table_lines(self, group: IssueGroup) -> list[str]:
        """The Markdown table of a group that its layout shows as a table.

        When the issues share one subject, the subject is given once above the table.
        """
        issues, layout, code, code_span = (group.issues, group.layout(), group.code,
                                           markdown_code_span)
        labels = [label for label, _ in issues[0].facts]
        aligns = ["---|" if wide else "--:|" for wide in layout.wide_fact_columns]
        shared = layout.shared_subject
        if shared is not None:
            return ([f"In {code_span(shared)}:", "",
                     f"| {self.FLAG_ID_COLUMN_HEAD} | " + " | ".join(labels) + " |",
                     "|---|" + "".join(aligns)]
                    + [line for i in issues
                       for line in self._table_rows_of_issue(
                           i, [code_span(i.flag_id), *(text for _, text in i.facts)])])
        return ([f"| {self.FLAG_ID_COLUMN_HEAD} | {TABLE_CODES[code]} | "
                 + " | ".join(labels) + " |",
                 "|---|---|" + "".join(aligns)]
                + [line for i in issues for line in
                   self._table_rows_of_issue(i, [code_span(i.flag_id), code_span(i.subject),
                                                 *(text for _, text in i.facts)])])

    def _table_rows_of_issue(self, issue: Issue, cells: list[str]) -> list[str]:
        """The table row of one issue, struck out and with the reason under it when disputed."""
        row = ("| " + " | ".join(markdown_table_cell(self.struck_out_if_disputed(c, issue))
                                 for c in cells) + " |")
        if not issue.is_disputed:
            return [row]
        return [row, f"| {markdown_table_cell(self.dispute_reason_line(issue))} |"
                + " |" * (len(cells) - 1)]

    def issue_list_item(self, issue: Issue) -> list[str]:
        """The Markdown list item of one issue: its facts as nested items, when it has them."""
        code_span = markdown_code_span
        reason = ([f"  - {self.dispute_reason_line(issue)}"]
                  if issue.is_disputed else [])
        if not issue.facts:
            return ["- " + self.struck_out_if_disputed(
                f"{code_span(issue.flag_id)} {code_span(issue.subject)}: "
                f"{on_one_line(issue.detail)}", issue)] + reason
        head = f"{code_span(issue.flag_id)} **{code_span(issue.subject)}**"
        return (["- " + self.struck_out_if_disputed(head, issue)]
                + [line for label, text in issue.facts
                   for line in self.fact_lines(label, text, issue, "  ")] + reason)

    def fact_lines(self, label: str, text: str, issue: Issue, indent: str) -> list[str]:
        """The list item of one fact; the bullets of its text are items one level deeper."""
        if label in self.MONO_FACTS:
            return [f"{indent}- " + self.struck_out_if_disputed(
                f"**{label}:** {self._fact_text(label, text)}", issue)]
        parts = paragraphs_and_bullets(text)
        lead = " ".join(t for is_bullet, t in parts[:1] if not is_bullet)
        lines = [f"{indent}- " + self.struck_out_if_disputed(
            f"**{label}:** {lead}".rstrip(), issue)]
        for is_bullet, part in parts[1 if lead else 0:]:
            lines.append(f"{indent}  - " + self.struck_out_if_disputed(part, issue) if is_bullet
                         else f"{indent}  " + self.struck_out_if_disputed(part, issue))
        return lines

    @classmethod
    def _fact_text(cls, label: str, text: str) -> str:
        """The text of one fact: a list of claims in code spans, else one line."""
        if label in cls.MONO_FACTS:
            return ", ".join(markdown_code_span(s) for s in text.split(", "))
        return on_one_line(text)

    @staticmethod
    def struck_out_if_disputed(text: str, issue: Issue) -> str:
        """`text` struck out in Markdown when the issue is disputed, else `text`."""
        return f"~~{text}~~" if issue.is_disputed and text else text

    @classmethod
    def dispute_reason_line(cls, issue: Issue) -> str:
        """The line under a disputed flag: the reason of the dispute."""
        reason = issue.dispute.reason if issue.dispute is not None else ""
        return f"*{cls.DISPUTED_BECAUSE} {on_one_line(reason)}*"
