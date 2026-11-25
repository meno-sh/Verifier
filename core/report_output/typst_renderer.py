"""The report of one run as Typst markup: one data block, then the fixed template."""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

import typst

from core.report_model.axis import Axis
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
from core.report_output import typst_template
from schema.primitives import read_only
from utils.text import paragraphs_and_bullets, typst_literal


class TypstReportRenderer:
    """Writes a VerificationReport as Typst markup, and compiles the markup to a PDF.

    The markup is one data block, then the fixed template.
    """
    # The colour of each board axis. The colour of a verdict is in its style.
    AXIS_COLOURS: ClassVar[Mapping[Axis, str]] = read_only({
        Axis.INTEGRITY: "#b42318",
        Axis.MISSING_INPUTS: "#6941c6",
        Axis.SOUNDNESS: "#c4320a",
        Axis.PRESENTATION: "#b54708",
        Axis.STRENGTH: "#0e7090",
        Axis.CAVEAT: "#667085",
    })
    # The longest subject that leaves room for the detail beside it. A group with a longer
    # subject, such as a claim and the claims it is inferred from, puts each subject on its
    # own line above its detail.
    WIDE_SUBJECT: ClassVar[int] = 40
    # The colour of the line with the reason of a dispute: the colour of the caveat axis.
    DISPUTE_REASON_COLOUR: ClassVar[str] = "#667085"
    # What separates the detail of a problem from the text it quotes (Problem.text_for_reader).
    QUOTE_MARK: ClassVar[str] = ' -- "'
    # The facts whose text is a list of claim ids, shown in a fixed-width font.
    MONO_FACTS: ClassVar[frozenset[str]] = frozenset({INFERRED_FROM})
    TEMPLATE: ClassVar[str] = typst_template.TEMPLATE
    # The longest subject, in its fixed-width font, that fits a column of a third or a half
    # of the page beside its flag id; a longer subject gets fewer columns.
    MONO_CHARS_IN_THIRD_OF_PAGE: ClassVar[int] = 20
    MONO_CHARS_IN_HALF_OF_PAGE: ClassVar[int] = 45

    def fact_data(self, label: str, text: str) -> tuple[str, str, bool, list[tuple[bool, str]]]:
        """One fact for the template: (label, text, fixed-width, parts).

        The parts are the paragraphs and the bullets of the text (paragraphs_and_bullets), so
        the PDF shows a list that a model wrote as a list.
        """
        return (label, text, label in self.MONO_FACTS, paragraphs_and_bullets(text))

    @staticmethod
    def without_backticks(text: str) -> str:
        """`text` with no backticks: the PDF shows a Markdown code span as plain text."""
        return text.replace("`", "")

    @staticmethod
    def compile_pdf(markup: str, title: str) -> bytes:
        """The PDF of the markup, with `title` at the top of the first page."""
        return typst.compile(markup.encode(), sys_inputs={"title": title})

    def axis_colours_json(self) -> dict[str, str]:
        """The colour of each axis, by the name of the axis, for the Typst template."""
        return {axis.value: colour for axis, colour in self.AXIS_COLOURS.items()}

    def render_report(self, report: VerificationReport) -> str:
        """The Typst markup of the report."""
        return self.markup_of_layout_data(self.report_layout_data(report))

    def render_start_failure(self, failure: RunStartFailure) -> str:
        """The Typst markup of the report of a run that could not start."""
        return self.markup_of_layout_data(self.start_failure_layout_data(failure))

    def markup_of_layout_data(self, data: dict[str, object]) -> str:
        """The Typst markup of a report: the layout data as one data block, then the template."""
        return f"#let data = {typst_literal(data)}\n{self.TEMPLATE}"

    def report_layout_data(self, report: VerificationReport) -> dict[str, object]:
        """The layout data of the report, which the template reads.

        The local page of `verifier web` reads the same data, saved as JSON, so the page and
        the PDF show the same parts.
        """
        v, second = report.verdict, report.second_verdict_to_show
        sections = report.sections_to_show
        data: dict[str, object] = {
            "verdict": v.value,
            "verdict-colour": v.style.report_colour,
            "verdict-without": second and second.value,
            "verdict-without-colour": second and second.style.report_colour,
            "disputed-flags-removed": report.disputed_flags_removed_words(),
            "dispute-reason-colour": self.DISPUTE_REASON_COLOUR,
            "commit": report.commit[:12] if report.commit else None,
            "action": v.style.report_action,
            "reason": None,
            "dispute": None if not report.shows_dispute_help else {
                "heading": report.DISPUTE_HEADING,
                "code": "\n".join(f"{command}  # {comment}"
                                   for command, comment in report.DISPUTE_COMMANDS),
                "send": report.DISPUTE_SEND},
            "facts": [(row.name, "\n".join([row.text, *(line for _, line in row.details)]))
                      for row in report.header_rows],
            "axis-colours": self.axis_colours_json(),
            "overview-heading": report.OVERVIEW_HEADING,
            "has-issues": bool(report.issues),
            "severities": [(axis.value, axis.effect, str(n))
                           for axis, n in report.issue_counts_by_severity()],
            "sections": self.sections_data(sections),
            "measures": [(m.claim_id, m.check_name, f"{m.value:.3g}") for m in
                         sorted(report.measured_numbers, key=lambda m: -m.value)],
            # the template reads each row by position: (check, reason, claims)
            "not-run": [(r.check_name, self.without_backticks(r.reason), r.claim_count)
                        for r in report.checks_not_run.rows],
            "not-run-claims": [
                (name, [(n.claim_id, self.without_backticks(n.reason)) for n in claims])
                for name, claims
                in report.checks_not_run.claims_of_checks_with_many_reasons.items()],
        }
        return data

    def start_failure_layout_data(self, failure: RunStartFailure) -> dict[str, object]:
        """The layout data of the report of a run that could not start."""
        outcome = failure.VERDICT
        data: dict[str, object] = {
            "verdict": outcome.value, "verdict-colour": outcome.style.report_colour,
            "verdict-without": None, "verdict-without-colour": None,
            "disputed-flags-removed": "",
            "dispute-reason-colour": self.DISPUTE_REASON_COLOUR,
            "commit": failure.commit[:12] if failure.commit else None,
            "action": RunStartFailure.ACTION, "dispute": None, "overview-heading": "",
            "has-issues": False, "severities": [],
            "reason": f"The run could not start: {failure.error}", "facts": [],
            "axis-colours": self.axis_colours_json(), "sections": [],
            "measures": [], "not-run": [], "not-run-claims": [],
        }
        return data

    def sections_data(self, sections: list[ReportSection]) -> list[dict[str, object]]:
        """The sections for the template, each with its groups; each has a unique anchor.

        A group of the claims section has the kind "claim" (claim_group_data); every other
        group has the kind "type" (issue_group_data).
        """
        out: list[dict[str, object]] = []
        for k, section in enumerate(sections):
            groups = [{**(self.claim_group_data(g) if isinstance(g, ClaimIssueGroup)
                          else {"kind": "type", **self.issue_group_data(g)}),
                       "anchor": f"group-{k}-{n}"}
                      for n, g in enumerate(section.groups)]
            out.append({"heading": section.kind.heading, "note": section.kind.note,
                        "warning": section.reader_warning,
                        "is-caveats": section.kind is ReportSectionKind.CAVEATS,
                        "anchor": f"section-{k}", "count-words": section.count_words(),
                        "count": section.count_table_cell(),
                        "issue-count": len(section.issues), "groups": groups})
        return out

    def claim_group_data(self, group: ClaimIssueGroup) -> dict[str, object]:
        """One claim for the template: its id, statement and sources, and its issues.

        Each issue is a dict: flag, code, axis, effect, detail, quote (or None), facts as
        (label, text, fixed-width) triples less the claim's own facts, and why, the reason
        of its dispute or None.
        """
        return {"kind": "claim", "claim-id": group.claim_id, "statement": group.statement,
                "place": group.place_words, "inferred-from": group.inferred_from_words,
                "count-words": group.count_words(),
                "issues": [self.claim_issue_data(i) for i in group.issues]}

    def claim_issue_data(self, issue: Issue) -> dict[str, object]:
        """One issue of a claim for the template (claim_group_data).

        An issue with facts shows only them, as a card, so its detail and quote are None.
        """
        facts = ClaimIssueGroup.facts_without_claim(issue)
        detail, quote = (None, None) if facts else self.split_quote(issue.detail)
        return {"flag": issue.flag_id, "code": issue.code, "axis": issue.axis.value,
                "effect": issue.axis.effect, "detail": detail, "quote": quote,
                "facts": [self.fact_data(label, text) for label, text in facts],
                "why": issue.dispute.reason if issue.dispute is not None else None}

    def issue_group_data(self, group: IssueGroup) -> dict[str, object]:
        """One issue type for the template, with its issues as tuples for the PDF.

        `warning` is the first sentence of the type (IssueGroup.reader_warning), or None. Each issue
        is (subject, detail, quote, facts, measured, flag, why). `why` is the
        reason of the dispute that names the issue, or None. `quote` is the text from the
        document that a problem quotes, or None. Each issue also has its facts, as (label,
        text, fixed-width) triples. When the layout is a table, `table` is true and `labels`
        are the column heads. Otherwise, when every issue has facts, `cards` is true and the
        PDF shows each issue as a card of labelled lines. When many issues say the same
        thing, `same` is that sentence and `subjects` lists what each one is about, so the
        PDF does not repeat the sentence on every line; `flags` gives the flag id of each of
        them, and `whys` the reason of each dispute.
        """
        code, issues, layout = group.code, group.issues, group.layout()
        table = layout.is_shown_as_table
        same = None if table else layout.shared_detail
        widest = max(len(i.subject) for i in issues)
        return {"code": code, "count": group.count_table_cell(),
                "count-words": group.count_words(),
                "issue-count": len(issues),
                "axis": group.axis.value, "effect": group.axis.effect,
                "warning": group.reader_warning,
                "same": same,
                "columns": (3 if widest <= self.MONO_CHARS_IN_THIRD_OF_PAGE
                            else 2 if widest <= self.MONO_CHARS_IN_HALF_OF_PAGE else 1),
                "stacked": widest > self.WIDE_SUBJECT,
                "table": table,
                "labels": [label for label, _ in issues[0].facts] if table else [],
                "subject-head": TABLE_CODES[code] if table else "",
                "wide": list(layout.wide_fact_columns) if table else [],
                "shared": layout.shared_subject,
                "cards": not same and not table and all(i.facts for i in issues),
                "subjects": [i.subject for i in issues],
                "flag-ids": [i.flag_id for i in issues],
                "dispute-reasons": [i.dispute.reason if i.dispute is not None else None
                                    for i in issues],
                "issues": [] if same else [
                    (i.subject, *self.split_quote(i.detail),
                     [self.fact_data(label, text) for label, text in i.facts],
                     None if i.measured is None else f"{i.measured:.3g}", i.flag_id,
                     i.dispute.reason if i.dispute is not None else None)
                    for i in issues]}

    @classmethod
    def split_quote(cls, detail: str) -> tuple[str, str | None]:
        """The detail of an issue, and the text it quotes from the document, or None."""
        text, sep, quote = detail.partition(cls.QUOTE_MARK)
        return (text, '"' + quote) if sep else (detail, None)


if set(TypstReportRenderer.AXIS_COLOURS) != set(Axis):
    raise RuntimeError("AXIS_COLOURS must give every axis a colour")
