#!/usr/bin/env python3
"""Check the fixes of the quantifier-docs PR, one case or more for each issue it closes.

The cases, by issue number of the v10 issue list:
  - 24: the coherence command is run again after an error, and may log before its answer;
  - 56: a damaged alert line does not hide the others, and an unreadable .tex file does
    not stop paper discovery;
  - 60: the judge's breakage prompt and the coherence chat keep every digit of a value
    (the judge reads the premises in the paper, which is sent as it is written);
  - 69: the README shows the commands for claim uncertainty;
  - 70: the quantifier reads the check's options with their defaults;
  - 73: the first error of the two scores is raised without a wait for the other;
  - 83: write-methods refuses a wrong need before the paid call;
  - 98: a reference to a label of another document is not dangling.
Returns 1 if any case fails.
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from checks.uncertainty import quantifier, uncertainty  # noqa: E402
from core import cli  # noqa: E402
from core.analysis import analysis_request, findings_analyzer  # noqa: E402
from core.commands import write_methods  # noqa: E402
from core.methods import method_writer  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from runtime.commands import alerts as alerts_command  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from schema import inputs  # noqa: E402
from utils import alerts, claim_files, models, structure  # noqa: E402

# the made-up model hosts of these tests need no key
models.KEYS.update(dict.fromkeys(("gpu", "gpu-host", "127.0.0.1", "10.0.0.5", "10.0.0.9",
                                  "example.invalid"), None))

FAILED: list[str] = []

CLAIM = uncertainty.Premise(id="c", statement="The method works.", value=None)
PREMISES = (uncertainty.Premise(id="p", statement="The test set has this many items.",
                                value=1234567.0),
            uncertainty.Premise(id="q", statement="The error is", value=inputs.PrintedFloat(
                "0.120")))
ATTACK = uncertainty.Attack(hypotheticals=(uncertainty.Hypothetical(
    design="the same model on a new test set", result="the accuracy drops to 51%"),))


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def git(root: Path, *args: str) -> None:
    """Run git in a test repository."""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def coherence(tmp: Path, script: str) -> quantifier.CoherenceQuantifier:
    """A coherence quantifier whose command runs `script`, and that does not wait."""
    path = tmp / "command.py"
    path.write_text(script)
    judge = quantifier.JudgeQuantifier("https://example.invalid/v1/chat/completions#m",
                                       uncertainty.CUTOFF.default)
    return quantifier.CoherenceQuantifier(f"{sys.executable} {path}", judge,
                                          sleep=lambda _: None)


def test_coherence_command(tmp: Path) -> None:
    """24: retries, and a log line before the answer."""
    alerts.configure(tmp / "alerts.jsonl", "test", tmp)
    counter = tmp / "runs"
    flaky = coherence(tmp, (
        "import pathlib, sys\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) + 1 if p.exists() else 1\n"
        "p.write_text(str(n))\n"
        "if n < 3:\n"
        "    sys.exit('the server is not reachable')\n"
        "print('loading the model')\n"
        "print('{\"breakage\": 0.25}')\n"))
    value = flaky.breakage(CLAIM, PREMISES, ATTACK)
    expect("24: two failed runs, then the breakage from the last line",
           value == 0.25 and counter.read_text() == "3", f"{value}, {counter.read_text()}")
    warns = [r for r in alerts.read(tmp / "alerts.jsonl") if r["key"] == "coherence.command_retry"]
    expect("24: each failed run before the last is a warn", len(warns) == 2, str(warns))

    broken = coherence(tmp, "import sys\nsys.exit('down')\n")
    try:
        broken.breakage(CLAIM, PREMISES, ATTACK)
        expect("24: a command that always fails raises after ATTEMPTS runs", False, "no error")
    except models.ProviderError as e:
        expect("24: a command that always fails raises after ATTEMPTS runs",
               f"run {quantifier.ATTEMPTS} of {quantifier.ATTEMPTS}" in str(e), str(e))

    wrong = coherence(tmp, "print('{\"breakage\": true}')\n")
    try:
        wrong.breakage(CLAIM, PREMISES, ATTACK)
        expect("70: a breakage that is not a number is refused", False, "no error")
    except ValueError as e:
        expect("70: a breakage that is not a number is refused", "true" in str(e).lower(), str(e))


def test_digits() -> None:
    """60: every digit of a value reaches the judge and the coherence chat."""
    judge = " ".join(quantifier.break_prompt(claim, (), ATTACK) for claim in PREMISES)
    chat = json.dumps(quantifier.coherence_messages(CLAIM, PREMISES, ATTACK))
    for name, text in (("judge's breakage prompts", judge), ("coherence chat", chat)):
        expect(f"60: 1234567 and 0.120 keep every digit in the {name}",
               "1234567" in text and "0.120" in text and "e+06" not in text, text[:300])
    expect("60: the versions change with the prompts",
           quantifier.JudgeQuantifier.RUBRIC == "6-paper-or-notes-prefix-scope-reasoned"
           and quantifier.CoherenceQuantifier.METRIC == "6-cutoff-of-the-judge")


def test_options() -> None:
    """70: the rules read the check's options with their defaults."""
    off = {uncertainty.WANT.key: False}
    expect("70: with the check off, the rules refuse nothing",
           quantifier.models_are_reachable(off) is None
           and quantifier.coherence_needs_one_source(off) is None)
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        quantifier.models_are_reachable({})
        expect("70: on by default, a model with no API key is refused", False, "no error")
    except ValueError as e:
        expect("70: on by default, a model with no API key is refused",
               "OPENROUTER_API_KEY is not set" in str(e), str(e))
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved
    try:
        quantifier.coherence_needs_one_source({uncertainty.WANT.key: True,
                                               uncertainty.QUANTIFIER.key: "coherence"})
        expect("70: coherence with no source is refused", False, "no error")
    except ValueError:
        expect("70: coherence with no source is refused", True)
    source = Path(quantifier.__file__).read_text()
    expect("70: the quantifier no longer calls uncertainty._check_unit",
           "_check_unit" not in source)


def test_both() -> None:
    """73: the first error is raised at once."""
    release = threading.Event()

    def slow() -> float:
        """A request that takes long."""
        release.wait(20)
        return 0.5

    def fails() -> float:
        """A request that fails at once."""
        raise models.ProviderError("the judge is down")

    start = time.monotonic()
    try:
        quantifier.both(slow, fails)
        expect("73: the error is raised", False, "no error")
    except models.ProviderError:
        took = time.monotonic() - start
        expect("73: the error is raised without a wait for the slow request", took < 5,
               f"{took:.1f} s")
    finally:
        release.set()
    scores = quantifier.both(lambda: 0.75, lambda: 0.25)
    expect("73: two scores still give one assessment",
           (scores.believability, scores.breakage) == (0.75, 0.25), str(scores))


def test_alert_log(tmp: Path) -> None:
    """56: a damaged alert line does not hide the other records."""
    paths = StatePaths(tmp)
    paths.alerts_log.parent.mkdir(parents=True, exist_ok=True)
    alerts.configure(paths.alerts_log, "test", tmp)
    alerts.notify("warn", "test.first", "the first record")
    with open(paths.alerts_log, "a") as fh:
        fh.write("not json\n")
    alerts.notify("warn", "test.second", "the second record")
    try:
        alerts.read(paths.alerts_log)
        expect("56: read() without a list still refuses a damaged line", False, "no error")
    except alerts.AlertError:
        expect("56: read() without a list still refuses a damaged line", True)
    bad: list[str] = []
    keys = [r["key"] for r in alerts.read(paths.alerts_log, bad)]
    expect("56: with a list, the good records are read and the damaged line is named",
           keys == ["test.first", "test.second"] and len(bad) == 1 and ":2:" in bad[0],
           f"{keys}, {bad}")
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(level="warn", key=None, since=None, root=paths.root)
    with redirect_stdout(out), redirect_stderr(err):
        rc = alerts_command.VerifierAlertsCommand().execute(args)
    expect("56: verifier alerts prints both records, names the line, and exits with 1",
           rc == 1 and "test.first" in out.getvalue() and "test.second" in out.getvalue()
           and "damaged line" in err.getvalue(), f"{rc}, {err.getvalue()}")


def test_unreadable_tex(tmp: Path) -> None:
    """56: a .tex path that cannot be read does not stop paper discovery."""
    alerts.configure(tmp / "alerts.jsonl", "test", tmp)
    root = tmp / "project"
    root.mkdir()
    (root / "paper.tex").write_text("\\documentclass{article}\n\\begin{document}\nHi.\n"
                                    "\\end{document}\n")
    locked = root / "locked.tex"
    locked.write_text("\\documentclass{article}\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "a paper")
    locked.chmod(0)                        # no permission to read it
    if os.access(locked, os.R_OK):
        print("  skip  56: this user can read a file with no permission")
        return
    found = [rel for _, rel, _ in structure.paper_candidates(str(root))]
    expect("56: paper_candidates skips the unreadable file", found == ["paper.tex"], str(found))
    docs = watched_project.WatchedProject.documents_in_folder(root)
    expect("56: documents() finds the paper", list(docs) == ["paper.tex"], str(docs))
    warns = [r for r in alerts.read(tmp / "alerts.jsonl") if r["key"] == "papers.unreadable"]
    expect("56: each unreadable read is a warn", len(warns) == 2, str(warns))


def test_docs() -> None:
    """69: the commands of README.md and SPEC.md."""
    readme = ((HERE.parent / "README.md").read_text()
              + (HERE.parent / "docs" / "SPEC.md").read_text())
    expect("69: the docs name the cutoff option, set no cutoff value, and show the quantifier",
           uncertainty.CUTOFF.flag in readme
           and not re.search(rf"{uncertainty.CUTOFF.key}=\d", readme)
           and "--option quantifier=coherence" in readme)
    expect("69: the README names no uncertainty option that the check no longer has",
           "uncertainty_threshold" not in readme and "--uncertainty-threshold" not in readme)


def test_write_methods(tmp: Path) -> None:
    """83: a need whose claim has no artifact source is refused before the paid call."""
    root = tmp / "project"
    root.mkdir()
    git(root, "init", "-q")
    (root / "note.md").write_text("# Notes\n\nThe method works on every split we tried.\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "notes")
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    state = root / ".verifier"
    state.mkdir()
    claim_files.save_scope(state, (structure.WHOLE,))
    alerts.configure(state / "alerts.jsonl", "test", root)
    claim_files.begin_round(root, state, sha)
    candidate = next(iter(json.loads((state / claim_files.CANDIDATES).read_text())))
    (state / claim_files.QUOTES).write_text(json.dumps({"works": {
        "created": "", "last_updated": "", "candidates": [candidate],
        "quote": "The method works on every split we tried", "value": None}}))
    (state / claim_files.CLASSES).write_text(json.dumps(
        {"note/works": {"created": "", "last_updated": "", "quotes": ["works"]}}))
    (state / "claim_register.json").write_text(json.dumps({"claims": [
        {"id": "note/works", "created": "", "last_updated": "",
         "statement": "The method works on every split.", "origin": "note.md"}]}))
    (state / write_methods.NEEDS).write_text(json.dumps(
        [{"claim": "note/works", "path": "runs/results.json", "quantity": "accuracy"}]))
    calls = []

    def fake(prompt: str, timeout: int = 0, model: str = "") -> str:
        """The paid method writer, offline."""
        calls.append(prompt)
        raise AssertionError("the method writer must not be called")

    real, method_writer.ask = method_writer.ask, fake
    err = io.StringIO()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli.main(["--state", str(state), "write-methods"])
    finally:
        method_writer.ask = real
    expect("83: write-methods stops with 1 and makes no paid call",
           rc == 1 and not calls, f"{rc}, {len(calls)} call(s)")
    expect("83: the error names the claim and method_needs.json",
           "note/works" in err.getvalue() and write_methods.NEEDS in err.getvalue(),
           err.getvalue())


def test_elsewhere(tmp: Path) -> None:
    """98: a supplement may refer to a table of the main paper."""
    main = tmp / "main.tex"
    supplement = tmp / "supplement.tex"
    main.write_text("\\documentclass{article}\n\\begin{document}\nTable~\\ref{tab:main} "
                    "gives the accuracy.\n\\begin{table}\\caption{Results.}\\label{tab:main}"
                    "\n\\begin{tabular}{lc}\nModel & Accuracy \\\\\nA & 81.2 \\\\\n"
                    "\\end{tabular}\n\\end{table}\n\\end{document}\n")
    supplement.write_text("\\documentclass{article}\n\\begin{document}\nAs in "
                          "Table~\\ref{tab:main}, model A is best. Figure~\\ref{fig:nowhere} "
                          "shows the curve.\n\\end{document}\n")
    tree = findings_analyzer.FindingsAnalyzer(extensions.builtin_check_registry()).analyse(
        analysis_request.AnalysisRequest(documents={"main.tex": (str(main),),
                                                    "supplement.tex": (str(supplement),)}))
    problem = tree.papers["supplement.tex"].checks["dangling_ref"]
    detail = " ".join(p.detail for p in getattr(problem, "problems", ()))
    expect("98: the reference to the main paper's table is not dangling",
           "tab:main" not in detail, detail)
    expect("98: a reference that no document defines is still dangling",
           "fig:nowhere" in detail, detail)


def main() -> int:
    """Run every case and report."""
    tests = (test_coherence_command, test_digits, test_options, test_both, test_alert_log,
             test_unreadable_tex, test_docs, test_write_methods, test_elsewhere)
    for test in tests:
        print(test.__name__)
        if test.__code__.co_argcount:
            with tempfile.TemporaryDirectory() as tmp:
                test(Path(tmp))
        else:
            test()
    print("all passed" if not FAILED else f"FAILED: {len(FAILED)} case(s).")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
