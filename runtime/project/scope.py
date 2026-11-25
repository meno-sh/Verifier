"""The notes a person selects at `verifier start`, and what the first round would read.

Each round reads candidates only from the selected files and folders (`claim_scope.json`
in the state directory). Before the first round, `select` shows how many candidates the
notes give, folder by folder, and asks which to keep. The counts come from the tracked
branch's tip, which the first round reads, not from the files the person has checked out.
With the counts comes an estimate of the first round's cost and time
(runtime/project/first_round_estimate.py).
"""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from runtime.project import first_round_estimate, git
from runtime.project.paths import StatePaths
from utils import claim_files, extract, structure

START, AGAIN, QUIT = "y", "s", "q"


class Cancelled(Exception):
    """The person ended the selection without starting."""


@dataclasses.dataclass(frozen=True)
class Counts:
    """The candidates a selection gives: per note, and from the commit messages."""
    notes: dict[str, int]
    commits: int

    @property
    def total(self) -> int:
        """Every candidate the first round would answer."""
        return sum(self.notes.values()) + self.commits


@contextlib.contextmanager
def branch_tree(root: Path, branch: str) -> Iterator[Path]:
    """A checkout of the tip of `branch`, made as the daemon's is (git.check_out), removed on exit.

    It is a clone that borrows the repo's objects, so nothing is written in the repo.
    """
    folder = Path(tempfile.mkdtemp(prefix="verifier-scope-"))
    tree = folder / "tree"
    try:
        git.check_out(root, tree, git.tip(root, branch))
        yield tree
    finally:
        shutil.rmtree(folder)


def count(tree: Path, within: tuple[str, ...]) -> Counts:
    """The candidates of the notes in `within`, and of every commit message, in `tree`."""
    notes: collections.Counter[str] = collections.Counter()
    commits = 0
    for c in structure.folder_candidates(str(tree), within=within):
        if c.get("kind") == extract.COMMIT:
            commits += 1
        else:
            notes[c["origin"]] += 1
    return Counts(dict(notes), commits)


def missing(tree: Path, within: tuple[str, ...]) -> list[str]:
    """The entries of `within` that are not a file or folder on the tracked branch."""
    return [p for p in within if p != structure.WHOLE and not (tree / p).exists()]


def by_folder(counts: Counts) -> list[tuple[str, int, int]]:
    """Per top-level folder (or note at the top): its path, notes and candidates."""
    notes: collections.Counter[str] = collections.Counter()
    found: collections.Counter[str] = collections.Counter()
    for rel, n in counts.notes.items():
        top = rel.split("/", 1)[0] + "/" if "/" in rel else rel
        notes[top] += 1
        found[top] += n
    return sorted((top, notes[top], found[top]) for top in notes)


def describe(counts: Counts) -> list[str]:
    """The lines that show what a selection gives, folder by folder."""
    rows = by_folder(counts)
    width = max([len(top) for top, _, _ in rows] + [len("commit messages")])
    lines = [f"  {top:<{width}}  {n:>5} note{'s' if n != 1 else ' '}  {c:>6} candidates"
             for top, n, c in rows]
    no_notes = " " * len("  00000 notes")
    lines.append(f"  {'commit messages':<{width}}{no_notes}  {counts.commits:>6} candidates")
    lines.append(f"  {'total':<{width}}{no_notes}  {counts.total:>6} candidates")
    return lines


def forecast(candidates: int) -> list[str]:
    """The lines that give the estimated cost and time of the first round."""
    e = first_round_estimate.estimate(candidates)
    parts = ", ".join(f"{name.replace('_', ' ')} {_dollars(*e.parts[name])}" for name in e.parts)
    return [f"Estimated cost of the first round: {_dollars(*e.dollars)} ({parts}).",
            f"Estimated time of the first round: {_hours(*e.minutes)}.",
            "These are rough guesses for the default models, from "
            f"{first_round_estimate.RATES_FILE.name}. A later commit costs less."]


def _dollars(low: float, high: float) -> str:
    """A cost, or a range of costs, in whole dollars."""
    return f"${low:,.0f}" if round(low) == round(high) else f"${low:,.0f} to ${high:,.0f}"


def _hours(low: float, high: float) -> str:
    """A time, or a range of times, in hours with one decimal."""
    return (f"{low / 60:.1f} hours" if round(low / 6) == round(high / 6)
            else f"{low / 60:.1f} to {high / 60:.1f} hours")


def select(paths: StatePaths, branch: str, given: Sequence[str] | None,
           ask: Callable[[str], str] | None = None) -> tuple[str, ...]:
    """Choose the files and folders to read claims from, save the choice, and return it.

    A saved selection is kept when `given` is None. Otherwise, with `ask` (a person at a
    terminal), the person sees the candidates of every note, then of their selection,
    and starts or selects again. Without `ask`, `given` is the selection.

    Args:
        paths: the working directory's state paths.
        branch: the tracked branch.
        given: the paths from `--claims-from`, or None.
        ask: reads one answer from the person, such as `input`; None with no terminal.

    Returns:
        The selection, as `claim_files.scope_paths` writes it.

    Raises:
        Cancelled: the person quit.
        ValueError: no selection is given or saved and nobody can be asked, or a path
            is outside the working directory or not on the tracked branch.
    """
    if given is None and (paths.state / claim_files.SCOPE).exists():
        return claim_files.read_scope(paths.state)
    if given is None and ask is None:
        raise ValueError("no notes are selected; give the files and folders to read claims "
                         "from with --claims-from (`--claims-from .` for every note)")
    with branch_tree(paths.root, branch) as tree:
        within = claim_files.scope_paths(paths.root, given) if given is not None else None
        while True:
            if within is None:
                within = _ask_paths(paths.root, tree, ask)
            if gone := missing(tree, within):
                raise ValueError(f"not on branch {branch}: {', '.join(gone)}")
            counts = count(tree, within)
            _show(f"Claims from {', '.join(within)} on branch {branch}:",
                  describe(counts) + forecast(counts.total))
            if ask is None:
                break
            answer = ask(f"Start with this selection? [{START} start / {AGAIN} select again / "
                         f"{QUIT} quit] ").strip().lower()
            if answer == START:
                break
            if answer == QUIT:
                raise Cancelled("nothing was started")
            if answer != AGAIN:
                print(f"answer {START}, {AGAIN} or {QUIT}")
                continue
            within = None
    claim_files.save_scope(paths.state, within)
    return within


def _ask_paths(root: Path, tree: Path, ask: Callable[[str], str]) -> tuple[str, ...]:
    """Show every note's candidates and read the person's selection."""
    every = count(tree, (structure.WHOLE,))
    _show("Every note on the tracked branch:", describe(every) + forecast(every.total))
    while True:
        text = ask("Read claims from (files or folders, separated by spaces; . for all): ")
        try:
            within = claim_files.scope_paths(root, text.split())
        except ValueError as e:
            print(f"error: {e}")
            continue
        if gone := missing(tree, within):
            print(f"error: not on the tracked branch: {', '.join(gone)}")
            continue
        return within


def _show(title: str, lines: list[str]) -> None:
    """Print a title and its lines."""
    print(title)
    for line in lines:
        print(line)
    sys.stdout.flush()
