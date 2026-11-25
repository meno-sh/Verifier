"""The git commands the runtime and its extensions run, in one place."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from utils import structure

# Who the daemon's commits to the state directory's own repo are by.
STATE_IDENTITY = {"GIT_AUTHOR_NAME": "verifier", "GIT_AUTHOR_EMAIL": "verifier@localhost",
                  "GIT_COMMITTER_NAME": "verifier", "GIT_COMMITTER_EMAIL": "verifier@localhost"}


def run_in_state(state: Path, *args: str) -> None:
    """Run git in the state directory as the verifier, and fail loudly."""
    subprocess.run(["git", "-C", str(state), *args], check=True, capture_output=True,
                   env={**os.environ, **STATE_IDENTITY})


def output(cwd: Path, *args: str) -> str:
    """Run git and return its trimmed output, or "" if it failed."""
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def head(root: Path) -> str:
    """The working directory's current commit, or "" before the first commit."""
    return output(root, "rev-parse", "HEAD")


def run(cwd: Path, *args: str) -> str:
    """Run git in the worker's repo, fail loudly, and return its trimmed output."""
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def tip(root: Path, branch: str) -> str:
    """The commit the branch points at. Raises if there is no such branch."""
    return run(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")


def has_branch(root: Path, branch: str) -> bool:
    """True if the branch exists and points at a commit, so tip() can read it."""
    out = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                          f"refs/heads/{branch}^{{commit}}"], capture_output=True, text=True)
    if out.returncode not in (0, 1):
        raise subprocess.CalledProcessError(out.returncode, out.args, out.stdout, out.stderr)
    return out.returncode == 0


def incoming(root: Path, base: str, new: str) -> list[str]:
    """The commits reachable from `new` and not from `base`, every parent before its child."""
    out = run(root, "rev-list", "--reverse", "--topo-order", f"{base}..{new}")
    return out.split() if out else []


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    """True if `older` is `newer` or one of its ancestors."""
    out = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer],
                         capture_output=True, text=True)
    if out.returncode not in (0, 1):
        raise subprocess.CalledProcessError(out.returncode, out.args, out.stdout, out.stderr)
    return out.returncode == 0


def git_path(root: Path, name: str) -> Path:
    """A path inside the repo's git directory, as git itself resolves it."""
    path = Path(run(root, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else root / path


def check_out(root: Path, tree: Path, sha: str) -> None:
    """Put the detached checkout `tree` of the worker's repo at `sha`, creating it if missing.

    The checkout is a clone that borrows the objects of the worker's repo
    (`git clone --shared`), not a worktree. A worktree writes into the repo's git
    folder, and the auditing agent may run as a user who can only read that folder.

    Each call brings the clone in step with the repo before it moves:
    - its branches and tags are fetched again, so they name what the repo's do;
      the fetch also copies objects the clone cannot borrow, as from a shallow repo;
    - its info/exclude is the repo's, plus `.verifier/`;
    - a clone whose HEAD the repo no longer has (the branch was rewritten, then git
      gc ran) is made again. Only the daemon and its jobs write in the clone.

    A checkout that an earlier version made as a worktree is made again as a clone.
    It fails if a file in that worktree was changed, and so does a move of the clone,
    rather than overwrite the change.
    """
    if (tree / ".git").is_file():           # a worktree, made by an earlier version
        _remove_worktree(root, tree)
    elif tree.exists() and not output(tree, "rev-parse", "-q", "--verify", "HEAD^{tree}"):
        shutil.rmtree(tree)
    source = str(root.resolve())
    if not tree.exists():
        run(root, "clone", "-q", "--shared", "--no-checkout", source, str(tree))
    exclude = git_path(root, "info/exclude")
    text = exclude.read_text() if exclude.exists() else ""
    tree_exclude = git_path(tree, "info/exclude")
    tree_exclude.parent.mkdir(exist_ok=True)
    separator = "\n" if text and not text.endswith("\n") else ""
    tree_exclude.write_text(f"{text}{separator}/{structure.STATE_DIR_NAME}/\n")
    run(tree, "fetch", "-q", "--prune", "--update-head-ok", source,
        "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    run(tree, "checkout", "-q", "--detach", sha)


def _remove_worktree(root: Path, tree: Path) -> None:
    """Delete a worktree checkout, refusing if a file in it was changed.

    Its record in the worker's repo is pruned when this user may write there. When
    not, the record stays: harmless, and `git worktree prune` by the repo's owner
    removes it once the path no longer holds a worktree.
    """
    changed = run(tree, "status", "--porcelain")
    if changed:
        raise subprocess.CalledProcessError(
            1, ["git", "status"], stderr=f"{tree} is a worktree with changed files; "
            f"move them away, then delete it:\n{changed}")
    shutil.rmtree(tree)
    output(root, "worktree", "prune")
