"""The numbers of the data files of a project, and a short list of likely sources for a claim.

`vcheck shortlist-sources` uses them. The auditing agent reads the short list of each
claim with no source first, and confirms or rejects each file. It searches by hand only
when the list is empty. The index does not replace the agent.

The index is a SQLite file in `.verifier/cache/`, outside git. A file is read again only
when its checksum changed.
"""
from __future__ import annotations

import contextlib
import csv
import dataclasses
import json
import os
import sqlite3
from collections.abc import Callable, Collection, Iterator
from pathlib import Path
from typing import ClassVar

from schema import inputs
from utils import checksums, extract, structure, text_search
from utils.text import first_part

INDEX_FILE = Path("cache") / "number_index.sqlite"      # in the state folder
LOG_SUFFIXES = (".log", ".txt")         # their first lines go to the text search only
LOG_LINES = 20
MIN_SIGNIFICANT_DIGITS = 2              # a number with fewer is not stored
INFORMATIVE_DIGITS = 3                  # a number with decimals and this many is informative
MOST_FILES_FOR_ONE_CELL = 5             # an informative number in more files gives no cell
MOST_FILES_FOR_A_NUMBER = 50            # a number in more files gives no candidate at all
PER_RULE = 5                            # the most files that one rule gives
# the rarity of a file with no informative number, which puts it after the others
NO_INFORMATIVE_NUMBER = 10**9
SCHEMA = """
create table if not exists files (path text primary key, sha256 text not null,
                                  words text not null);
create table if not exists numbers (path text not null, row integer, column text,
                                    text text not null, value real not null,
                                    decimals integer not null);
create index if not exists numbers_by_value on numbers (decimals, value);
create index if not exists numbers_by_path on numbers (path);
"""


def significant_digits(text: str) -> int:
    """The significant digits of a printed number: 3 for 0.713, 3 for 100, 1 for 0.05."""
    mantissa = text.lower().split("e")[0].lstrip("+-−").replace(".", "")
    return len(mantissa.lstrip("0"))


def is_informative(text: str) -> bool:
    """Whether a printed number is rare enough to point to a file.

    It must have 3 or more significant digits, and decimals. An integer, such as a year
    or a count, matches too much by chance.
    """
    return (significant_digits(text) >= INFORMATIVE_DIGITS
            and inputs.PrintedFloat.count_printed_decimals(inputs.PrintedFloat(text)) > 0)


@dataclasses.dataclass(frozen=True)
class SourceCandidate:
    """One file that can be the source of a claim, and why."""
    RULES: ClassVar[tuple[str, ...]] = ("shared-row", "informative-cell", "changed-file",
                                        "text-search")
    path: str                   # relative to the project root
    rule: str                   # the first rule that found it, one of RULES
    rank: int                   # the place in the short list, from 1
    row: int | None = None      # the row (from 1) of the numbers, when a number found it
    column: str | None = None   # the column or JSON key of the number, when it has one
    numbers: tuple[str, ...] = ()       # the numbers of the claim that the file holds

    def to_json(self) -> dict[str, object]:
        """The candidate as the short list file holds it."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class _Cell:
    """One number of a data file, where it is, as the index holds it."""
    path: str
    row: int | None
    column: str | None
    text: str


class SourceIndex:
    """The numbers and the words of the data files of one project."""

    def __init__(self, root: Path, state_dir: Path) -> None:
        """The index of the project in `root`, kept in its state folder `state_dir`."""
        self.root = root
        self.path = state_dir / INDEX_FILE

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """The index database, with its tables made if missing, for one transaction.

        The changes are committed at the end of the block, and the database is closed.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path)
        try:
            with con:
                con.executescript(SCHEMA)
                yield con
        finally:
            con.close()

    def update(self) -> tuple[int, list[str]]:
        """Read again each data and log file whose checksum changed; forget removed files.

        Only regular files are read, so a folder whose name ends in `.csv` is left out.
        Files over structure.MAX_BYTES are left out too, and listed with the files that could
        not be read.

        Returns:
            (how many files were read, the files that could not be read, with the reason).
            A file that could not be read is in the index with no number.
        """
        files = {rel for rel in structure.data_file_paths(str(self.root),
                                                          (*extract.DATA_SUFFIXES,
                                                           *LOG_SUFFIXES))
                 if os.path.isfile(self.root / rel)}
        found = {rel for rel in files if os.path.getsize(self.root / rel) <= structure.MAX_BYTES}
        read = 0
        unreadable = [f"{rel}: over {structure.MAX_BYTES} bytes, left out of the index"
                      for rel in sorted(files - found)]
        with self._connect() as con:
            known = dict(con.execute("select path, sha256 from files").fetchall())
            for rel in sorted(set(known) - found):
                con.execute("delete from files where path = ?", (rel,))
                con.execute("delete from numbers where path = ?", (rel,))
            for rel in sorted(found):
                digest = checksums.sha256_of_file(str(self.root / rel))
                if known.get(rel) == digest:
                    continue
                try:
                    cells, columns = self._read_file(rel)
                except (OSError, ValueError, csv.Error) as e:
                    unreadable.append(f"{rel}: {type(e).__name__}: {first_part(str(e), 200)}")
                    cells, columns = [], []
                con.execute("delete from numbers where path = ?", (rel,))
                con.execute("insert or replace into files values (?, ?, ?)",
                            (rel, digest, " ".join([rel, *columns])))
                con.executemany("insert into numbers values (?, ?, ?, ?, ?, ?)",
                                # PrintedFloat reads a thousands separator ("1,300")
                                [(c.path, c.row, c.column, c.text,
                                  float(inputs.PrintedFloat(c.text)),
                                  inputs.PrintedFloat.count_printed_decimals(
                                      inputs.PrintedFloat(c.text)))
                                 for c in cells])
                read += 1
        return read, unreadable

    def _read_file(self, rel: str) -> tuple[list[_Cell], list[str]]:
        """The numbers of one file with 2 or more significant digits, and its words.

        The words are the column names or JSON keys of a data file, and the first lines
        of a log file.

        Raises:
            OSError, ValueError, csv.Error: the file cannot be read.
        """
        path = self.root / rel
        if rel.endswith(LOG_SUFFIXES):
            with open(path, errors="replace") as fh:
                return [], [line.strip() for _, line in zip(range(LOG_LINES), fh, strict=False)]
        found = list(_numbers_of_data_file(path))
        cells = [_Cell(rel, row, column, text) for row, column, text in found
                 if significant_digits(text) >= MIN_SIGNIFICANT_DIGITS]
        columns = sorted({column for _, column, _ in found if column})
        return cells, columns

    def documents(self) -> dict[str, str]:
        """The words of each file, keyed by its path: the documents of the text search."""
        with self._connect() as con:
            return dict(con.execute("select path, words from files").fetchall())

    def cells_with(self, number: inputs.PrintedFloat) -> list[_Cell]:
        """The cells whose number could be `number`, printed at least as precisely.

        A cell matches when PrintedFloat.could_be_same_value is true and its number has
        at least the precision of `number` at that scale. A source is never less precise
        than the number printed from it: the cell 20 is not the source of 0.2029.

        For each scale and each count of printed decimals, the query asks only for the
        values within half a unit of that count around the scaled interval of `number`.
        """
        out = []
        low, high = number.interval()
        with self._connect() as con:
            counts = [d for (d,) in con.execute("select distinct decimals from numbers")]
            for scale in inputs.PrintedFloat.SCALES:
                for decimals in counts:
                    half = 0.5 * 10.0 ** -decimals
                    if half > (high - low) / 2 * scale * (1 + 1e-9):
                        continue            # the cell is less precise than the claim
                    rows = con.execute(
                        "select path, row, column, text from numbers where decimals = ? "
                        "and value between ? and ?",
                        (decimals, low * scale - half, high * scale + half)).fetchall()
                    out += [_Cell(*r) for r in rows
                            if number.could_be_same_value(inputs.PrintedFloat(r[3]))]
        return sorted(set(out), key=lambda c: (c.path, c.row or 0, c.column or ""))

    def row_sizes(self, rows: list[tuple[str, int]]) -> dict[tuple[str, int], int]:
        """How many numbers the index holds in each row, given as (path, row)."""
        with self._connect() as con:
            return {(path, row): con.execute(
                "select count(*) from numbers where path = ? and row = ?",
                (path, row)).fetchone()[0] for path, row in rows}

    def shortlist(self, numbers: Collection[str], query: str,
                  changed_files: Collection[str] = (),
                  search: text_search.TextSearch | None = None) -> list[SourceCandidate]:
        """The likely source files of one claim, best first: at most 5 for each rule.

        The rules, in rank order:
          1. shared-row: a row that holds two or more informative numbers of the claim
             (is_informative).
          2. informative-cell: a cell that holds an informative number of the claim, when
             5 files or fewer hold it.
          3. changed-file: a file of `changed_files` that holds a number of the claim.
          4. text-search: a file that `search` finds for `query`, in its own order.
        In rules 1 to 3, a file whose rarest informative number is in fewer files of the
        project ranks higher. A file that an earlier rule gave is not given again. A
        number in more than 50 files gives no candidate by rules 2 and 3. It still counts
        for rule 1, because two numbers of the claim in one row are rare by chance.

        Args:
            numbers: the printed numbers of the claim, such as its value.
            query: the words of the claim.
            changed_files: the files that the checked commit changed.
            search: the text search; None for no text search.
        """
        every: dict[str, list[_Cell]] = {
            text: self.cells_with(inputs.PrintedFloat(text)) for text in sorted(set(numbers))
            if significant_digits(text) >= MIN_SIGNIFICANT_DIGITS}
        files_holding = {text: len({c.path for c in found}) for text, found in every.items()}
        # a number in many files says little alone; in one row with another, it still counts
        cells = {text: found for text, found in every.items()
                 if files_holding[text] <= MOST_FILES_FOR_A_NUMBER}

        def rarity(held: Collection[str]) -> tuple[int, int]:
            """The sort key of a candidate: its rarest informative number, then its rarest."""
            informative = [files_holding[t] for t in held if is_informative(t)]
            return (min(informative, default=NO_INFORMATIVE_NUMBER),
                    min(files_holding[t] for t in held))

        rows: dict[tuple[str, int], dict[str, _Cell]] = {}
        for text, found in every.items():
            if is_informative(text):
                for c in found:
                    if c.row is not None:
                        rows.setdefault((c.path, c.row), {}).setdefault(text, c)
        shared = {key: held for key, held in rows.items() if len(held) >= 2}
        sizes = self.row_sizes(list(shared))
        by_rule: list[tuple[str, list[tuple[str, _Cell | None, tuple[str, ...]]]]] = [
            # then more numbers of the claim, then the smaller row, where chance matches less
            ("shared-row", [(key[0], next(iter(held.values())), tuple(sorted(held)))
                            for key, held in sorted(
                                shared.items(), key=lambda kv: (rarity(kv[1]), -len(kv[1]),
                                                                sizes[kv[0]], kv[0]))]),
            ("informative-cell", self._files_by_rarity(
                {t: f for t, f in cells.items()
                 if is_informative(t) and files_holding[t] <= MOST_FILES_FOR_ONE_CELL},
                rarity)),
            ("changed-file", self._files_by_rarity(
                {t: [c for c in f if c.path in changed_files] for t, f in cells.items()},
                rarity)),
            ("text-search", [(path, None, ()) for path in
                             (search.search(query, PER_RULE) if search is not None else [])]),
        ]
        chosen: dict[str, SourceCandidate] = {}
        for rule, ranked in by_rule:
            given = 0
            for path, cell, held in ranked:
                if path in chosen or given >= PER_RULE:
                    continue
                chosen[path] = SourceCandidate(path=path, rule=rule, rank=len(chosen) + 1,
                                               row=cell.row if cell else None,
                                               column=cell.column if cell else None,
                                               numbers=held)
                given += 1
        return list(chosen.values())

    @staticmethod
    def _files_by_rarity(cells: dict[str, list[_Cell]],
                         rarity: Callable[[Collection[str]], tuple[int, int]]
                         ) -> list[tuple[str, _Cell | None, tuple[str, ...]]]:
        """Each file of `cells` with its first cell and its numbers, the rarest first."""
        held: dict[str, dict[str, _Cell]] = {}
        for text, found in cells.items():
            for c in found:
                held.setdefault(c.path, {}).setdefault(text, c)
        return [(path, next(iter(numbers.values())), tuple(sorted(numbers)))
                for path, numbers in sorted(held.items(),
                                            key=lambda kv: (rarity(kv[1]), kv[0]))]


def _numbers_of_data_file(path: Path) -> Iterator[tuple[int | None, str | None, str]]:
    """Each number of a data file as (row from 1, column or key, the printed text).

    A JSON number keeps its printed text, so 0.50 stays 0.50. A row of a JSON file is a
    record of its `records` or `rows` list, or of its top-level list.

    Raises:
        OSError, ValueError, csv.Error: the file cannot be read.
    """
    name = path.name
    if name.endswith(".csv"):
        with open(path, newline="", errors="replace") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            for i, row in enumerate(reader, start=1):
                for j, cell in enumerate(row):
                    for text in extract.numbers(cell):
                        yield i, header[j] if j < len(header) else None, text
        return
    if name.endswith(".jsonl"):
        with open(path, errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if line.strip():
                    for key, text in _json_numbers(_load_json(line), ""):
                        yield i, key, text
        return
    with open(path, errors="replace") as fh:
        data = _load_json(fh.read())
    records = (data.get("records") or data.get("rows")) if isinstance(data, dict) else data
    if isinstance(records, list):
        for i, record in enumerate(records, start=1):
            for key, text in _json_numbers(record, ""):
                yield i, key, text
    else:
        for key, text in _json_numbers(data, ""):
            yield None, key, text


class _JsonNumber(str):
    """A number of a JSON file, kept as its printed text."""

    __slots__ = ()


def _load_json(text: str) -> object:
    """The JSON of `text`, with each number kept as its printed text (a _JsonNumber).

    NaN and Infinity give None, because they are no number to match.
    """
    return json.loads(text, parse_float=_JsonNumber, parse_int=_JsonNumber,
                      parse_constant=lambda name: None)


def _json_numbers(value: object, key: str) -> Iterator[tuple[str | None, str]]:
    """Each number in a JSON value as (its dotted key, or None at the top, its text).

    A string that is one whole printed number counts as a number too.
    """
    if isinstance(value, _JsonNumber):
        yield key or None, str(value)
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _json_numbers(v, f"{key}.{k}" if key else str(k))
    elif isinstance(value, list):
        for v in value:
            yield from _json_numbers(v, key)
    elif isinstance(value, str) and extract.numbers(value) == [value.strip()]:
        yield key or None, value.strip()
