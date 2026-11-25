#!/usr/bin/env python3
"""Assert that `vcheck run` writes the same report files as the saved golden copies.

The refactor of the path from the claim register to the report must not change what a
reader sees. This test runs `vcheck run` on the fixture working directory in five cases
and compares the Markdown, the Typst markup, the saved JSON, the printed text and the
exit code with the copies in tests/fixtures/report_golden/:

  - no_dispute: the whole fixture register, with no disputed flag;
  - disputed_gate_flag: a claim whose number disagrees, with that gate flag disputed;
  - disputed_check_flag: two claims with a comparison flag, one of them disputed;
  - run_could_not_start: a claim register that is not JSON;
  - claim_checks_not_run: the whole fixture register, with `--claim-checks-not-run`, so
    only the papers are checked and the report flags that no claim was checked.

Each case runs two times, and the second run, which gives the stored results of the
claim checks, must write the same files. It also loads each saved JSON and writes it
again with the writer of `vcheck run`, and compares the bytes, so a reader of a saved
report keeps every key.

Run with `--update` to write the golden copies again after a change of behaviour that a
pull request names. Returns 1 if any case fails, so it can gate a commit.
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
from core.analysis import check_result_cache  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from core.report_model import report_builder  # noqa: E402
from core.report_output import report_store  # noqa: E402
from utils import version  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "report_golden"
COMMIT = "c" * 40
WHY = "The method reads runs/old/; the notes use runs/fixed/."
# The files of one case: what `vcheck run` saved, what it printed, and its exit code.
OUTPUT_FILES = ("report.md", "report.typ", "report.json", "printed.txt", "exit_code.txt")
ROOT_PLACEHOLDER = "<working-directory>"
TOOL_COMMIT_PLACEHOLDER = "<verifier-commit>"
FAILED = []
REGISTER_FILE = watched_project.WatchedProject.REGISTER_FILE


# The options of `vcheck run` in each case that has any.
OPTIONS_OF_CASE = {"claim_checks_not_run": (
    "--claim-checks-not-run", "vcheck label-candidates failed (exit 1): error: a model call "
                              "failed, so no label was written")}


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def dispute_of_first_issue(root: Path, state: Path, code: str) -> list[dict]:
    """A dispute, as fakes.fixed_disputes reads it, of the first issue of type `code`."""
    report = report_builder.VerificationReportBuilder().build_report(
        test_core.analyse_project(root, state), None)
    issue = next(i for i in report.issues if i.code == code)
    return [{"code": issue.code, "subject": issue.subject, "detail": issue.detail,
             "dispute_id": "D-1", "reason": WHY}]


def prepare_case(case: str, tmp: Path) -> tuple[Path, Path]:
    """The working directory and state folder of one case, with its disputes saved."""
    if case in ("no_dispute", "claim_checks_not_run"):
        return test_core.workdir(tmp)
    if case == "disputed_gate_flag":
        root, state = test_core.workdir(tmp, [test_core.claim_entry("disagrees")])
        disputes = dispute_of_first_issue(root, state, "number-disagrees")
    elif case == "disputed_check_flag":
        root, state = test_core.workdir(tmp, [test_core.claim_entry(c) for c in ("good", "prior")])
        disputes = dispute_of_first_issue(root, state, "undeclared-difference")
    elif case == "run_could_not_start":
        root, state = test_core.workdir(tmp)
        (state / REGISTER_FILE).write_text("{ this is not JSON")
        return root, state
    else:
        raise ValueError(f"no golden case {case!r}")
    (state / fixed_disputes.FILE).write_text(json.dumps(disputes))
    return root, state


def outputs_of_vcheck_run(root: Path, state: Path, *options: str) -> dict[str, str]:
    """Run `vcheck run` once with `options`, and give each output file of the case as text.

    The path of the temporary working directory and the commit of the Verifier code are
    replaced with placeholders, so the copies do not depend on where the test runs or on
    the commit under test.
    """
    printed = io.StringIO()
    extensions.ExtensionSet.save_names(state, (fixed_disputes.NAME,))
    with contextlib.redirect_stdout(printed):
        exit_code = cli.main(["--state", str(state), "--root", str(root), "run",
                              "--commit", COMMIT, *options])
    reports = state / report_store.ReportStore.REPORTS_DIR
    texts = {"report.md": (reports / f"{COMMIT}.md").read_text(),
             "report.typ": (reports / f"{COMMIT}.typ").read_text(),
             "report.json": (reports / f"{COMMIT}.json").read_text(),
             "printed.txt": printed.getvalue(),
             "exit_code.txt": f"{exit_code}\n"}
    commit_key = f'"tool_commit": "{version.verifier_short_commit()}"'
    return {name: text.replace(str(root), ROOT_PLACEHOLDER)
            .replace(commit_key, f'"tool_commit": "{TOOL_COMMIT_PLACEHOLDER}"')
            for name, text in texts.items()}


def test_case(case: str, update: bool) -> None:
    """Compare (or, with `update`, write) the output files of one case."""
    with tempfile.TemporaryDirectory() as tmp:
        root, state = prepare_case(case, Path(tmp))
        options = OPTIONS_OF_CASE.get(case, ())
        got = outputs_of_vcheck_run(root, state, *options)
        saved_json = (state / report_store.ReportStore.REPORTS_DIR / f"{COMMIT}.json").read_text()
        stored = len(list((state / check_result_cache.CACHE_DIR / "results").glob("*.json")))
        again = outputs_of_vcheck_run(root, state, *options)
    folder = GOLDEN / case
    if update:
        folder.mkdir(parents=True, exist_ok=True)
        for name in OUTPUT_FILES:
            (folder / name).write_text(got[name])
        print(f"  wrote {folder.relative_to(GOLDEN.parent.parent)}")
        return
    for name in OUTPUT_FILES:
        want = (folder / name).read_text()
        expect(f"{case}: {name} is the same as the golden copy", got[name] == want,
               first_difference(want, got[name]))
    expect(f"{case}: a second run, with the stored check results, writes the same files",
           # a run that cannot start, or that checks no claim, stores nothing
           again == got
           and (stored > 0) == (case not in ("run_could_not_start", "claim_checks_not_run")),
           f"{stored} stored results; " + next((first_difference(got[n], again[n])
                                                for n in OUTPUT_FILES if got[n] != again[n]), ""))
    rewritten = json.dumps(json.loads(saved_json), indent=2, sort_keys=True) + "\n"
    expect(f"{case}: the saved JSON, read and written again, has the same bytes",
           rewritten == saved_json)


def test_papers_only_keeps_stored_results() -> None:
    """Runs that check no claim keep the stored claim results, and an empty reason is refused."""
    with tempfile.TemporaryDirectory() as tmp:
        root, state = prepare_case("no_dispute", Path(tmp))
        outputs_of_vcheck_run(root, state)
        results = state / check_result_cache.CACHE_DIR / "results"
        stored = sorted(p.name for p in results.glob("*.json"))
        for _ in range(4):
            outputs_of_vcheck_run(root, state, *OPTIONS_OF_CASE["claim_checks_not_run"])
        expect("papers only: four runs that check no claim keep every stored claim result",
               stored and sorted(p.name for p in results.glob("*.json")) == stored,
               repr(stored))
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            try:
                cli.main(["--state", str(state), "--root", str(root), "run", "--commit", COMMIT,
                          "--claim-checks-not-run", " "])
                refused = False
            except ValueError as e:
                refused = "empty" in str(e)
        expect("papers only: an empty reason is refused", refused, printed.getvalue())


def first_difference(want: str, got: str) -> str:
    """The first line where two texts differ, for the failure message."""
    for number, (a, b) in enumerate(zip(want.splitlines(), got.splitlines(), strict=False), 1):
        if a != b:
            return f"line {number}: want {a!r}, got {b!r}"
    return f"want {len(want.splitlines())} lines, got {len(got.splitlines())}"


def main() -> int:
    """Run each case; with --update, write the golden copies instead."""
    update = "--update" in sys.argv[1:]
    for case in ("no_dispute", "disputed_gate_flag", "disputed_check_flag",
                 "run_could_not_start", "claim_checks_not_run"):
        print(case)
        test_case(case, update)
    if not update:
        print("papers_only_keeps_stored_results")
        test_papers_only_keeps_stored_results()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll golden report cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
