"""Files that several processes share: locks, whole-file writes, JSON files and JSON lines.

The daemon, `vcheck`, `verifier`, the git hook and the agent's shell are different
processes. They share files through the classes here, so no other module locks a file
or moves a new file into place.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO

# One append at a time inside this process; FileLock does it across processes.
_APPEND_LOCK = threading.Lock()
# The open lock files of the ProcessLocks this process holds. A lock file that closes
# frees its lock, so they stay open here until the process ends.
_HELD_PROCESS_LOCKS: dict[Path, IO[str]] = {}


class FileLock:
    """An exclusive lock on a lock file, which every process that uses the file respects."""

    def __init__(self, path: Path) -> None:
        """The lock of the lock file `path`. The file is made at the first use."""
        self.path = path

    @classmethod
    def in_temporary_folder(cls, purpose: str, guarded: Path) -> FileLock:
        """A lock for `guarded` whose lock file is in the system's temporary folder.

        Use it when the folder of `guarded` can be in a git repository, where a lock file
        would show. The name of the lock file comes from `purpose` and the path of `guarded`.
        """
        digest = hashlib.sha256(str(guarded.resolve()).encode()).hexdigest()[:16]
        return cls(Path(tempfile.gettempdir()) / f"verifier-{purpose}-{digest}.lock")

    @contextlib.contextmanager
    def held(self) -> Iterator[None]:
        """Hold the lock for the block, and wait for it when another process holds it."""
        with self._opened() as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield

    @contextlib.contextmanager
    def held_if_free(self) -> Iterator[bool]:
        """Hold the lock for the block if no other process holds it. Gives True if held."""
        with self._opened() as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True

    def _opened(self) -> IO[str]:
        """The lock file, opened with no change to its text."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.path, "a")


class ProcessLock:
    """A lock that one process holds for its whole life, with its pid in the lock file."""

    def __init__(self, path: Path) -> None:
        """The lock of the lock file `path`."""
        self.path = path

    def take_for_this_process(self) -> bool:
        """Take the lock until this process ends, and write its pid. False if another holds it.

        The lock stays held also when this object is gone.
        """
        assert self.path.resolve() not in _HELD_PROCESS_LOCKS, \
            f"this process already holds {self.path}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115 - the process keeps the lock open
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        fh.truncate(0)
        fh.write(str(os.getpid()))
        fh.flush()
        _HELD_PROCESS_LOCKS[self.path.resolve()] = fh
        return True

    def holder_pid(self) -> int | None:
        """The pid of the process that holds the lock, or None when no process holds it.

        Raises:
            RuntimeError: a process holds the lock but has not written its pid yet.
        """
        if not self.path.exists():
            return None
        with open(self.path) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                text = fh.read().strip()
                if not text.isdigit() or int(text) <= 0:
                    raise RuntimeError(f"{self.path} is locked but holds no pid: "
                                       f"{text!r}") from None
                return int(text)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return None


class AtomicallyWrittenFile:
    """A file that a reader always sees whole, never half written.

    Each write makes a new file in the same folder and moves it into place. The new file
    keeps the mode of the old one, or gets `mode`, or the usual mode for a new file.
    """

    def __init__(self, path: Path, mode: int | None = None) -> None:
        """The file `path`. `mode`, when given, is the mode of every write."""
        self.path = path
        self.mode = mode

    def write_text(self, text: str) -> None:
        """Replace the file with `text`."""
        self.write_bytes(text.encode())

    def write_bytes(self, data: bytes) -> None:
        """Replace the file with `data`."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = (self.mode if self.mode is not None
                else self.path.stat().st_mode & 0o777 if self.path.exists()
                else 0o666 & ~_umask())
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.",
                                   suffix=".tmp")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read_text(self) -> str:
        """The text of the file.

        Raises:
            FileNotFoundError: there is no file.
        """
        return self.path.read_text()


class JsonFile:
    """One JSON file, written whole, in one fixed format, with a newline at the end."""

    def __init__(self, path: Path, *, indent: int | None = None, sort_keys: bool = False,
                 ensure_ascii: bool = True, lock: FileLock | None = None,
                 mode: int | None = None) -> None:
        """The JSON file `path`, written with the json.dumps settings given.

        `lock` is needed only for `change`. `mode` is as in AtomicallyWrittenFile.
        """
        self.path = path
        self.mode = mode
        self.indent = indent
        self.sort_keys = sort_keys
        self.ensure_ascii = ensure_ascii
        self.lock = lock

    def exists(self) -> bool:
        """Whether the file exists."""
        return self.path.exists()

    def read(self) -> object:
        """The JSON value of the file.

        Raises:
            FileNotFoundError: there is no file.
            json.JSONDecodeError: the file is not JSON.
        """
        return json.loads(self.path.read_text())

    def write(self, value: object) -> None:
        """Replace the file with `value`."""
        AtomicallyWrittenFile(self.path, self.mode).write_text(
            json.dumps(value, indent=self.indent, sort_keys=self.sort_keys,
                       ensure_ascii=self.ensure_ascii) + "\n")

    def change(self, update: Callable[[object | None], object]) -> None:
        """Read the file, and write what `update` gives, with the lock held for both.

        `update` gets None when there is no file. Two processes that change the file at
        the same time change it one after the other, so neither loses the other's change.
        """
        assert self.lock is not None, f"{self.path} has no lock, so it cannot be changed"
        with self.lock.held():
            self.write(update(self.read() if self.exists() else None))


class JsonFileFolder:
    """A folder with one JSON file for each item, each written whole.

    An item's name is its file name. A file whose name starts with "." is not an item:
    it is a file that a writer has not moved into place yet.
    """

    def __init__(self, folder: Path, *, indent: int | None = None) -> None:
        """The items in `folder`, written with `indent`."""
        self.folder = folder
        self.indent = indent

    def file(self, name: str) -> JsonFile:
        """The JSON file of one item."""
        return JsonFile(self.folder / name, indent=self.indent)

    def names(self) -> list[str]:
        """The names of the items, sorted; none when the folder does not exist."""
        if not self.folder.exists():
            return []
        return sorted(p.name for p in self.folder.iterdir()
                      if p.is_file() and p.suffix == ".json" and not p.name.startswith("."))

    def write(self, name: str, value: object) -> Path:
        """Write one item, and give its path."""
        self.file(name).write(value)
        return self.folder / name

    def read(self, name: str) -> object:
        """The JSON value of one item.

        Raises:
            FileNotFoundError: there is no such item.
            json.JSONDecodeError: the item is not JSON.
        """
        return self.file(name).read()

    def move_to(self, name: str, other: JsonFileFolder) -> None:
        """Move one item into `other`, in one step."""
        other.folder.mkdir(parents=True, exist_ok=True)
        os.replace(self.folder / name, other.folder / name)

    def remove(self, name: str) -> None:
        """Remove one item."""
        (self.folder / name).unlink()


class DamagedJsonLineError(ValueError):
    """A whole line of a JSON lines file is not a JSON object."""

    def __init__(self, path: Path, line_number: int, reason: str) -> None:
        """The line `line_number` (from 1) of `path`, and why it cannot be read."""
        super().__init__(f"{path}:{line_number}: {reason}")
        self.path = path
        self.line_number = line_number


class AppendOnlyJsonLinesFile:
    """Reads and appends the rows of one JSON lines file. Nothing is ever rewritten.

    Each row is appended in one write of one line, so rows that two processes append at
    the same time do not mix. A command that reads the file and then appends to it, such
    as one that gives the next id, holds `lock` for both, so two commands at once cannot
    give the same id.
    """

    def __init__(self, path: Path, lock: FileLock | None = None, *, sort_keys: bool = False,
                 sync: bool = False) -> None:
        """The rows of `path`.

        Args:
            path: the JSON lines file.
            lock: the lock for a read and then an append; needed only for `holding_lock`.
            sort_keys: write the keys of each row sorted, else in the order of the row.
            sync: make each append reach the disk before `append_row` returns.
        """
        self.path = path
        self.lock = lock
        self.sort_keys = sort_keys
        self.sync = sync

    def read_rows(self, on_damaged_line: Callable[[DamagedJsonLineError], None] | None = None
                  ) -> list[dict[str, object]]:
        """Every row, oldest first, as `read_rows_with_line_numbers` reads them."""
        return [row for _, row in self.read_rows_with_line_numbers(on_damaged_line)]

    def read_rows_with_line_numbers(
            self, on_damaged_line: Callable[[DamagedJsonLineError], None] | None = None
    ) -> list[tuple[int, dict[str, object]]]:
        """Every row with its line number (from 1), oldest first; none when there is no file.

        Each row is a JSON object. A last line with no newline is left out, because a
        writer is still appending it.

        Args:
            on_damaged_line: gets each whole line that is not JSON, and the other rows are
                still read. When None, such a line raises.

        Raises:
            DamagedJsonLineError: a whole line is not JSON, and `on_damaged_line` is None.
        """
        if not self.path.exists():
            return []
        lines = self.path.read_bytes().split(b"\n")[:-1]    # the part after the last newline
        rows = []
        for i, raw in enumerate(lines, start=1):
            try:
                row = json.loads(raw)
            except ValueError as e:
                problem = f"not JSON ({e})"
            else:
                if isinstance(row, dict):
                    rows.append((i, row))
                    continue
                problem = "not a JSON object"
            damaged = DamagedJsonLineError(self.path, i, problem)
            if on_damaged_line is None:
                raise damaged
            on_damaged_line(damaged)
        return rows

    def append_row(self, row: dict[str, object]) -> None:
        """Append one row, as one line.

        Raises:
            ValueError: json cannot write a value of the row; nothing is written.
            OSError: the file cannot be written.
        """
        data = (json.dumps(row, sort_keys=self.sort_keys, allow_nan=False) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            with _APPEND_LOCK:
                fcntl.flock(fd, fcntl.LOCK_EX)
                written = os.write(fd, data)
                if written != len(data):
                    raise OSError(f"wrote {written} of {len(data)} bytes to {self.path}")
                if self.sync:
                    os.fsync(fd)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def holding_lock(self) -> Iterator[None]:
        """Hold the lock for the block."""
        assert self.lock is not None, f"{self.path} has no lock"
        with self.lock.held():
            yield


class AppendOnlyJsonLinesFolder:
    """The append-only JSON lines files of one folder, which share one lock.

    A command that reads the files and then appends to them holds the lock for both, so
    two commands at once cannot give out the same id. Each row is written with its keys
    sorted.
    """

    def __init__(self, folder: Path, lock: FileLock) -> None:
        """The files in `folder`, all guarded by `lock`."""
        self.folder = folder
        self.lock = lock

    def file(self, name: str) -> AppendOnlyJsonLinesFile:
        """The file `name` of the folder."""
        return AppendOnlyJsonLinesFile(self.folder / name, self.lock, sort_keys=True)

    @contextlib.contextmanager
    def holding_lock(self) -> Iterator[None]:
        """Hold the lock of the folder for the block."""
        with self.lock.held():
            yield


def _umask() -> int:
    """The process's umask; reading it means setting it, so it is set back at once."""
    mask = os.umask(0)
    os.umask(mask)
    return mask
