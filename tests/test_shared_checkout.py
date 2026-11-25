#!/usr/bin/env python3
"""Check that the Verifier's checkout is a shared clone that writes nothing into the repo.

Covers: the checkout reads a repo whose git folder it cannot write, as when the
auditing agent runs as another user; a checkout of a worktree, as a harness makes
them; a commit made after the clone, and branches that follow the repo's; the repo's
info/exclude and `.verifier/` excluded in the clone; a shallow repo; a branch
rewritten and then pruned by git gc; and a checkout that an earlier version made
as a worktree. Returns 1 if any case fails.
"""
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_runtime as rt  # noqa: E402

from runtime.project import git as runtime_git  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def set_writable(top: Path, writable: bool) -> None:
    """Add or remove write permission on `top` and everything in it."""
    for here, dirs, files in os.walk(top):
        for name in [*dirs, *files, ""]:
            p = Path(here) / name
            if p.is_symlink():
                continue
            mode = p.stat().st_mode
            p.chmod(mode | stat.S_IWUSR if writable else mode & ~0o222)


def worktrees(root: Path) -> int:
    """How many worktrees git lists for the repo, the main one included."""
    return rt.git(root, "worktree", "list", "--porcelain").count("worktree ")


def test_read_only_repo(tmp: Path) -> None:
    """The checkout is made and moved while the repo's git folder cannot be written."""
    if os.geteuid() == 0:
        print("  skipped: root can write a read-only folder, so this case proves nothing")
        return
    paths = rt.repo(tmp)
    root = paths.root
    first = rt.git(root, "rev-parse", "HEAD")
    second = rt.commit(paths, "a second commit")
    set_writable(root / ".git", False)
    try:
        runtime_git.check_out(root, paths.checkout, first)
        at_first = rt.git(paths.checkout, "rev-parse", "HEAD") == first
        runtime_git.check_out(root, paths.checkout, second)
        at_second = rt.git(paths.checkout, "rev-parse", "HEAD") == second
        made = True
    except Exception as e:  # noqa: BLE001 - the case reports any failure
        made, at_first, at_second = False, False, False
        print(f"        {getattr(e, 'stderr', e)}")
    finally:
        set_writable(root / ".git", True)
    expect("a read-only repo still gives a checkout", made)
    expect("the checkout is at the first commit", at_first)
    expect("the checkout moves to the next commit", at_second)
    expect("the repo has no new worktree", worktrees(root) == 1, str(worktrees(root)))


def test_from_worktree(tmp: Path) -> None:
    """A harness checkout is itself a worktree; its commits reach the Verifier's clone."""
    paths = rt.repo(tmp)
    rt.git(paths.root, "branch", "session")
    session = tmp / "session"
    rt.git(paths.root, "worktree", "add", "-q", str(session), "session")
    tree = session / ".verifier" / "checkout"
    before = rt.commit(paths, "on the session branch", tree=session)
    runtime_git.check_out(session, tree, before)
    after = rt.commit(paths, "made after the clone", tree=session)
    runtime_git.check_out(session, tree, after)
    expect("a commit made after the clone is checked out",
           rt.git(tree, "rev-parse", "HEAD") == after)
    expect("the checkout holds that commit's files",
           "made after the clone" in (tree / "notes.md").read_text())
    expect("the clone's branch names the repo's tip",
           rt.git(tree, "rev-parse", "refs/heads/session") == after)
    (tree / ".verifier").mkdir()
    (tree / ".verifier" / "outputs").symlink_to(tmp, target_is_directory=True)
    status = rt.git(tree, "status", "--porcelain")
    expect("git sees no change in the checkout", status == "", repr(status))


def test_repo_exclude(tmp: Path) -> None:
    """A path the worker ignores only in info/exclude is ignored in the clone too."""
    paths = rt.repo(tmp)
    exclude = runtime_git.git_path(paths.root, "info/exclude")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("/data/")
    runtime_git.check_out(paths.root, paths.checkout, rt.git(paths.root, "rev-parse", "HEAD"))
    (paths.checkout / "data").mkdir()
    (paths.checkout / "data" / "x.csv").write_text("1\n")
    status = rt.git(paths.checkout, "status", "--porcelain")
    expect("the repo's info/exclude applies in the clone", status == "", repr(status))


def test_shallow_repo(tmp: Path) -> None:
    """A shallow repo lends no objects, so the fetch brings each new commit."""
    paths = rt.repo(tmp)
    rt.commit(paths, "a second commit")
    shallow = tmp / "shallow"
    rt.git(tmp, "clone", "-q", "--depth", "1", f"file://{paths.root}", str(shallow))
    tree = shallow / ".verifier" / "checkout"
    runtime_git.check_out(shallow, tree, rt.git(shallow, "rev-parse", "HEAD"))
    (shallow / "notes.md").write_text("new\n")
    rt.git(shallow, "commit", "-q", "-am", "made after the clone")
    new = rt.git(shallow, "rev-parse", "HEAD")
    try:
        runtime_git.check_out(shallow, tree, new)
        moved = rt.git(tree, "rev-parse", "HEAD") == new
    except subprocess.CalledProcessError as e:
        moved = False
        print(f"        {e.stderr}")
    expect("the clone of a shallow repo moves to a new commit", moved)


def test_rewritten_and_pruned(tmp: Path) -> None:
    """The commit the clone was at is gone from the repo; the clone is made again."""
    paths = rt.repo(tmp)
    root, tree = paths.root, paths.checkout
    old = rt.commit(paths, "to be rewritten")
    runtime_git.check_out(root, tree, old)
    rt.git(root, "reset", "-q", "--hard", "HEAD~1")
    new = rt.commit(paths, "the rewrite")
    rt.git(root, "reflog", "expire", "--expire=now", "--all")
    rt.git(root, "gc", "-q", "--prune=now")
    try:
        runtime_git.check_out(root, tree, new)
        moved = rt.git(tree, "rev-parse", "HEAD") == new
    except subprocess.CalledProcessError as e:
        moved = False
        print(f"        {e.stderr}")
    expect("after a rewrite and git gc, the checkout moves to the new tip", moved)


def test_old_worktree_checkout(tmp: Path) -> None:
    """A checkout made as a worktree by an earlier version becomes a clone."""
    paths = rt.repo(tmp)
    root, tree = paths.root, paths.checkout
    sha = rt.git(root, "rev-parse", "HEAD")
    rt.git(root, "worktree", "add", "-q", "--detach", str(tree), sha)
    (tree / "scratch.txt").write_text("written in the old checkout\n")
    try:
        runtime_git.check_out(root, tree, sha)
        refused = False
    except subprocess.CalledProcessError:
        refused = True
    expect("a worktree with a changed file is not deleted",
           refused and (tree / "scratch.txt").exists())
    (tree / "scratch.txt").unlink(missing_ok=True)
    runtime_git.check_out(root, tree, sha)
    expect("the old worktree is now a clone", (tree / ".git").is_dir())
    expect("the clone is at the commit", rt.git(tree, "rev-parse", "HEAD") == sha)
    expect("the repo no longer lists the old worktree", worktrees(root) == 1,
           str(worktrees(root)))


def main() -> int:
    """Run every case; return 1 if any failed."""
    for group in (test_read_only_repo, test_from_worktree, test_repo_exclude, test_shallow_repo,
                  test_rewritten_and_pruned, test_old_worktree_checkout):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    print("\n" + (f"{len(FAILED)} case(s) failed." if FAILED
                  else "All shared-checkout cases passed."))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
