#!/usr/bin/env python3
"""Assert that the Typst markup of a report holds the same findings as the Markdown report.

Covers:
  - a Python value becomes a Typst literal, and a string with quotes, backslashes and
    control characters stays one string;
  - the markup of the fixture run gives the verdict, and the sections and issues in the
    order of the Markdown report;
  - many issues that say the same thing give that sentence once, with the subjects;
  - a problem that quotes the document gives the quote apart from the detail;
  - a group with a long subject puts each subject above its detail, and the markup compiles;
  - a group whose issues all have facts gives cards of labelled lines, in both reports;
  - a number the data does not give is an issue of its claim, with the paper's and the data's
    number;
  - issues on one document give the document once, and a long fact is a wide column;
  - issues with a measured number come highest first, and a card shows the number;
  - a check that did not run for many reasons gets one row, and its claims a table;
  - an indented notice line is a detail of the header row above it;
  - `vcheck run` saves latest.md, latest.typ, latest.pdf and latest.layout.json, and
    `verifier report --json` gives the markup under "typst" and the layout data, which is
    the data block of the markup, under "layout";
  - the markup compiles to a PDF.
Returns 1 if any case fails, so it can gate a commit.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_core  # noqa: E402

from core import cli  # noqa: E402
from core.report_model import checks_not_run, issue_grouping, report_header, verification_report  # noqa: E402
from core.report_model.axis import Axis  # noqa: E402
from core.report_model.issues import INFERRED_FROM, Issue, IssueGroup  # noqa: E402
from core.report_output import (  # noqa: E402
    markdown_renderer,
    report_store,
    typst_renderer,
    typst_template,
)
from runtime.project import project_status  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils.text import paragraphs_and_bullets, typst_literal  # noqa: E402

TYPST = typst_renderer.TypstReportRenderer()
MARKDOWN = markdown_renderer.MarkdownReportRenderer()

FAILED = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def test_literal(tmp: Path) -> None:
    """Values become Typst literals."""
    got = typst_literal({"a": [1, 2.5, None, True], "b": ("x",), "c": {}})
    expect("literal: a dictionary, arrays, a one-item array and an empty dictionary",
           got == '("a": (1, 2.5, none, true), "b": ("x",), "c": (:))', got)
    got = typst_literal('say "hi" \\ now\nand\tthen')
    expect("literal: quotes, a backslash, a newline and a tab are escaped",
           got == '"say \\"hi\\" \\\\ now\\nand\\u{9}then"', got)
    try:
        typst_literal(object())
        expect("literal: an unknown type is refused", False)
    except TypeError:
        expect("literal: an unknown type is refused", True)


def one_section_of(groups: list[dict]) -> dict:
    """The data of one section of the template that holds these issue type groups."""
    return {"sections": [{"heading": "Section", "note": "Note.", "anchor": "section-test",
                          "count-words": "some issues", "count": "3", "issue-count": 3,
                          "warning": None, "is-caveats": False,
                          "groups": [{**g, "kind": "type", "anchor": f"group-test-{n}"}
                                     for n, g in enumerate(groups)]}]}


def test_fixture(tmp: Path) -> None:
    """The fixture run gives the verdict and the issue types in the Markdown report's order."""
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    markup = test_core.typst_of(tree, "abcdef1234567890", [report_header.ReportHeaderRow(
        name=report_header.METHODS_RAN_IN, text="x")])
    data = markup.split("\n", 1)[0]
    expect("fixture: the markup starts with the data block",
           data.startswith("#let data = (") and '"verdict": "MISSING INPUTS"' in data, data[:80])
    expect("fixture: the header table leaves out the row of where the methods ran",
           '("Methods ran in", "x")' not in data and '"Claims checked"' in data)
    flags = [i.flag_id for s in test_core.report_sections_of(tree) for i in s.issues]
    places = [data.index(f'"{flag}"') for flag in flags]
    expect("fixture: the issues are in the order of the Markdown report",
           places == sorted(places), f"{flags}")
    headings = [data.index(f'"heading": "{h}"') for h in
                ("Setup Issues Needing Attention", "Claims Needing Attention", "Caveats")]
    expect("fixture: the sections are setup, claims, then caveats",
           headings == sorted(headings), repr(headings))
    expect("fixture: the template follows the data block", markup.endswith(typst_template.TEMPLATE))


def test_same_detail(tmp: Path) -> None:
    """Many claims with no source give the sentence once, and each claim's id."""
    many = [{**test_core.claim_entry("no-source"), "id": f"plain-{i}"} for i in range(5)]
    root, state = test_core.workdir(tmp, many)
    group = next(TYPST.issue_group_data(g) for g in
                 test_core.type_groups_of(test_core.analyse_project(root, state))
                 if g.code == "no-source")
    expect("same detail: the sentence is given once",
           group["same"] == "no source declared" and group["issues"] == [], repr(group))
    expect("same detail: each claim is listed",
           [s.split(" ")[0] for s in group["subjects"]] == [f"plain-{i}" for i in range(5)],
           repr(group["subjects"]))


def test_bullets(tmp: Path) -> None:
    """A list that a model writes in a fact is a list in both reports."""
    expect('bullets: each "- " line is a bullet, an indented line continues it',
           paragraphs_and_bullets("lead\nmore\n- a\n  b\n- c\nend")
           == [(False, "lead more"), (True, "a b"), (True, "c"), (False, "end")])
    issue = Issue(code="u", axis=Axis.STRENGTH, subject="s", detail="d", flag_id="F-1",
                  facts=(("Worst case", "- one\n- two"),))
    expect("bullets: the Markdown gives each bullet as an item under its fact",
           MARKDOWN.issue_list_item(issue)[1:] == ["  - **Worst case:**", "    - one",
                                                   "    - two"],
           repr(MARKDOWN.issue_list_item(issue)))


def test_quote(tmp: Path) -> None:
    """A problem's quote from the document is apart from its detail."""
    got = TYPST.split_quote('row 2 is short -- "We get 81.2%."')
    expect("quote: the detail and the quote are apart",
           got == ("row 2 is short", '"We get 81.2%."'), repr(got))
    expect("quote: a detail with no quote has none",
           TYPST.split_quote("fig:a is never referenced") == ("fig:a is never referenced", None))


def test_stacked(tmp: Path) -> None:
    """A long subject goes above its detail, so the detail keeps the width of the page."""
    sources = ", ".join(f"appendix/r{i}-acc" for i in range(12))
    long = f"gibbs/co-improve (inferred from {sources})"
    wide = TYPST.issue_group_data(IssueGroup(code="claim-uncertainty", issues=tuple([
        Issue(code="claim-uncertainty", axis=Axis.STRENGTH, subject=subject, detail="0.88")
        for subject in (long, "short/claim")])))
    narrow = TYPST.issue_group_data(IssueGroup(code="number-disagrees", issues=tuple([
        Issue(code="number-disagrees", axis=Axis.INTEGRITY, subject="gibbs/ols-r2",
                  detail="the data gives 0.66")])))
    expect("stacked: a group with a long subject is stacked", wide["stacked"], repr(wide))
    expect("stacked: a group with short subjects is not stacked", not narrow["stacked"],
           repr(narrow))
    root, state = test_core.workdir(tmp)
    markup = test_core.typst_of(test_core.analyse_project(root, state), None)
    data, template = markup.split("\n", 1)
    groups = typst_literal(one_section_of([wide, narrow]))
    patched = f"{data}\n#let data = data + {groups}\n{template}"
    expect("stacked: the markup with a stacked group compiles",
           TYPST.compile_pdf(patched, "fixture").startswith(b"%PDF"))


def test_cards(tmp: Path) -> None:
    """Issues with facts give one card each: the claim's id, then one line for each fact."""
    facts = (("Claim", "A rises."), (INFERRED_FROM, "a, b"), ("Uncertainty", "0.9"))
    issue = Issue(code="uncertain-inference", axis=Axis.STRENGTH, subject="top",
                      detail="uncertainty 0.9", facts=facts, flag_id="F-123abc")
    group = TYPST.issue_group_data(IssueGroup(code="uncertain-inference", issues=tuple([issue])))
    expect("cards: a group whose issues have facts is shown as cards", group["cards"],
           repr(group))
    expect("cards: the claim ids are in a fixed-width font, the other facts are not",
           [f[2] for f in group["issues"][0][3]] == [False, True, False], repr(group))
    plain = TYPST.issue_group_data(IssueGroup(
        code="x", issues=(Issue(code="x", axis=Axis.STRENGTH, subject="s", detail="d"),)))
    expect("cards: a group with no facts is not shown as cards", not plain["cards"])
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    data, template = test_core.typst_of(tree, None).split("\n", 1)
    patched = f"{data}\n#let data = data + {typst_literal(one_section_of([group]))}\n{template}"
    expect("cards: the markup with cards compiles",
           TYPST.compile_pdf(patched, "fixture").startswith(b"%PDF"))
    lines = MARKDOWN.issue_list_item(issue)
    expect("cards: the Markdown gives the flag id and the claim id, then a line for each fact",
           lines == ["- `F-123abc` **`top`**", "  - **Claim:** A rises.",
                     "  - **Inferred from:** `a`, `b`", "  - **Uncertainty:** 0.9"], repr(lines))


def test_table(tmp: Path) -> None:
    """A number the data does not give is an issue of its claim, with one line for each fact."""
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    issues = test_core.issues_of_type(tree, "number-disagrees")
    expect("table: the fixture has a number the data does not give", bool(issues))
    if not issues:
        return
    labels = [label for label, _ in issues[0].facts]
    expect("table: the facts are the paper's number, the data's, the difference, the tolerance",
           labels == ["Paper", "Data", "Difference", "Tolerance"], repr(issues[0]))
    text = test_core.markdown_of(tree)
    expect("table: the Markdown gives the issue under its claim, then a line for each fact",
           ": `disagrees`\n\n" in text
           and f"- `{issues[0].flag_id}` `number-disagrees`, integrity issue (rejecting)\n"
           in text and "  - **Paper:** 0.2\n  - **Data:** 0.075\n" in text, text)
    data = test_core.typst_of(tree, None).split("\n", 1)[0]
    expect("table: the markup gives the facts of the claim's issue",
           '"code": "number-disagrees", "axis": "integrity"' in data
           and '("Paper", "0.2", false, ((false, "0.2"),))' in data, data[:300])
    expect("table: the markup compiles",
           TYPST.compile_pdf(test_core.typst_of(tree, None), "fixture").startswith(b"%PDF"))

    other = Issue(code="other", axis=Axis.INTEGRITY, subject="s", detail="d",
                      facts=(("Paper", "1"),))
    expect("table: a type that is not in TABLE_CODES is not a table",
           not IssueGroup(code="other", issues=(other,)).layout().is_shown_as_table)


def test_shared(tmp: Path) -> None:
    """Issues on one document give its path once; a long fact is a wide column on the left."""
    sentence = "Figure shows the same pattern for bootstrap (100 seeds, n=50, 15 rounds)..."
    issues = [Issue(code="number-not-in-any-table", axis=Axis.PRESENTATION, subject="p.tex",
                        detail=f"{n} is stated", facts=(("Number", n), ("Sentence", sentence)),
                        flag_id=f"F-{n[2:]}")
              for n in ("0.164", "0.408")]
    group = TYPST.issue_group_data(IssueGroup(code="number-not-in-any-table", issues=tuple(issues)))
    expect("shared: the one document is given once",
           group["shared"] == "p.tex" and group["table"], repr(group))
    expect("shared: the sentence is a wide column, the number is not",
           group["wide"] == [False, True], repr(group["wide"]))
    lines = MARKDOWN.table_lines(IssueGroup(code="number-not-in-any-table", issues=tuple(issues)))
    expect("shared: the Markdown gives the document, then a table with no document column",
           lines[:4] == ["In `p.tex`:", "", "| Flag | Number | Sentence |", "|---|--:|---|"]
           and lines[4].startswith("| `F-164` | 0.164 | Figure"), repr(lines))
    one = TYPST.issue_group_data(IssueGroup(code="x", issues=tuple(issues[:1])))
    expect("shared: one issue alone keeps its subject", one["shared"] is None, repr(one))
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    data, template = test_core.typst_of(tree, None).split("\n", 1)
    plain = [Issue(code="orphan-float", axis=Axis.PRESENTATION, subject="p.tex",
                       detail=f"fig:{k} is never referenced") for k in "ab"]
    orphans = TYPST.issue_group_data(IssueGroup(code="orphan-float", issues=tuple(plain)))
    groups = typst_literal(one_section_of([group, orphans]))
    expect("shared: the markup with a shared table and a shared list compiles",
           TYPST.compile_pdf(f"{data}\n#let data = data + {groups}\n{template}",
                               "fixture").startswith(b"%PDF"))


def test_measured(tmp: Path) -> None:
    """A group whose issues all have a measured number has the highest first."""
    issues = [Issue(code="u", axis=Axis.STRENGTH, subject=s, detail="d", facts=(("C", "x"),),
                        measured=m) for s, m in (("low", 0.6), ("high", 0.93))]
    group = TYPST.issue_group_data(IssueGroup(code="u", issues=tuple(issues)))
    expect("measured: the card gives the number", group["issues"][0][4] == "0.6", repr(group))
    grouper = issue_grouping.IssueSectioner(issue_grouping.FlagIdAssigner())
    ordered = grouper.order_by_measured_number(issues)
    expect("measured: the highest number comes first",
           [i.subject for i in ordered] == ["high", "low"], repr(ordered))
    mixed = [*issues, Issue(code="u", axis=Axis.STRENGTH, subject="none", detail="d")]
    expect("measured: a group with an issue that has no number keeps its order",
           grouper.order_by_measured_number(mixed) == mixed)


def not_run_rows(triples: list[tuple[str, str, str]]
                 ) -> tuple[list[tuple[str, str, int]], dict[str, list[tuple[str, str]]]]:
    """The rows of the table of checks that did not run, and the claims listed apart."""
    table = checks_not_run.ChecksNotRunCollector().table_of_checks_not_run(
        [checks_not_run.CheckNotRunOnClaim(check_name=name, claim_id=cid, reason=reason)
         for name, cid, reason in triples])
    return ([(r.check_name, r.reason, r.claim_count) for r in table.rows],
            {name: [(n.claim_id, n.reason) for n in claims]
             for name, claims in table.claims_of_checks_with_many_reasons.items()})


def test_not_run(tmp: Path) -> None:
    """A check with more than MAX_REASONS reasons gets one row; its claims are listed apart."""
    triples = [("caption", f"c{i}", "the claim gives no `model`") for i in range(3)]
    triples += [("claim_uncertainty", f"u{i}", f"u{i}-dep failed its gate") for i in range(5)]
    rows, per_claim = not_run_rows(triples)
    expect("not run: one row for each check, the most claims first",
           rows == [("claim_uncertainty", "5 different reasons; see each claim below", 5),
                    ("caption", "the claim gives no `model`", 3)], repr(rows))
    expect("not run: the claims of the check with many reasons, each with its reason",
           per_claim == {"claim_uncertainty": [(f"u{i}", f"u{i}-dep failed its gate")
                                               for i in range(5)]}, repr(per_claim))
    few = not_run_rows(triples[:5])
    expect("not run: a check with few reasons keeps one row for each reason",
           len(few[0]) == 3 and few[1] == {}, repr(few))


def test_notice_details(tmp: Path) -> None:
    """A row with details comes first under the verdict, and its details are nested items."""
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    waiting = report_header.ReportHeaderRow(
        name="Waiting for you", text="1 review(s).",
        details=((1, "R-1  Was it rerun?"), (2, "confirm  the new file counts")))
    rows = report_header.ReportHeaderBuilder().build_header_rows(tree, (waiting,))
    expect("notice: the given row is first, then the counts of the run",
           rows[0] == waiting and rows[1].name == "Documents checked", repr(rows[:2]))
    text = test_core.markdown_of(tree, None, [waiting])
    expect("notice: the details are nested items in the Markdown",
           "- **Waiting for you:** 1 review(s).\n  - R-1 Was it rerun?\n"
           "    - confirm the new file counts\n" in text, text[:400])


def test_saved(tmp: Path) -> None:
    """`vcheck run` saves the report as Markdown, Typst, PDF and layout data; `report --json`
    gives the markup and the layout data."""
    root, state = test_core.workdir(tmp)
    with contextlib.redirect_stdout(io.StringIO()):
        cli.main(["--state", str(state), "--root", str(root), "run",
                  "--commit", "abcdef1234567890"])
    saved = state / report_store.ReportStore.REPORTS_DIR
    expect("saved: the .md, .typ and .pdf of the commit and of the latest are written",
           all((saved / f"{name}{suffix}").exists() for name in ("latest", "abcdef1234567890")
               for suffix in (".md", ".typ", ".pdf"))
           and (saved / "latest.pdf").read_bytes().startswith(b"%PDF"),
           repr(sorted(p.name for p in saved.iterdir())))
    report = project_status.latest_report(StatePaths(root=root))
    expect("saved: the latest report gives the markup under typst",
           report is not None and report.typst == (saved / "latest.typ").read_text()
           and json.loads(json.dumps(report.to_json()))["typst"].startswith("#let data = ("))
    layout = json.loads((saved / "latest.layout.json").read_text())
    expect("saved: the layout file holds the data block of the markup, and the latest report "
           "gives it under layout",
           (saved / "abcdef1234567890.layout.json").exists()
           and (saved / "latest.typ").read_text().startswith(
               f"#let data = {typst_literal(layout)}\n")
           and report is not None and report.to_json()["layout"] == layout)


def test_compile(tmp: Path) -> None:
    """The markup compiles to a PDF."""
    root, state = test_core.workdir(tmp)
    markup = test_core.typst_of(test_core.analyse_project(root, state), "abcdef1234567890",
                                [report_header.ReportHeaderRow(
                                    name=report_header.METHODS_RAN_IN, text="x")])
    pdf = TYPST.compile_pdf(markup, "fixture")
    expect("compile: the markup gives a PDF", pdf.startswith(b"%PDF"))
    failed = TYPST.render_start_failure(
        verification_report.RunStartFailure(commit="abcdef1234567890",
                                            error="PluginCheckError: no such check"))
    expect("compile: the report of a run that could not start gives a PDF",
           TYPST.compile_pdf(failed, "fixture").startswith(b"%PDF"))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_literal, test_fixture, test_same_detail, test_bullets, test_quote,
                  test_stacked,
                  test_cards, test_table, test_shared, test_measured, test_not_run,
                  test_notice_details, test_saved, test_compile):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    failed = FAILED + test_core.FAILED
    if failed:
        print(f"\nFAILED: {len(failed)} case(s).")
        return 1
    print("\nAll typeset cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
