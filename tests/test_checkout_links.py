#!/usr/bin/env python3
"""Assert that a checkout of the project can reach the project's gitignored files.

Covers:
  - the daemon's checkout: a gitignored folder is a real folder whose entries are links
    to the project's current copies, a gitignored file is a link, a gitignored folder
    inside a tracked folder is linked at its own path, and the outputs folder is still
    linked;
  - what is never linked: `.env` and other `.env*` files, and tool folders
    (`.venv`, `venv`, `node_modules`, `__pycache__`);
  - a path the checkout already has is kept, linking again adds new entries only, and
    an entry added to the project later appears when the links are made again;
  - git sees no change in the checkout, so the checkout can still move;
  - moving the checkout to a commit that starts tracking a file under a linked folder
    works, and leaves the project's copy alone;
  - a job's checkout gets the same links.

Returns 1 if any case fails, so it can gate a commit.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.agent import jobs  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402

FAILED = []

IGNORE = """runs/
sub/cache/
*.log
.env
.env.*
.venv/
venv/
node_modules/
__pycache__/
"""


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


def write(path: Path, text: str) -> None:
    """Write a file, creating its folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def project(tmp: Path) -> StatePaths:
    """A project with one commit, gitignored results and secrets, and the daemon's checkout."""
    root = tmp / "w"
    write(root / ".gitignore", IGNORE)
    write(root / "notes.md", "Accuracy is 0.5.\n")
    write(root / "sub" / "keep.txt", "tracked\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    write(root / "runs" / "main" / "results.json", '{"acc": 0.5}\n')
    write(root / "sub" / "cache" / "a.txt", "cached\n")
    write(root / "train run.log", "step 1\n")
    write(root / "notes.log", "worker log\n")
    write(root / ".env", "KEY=secret\n")
    write(root / ".env.local", "KEY=secret\n")
    write(root / ".venv" / "bin" / "python", "")
    write(root / "venv" / "bin" / "python", "")
    write(root / "node_modules" / "x" / "index.js", "")
    write(root / "sub" / "__pycache__" / "m.pyc", "")
    paths = StatePaths(root=root)
    paths.ensure()
    # As `verifier start` does: keep .verifier/ out of the worker's git.
    exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else root / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with open(exclude, "a") as f:
        f.write("/.verifier/\n")
    head = git(root, "rev-parse", "HEAD")
    runtime_git.check_out(root, paths.checkout, head)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    paths.checked_file.write_text(head + "\n")       # as the daemon records it
    return paths


def is_link_to(place: Path, source: Path) -> bool:
    """True if `place` is a link that leads to `source`."""
    return place.is_symlink() and place.resolve() == source.resolve()


def never_linked(tree: Path) -> list[str]:
    """The paths in `tree` that must not be there."""
    banned = (".env", ".env.local", ".venv", "venv", "node_modules", "sub/__pycache__")
    return [p for p in banned if os.path.lexists(tree / p)]


def test_daemon_checkout(tmp: Path) -> None:
    """The daemon's checkout links the gitignored paths and nothing it must not."""
    paths = project(tmp)
    root, tree = paths.root, paths.checkout
    write(tree / "train run.log", "the checkout's own copy\n")
    paths.link_outputs(tree)

    expect("a gitignored folder is a real folder whose entries are links",
           (tree / "runs").is_dir() and not (tree / "runs").is_symlink()
           and is_link_to(tree / "runs" / "main", root / "runs" / "main")
           and (tree / "runs" / "main" / "results.json").read_text() == '{"acc": 0.5}\n')
    expect("a gitignored file is a link",
           is_link_to(tree / "notes.log", root / "notes.log"))
    expect("a gitignored folder inside a tracked folder is linked at its own path",
           is_link_to(tree / "sub" / "cache" / "a.txt", root / "sub" / "cache" / "a.txt")
           and (tree / "sub" / "keep.txt").read_text() == "tracked\n"
           and not (tree / "sub").is_symlink())
    expect("the outputs folder is still linked",
           is_link_to(tree / ".verifier" / "outputs", paths.outputs))
    expect("no .env* file and no tool folder is linked", not never_linked(tree),
           repr(never_linked(tree)))
    expect("a path the checkout already has is kept",
           not (tree / "train run.log").is_symlink()
           and (tree / "train run.log").read_text() == "the checkout's own copy\n")

    write(root / "runs" / "main" / "results.json", '{"acc": 0.7}\n')
    write(root / "runs" / "late" / "results.json", '{"acc": 0.9}\n')
    expect("the checkout sees the project's current files",
           (tree / "runs" / "main" / "results.json").read_text() == '{"acc": 0.7}\n')

    paths.link_outputs(tree)
    expect("an entry added to the project later appears when the links are made again",
           is_link_to(tree / "runs" / "late", root / "runs" / "late")
           and is_link_to(tree / "runs" / "main", root / "runs" / "main")
           and not (tree / "train run.log").is_symlink())
    status = git(tree, "status", "--porcelain")
    expect("git sees no change in the checkout", status == "", repr(status))


def test_checkout_moves(tmp: Path) -> None:
    """A commit that starts tracking a file under a linked folder does not reach the project."""
    paths = project(tmp)
    root, tree = paths.root, paths.checkout
    paths.link_outputs(tree)
    git(root, "add", "-f", "runs/main/results.json")
    git(root, "commit", "-q", "-m", "track one result")
    write(root / "runs" / "main" / "results.json", '{"acc": "worker edit"}\n')
    moved = True
    try:
        runtime_git.check_out(root, tree, git(root, "rev-parse", "HEAD"))
        paths.link_outputs(tree)
    except subprocess.CalledProcessError as e:
        moved = False
        print(f"        {e.stderr}")
    expect("the checkout moves to a commit that tracks a file under a linked folder", moved)
    expect("the checkout holds the committed copy, as a real folder",
           not (tree / "runs").is_symlink()
           and (tree / "runs" / "main" / "results.json").read_text() == '{"acc": 0.5}\n')
    expect("the project's copy is left alone",
           (root / "runs" / "main" / "results.json").read_text() == '{"acc": "worker edit"}\n')


def test_job_checkout(tmp: Path) -> None:
    """A job's own checkout gets the same links."""
    paths = project(tmp)
    paths.link_outputs(paths.checkout)
    jobs.start(paths, "look", ["cat", "runs/main/results.json"], checkin=None)
    tree = paths.jobs / "look.tree"
    log = paths.jobs / "look.log"
    for _ in range(50):
        if (paths.jobs / "look.status").exists():
            break
        time.sleep(0.1)
    expect("a job's checkout links the gitignored folder",
           is_link_to(tree / "runs" / "main", paths.root / "runs" / "main"))
    expect("a job's checkout has no .env* file and no tool folder", not never_linked(tree),
           repr(never_linked(tree)))
    expect("a job reads a gitignored file", '"acc": 0.5' in log.read_text(), log.read_text())


def main() -> int:
    """Run every case in a fresh folder and return 1 if any failed."""
    for group in (test_daemon_checkout, test_checkout_moves, test_job_checkout):
        print(group.__doc__)
        tmp = Path(tempfile.mkdtemp())
        try:
            group(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
