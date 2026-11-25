"""Install the git hook that wakes the verifier when a tracked branch moves.

It also keeps `.verifier/` out of git. The hook is `reference-transaction` (git 2.28 or
later). All checkouts of a repo share one hooks folder, so one hook serves them all: it
reads the watch list in the shared git folder, one line of branch and inbox for each
watching checkout. A move of a tracked branch (a commit, a merge, a pull, a `git branch
-f`) writes one event naming the new commit into each matching inbox. The hook makes no
network call and never waits for the verifier, so git is never held up, and a move made
while the daemon is down is still delivered when it starts (runtime/daemon.py).
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from runtime.project import git
from utils import file_storage, structure

MARKER = "# installed by the verifier"
KEPT_SUFFIX = ".before-verifier"
HOOK_NAME = "reference-transaction"
REPLACED_HOOK_NAME = "post-commit"      # what earlier versions installed instead
MIN_GIT = (2, 28)                       # the first git with the reference-transaction hook

WATCHES = "verifier-watches"             # the watch list, in the shared git folder

HOOK = """#!/bin/sh
{marker}: tell the verifier when a tracked branch moves, without waiting for it.
hooks="$(dirname "$0")"
input="$(cat)"
if [ -x "$hooks/{name}{kept}" ]; then
  printf '%s\\n' "$input" | "$hooks/{name}{kept}" "$@" || exit $?
fi
[ "$1" = committed ] || exit 0
watches={watches}
[ -f "$watches" ] || exit 0
tab="$(printf '\\t')"
printf '%s\\n' "$input" | while read -r old new ref; do
  case "$new" in *[!0]*) ;; *) continue ;; esac
  while IFS="$tab" read -r watched d; do
    [ "$watched" = "$ref" ] && [ -d "$d" ] || continue
    name="$(date +%s%N)-branch_moved-$new.json"
    tmp="$d/.$name.tmp"
    printf '{{"kind": "branch_moved", "sha": "%s"}}' "$new" > "$tmp" && mv "$tmp" "$d/$name"
  done < "$watches"
done
exit 0
"""


def git_version() -> tuple[int, int]:
    """The installed git's major and minor version."""
    out = subprocess.run(["git", "--version"], capture_output=True, text=True,
                         check=True).stdout.split()
    major, minor = out[2].split(".")[:2]
    return int(major), int(minor)


def hooks_problem(root: Path) -> str | None:
    """Why the hook cannot go in the hooks folder git uses for `root`, or None if it can.

    A `core.hooksPath` among the files of a checkout of the repo (a relative path, for
    example, gives each checkout its own) lets a commit take in the hook. That is refused
    unless git ignores the hook there. The repo's git folder is always fine.
    """
    hook = git.git_path(root, f"hooks/{HOOK_NAME}").resolve()
    if hook.is_relative_to(watches_file(root).parent):
        return None
    for line in git.run(root, "worktree", "list", "--porcelain").splitlines():
        if not line.startswith("worktree "):
            continue
        top = Path(line.removeprefix("worktree ")).resolve()
        if hook.is_relative_to(top) and not git.output(top, "check-ignore", str(hook)):
            return (f"core.hooksPath puts the git hooks in {hook.parent}, among the files of "
                    f"{top}, where a commit can take in the hook. Unset core.hooksPath, set "
                    f"it to a folder outside every checkout, or have git ignore that folder")
    return None


def install(root: Path, branch: str) -> Path:
    """Watch `branch` for the working directory `root`, and return the hook's path.

    Idempotent. Puts the hook in the repo's hooks folder if it is not there, and sets this
    working directory's line in the watch list. Also removes the post-commit hook earlier
    versions installed, which would otherwise wake the verifier for commits on every
    branch. An existing hook of the same name is kept as
    `reference-transaction.before-verifier` and run first, with the same input; its exit
    status is kept, so it can still refuse a transaction. The hooks directory comes from
    git, so `core.hooksPath` is honoured, unless `hooks_problem` refuses it.
    """
    if git_version() < MIN_GIT:
        raise RuntimeError(f"git {'.'.join(map(str, git_version()))} has no reference-transaction "
                           f"hook; the verifier needs git {'.'.join(map(str, MIN_GIT))} or later")
    problem = hooks_problem(root)
    if problem:
        raise RuntimeError(problem)
    _remove(root, REPLACED_HOOK_NAME)
    hook = git.git_path(root, f"hooks/{HOOK_NAME}")
    hook.parent.mkdir(parents=True, exist_ok=True)
    text = hook.read_text(errors="ignore") if hook.exists() else None
    if text is not None and MARKER in text:
        _keep_old_watch(root, text)
    elif text is not None:
        kept = hook.with_name(hook.name + KEPT_SUFFIX)
        if kept.exists():
            raise FileExistsError(f"{kept} already exists; move it aside before installing")
        os.replace(hook, kept)
    # git can run the hook at any time, so it is never half written
    file_storage.AtomicallyWrittenFile(hook, mode=0o755).write_text(
        HOOK.format(marker=MARKER, name=HOOK_NAME, kept=KEPT_SUFFIX,
                    watches=shlex.quote(str(watches_file(root)))))
    _set_watch(root, f"refs/heads/{branch}")
    exclude_state_dir(root)
    return hook


def uninstall(root: Path) -> None:
    """Stop watching for `root`. With no watch left, remove the hook and put back the old one.

    Also removes the hook earlier versions installed.
    """
    if not _set_watch(root, None):           # no checkout watches any more
        _remove(root, HOOK_NAME)
    _remove(root, REPLACED_HOOK_NAME)


def watches_file(root: Path) -> Path:
    """The watch list, in the git folder that every checkout of the repo shares."""
    common = Path(git.run(root, "rev-parse", "--git-common-dir"))
    return (common if common.is_absolute() else root / common).resolve() / WATCHES


def _keep_old_watch(root: Path, text: str) -> None:
    """Move the inbox written into a hook of an earlier version to the watch list.

    Earlier versions wrote one checkout's inbox (`d=...`) into the hook itself. That
    checkout's tracked branch is in its state directory, next to the inbox.
    """
    found = re.search(r"^d=(.+)$", text, re.MULTILINE)
    inbox = found and Path(shlex.split(found.group(1))[0])
    if inbox and (inbox.parent / "branch").exists():
        _set_watch(root, f"refs/heads/{(inbox.parent / 'branch').read_text().strip()}", str(inbox))


def _set_watch(root: Path, ref: str | None, inbox: str | None = None) -> int:
    """Set the line for `root` in the watch list to `ref`, or remove it if `ref` is None.

    `inbox` names another working directory's inbox instead of the one of `root`. Lines
    of working directories whose state folder is gone are dropped too. Returns how many
    lines are left.
    """
    path = watches_file(root)
    inbox = inbox or str(root.resolve() / structure.STATE_DIR_NAME / "inbox")
    if "\t" in inbox or "\n" in inbox:
        raise ValueError(f"the verifier cannot watch a path with a tab or a line break: {inbox}")
    with file_storage.FileLock(path.with_name(WATCHES + ".lock")).held():
        lines = path.read_text().splitlines() if path.exists() else []
        keep = [line for line in lines if (d := line.partition("\t")[2]) != inbox
                and Path(d).parent.is_dir()]
        if ref is not None:
            keep.append(f"{ref}\t{inbox}")
        file_storage.AtomicallyWrittenFile(path).write_text("".join(f"{line}\n" for line in keep))
    return len(keep)


def _remove(root: Path, name: str) -> None:
    """Remove the verifier's hook called `name`, if there is one, and put back the one it kept."""
    hook = git.git_path(root, f"hooks/{name}")
    if hook.exists() and MARKER in hook.read_text(errors="ignore"):
        hook.unlink()
        kept = hook.with_name(hook.name + KEPT_SUFFIX)
        if kept.exists():
            os.replace(kept, hook)


def exclude_state_dir(root: Path) -> None:
    """List `.verifier/` in the repo's info/exclude, which changes no file the worker tracks."""
    exclude = git.git_path(root, "info/exclude")
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = f"/{structure.STATE_DIR_NAME}/"
    existing = exclude.read_text().splitlines() if exclude.exists() else []
    if line not in existing:
        with open(exclude, "a") as fh:
            fh.write(("\n" if existing and existing[-1] else "") + line + "\n")
