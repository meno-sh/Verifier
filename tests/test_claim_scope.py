#!/usr/bin/env python3
"""Check that only the selected notes give candidates, and the selection at `verifier start`.

Builds a git repository with notes at the top, in a folder, in a folder inside it, and in a
folder whose name starts with the name of another, and commits whose messages state numbers.
The cases check that:
  - a scope entry keeps the note itself and the notes in its folders, and nothing else;
  - only the selected notes give candidates, and every commit message still does;
  - a round reads the saved selection, and a narrower one removes the candidates it leaves out;
  - a missing or damaged selection file stops the round with a clear error;
  - a selected path is made relative to the working directory, and one outside it is refused;
  - `scope.select` keeps a saved selection, refuses to guess when nobody can be asked, takes
    `--claims-from`, refuses a path the tracked branch does not have, and at a terminal shows
    the counts with the estimated cost, and starts, selects again or quits;
  - the counts come from the tracked branch, not from the files that are checked out;
  - `verifier init` and `verifier start` save the selection before they record the branch.
Returns 1 if any case fails.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.registry import extensions  # noqa: E402
from runtime.agent import opencode  # noqa: E402
from runtime.project import project_preparation, scope  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import claim_files, structure  # noqa: E402

FAILED = []
NOTES = {
    "README.md": "# Project\n\nThe main run reaches 0.81 accuracy.\n",
    "notes/run1.md": "# Run 1\n\nThe loss fell to 1.25 after 3 epochs.\n",
    "notes/deep/run2.md": "# Run 2\n\nThe second run reaches 0.64 accuracy.\n",
    "notes-old/draft.md": "# Draft\n\nThe draft run reaches 0.12 accuracy.\n",
    "paper/main.tex": ("\\documentclass{article}\n\\begin{document}\nWe reach 91.2\\% accuracy."
                       "\n\\end{document}\n"),
}


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


def project(tmp: Path) -> StatePaths:
    """A git repo with the notes of NOTES on branch main, in one commit."""
    root = tmp / "w"
    for rel, text in NOTES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text(".verifier/\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first run: accuracy 0.81")
    paths = StatePaths(root=root)
    paths.ensure()
    return paths


def origins(found: list[dict]) -> set[str]:
    """The notes that gave candidates."""
    return {c["origin"] for c in found if c.get("kind") != "commit message"}


def answers(*given: str):
    """An `ask` that gives these answers in order, and records each question."""
    asked = []
    left = list(given)

    def ask(question: str) -> str:
        asked.append(question)
        return left.pop(0)
    ask.asked = asked
    return ask


def quiet(fn, *args, **kwargs):
    """Call `fn` and return its result (or exception) and what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            return fn(*args, **kwargs), out.getvalue()
        except (ValueError, scope.Cancelled) as e:
            return e, out.getvalue()


def test_within() -> None:
    """A scope entry keeps the note itself and the notes in its folders."""
    within = ("notes", "README.md")
    expect("scope: a note in a selected folder is kept",
           structure.within_scope("notes/a.md", within))
    expect("scope: a note in a folder inside a selected folder is kept",
           structure.within_scope("notes/deep/a.md", within))
    expect("scope: a selected note is kept", structure.within_scope("README.md", within))
    expect("scope: a folder whose name starts with a selected name is left out",
           not structure.within_scope("notes-old/a.md", within))
    expect("scope: `.` keeps every note",
           structure.within_scope("x/y.md", (structure.WHOLE,)))
    try:
        structure.within_scope("a.md", ())
        expect("scope: an empty scope is refused", False)
    except ValueError:
        expect("scope: an empty scope is refused", True)


def test_candidates(tmp: Path) -> None:
    """Only the selected notes give candidates; commit messages always do."""
    paths = project(tmp)
    root = str(paths.root)
    every = structure.folder_candidates(root)
    expect("candidates: with no scope, every note gives candidates",
           origins(every) == set(NOTES), str(origins(every)))
    some = structure.folder_candidates(root, within=("notes", "paper/main.tex"))
    expect("candidates: only the selected notes give candidates",
           origins(some) == {"notes/run1.md", "notes/deep/run2.md", "paper/main.tex"},
           str(origins(some)))
    expect("candidates: every commit message still gives candidates",
           any(c.get("kind") == "commit message" for c in some))


def test_round(tmp: Path) -> None:
    """A round reads the saved selection; a narrower one removes what it leaves out."""
    paths = project(tmp)
    sha = git(paths.root, "rev-parse", "HEAD")
    try:
        claim_files.begin_round(paths.root, paths.state, sha)
        expect("round: no saved selection stops the round", False)
    except claim_files.ClaimFilesError as e:
        expect("round: no saved selection stops the round, and says to run verifier start",
               "verifier start" in str(e), str(e))
    claim_files.save_scope(paths.state, (structure.WHOLE,))
    claim_files.begin_round(paths.root, paths.state, sha)
    first = claim_files.load(paths.state).candidates
    claim_files.save_scope(paths.state, ("notes",))
    summary = claim_files.begin_round(paths.root, paths.state, sha)
    later = claim_files.load(paths.state).candidates
    expect("round: a narrower selection removes the candidates it leaves out",
           origins(list(first.values())) == set(NOTES)
           and origins(list(later.values())) == {"notes/run1.md", "notes/deep/run2.md"},
           summary)
    for text, name in (("[1]", "a list"), ('{"paths": []}', "no path"),
                       ('{"paths": ["a"], "x": 1}', "an extra key"), ("{", "not JSON")):
        (paths.state / claim_files.SCOPE).write_text(text)
        try:
            claim_files.read_scope(paths.state)
            expect(f"round: a selection file with {name} is refused", False)
        except claim_files.ClaimFilesError:
            expect(f"round: a selection file with {name} is refused", True)


def test_scope_paths(tmp: Path) -> None:
    """A selected path is made relative to the working directory."""
    root = tmp / "w"
    root.mkdir()
    expect("paths: relative, absolute and the root itself, sorted and once each",
           claim_files.scope_paths(root, ["notes/", str(root / "paper"), ".", "notes"])
           == (".", "notes", "paper"))
    for bad, name in ((["../x"], "outside the working directory"), ([], "empty"),
                      ([" "], "a blank path")):
        try:
            claim_files.scope_paths(root, bad)
            expect(f"paths: {name} is refused", False)
        except ValueError:
            expect(f"paths: {name} is refused", True)


def test_select(tmp: Path) -> None:
    """The selection at start: saved, given, refused, and asked at a terminal."""
    paths = project(tmp)
    result, _ = quiet(scope.select, paths, "main", None, None)
    expect("select: with nothing given or saved and nobody to ask, it is refused",
           isinstance(result, ValueError) and "--claims-from" in str(result), repr(result))
    result, out = quiet(scope.select, paths, "main", ["notes"], None)
    expect("select: --claims-from is saved, and its counts are shown",
           result == ("notes",) and claim_files.read_scope(paths.state) == ("notes",)
           and "notes/" in out and "README.md" not in out and "commit messages" in out
           and "Estimated cost of the first round: $" in out, out)
    result, _ = quiet(scope.select, paths, "main", None, answers())
    expect("select: a saved selection is kept, and nobody is asked", result == ("notes",))
    result, _ = quiet(scope.select, paths, "main", ["nothing-here"], None)
    expect("select: a path the tracked branch does not have is refused",
           isinstance(result, ValueError) and "nothing-here" in str(result), repr(result))
    expect("select: a refused selection leaves the saved one",
           claim_files.read_scope(paths.state) == ("notes",))

    (paths.state / claim_files.SCOPE).unlink()
    ask = answers("nope", "paper", "s", "notes README.md", "maybe", "y")
    result, out = quiet(scope.select, paths, "main", None, ask)
    expect("select: at a terminal, every note's counts are shown first",
           out.startswith("Every note") and "notes-old/" in out.split("Read claims")[0], out)
    expect("select: a path not on the branch is asked again, then select again, then start",
           result == ("README.md", "notes") and len(ask.asked) == 6
           and "not on the tracked branch: nope" in out and "answer y, s or q" in out, out)
    expect("select: the started selection is saved",
           claim_files.read_scope(paths.state) == ("README.md", "notes"))
    result, _ = quiet(scope.select, paths, "main", ["paper"], answers("q"))
    expect("select: quit raises Cancelled and keeps the saved selection",
           isinstance(result, scope.Cancelled)
           and claim_files.read_scope(paths.state) == ("README.md", "notes"), repr(result))
    expect("select: nothing is added to the repo's worktrees",
           len(git(paths.root, "worktree", "list").splitlines()) == 1,
           git(paths.root, "worktree", "list"))


def test_branch(tmp: Path) -> None:
    """The counts come from the tracked branch, not from the checked-out files."""
    paths = project(tmp)
    git(paths.root, "checkout", "-q", "-b", "side")
    (paths.root / "extra").mkdir()
    (paths.root / "extra" / "new.md").write_text("A new note says 0.5.\n")
    git(paths.root, "add", "-A")
    git(paths.root, "commit", "-q", "-m", "side note")
    result, out = quiet(scope.select, paths, "main", ["."], None)
    expect("branch: a note only on another branch is not counted",
           result == (".",) and "extra/" not in out, out)
    result, _ = quiet(scope.select, paths, "main", ["extra"], None)
    expect("branch: a folder only on another branch is refused",
           isinstance(result, ValueError), repr(result))


def test_prepare(tmp: Path) -> None:
    """`init` and `start` save the selection before they record the branch."""
    paths = project(tmp)
    real = project_preparation.run_problem, opencode.version
    project_preparation.run_problem = lambda project, model, saved_values=None: None
    opencode.version = lambda: opencode.PINNED_VERSION
    try:
        problem, _ = quiet(project_preparation.prepare, paths, "main", "m", None)
        expect("prepare: with no selection and no terminal, nothing is recorded",
               "--claims-from" in (problem or "") and not paths.branch_file.exists(),
               repr(problem))
        problem, _ = quiet(project_preparation.prepare, paths, "main", "m", ["paper"], answers("q"))
        expect("prepare: quit at the terminal records nothing",
               problem == "nothing was started" and not paths.branch_file.exists(),
               repr(problem))
        problem, _ = quiet(project_preparation.prepare, paths, "main", "m", ["paper"])
        expect("prepare: --claims-from saves the selection and records the branch",
               problem is None and paths.branch() == "main"
               and claim_files.read_scope(paths.state) == ("paper",), repr(problem))
        saved = extensions.ExtensionSet.saved_names(paths.state)
        problem, _ = quiet(project_preparation.prepare, paths, "main", "m", None,
                           options=["no_such_option=1"], extension_names=())
        expect("prepare: a wrong --option saves neither the options nor new extensions",
               "names no run option" in (problem or "")
               and extensions.ExtensionSet.saved_names(paths.state) == saved
               and not (paths.state / "options.json").exists(), repr(problem))
    finally:
        project_preparation.run_problem, opencode.version = real


def main() -> int:
    """Run every case and report."""
    test_within()
    for case in (test_candidates, test_round, test_scope_paths, test_select, test_branch,
                 test_prepare):
        with tempfile.TemporaryDirectory() as tmp:
            case(Path(tmp))
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
