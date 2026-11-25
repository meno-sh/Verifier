#!/usr/bin/env python3
"""Assert that the claim-register core gives typed findings that agree with the pipeline.

Covers:
  - the fixture working directory: each claim's gate result matches the status the
    static pipeline's reference gives it (tests/test_pipeline.py), and the verdict is
    MISSING INPUTS (2 unreadable entries);
  - a computational method and a claim check saved in the state directory register
    like the built-in ones, run, and reach the verdict;
  - a plug-in check that crashes, or reports a kind it did not declare, is recorded as
    Skipped and never stops the run; one that claims a built-in kind is refused;
  - a computational method that crashes, or returns something other than a Computed, rejects
    its claim, and so does a setup the claim states that the method does not report;
  - a pin (utils/claim_files.py) records checksums, refuses to re-pin, and a later
    change to the pinned file rejects the claim;
  - a claim traced to the note it was written in (its `origin`) is a caveat, not a pass;
  - a file matching only a proposed checksum is recomputed and counted as a caveat;
  - the report gives the setup issues, then the claims in register order, then the caveats;
  - the saved JSON lists each issue with its flag id, and its report id skips the commit;
  - a flag id depends only on the issue, and ids that would collide get longer;
  - a superseded claim is listed and not checked, and its successor must exist;
  - a computational method returning a badly typed Computed fails only its claim, method files
    may define dataclasses, and setup numbers compare as numbers.

Returns 1 if any case fails, so it can gate a commit.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli  # noqa: E402
from core.analysis import verdict_policy  # noqa: E402
from core.gate import artifact_pins  # noqa: E402
from core.gate import provenance as provenance_check  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import plugin_checks  # noqa: E402
from core.report_model import (  # noqa: E402
    issue_collector,
    issue_grouping,
    issues,
    report_builder,
    report_header,
)
from core.report_model.axis import Axis  # noqa: E402
from core.report_output import markdown_renderer, report_store, saved_report, typst_renderer  # noqa: E402
from schema import inputs, provenance, recompute, verdict  # noqa: E402
from schema.primitives import CheckCrashed, Flagged, NotChecked, Passed, Skipped  # noqa: E402
from utils import checksums, claim_files  # noqa: E402
from utils.text import markdown_code_span, markdown_table_cell  # noqa: E402

VERDICT_POLICY = verdict_policy.VerdictPolicy()

HERE = Path(__file__).resolve().parent
METHOD_ENV = watched_project.WatchedProject.METHOD_ENV
OPTIONS_FILE = watched_project.WatchedProject.OPTIONS_FILE
REGISTER_FILE = watched_project.WatchedProject.REGISTER_FILE
WORKDIR = HERE / "fixtures" / "workdir"

# The gate result each fixture claim gets, as in the static pipeline's reference.
GATE = {
    "good": (provenance.Intact, recompute.Matches),
    "prior": (provenance.Intact, recompute.Matches),
    "disagrees": (provenance.Intact, recompute.Disagrees),
    "missing": (provenance.Missing, Skipped),
    "tampered": (provenance.Changed, Skipped),
    "setup-mismatch": (provenance.Intact, recompute.SetupDisagrees),
    "traced": (provenance.Traced, Skipped),
    "anchor-gone": (provenance.AnchorMissing, Skipped),
    "no-source": (provenance.NoSource, Skipped),
}

CHECK_FILE = '''
from core.registry.plugin_checks import PluginCheck
from schema.inputs import FieldType
from schema.primitives import Flagged, Passed, Problem, ProblemKind, Severity

FEW_EPISODES = ProblemKind(code="few-episodes", severity=Severity.STRENGTH)

class EpisodesCheck(PluginCheck):
    name = "episodes"
    title = "Enough episodes"
    why = "Flags a result scored on fewer than 5 episodes."
    problem_kinds = (FEW_EPISODES,)
    fields = {"seeds": FieldType.INTEGER}
    needs = ("seeds",)
    needs_recompute = True

    def check(self, claim, recomputed, context):
        if recomputed.n < 5:
            return Flagged(problems=(Problem(kind=FEW_EPISODES,
                                             detail=f"only {recomputed.n} episodes"),))
        return Passed()
'''

METHOD = '''
import glob, json
from checks.interface import Computed, method

@method("row_count")
def row_count(path):
    rows = [r for p in glob.glob(path) for r in json.load(open(p))["records"]]
    return Computed(value=len(rows) / 10, n=len(rows))
'''

FAILED = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def markdown_of(tree: object, commit: str | None = None, notices: list | None = None,
                disputes: list | None = None) -> str:
    """The Markdown report of a tree, as `vcheck run` writes it."""
    report = report_builder.VerificationReportBuilder().build_report(
        tree, commit, notices or (), disputes or ())
    return markdown_renderer.MarkdownReportRenderer().render_report(report)


def typst_of(tree: object, commit: str | None = None, notices: list | None = None,
             disputes: list | None = None) -> str:
    """The Typst markup of the report of a tree, as `vcheck run` writes it."""
    report = report_builder.VerificationReportBuilder().build_report(
        tree, commit, notices or (), disputes or ())
    return typst_renderer.TypstReportRenderer().render_report(report)


def report_sections_of(tree: object, disputes: tuple = ()) -> tuple:
    """The sections of the report of a tree, in report order."""
    located = issue_collector.IssueCollector().collect_issues(tree)
    return issue_grouping.IssueSectioner(issue_grouping.FlagIdAssigner()).sort_into_sections(
        located, [c.claim for c in tree.claims], disputes)


def type_groups_of(tree: object, disputes: tuple = ()) -> list:
    """The groups of one issue type (the setup and caveats sections), in report order."""
    return [g for s in report_sections_of(tree, disputes) for g in s.groups
            if isinstance(g, issues.IssueGroup)]


def issues_of_type(tree: object, code: str, disputes: tuple = ()) -> list:
    """The issues of one type in the report of a tree, in report order, or none."""
    return [i for s in report_sections_of(tree, disputes) for i in s.issues if i.code == code]


def analyse_project(root: Path, state: Path) -> object:
    """The findings of the watched project in `root`, with the extensions of the environment."""
    return watched_project.WatchedProject.load(root, state).analyse()


def workdir(tmp: Path, claims: list | None = None) -> tuple[Path, Path]:
    """A copy of the fixture working directory with its claims as the saved claim register."""
    root = tmp / "w"
    shutil.copytree(WORKDIR, root)
    state = root / ".verifier"
    state.mkdir()
    raw = json.loads((root / "claims.json").read_text())
    if claims is not None:
        raw["claims"] = claims
    (state / REGISTER_FILE).write_text(json.dumps(raw))
    shutil.copytree(root / "methods", state / "methods")
    turn_off_paid_model_checks(state)
    return root, state


def turn_off_paid_model_checks(state: Path) -> None:
    """Turn off claim_uncertainty, which calls models, in the project's run options."""
    (state / OPTIONS_FILE).write_text(json.dumps({"want_claim_uncertainty": False}))


def claim_entry(cid: str) -> dict:
    """One claim from the fixture register, by id."""
    raw = json.loads((WORKDIR / "claims.json").read_text())
    return next(c for c in raw["claims"] if c["id"] == cid)


def test_fixture(tmp: Path) -> None:
    """Every gate result agrees with the static pipeline's reference for the same claim."""
    root, state = workdir(tmp)
    tree = analyse_project(root, state)
    got = {c.claim.id: (type(c.provenance), type(c.recompute)) for c in tree.claims}
    for cid, want in GATE.items():
        expect(f"fixture: {cid} gives {want[0].__name__}, {want[1].__name__}",
               got.get(cid) == want, f"got {got.get(cid)}")
    expect("fixture: the 2 malformed entries are unreadable",
           sorted(u.id for u in tree.unreadable_claims) == ["bad-reason", "unknown-method"])
    expect("fixture: the unreadable entries make the verdict MISSING INPUTS",
           VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.MISSING_INPUTS)
    good = next(c for c in tree.claims if c.claim.id == "good")
    expect("fixture: good passes seeds and caption, and its comparison is flagged",
           isinstance(good.checks["seeds"], Passed) and isinstance(good.checks["caption"], Passed)
           and isinstance(good.checks["comparability"], Flagged))


def test_report_layout(tmp: Path) -> None:
    """The report gives the setup issues, then the claims in register order, then the caveats."""
    root, state = workdir(tmp)
    text = markdown_of(analyse_project(root, state), "abcdef1234567890")
    expect("report: the first line is the verdict, which the daemon reads",
           text.startswith("Verdict: MISSING INPUTS  (commit abcdef123456)\n"), text[:60])
    lines = text.splitlines()
    headings = [line.removeprefix("## ") for line in lines if line.startswith("## ")]
    expect("report: the dispute help, the overview, then the sections: setup, claims, caveats",
           headings[:5] == ["How to Dispute False Issues", "Overview of Issues",
                            "Setup Issues Needing Attention", "Claims Needing Attention",
                            "Caveats"], repr(headings))
    expect("report: the overview gives each section with a link, and each severity",
           "| [Setup Issues Needing Attention](#setup-issues-needing-attention) | 2 |" in text
           and "| [Claims Needing Attention](#claims-needing-attention) | 13 |" in text
           and "| [Caveats](#caveats) | 1 |" in text
           and "| integrity issue (rejecting) | 5 |" in text
           and "| missing input (cannot pass) | 2 |" in text, text)
    setup, claims, caveats = (text.index(f"## {h}") for h in headings[2:5])
    expect("report: an unreadable register entry is a setup issue, by type",
           setup < text.index("### `claim-unreadable`") < claims, text)
    expect("report: a claim with no source is a caveat, by type",
           caveats < text.index("### `no-source`"), text)
    register = [e["id"] for e in json.loads((state / "claim_register.json").read_text())["claims"]]
    claim_lines = lines[lines.index(f"## {headings[3]}"):lines.index(f"## {headings[4]}")]
    shown = [line.split("`")[1] for line in claim_lines if line.startswith("### Claim ")]
    places = [int(line.split()[2]) for line in claim_lines if line.startswith("### Claim ")]
    readable = [c for c in register if c not in ("bad-reason", "unknown-method")]
    expect("report: the claims come in the order of the claim register, with their places",
           shown and shown == [c for c in register if c in shown]
           and places == [readable.index(c) + 1 for c in shown], repr((shown, places)))
    start = next(n for n, line in enumerate(lines) if line.endswith(": `prior`"))
    prior = lines[start:]
    prior = prior[:next(n for n, line in enumerate(prior[1:], 1) if line.startswith("##"))]
    rank_of_effect = {a.effect: a.rank for a in Axis}
    ranks = [rank_of_effect[line.split(", ", 1)[1].split(":", 1)[0]]
             for line in prior if line.startswith("- `F-")]
    expect("report: a claim gives its statement, then its issues, most severe first",
           "*The untrained model has a Brier score of 0.205.*" in prior
           and len(ranks) == 5 and ranks == sorted(ranks) and len(set(ranks)) > 1, repr(prior))
    expect("report: each issue of a claim names its type and its severity",
           any(line.startswith("- `F-") and "`undeclared-difference`, strength issue "
               "(disclosure): compared against good" in line for line in prior), repr(prior))
    loop_b = readable.index("loop-b") + 1
    expect("report: a claim inferred from others gives their places under its statement",
           f": `loop-a`\n\n*This claim rests on loop-b.*\n\nInferred from claim {loop_b}.\n"
           in text, text)
    expect("report: the checks that did not run are a table in the caveats section",
           text.index("## Caveats") < text.index("### Checks that did not run")
           and "| `seeds` | no `seeds` given | 6 |" in text, text)
    many = [{**claim_entry("no-source"), "id": f"plain-{i}"} for i in range(5)]
    root, state = workdir(tmp / "many", [claim_entry("prior"), *many])
    text = markdown_of(analyse_project(root, state))
    expect("report: 5 issues that say the same thing give the sentence once",
           "### `no-source`\n\n5 issues, caveat (pass with caveats)\n\nAll 5 say: no source "
           "declared\n\n`F-" in text
           and " `plain-0 (value 0.5)`, `F-" in text and " `plain-1 (value 0.5)`" in text, text)


def test_saved_report(tmp: Path) -> None:
    """The saved JSON lists each issue with its flag id, and an id that skips the commit."""
    root, state = workdir(tmp)
    reports = []
    for commit in ("a" * 40, "b" * 40):
        with open(os.devnull, "w") as out:
            sys.stdout, saved = out, sys.stdout
            try:
                cli.main(["--state", str(state), "--root", str(root), "run", "--commit", commit])
            finally:
                sys.stdout = saved
        saved = state / report_store.ReportStore.REPORTS_DIR / f"{commit}.json"
        reports.append(json.loads(saved.read_text()))
    first, second = reports
    issues = first["issues"]
    expect("saved: the report has the issues, no second verdict yet, and an id",
           issues and first["verdict_without_disputed"] is None
           and len(first["report_id"]) == saved_report.SavedReport.REPORT_ID_LENGTH,
           repr(first)[:300])
    expect("saved: each issue has its flag id, impact and no dispute",
           all(i["id"].startswith(issue_grouping.FlagIdAssigner.FLAG_ID_PREFIX)
               and i["impact"] == Axis(i["axis"]).impact(i["disputable"])
               and i["disputed"] is None for i in issues), repr(issues[:2]))
    text = (state / report_store.ReportStore.REPORTS_DIR / f"{'a' * 40}.md").read_text()
    places = [text.find(f"`{i['id']}`") for i in issues]
    expect("saved: the Markdown shows each flag id, in the order of the issues",
           -1 not in places and places == sorted(places), repr(places))
    expect("saved: the same issues at another commit give the same ids and report id",
           second["report_id"] == first["report_id"]
           and [i["id"] for i in second["issues"]] == [i["id"] for i in issues])
    notices = [report_header.ReportHeaderRow(name=report_header.METHODS_RAN_IN,
                                             text="the standard library only (no uv.lock)")]
    saved = saved_report.SavedReport.from_json(first)

    def report_id(issue_items: list, run_error: str | None,
                  lines: list[report_header.ReportHeaderRow]) -> str:
        return saved_report.SavedReport.compute_report_id(
            saved.verdict, saved.verdict_without_disputed_flags,
            [saved_report.SavedIssue.from_json(i) for i in issue_items], run_error, lines)
    expect("saved: the report id is computed from what a reader sees",
           report_id(issues, None, notices) == first["report_id"])
    changed = report_id(issues[1:], None, notices)
    expect("saved: other issues give another report id", changed != first["report_id"])
    errors = [report_id(issues, e, notices) for e in ("PluginCheckError: x", "OptionsError: y")]
    other = report_id(issues, None, [*notices, report_header.ReportHeaderRow(
        name="Waiting for you", text="R-1")])
    expect("saved: another start error or another notice gives another report id",
           errors[0] != errors[1] and other != first["report_id"], repr(errors))


def test_saved_report_forms(tmp: Path) -> None:
    """A saved report reads with exactly its keys, writes back the same, and refuses others."""
    old = {"commit": "a", "verdict": "PASS", "methods_ran_in": None, "run_error": None,
           "findings": {"papers": {}, "claims": [], "unreadable_claims": []}}
    current = {**old, "verdict_without_disputed": None, "issues": [], "report_id": "0" * 12}
    read = saved_report.SavedReport.from_json(current)
    expect("saved forms: a report with every key reads, and is written back the same",
           read.to_json() == current, repr(read.to_json()))
    for missing in ((), ("run_error",), ("run_error", "methods_ran_in")):
        older = {k: v for k, v in old.items() if k not in missing}
        try:
            saved_report.SavedReport.from_json(older)
            expect(f"saved forms: an older report (no issues, no {missing}) is refused",
                   False, "no error")
        except saved_report.SavedReportError as e:
            expect(f"saved forms: an older report (no issues, no {missing}) is refused, and "
                   "the error says to run the check again", "run the check again" in str(e),
                   str(e))
    for name, bad in (("a missing key", {k: v for k, v in current.items() if k != "verdict"}),
                      ("an unknown verdict", {**current, "verdict": "MAYBE"}),
                      ("an extra key", {**current, "notices": []})):
        try:
            saved_report.SavedReport.from_json(bad)
            expect(f"saved forms: {name} is refused", False, "no error")
        except saved_report.SavedReportError:
            expect(f"saved forms: {name} is refused", True)


def test_flag_ids(tmp: Path) -> None:
    """A flag id comes from the code, subject and detail; ids that would collide get longer."""
    def issue(n: int) -> issues.Issue:
        return issues.Issue(code="c", axis=Axis.STRENGTH, subject=f"s{n}", detail="d")
    ids_of = issue_grouping.FlagIdAssigner().assign_flag_ids
    assigner = issue_grouping.FlagIdAssigner
    expect("flag ids: the same issue gives the same id",
           ids_of([issue(1)]) == ids_of([issue(1), issue(2)])[:1])
    expect("flag ids: an id has the prefix and 6 hex characters",
           len(ids_of([issue(1)])[0]) == len(assigner.FLAG_ID_PREFIX) + assigner.FLAG_ID_LENGTH)
    expect("flag ids: two issues that are the same get the same id",
           len(set(ids_of([issue(1), issue(1)]))) == 1)

    class OneCharacterIds(issue_grouping.FlagIdAssigner):
        FLAG_ID_LENGTH = 1                    # 17 issues share some first hex character

    ids = OneCharacterIds().assign_flag_ids([issue(n) for n in range(17)])
    long = len(assigner.FLAG_ID_PREFIX) + assigner.FLAG_ID_LENGTH_ON_COLLISION
    expect("flag ids: ids that would collide get the long length, and all ids differ",
           len(set(ids)) == 17 and any(len(i) == long for i in ids), repr(ids))


def test_markdown_escapes(tmp: Path) -> None:
    """Backticks, pipes and new lines cannot break a code span, a table row or a list item."""
    expect("markdown: a code span holds backticks",
           markdown_code_span("a`b") == "``a`b``" and markdown_code_span("`x") == "`` `x ``")
    expect("markdown: a new line in a code span becomes a space",
           markdown_code_span("a\nb") == "`a b`")
    expect("markdown: a pipe in a cell is escaped", markdown_table_cell("a|b\nc") == "a\\|b c")


def test_saved(tmp: Path) -> None:
    """A method and a plug-in check run, and the check's problem reaches the verdict."""
    good = {**claim_entry("good"), "compare_to": None, "varies": []}
    counted = {"id": "counted", "value": 0.5,
               "source": {"kind": "artifact", "path": "runs/main/results.json",
                          "method": "row_count"}}
    root, state = workdir(tmp, [good, counted])
    (state / "checks").mkdir()
    (state / "checks" / "episodes.py").write_text(CHECK_FILE)
    (state / "methods" / "row_count.py").write_text(METHOD)
    tree = analyse_project(root, state)
    by_id = {c.claim.id: c for c in tree.claims}
    expect("computational method: the claim parses and recomputes",
           isinstance(by_id["counted"].recompute, recompute.Matches),
           repr(by_id["counted"].recompute))
    saved = by_id["good"].checks.get("plugin_check:episodes")
    expect("plug-in check: its result sits with the built-in ones, under plugin_check:episodes",
           list(by_id["good"].checks)[-1] == "plugin_check:episodes"
           and isinstance(by_id["counted"].checks["plugin_check:episodes"], NotChecked))
    expect("plug-in check: its own strength kind gives PASS WITH DISCLOSURE",
           isinstance(saved, Flagged) and saved.problems[0].kind.code == "few-episodes"
           and VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.PASS_WITH_DISCLOSURE,
           f"{saved!r} {VERDICT_POLICY.verdict_for(tree).value}")

    (state / "checks" / "episodes.py").write_text(CHECK_FILE.replace("recomputed.n < 5", "1 / 0"))
    by_id = {c.claim.id: c for c in analyse_project(root, state).claims}
    saved = by_id["good"].checks["plugin_check:episodes"]
    expect("plug-in check: a crash is CheckCrashed with the error, and the run continues",
           isinstance(saved, CheckCrashed) and "ZeroDivisionError" in saved.because,
           repr(saved))
    with contextlib.redirect_stdout(io.StringIO()):
        code = cli.main(["--state", str(state), "--root", str(root), "test-check",
                         "plugin_check:episodes", "--claim", "good"])
    expect("vcheck test-check gives exit code 1 for a check that crashed", code == 1, str(code))

    (state / "checks" / "episodes.py").write_text(
        CHECK_FILE.replace("kind=FEW_EPISODES,", "kind=ProblemKind(code='other', "
                                          "severity=Severity.STRENGTH),"))
    by_id = {c.claim.id: c for c in analyse_project(root, state).claims}
    saved = by_id["good"].checks["plugin_check:episodes"]
    expect("plug-in check: a kind it did not declare is refused and recorded as Skipped",
           isinstance(saved, Skipped) and "undeclared" in saved.because, repr(saved))

    (state / "checks" / "episodes.py").write_text(
        CHECK_FILE.replace('code="few-episodes"', 'code="too-few-seeds"'))
    try:
        analyse_project(root, state)
        expect("plug-in check: claiming a built-in check's kind stops the run", False, "ran")
    except plugin_checks.PluginCheckError as e:
        expect("plug-in check: claiming a built-in check's kind stops the run",
               "too-few-seeds" in str(e), str(e))
    (state / "checks" / "episodes.py").write_text(
        CHECK_FILE.replace('name = "episodes"', ''))
    try:
        analyse_project(root, state)
        expect("plug-in check: a check with no name stops the run with a clear error", False,
               "ran")
    except plugin_checks.PluginCheckError as e:
        expect("plug-in check: a check with no name stops the run with a clear error",
               "has no `name`" in str(e), str(e))
    (state / "checks" / "episodes.py").write_text(CHECK_FILE)

    (state / "methods" / "row_count.py").write_text(METHOD.replace("len(rows) / 10", "rows['x']"))
    tree = analyse_project(root, state)
    counted_rc = {c.claim.id: c for c in tree.claims}["counted"].recompute
    expect("computational method: a crash rejects the claim",
           isinstance(counted_rc, recompute.MethodFailed)
           and VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.REJECT, repr(counted_rc))

    (state / "methods" / "row_count.py").write_text(
        METHOD.replace("return Computed(value=len(rows) / 10, n=len(rows))",
                       "return len(rows) / 10, len(rows)"))
    counted_rc = {c.claim.id: c for c in analyse_project(root, state).claims}["counted"].recompute
    expect("computational method: returning a tuple instead of a Computed rejects the claim",
           isinstance(counted_rc, recompute.MethodFailed) and "Computed" in counted_rc.error,
           repr(counted_rc))

    (state / "methods" / "row_count.py").write_text(METHOD)
    raw = json.loads((state / REGISTER_FILE).read_text())
    raw["claims"][1]["setup"] = {"model": "Qwen3-8B"}
    (state / REGISTER_FILE).write_text(json.dumps(raw))
    counted_rc = {c.claim.id: c for c in analyse_project(root, state).claims}["counted"].recompute
    expect("computational method: a setup the claim states but the method does not report "
           "rejects it",
           isinstance(counted_rc, recompute.SetupUnrecorded) and counted_rc.field is None,
           repr(counted_rc))


def test_pin(tmp: Path) -> None:
    """Pinning records checksums; a later change to the file rejects the claim."""
    entry = claim_entry("good")
    entry = {**entry, "compare_to": None, "varies": [],
             "source": {k: v for k, v in entry["source"].items() if k != "sha256"}}
    root, state = workdir(tmp, [entry])
    path = state / REGISTER_FILE
    raw = json.loads(path.read_text())
    claim_files.pin(raw["claims"][0], root)
    path.write_text(json.dumps(raw))
    expect("pin: records checksums", list(raw["claims"][0]["source"]["sha256"]) == ["results.json"])
    try:
        claim_files.pin(raw["claims"][0], root)
        refused = False
    except ValueError:
        refused = True
    expect("pin: refuses to re-pin an already pinned claim", refused)
    tree = analyse_project(root, state)
    expect("pin: the pinned claim is intact",
           isinstance(tree.claims[0].provenance, provenance.Intact))
    results = root / "runs" / "main" / "results.json"
    results.write_text(results.read_text().replace("0.9", "0.8", 1))
    tree = analyse_project(root, state)
    expect("pin: a later change to the file rejects the claim",
           isinstance(tree.claims[0].provenance, provenance.Changed)
           and VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.REJECT,
           repr(tree.claims[0].provenance))


def test_pins_of_files_with_one_name(tmp: Path) -> None:
    """Files with the same name in different folders each get their own pin."""
    for folder, text in (("a", "1"), ("b", "2"), ("a-b", "3")):
        (tmp / "runs" / folder).mkdir(parents=True)
        (tmp / "runs" / folder / "metrics.json").write_text(text)
    entry = {"id": "c", "source": {"kind": "artifact", "path": "runs/*/metrics.json"}}
    claim_files.pin(entry, tmp)
    expect("same name: each file has a pin, keyed by its path from the folder before the "
           "wildcard",
           sorted(entry["source"]["sha256"]) == ["a-b/metrics.json", "a/metrics.json",
                                                 "b/metrics.json"],
           repr(entry["source"]["sha256"]))
    source = inputs.ArtifactSource(path=str(tmp / "runs" / "*" / "metrics.json"), method="m",
                                   sha256=entry["source"]["sha256"])
    pins = artifact_pins.ArtifactPins()
    expect("same name: the files are intact right after the pin",
           pins.compare_files_with_pins(source) == (), repr(pins.compare_files_with_pins(source)))
    (tmp / "runs" / "a" / "metrics.json").write_text("changed")
    found = pins.compare_files_with_pins(source)
    expect("same name: a change to the first file of the same name is found",
           isinstance(found, provenance.Changed) and found.file == "a/metrics.json", repr(found))
    expect("same name: a key of a file name alone is the file name",
           claim_files.pin_key_of_file("data/neg_v*.json", "data/neg_v1.json") == "neg_v1.json")


def test_pinned_folder(tmp: Path) -> None:
    """A folder with the name of a pinned file is not a pinned file: the pinned file is Missing."""
    runs = tmp / "runs"
    (runs / "results.json").mkdir(parents=True)
    (runs / "other.json").write_text("{}")
    source = inputs.ArtifactSource(path=str(runs / "*.json"), method="m",
                                   sha256={"results.json": "0" * 64,
                                           "other.json": checksums.sha256_of_file(
                                               str(runs / "other.json"))})
    pins = artifact_pins.ArtifactPins()
    expect("folder: a folder named in the pins is not a pinned file",
           set(pins.pinned_files(source)) == {"other.json"}, repr(pins.pinned_files(source)))
    claim = inputs.Claim(id="c", value=0.1, source=source)
    found = provenance_check.ProvenanceChecker(pins).check_claim_source(claim)
    expect("folder: a pinned file that is now a folder is Missing, with no crash",
           isinstance(found, provenance.Missing) and found.path.endswith("results.json"),
           repr(found))
    (runs / "results.json").rmdir()
    found = provenance_check.ProvenanceChecker(pins).check_claim_source(claim)
    expect("a pinned file that is gone is Missing, also when other files still match",
           isinstance(found, provenance.Missing) and found.path.endswith("results.json"),
           repr(found))
    (runs / "only").mkdir()
    folder_only = inputs.Claim(id="f", value=0.1, source=inputs.ArtifactSource(
        path=str(runs / "only"), method="m"))
    found = provenance_check.ProvenanceChecker(pins).check_claim_source(folder_only)
    expect("a path that matches only a folder is Missing, as a folder cannot be pinned",
           isinstance(found, provenance.Missing), repr(found))


def test_self_trace(tmp: Path) -> None:
    """A claim traced to the note it was written in is a caveat, not a pass."""
    root, state = workdir(tmp, [{**claim_entry("traced"), "origin": "notes/results.md"}])
    tree = analyse_project(root, state)
    expect("self-trace: a trace to another file is Traced",
           isinstance(tree.claims[0].provenance, provenance.Traced))
    root, state = workdir(tmp / "again", [{**claim_entry("traced"), "origin": "logs/run.md"}])
    tree = analyse_project(root, state)
    expect("self-trace: a trace to the claim's own note is SelfTraced, a caveat",
           isinstance(tree.claims[0].provenance, provenance.SelfTraced)
           and VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.PASS_WITH_CAVEATS,
           f"{tree.claims[0].provenance!r} {VERDICT_POLICY.verdict_for(tree).value}")


def test_proposed_pin(tmp: Path) -> None:
    """A file matching only its proposed checksum is recomputed, and counted as a caveat."""
    entry = {**claim_entry("good"), "compare_to": None, "varies": []}
    root, state = workdir(tmp, [entry])
    results = root / "runs" / "main" / "results.json"
    results.write_text(results.read_text().replace('"base", "post": 0.5', '"base", "post": 0.6'))
    raw = json.loads((state / REGISTER_FILE).read_text())
    raw["claims"][0]["source"]["proposed_sha256"] = {
        "results.json": checksums.sha256_of_file(results)}
    (state / REGISTER_FILE).write_text(json.dumps(raw))
    tree = analyse_project(root, state)
    c = tree.claims[0]
    expect("proposed pin: the claim is provisionally intact and still recomputed",
           isinstance(c.provenance, provenance.ProvisionallyIntact)
           and isinstance(c.recompute, recompute.Matches), f"{c.provenance!r} {c.recompute!r}")
    expect("proposed pin: the verdict counts it as a caveat",
           VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.PASS_WITH_CAVEATS,
           VERDICT_POLICY.verdict_for(tree).value)


def test_superseded(tmp: Path) -> None:
    """A claim replaced by a later one is not checked; a dangling replacement is unreadable."""
    old = {**claim_entry("disagrees"), "superseded_by": "fixed"}
    new = {**claim_entry("disagrees"), "id": "fixed", "value": 0.075, "tol": 0.001}
    root, state = workdir(tmp, [old, new])
    tree = analyse_project(root, state)
    expect("superseded: the old claim is listed, not checked, and the verdict follows the new one",
           [c.id for c in tree.superseded] == ["disagrees"]
           and [c.claim.id for c in tree.claims] == ["fixed"]
           and VERDICT_POLICY.verdict_for(tree) is not verdict.Verdict.REJECT
           and "**Superseded, not checked:** 1 (disagrees -> fixed)" in markdown_of(tree),
           f"{VERDICT_POLICY.verdict_for(tree).value} {markdown_of(tree)}")
    root, state = workdir(tmp / "again", [{**old, "superseded_by": "nowhere"}])
    tree = analyse_project(root, state)
    expect("superseded: a claim superseded by a missing claim is unreadable, so MISSING INPUTS",
           [u.id for u in tree.unreadable_claims] == ["disagrees"]
           and VERDICT_POLICY.verdict_for(tree) is verdict.Verdict.MISSING_INPUTS,
           repr(tree.unreadable_claims))


def test_plugin_edges(tmp: Path) -> None:
    """Bad Computed values fail one claim; method files may use dataclasses; no state is fine."""
    entry = {"id": "c", "value": 0.5, "setup": {"lr": 1.0},
             "source": {"kind": "artifact", "path": "runs/main/results.json", "method": "m"}}
    root, state = workdir(tmp, [entry])
    for body, check in [
            ('Computed(value="0.5", n=1)', lambda rc: isinstance(rc, recompute.MethodFailed)),
            ('Computed(value=0.5, n=1, setup=[1])',
             lambda rc: isinstance(rc, recompute.MethodFailed)),
            ('Computed(value=0.5, n=1, setup={"lr": 1})',
             lambda rc: isinstance(rc, recompute.Matches))]:
        (state / "methods" / "m.py").write_text(
            "from __future__ import annotations\nimport dataclasses\n"
            "from checks.interface import Computed, method\n\n"
            "@dataclasses.dataclass\nclass Note:\n    text: str\n\n"
            f"@method('m')\ndef m(path):\n    return {body}\n")
        rc = analyse_project(root, state).claims[0].recompute
        expect(f"method edge: {body} gives {'Matches' if 'lr' in body else 'MethodFailed'}",
               check(rc), repr(rc))
    empty = tmp / "empty"
    empty.mkdir()
    expect("vcheck validate works before the state directory exists",
           cli.main(["--state", str(empty / ".verifier"), "validate"]) == 0)


def test_environment(tmp: Path) -> None:
    """A method imports what the project's locked environment holds, and the report says so."""
    entry = {"id": "c", "value": 0.5,
             "source": {"kind": "artifact", "path": "runs/main/results.json", "method": "m"}}
    root, state = workdir(tmp, [entry])
    (state / "methods" / "m.py").write_text(
        "import fancy\nfrom checks.interface import Computed, method\n\n"
        "@method('m')\ndef m(path):\n    print('noise')\n"
        "    return Computed(value=fancy.HALF, n=1)\n")
    rc = analyse_project(root, state).claims[0].recompute
    expect("environment: without it, a method importing a library fails only its claim",
           isinstance(rc, recompute.MethodFailed) and "fancy" in rc.error, repr(rc))
    venv = state / METHOD_ENV
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    next(venv.glob("lib/python*/site-packages")).joinpath("fancy.py").write_text("HALF = 0.5\n")
    (root / "uv.lock").write_text("version = 1\n")
    tree = analyse_project(root, state)
    expect("environment: with it, the method recomputes the claim, printing as it likes",
           isinstance(tree.claims[0].recompute, recompute.Matches), repr(tree.claims[0].recompute))
    expect("environment: the report names the uv.lock it pins",
           watched_project.WatchedProject.load(root, state).method_environment_row()
           .text.startswith("the environment uv.lock pins"))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_fixture, test_report_layout, test_saved_report, test_flag_ids,
                  test_markdown_escapes, test_saved, test_saved_report_forms, test_pin,
                  test_pins_of_files_with_one_name, test_pinned_folder,
                  test_self_trace, test_proposed_pin, test_superseded, test_plugin_edges,
                  test_environment):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll core cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
