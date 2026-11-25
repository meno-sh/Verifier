#!/usr/bin/env python3
"""Assert that a working directory's run checks each of its documents on its own.

A document is a tracked .tex file with \\documentclass outside a comment. Covers:
  - a main paper and a supplement that both define \\label{tab:main} give no
    duplicate-label finding, and each gets its own paper findings; read as one paper,
    the same two files do give one, which is why they are read apart;
  - a problem in one document is reported under that document's path, in the board
    and in the run's report;
  - untracked, gitignored and commented-out documents, and notes, are not documents;
  - a working directory with no document gives no paper findings, and says so;
  - `vcheck run` in the daemon's checkout runs the paper checks and saves them;
  - the static verifier keeps reading the files it is given as one document.
Returns 1 if any case fails, so it can gate a commit.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli  # noqa: E402
from core.analysis import analysis_request, findings_analyzer  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from core.report_model import report_builder, triage_board  # noqa: E402
from core.report_output import markdown_renderer  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from schema import findings  # noqa: E402
from schema.primitives import Off  # noqa: E402

FAILED = []

MAIN = r"""\documentclass{article}
\begin{document}
\section{Results}
Table~\ref{tab:main} gives the accuracy of each model.
\begin{table}
\caption{Main results.}
\label{tab:main}
\begin{tabular}{lc}
Model & Accuracy \\
A & 81.2 \\
\end{tabular}
\end{table}
\end{document}
"""
SUPPLEMENT = r"""\documentclass{article}
\begin{document}
\section{More results}
Table~\ref{tab:main} gives the accuracy on the second split.
\begin{table}
\caption{Results on the second split.}
\label{tab:main}
\begin{tabular}{lc}
Model & Accuracy \\
A & 79.5 \\
\end{tabular}
\end{table}
\end{document}
"""
# A reference to a label no document defines: a dangling-ref finding in this document only.
DANGLING = r"Figure~\ref{fig:nowhere} shows the curve." + "\n"


def markdown_of(tree: object) -> str:
    """The Markdown report of a tree, as `vcheck run` writes it."""
    report = report_builder.VerificationReportBuilder().build_report(tree, None)
    return markdown_renderer.MarkdownReportRenderer().render_report(report)

def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def git(root: Path, *args: str) -> str:
    """Run git in the test repo with a fixed identity."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def project(root: Path, files: dict[str, str], ignore: str = "") -> Path:
    """A git repo with `files` committed, and `.gitignore` holding `ignore`."""
    root.mkdir(parents=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / ".gitignore").write_text(ignore)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    return root


def kinds(paper: findings.PaperFindings) -> set[str]:
    """The codes of every problem in one document's findings."""
    return {p.kind.code for p in paper.problems()}


def test_two_documents(tmp: Path) -> None:
    """A main paper and a supplement are checked apart, each under its own path."""
    root = project(tmp / "w", {"main.tex": MAIN, "supp/supplement.tex": SUPPLEMENT})
    tree = watched_project.WatchedProject.load(root, root / ".verifier").analyse()
    expect("two documents: each gets its own findings",
           list(tree.papers) == ["main.tex", "supp/supplement.tex"], f"got {list(tree.papers)}")
    expect("two documents: the label both define is no duplicate",
           all("duplicate-label" not in kinds(p) for p in tree.papers.values()),
           f"got {[kinds(p) for p in tree.papers.values()]}")
    expect("two documents: the paper checks ran, not Off",
           all(not isinstance(p.checks["dangling_ref"], Off) for p in tree.papers.values()))
    expect("two documents: the inputs list both files",
           sorted(os.path.relpath(p, root) for p in tree.inputs.paper_paths)
           == ["main.tex", "supp/supplement.tex"], f"got {tree.inputs.paper_paths}")
    merged = findings_analyzer.FindingsAnalyzer(extensions.builtin_check_registry()).analyse(
        analysis_request.AnalysisRequest(documents={
            "both": (str(root / "main.tex"), str(root / "supp/supplement.tex"))}))
    expect("two files read as one paper do give a duplicate label",
           "duplicate-label" in kinds(merged.papers["both"]))


def test_problem_under_its_document(tmp: Path) -> None:
    """A problem in one document is reported under that document's path."""
    supplement = SUPPLEMENT.replace(r"\end{document}", DANGLING + r"\end{document}")
    root = project(tmp / "w", {"main.tex": MAIN, "supp/supplement.tex": supplement})
    tree = watched_project.WatchedProject.load(root, root / ".verifier").analyse()
    expect("problem: the dangling reference is in the supplement's findings only",
           "dangling-ref" in kinds(tree.papers["supp/supplement.tex"])
           and "dangling-ref" not in kinds(tree.papers["main.tex"]))
    board = triage_board.TriageBoardBuilder().build_board(tree)
    entries = [e for axis in board.entries_by_axis.values() for e in axis
               if e.code == "dangling-ref"]
    expect("problem: the board names the document",
           len(entries) == 1 and entries[0].detail.startswith("supp/supplement.tex: "),
           f"got {entries}")
    text = markdown_of(tree)
    expect("problem: the report lists it as a caveat, under its type, with the document's path",
           "## Caveats" in text
           and "*These flags lean to caution" in text
           and "### `dangling-ref`\n\n1 issue, caveat (pass with caveats)\n" in text
           and " `supp/supplement.tex`: the reference to 'fig:nowhere'" in text, text)
    expect("problem: the report names both documents",
           "**Documents checked:** 2 (main.tex, supp/supplement.tex)" in text, text)


def test_what_is_not_a_document(tmp: Path) -> None:
    """Untracked, gitignored and commented-out documents, and notes, are left out."""
    root = project(tmp / "w", {"main.tex": MAIN, "notes/results.md": "We get 81.2%.\n",
                               "old.tex": "% \\documentclass{article}\nNo document here.\n"},
                   ignore="build/\n")
    (root / "build").mkdir()
    (root / "build" / "copy.tex").write_text(MAIN)
    (root / "draft.tex").write_text(MAIN)
    got = watched_project.WatchedProject.documents_in_folder(root)
    expect("not a document: only the tracked main paper counts",
           got == {"main.tex": [str(root / "main.tex")]}, f"got {got}")


def test_no_document(tmp: Path) -> None:
    """With no document, there are no paper findings, and the report says why."""
    root = project(tmp / "w", {"notes/results.md": "We get 81.2%.\n"})
    tree = watched_project.WatchedProject.load(root, root / ".verifier").analyse()
    expect("no document: papers is empty", dict(tree.papers) == {}, f"got {tree.papers}")
    expect("no document: the inputs name no paper",
           tree.inputs.paper_paths == () and tree.inputs.paper_sha256 is None)
    expect("no document: the report says the paper checks are off",
           "**Documents checked:** 0 (no tracked .tex file has \\documentclass, "
           "so the paper checks are off)" in markdown_of(tree))


def test_vcheck_run_in_checkout(tmp: Path) -> None:
    """`vcheck run` in the daemon's checkout runs the paper checks and saves them."""
    root = project(tmp / "w", {"main.tex": MAIN, "supp/supplement.tex": SUPPLEMENT})
    paths = StatePaths(root=root)
    paths.ensure()
    sha = runtime_git.head(root)
    runtime_git.check_out(root, paths.checkout, sha)
    paths.link_outputs(paths.checkout)
    code = cli.main(["--state", str(paths.state), "--root", str(paths.checkout),
                     "run", "--commit", sha])
    saved = json.loads((paths.state / "reports" / "latest.json").read_text())
    papers = saved["findings"]["papers"]
    expect("vcheck run: it finishes with a verdict", code in (0, 1, 2), f"exit {code}")
    expect("vcheck run: the saved findings hold both documents",
           sorted(papers) == ["main.tex", "supp/supplement.tex"], f"got {sorted(papers)}")
    expect("vcheck run: the paper checks ran in the checkout",
           all(p["checks"]["dangling_ref"]["type"] != "Off" for p in papers.values()))


def test_static_verify(tmp: Path) -> None:
    """The static verifier reads the files it is given as one document, named by the first."""
    tmp.mkdir(exist_ok=True)
    main_file, notes = tmp / "main.tex", tmp / "notes.md"
    main_file.write_text(MAIN)
    notes.write_text("We get 81.2% accuracy.\n")
    done = subprocess.run([sys.executable, "-m", "runtime.cli", "static-check", "--paper",
                           str(main_file), str(notes), "--json"], capture_output=True,
                          text=True, cwd=tmp,
                          env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])})
    try:
        papers = json.loads(done.stdout)["findings"]["papers"]
    except (json.JSONDecodeError, KeyError) as e:
        expect("static-check: prints its findings as JSON", False, f"{e}: {done.stderr[-300:]}")
        return
    expect("static-check: the files given are one document, named by the first",
           list(papers) == [str(main_file)], f"got {list(papers)}")


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_two_documents, test_problem_under_its_document,
                  test_what_is_not_a_document, test_no_document, test_vcheck_run_in_checkout,
                  test_static_verify):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll paper document cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
