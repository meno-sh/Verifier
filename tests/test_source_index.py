#!/usr/bin/env python3
"""Check the number index and the source short list of core/methods/source_index.py.

The cases check that:
  - the index finds 71.3 for the claim number 0.713 and 80.25 for 80.2, and keeps the
    printed text of a JSON number;
  - it reads a changed file again, skips an unchanged one, and forgets a removed one;
  - a folder whose name ends in `.csv` gives no crash, and a broken file is reported;
  - the short list ranks a shared row above a single cell and a small row above a
    wide one, makes no shared row from a short number, gives a changed file for a
    number in more than 5 files, and no candidate for a number in more than 50 files;
  - BM25 finds a file by a column name;
  - `vcheck shortlist-sources` writes its file in a fixed form.
Returns 1 if any case fails.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import cli  # noqa: E402
from core.commands import shortlist_sources  # noqa: E402
from core.methods import source_index  # noqa: E402
from schema import inputs  # noqa: E402
from utils import structure, text_search  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def write(root: Path, rel: str, text: str) -> None:
    """Write one file of the project."""
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


def project(tmp: Path) -> tuple[Path, Path]:
    """A project with a CSV table, a JSON results file and a log, and its state folder."""
    root, state = tmp / "project", tmp / "project" / ".verifier"
    write(root, "results/table.csv", "model,accuracy,loss\nbase,71.3,0.412\ntuned,80.25,0.333\n")
    write(root, "results/run.json", json.dumps({"records": [
        {"model": "tuned", "accuracy": 0.8025, "calibration_error": 0.0517}]}))
    write(root, "logs/train.log", "epoch 1 calibration started\n")
    state.mkdir(parents=True)
    return root, state


def numbers(index: source_index.SourceIndex, text: str) -> list[tuple[str, str]]:
    """The (path, printed text) of each cell the index finds for `text`."""
    return [(c.path, c.text) for c in index.cells_with(inputs.PrintedFloat(text))]


def test_index(tmp: Path) -> None:
    """The index matches rounded values and reads only changed files."""
    root, state = project(tmp)
    (root / "folder.csv").mkdir()
    write(root, "broken.json", "{not json")
    index = source_index.SourceIndex(root, state)
    read, unreadable = index.update()
    expect("index: every regular file is read once, and a folder named .csv is left out",
           read == 4, str(read))
    expect("index: a broken file is reported with its reason",
           len(unreadable) == 1 and unreadable[0].startswith("broken.json: JSONDecodeError"),
           repr(unreadable))
    expect("index: 0.713 finds 71.3 in the table",
           numbers(index, "0.713") == [("results/table.csv", "71.3")],
           repr(numbers(index, "0.713")))
    expect("index: 80.2 finds 80.25 and the JSON number 0.8025 with its printed text",
           sorted(numbers(index, "80.2")) == [("results/run.json", "0.8025"),
                                              ("results/table.csv", "80.25")],
           repr(numbers(index, "80.2")))
    expect("index: an unchanged file is not read again", index.update()[0] == 0)
    write(root, "results/counts.csv", 'split,examples\ntrain,"1,300"\n')
    read, unreadable = index.update()
    expect("index: a number with a thousands separator is read, with no crash",
           read == 1 and not unreadable
           and numbers(index, "1300") == [("results/counts.csv", "1,300")],
           repr((read, unreadable, numbers(index, "1300"))))
    write(root, "results/table.csv", "model,accuracy\nbase,65.4\n")
    (root / "logs" / "train.log").unlink()
    again = index.update()[0]
    expect("index: a changed file is read again, and a removed file is forgotten",
           again == 1 and numbers(index, "71.3") == [] and numbers(index, "65.4")
           == [("results/table.csv", "65.4")] and "logs/train.log" not in index.documents(),
           f"{again} {numbers(index, '65.4')}")


def test_shortlist(tmp: Path) -> None:
    """A shared row ranks first; a common number gives nothing; BM25 finds a column."""
    root, state = project(tmp)
    write(root, "single.csv", "a\n0.412\n")
    index = source_index.SourceIndex(root, state)
    index.update()
    search = text_search.Bm25TextSearch(index.documents())
    got = index.shortlist(["71.3", "0.412"], "the accuracy and the loss of the base model")
    expect("shortlist: a row with two numbers of the claim ranks above a single cell",
           [(c.path, c.rule, c.row) for c in got]
           == [("results/table.csv", "shared-row", 1), ("single.csv", "informative-cell", 1)],
           repr(got))
    write(root, "wide.csv", "a,b,c,d,e,f\n71.3,0.412,1.11,2.22,3.33,4.44\n")
    write(root, "dates.csv", "year,score\n2025,0.412\n")
    index.update()
    got = index.shortlist(["71.3", "0.412", "2025"], "")
    expect("shortlist: the smaller row ranks first, and 2025 makes no shared row",
           [(c.path, c.rule) for c in got][:2] == [("results/table.csv", "shared-row"),
                                                   ("wide.csv", "shared-row")]
           and "dates.csv" not in [c.path for c in got if c.rule == "shared-row"], repr(got))
    for k in range(51):
        write(root, f"many/file{k}.csv", "x\n0.555\n")
    index.update()
    expect("shortlist: a number in more than 50 files gives no candidate",
           index.shortlist(["0.555"], "") == [])
    for k in range(6):
        write(root, f"some/file{k}.csv", "x\n0.666\n")
    index.update()
    got = index.shortlist(["0.666"], "", changed_files=["some/file3.csv"])
    expect("shortlist: a number in 6 files gives no cell, but a file the commit changed",
           [(c.path, c.rule) for c in got] == [("some/file3.csv", "changed-file")], repr(got))
    found = index.shortlist([], "the calibration error of the tuned model", search=search)
    expect("shortlist: BM25 finds a file by its JSON key",
           found and found[0].path == "results/run.json" and found[0].rule == "text-search",
           repr(found))
    expect("search: a log file is found by its first lines",
           "logs/train.log" in search.search("calibration started", 3),
           repr(search.search("calibration started", 3)))


def test_rarity_and_cap(tmp: Path) -> None:
    """A rarer number ranks first in a rule; each rule gives at most 5 files."""
    root, state = project(tmp)
    write(root, "common_a.csv", "x,y\n0.111,0.222\n")
    write(root, "common_b.csv", "x,y\n0.111,0.222\n")
    write(root, "rare.csv", "x,y\n0.111,0.345\n")
    for k in range(7):
        write(root, f"changed/f{k}.csv", "x\n0.777\n")
    index = source_index.SourceIndex(root, state)
    index.update()
    got = index.shortlist(["0.111", "0.222", "0.345"], "")
    expect("shortlist: the row whose rarest number is in the fewest files ranks first",
           [c.path for c in got if c.rule == "shared-row"][0] == "rare.csv", repr(got))
    changed = [f"changed/f{k}.csv" for k in range(7)]
    got = index.shortlist(["0.777"], "", changed_files=changed)
    expect("shortlist: one rule gives at most 5 files, and the list has no total limit",
           [c.rule for c in got] == ["changed-file"] * 5, repr(got))


def test_command(tmp: Path) -> None:
    """The command writes one short list for each claim with no source."""
    root, state = project(tmp)
    (state / "claim_register.json").write_text(json.dumps({"claims": [
        {"id": "base/accuracy", "value": 0.713, "statement": "The base model reaches 0.713."},
        {"id": "tuned/accuracy", "value": 0.8025, "statement": "The tuned model reaches 0.8025.",
         "source": {"kind": "artifact", "path": "results/run.json"}}]}))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["--state", str(state), "--root", str(root), "shortlist-sources"])
    data = json.loads((state / shortlist_sources.SHORTLISTS).read_text())
    expect("command: one short list for the claim with no source, in a fixed form",
           rc == 0 and data == {"commit": "", "claims": {"base/accuracy": [
               {"path": "results/table.csv", "rule": "informative-cell", "rank": 1, "row": 1,
                "column": "accuracy", "numbers": ["0.713"]}]}},
           out.getvalue() + json.dumps(data))


def git(root: Path, *args: str) -> str:
    """Run git in `root`, and give its output."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def test_changed_files(tmp: Path) -> None:
    """The changed files are relative to the project folder; the first commit gives all."""
    repo, project = tmp / "repo", tmp / "repo" / "paper"
    write(project, "results/run.csv", "a\n1\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    write(project, "results/run.csv", "a\n2\n")
    write(repo, "elsewhere.txt", "x")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two")
    expect("changed files: relative to the project folder, and none outside it",
           structure.changed_files(str(project), "HEAD") == ["results/run.csv"],
           repr(structure.changed_files(str(project), "HEAD")))
    first = git(repo, "rev-list", "--max-parents=0", "HEAD")
    expect("changed files: the first commit gives every file it added",
           structure.changed_files(str(project), first) == ["results/run.csv"],
           repr(structure.changed_files(str(project), first)))


def main() -> int:
    """Run every case in a new temporary folder."""
    for case in (test_index, test_shortlist, test_rarity_and_cap, test_command,
                 test_changed_files):
        print(case.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            case(Path(tmp))
    if FAILED:
        print(f"\n{len(FAILED)} case(s) failed: {FAILED}")
        return 1
    print("\nAll source index cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
