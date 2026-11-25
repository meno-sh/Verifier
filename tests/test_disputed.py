#!/usr/bin/env python3
"""Assert that disputed flags leave the verdict as it is, and change only what the report shows.

Covers:
  - `without` removes one disputed problem from a check result, and a result with no
    problem left is Passed;
  - a disputed gate flag leaves its claim out, and the second verdict is then PASS WITH
    CAVEATS at best;
  - a dispute whose detail differs from the flag does not apply, and a flag nobody can
    dispute (a claim register entry that cannot be read) stays;
  - the Markdown has two verdict lines only when the two verdicts differ;
  - a disputed flag is struck out with its reason under it, in each layout of the report:
    a table, many issues with the same detail, one subject, cards, and the plain list;
  - the counts say how many issues are disputed, and the markup with disputes compiles;
  - `vcheck run` saves the dispute of each issue and the verdict without the disputed
    flags, and its exit code still follows the verdict with all flags.
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
from fakes import fixed_disputes  # noqa: E402

from core import cli  # noqa: E402
from core.analysis import verdict_policy  # noqa: E402
from core.registry import extensions  # noqa: E402
from core.report_model.axis import Axis  # noqa: E402
from core.report_model.disputed_flags import DisputedFlag, DisputedFlagSet  # noqa: E402
from core.report_model.issues import (  # noqa: E402
    ClaimIssueGroup,
    FlagDispute,
    Issue,
    IssueGroup,
    ReportSection,
    ReportSectionKind,
)
from core.report_output import (  # noqa: E402
    markdown_renderer,
    report_store,
    typst_renderer,
    typst_template,
)
from schema.primitives import Flagged, Passed  # noqa: E402
from schema.verdict import Verdict  # noqa: E402
from utils.text import typst_literal  # noqa: E402

TYPST = typst_renderer.TypstReportRenderer()
MARKDOWN = markdown_renderer.MarkdownReportRenderer()

VERDICT_POLICY = verdict_policy.VerdictPolicy()

FAILED = []
WHY = "The method reads runs/old/; the notes use runs/fixed/."


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def disputed_flag_for(issue: Issue, n: int = 1,
                      detail: str | None = None) -> DisputedFlag:
    """A dispute D-n of the issue's flag, or of a flag with another detail."""
    return DisputedFlag(code=issue.code, subject=issue.subject,
                        detail=issue.detail if detail is None else detail,
                        dispute=FlagDispute(dispute_id=f"D-{n}", reason=WHY))


def issues_of_type(tree: object, code: str) -> list[Issue]:
    """The issues of one type in the tree."""
    return test_core.issues_of_type(tree, code)


def without_disputed_flags(tree: object, flags: list[DisputedFlag]) -> object:
    """A copy of the tree without the disputed flags."""
    return DisputedFlagSet(flags, VERDICT_POLICY).findings_without_disputed_flags(tree)


def second_verdict(tree: object, flags: list[DisputedFlag]) -> Verdict | None:
    """The verdict without the disputed flags, or None when no flag is disputed."""
    return DisputedFlagSet(flags, VERDICT_POLICY).verdict_without_disputed_flags(tree)


def test_check_flag(tmp: Path) -> None:
    """A disputed problem leaves its check result; a result with none left is Passed."""
    root, state = test_core.workdir(tmp, [test_core.claim_entry(c) for c in ("good", "prior")])
    tree = test_core.analyse_project(root, state)
    flags = issues_of_type(tree, "undeclared-difference")
    expect("check: the two claims each have a comparison flag", len(flags) == 2, repr(flags))
    copy = without_disputed_flags(tree, [disputed_flag_for(flags[0])])
    results = [c.checks["comparability"] for c in copy.claims]
    expect("check: the disputed problem is gone, and its result is Passed",
           isinstance(results[0], Passed) and isinstance(results[1], Flagged), repr(results))
    expect("check: the tree itself does not change",
           all(isinstance(c.checks["comparability"], Flagged) for c in tree.claims))
    expect("check: the second verdict is not worse than the first",
           list(Verdict).index(second_verdict(tree, [disputed_flag_for(flags[0])]))
           >= list(Verdict).index(VERDICT_POLICY.verdict_for(tree)))
    expect("check: with no dispute, there is no second verdict",
           second_verdict(tree, []) is None)


def test_gate_flag(tmp: Path) -> None:
    """A disputed gate flag leaves its claim out, so the second verdict is a caveat at best."""
    root, state = test_core.workdir(tmp, [test_core.claim_entry("disagrees")])
    tree = test_core.analyse_project(root, state)
    flag = issues_of_type(tree, "number-disagrees")[0]
    expect("gate: the claim rejects", VERDICT_POLICY.verdict_for(tree) is Verdict.REJECT)
    copy = without_disputed_flags(tree, [disputed_flag_for(flag)])
    expect("gate: the copy has no claim", copy.claims == (), repr(copy.claims))
    expect("gate: the second verdict is PASS WITH CAVEATS",
           second_verdict(tree, [disputed_flag_for(flag)])
           is Verdict.PASS_WITH_CAVEATS)
    other = disputed_flag_for(flag, detail=flag.detail + " (an older number)")
    expect("gate: a dispute with another detail does not apply",
           second_verdict(tree, [other]) is None
           and issues_of_type(tree, "number-disagrees")[0].dispute is None)


def test_not_disputable(tmp: Path) -> None:
    """A claim register entry that cannot be read keeps its flag, even when it is named."""
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    flags = issues_of_type(tree, "claim-unreadable")
    marked = test_core.issues_of_type(tree, "claim-unreadable",
                                      tuple(disputed_flag_for(f) for f in flags))
    expect("not disputable: the unreadable entries are not marked",
           flags and all(i.dispute is None for i in marked), repr(marked))
    expect("not disputable: they give no second verdict",
           second_verdict(tree, [disputed_flag_for(f) for f in flags])
           is None)


def test_markdown(tmp: Path) -> None:
    """Two verdict lines only when the verdicts differ, and struck flags with their reason."""
    root, state = test_core.workdir(tmp, [test_core.claim_entry("disagrees")])
    tree = test_core.analyse_project(root, state)
    flag = issues_of_type(tree, "number-disagrees")[0]
    text = test_core.markdown_of(tree, "a" * 40, None, [disputed_flag_for(flag)])
    lines = text.splitlines()
    expect("markdown: the verdict with all flags is struck out, then the verdict without them",
           lines[0] == f"Verdict: ~~REJECT~~  (commit {'a' * 12})"
           and lines[1] == "Verdict: PASS WITH CAVEATS  (1 disputed flag removed)", lines[:2])
    expect("markdown: the claim's issue and its facts are struck out, with the reason under it",
           f"- ~~`{flag.flag_id}` `number-disagrees`, integrity issue (rejecting)~~" in text
           and "  - ~~**Paper:** 0.2~~" in text
           and f"  - *Disputed because: {WHY}*" in text, text)
    expect("markdown: the counts say how many are disputed",
           "| [Claims Needing Attention](#claims-needing-attention) | 1 (1 disputed) |" in text,
           text)
    plain = test_core.markdown_of(tree, "a" * 40)
    expect("markdown: with no dispute, one verdict line",
           plain.splitlines()[0] == f"Verdict: REJECT  (commit {'a' * 12})"
           and plain.splitlines()[1] == "", plain[:120])
    root, state = test_core.workdir(tmp / "two", [test_core.claim_entry(c)
                                                  for c in ("good", "prior")])
    tree = test_core.analyse_project(root, state)
    flags = issues_of_type(tree, "undeclared-difference")
    text = test_core.markdown_of(tree, None, None, [disputed_flag_for(flags[0])])
    expect("markdown: a disputed flag that leaves the verdict as it is gives one verdict line",
           not text.splitlines()[1].startswith("Verdict:"), text[:200])
    expect("markdown: a plain list item is struck out, with the reason as a nested item",
           f"- ~~`{flags[0].flag_id}` `undeclared-difference`, strength issue (disclosure): "
           in text
           and f"  - *Disputed because: {WHY}*" in text
           and f"- `{flags[1].flag_id}` `undeclared-difference`, strength issue (disclosure): "
           in text,
           text)


def test_layouts(tmp: Path) -> None:
    """Many issues with one detail, one subject, and cards: each gives the struck form."""
    same = [Issue(code="s", axis=Axis.STRENGTH, subject=f"c{n}", detail="same", flag_id=f"F-{n}")
            for n in range(5)]
    same[2] = Issue(code="s", axis=Axis.STRENGTH, subject="c2", detail="same", flag_id="F-2",
                        dispute=FlagDispute(dispute_id="D-1", reason=WHY))
    one = [Issue(code="o", axis=Axis.PRESENTATION, subject="p.tex", detail=f"d{n}",
                     flag_id=f"F-o{n}",
                     dispute=FlagDispute(dispute_id="D-1", reason=WHY) if n else None)
           for n in range(2)]
    card = Issue(code="u", axis=Axis.STRENGTH, subject="top", detail="u", flag_id="F-c",
                     facts=(("Claim", "A rises."),),
                     dispute=FlagDispute(dispute_id="D-1", reason=WHY))
    root, state = test_core.workdir(tmp)
    tree = test_core.analyse_project(root, state)
    data, template = test_core.typst_of(tree, None).split("\n", 1)
    claim = ClaimIssueGroup(claim_id="top", statement="A rises.", register_place=1,
                            register_size=2, inferred_from_places=(2,), issues=(card,))
    sections = typst_literal({"sections": TYPST.sections_data([
        ReportSection(kind=ReportSectionKind.SETUP, groups=tuple(
            IssueGroup(code=code, issues=tuple(issues))
            for code, issues in (("s", same), ("o", one), ("u", [card])))),
        ReportSection(kind=ReportSectionKind.CLAIMS, groups=(claim,))])})
    expect("layouts: the markup with disputes in each layout, and in a claim, compiles",
           TYPST.compile_pdf(f"{data}\n#let data = data + {sections}\n{template}",
                               "fixture").startswith(b"%PDF"))
    issue = TYPST.claim_group_data(claim)["issues"][0]
    expect("layouts: an issue of a claim gives its reason, and leaves out the claim's facts",
           issue["why"] == WHY and issue["facts"] == [] and issue["code"] == "u", repr(issue))
    group = TYPST.issue_group_data(IssueGroup(code="s", issues=tuple(same)))
    expect("layouts: the same-detail group gives the reason of the disputed subject only",
           group["dispute-reasons"] == [None, None, WHY, None, None]
           and group["count-words"] == "5 issues (1 disputed)", repr(group))
    expect("layouts: the PDF sentence counts all issues, with no word on the disputes",
           group["issue-count"] == 5 and "All #g.issue-count say:" in typst_template.TEMPLATE)
    expect("layouts: a card is struck out, and the reason follows its facts",
           MARKDOWN.issue_list_item(card) == ["- ~~`F-c` **`top`**~~",
                                              "  - ~~**Claim:** A rises.~~",
                                              f"  - *Disputed because: {WHY}*"],
           repr(MARKDOWN.issue_list_item(card)))


def test_saved(tmp: Path) -> None:
    """`vcheck run` saves each dispute and the second verdict; the exit code does not change."""
    root, state = test_core.workdir(tmp, [test_core.claim_entry("disagrees")])
    tree = test_core.analyse_project(root, state)
    flag = issues_of_type(tree, "number-disagrees")[0]
    (state / fixed_disputes.FILE).write_text(json.dumps([{
        "code": flag.code, "subject": flag.subject, "detail": flag.detail, "dispute_id": "D-1",
        "reason": WHY}]))
    extensions.ExtensionSet.save_names(state, (fixed_disputes.NAME,))
    with contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(["--state", str(state), "--root", str(root), "run", "--commit",
                         "c" * 40])
    path = state / report_store.ReportStore.REPORTS_DIR / f"{'c' * 40}.json"
    saved = json.loads(path.read_text())
    expect("saved: the verdict and the exit code are those with all flags",
           saved["verdict"] == "REJECT" and code == 2, f"{saved['verdict']} {code}")
    expect("saved: the verdict without the disputed flags",
           saved["verdict_without_disputed"] == "PASS WITH CAVEATS",
           saved["verdict_without_disputed"])
    item = next(i for i in saved["issues"] if i["code"] == "number-disagrees")
    expect("saved: the issue names its dispute and the reason",
           item["disputed"] == {"dispute_id": "D-1", "reason": WHY}, repr(item))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_check_flag, test_gate_flag, test_not_disputable, test_markdown,
                  test_layouts, test_saved):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll disputed-flag cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
