#!/usr/bin/env python3
"""Check the shared files of utils/file_storage.py, also with several processes at once.

The cases check that:
  - a FileLock makes processes that change one JSON file change it one after the other;
  - a FileLock that another process holds is not free, and is free after it ends;
  - a ProcessLock gives the pid of the process that holds it, and None after it ends;
  - an atomically written file keeps the mode of the old file, or takes the mode given,
    and leaves no temporary file;
  - a JSON lines file leaves out a last line with no newline, and reports a damaged line;
  - rows that many processes append at once are all there, one on each line;
  - a JSON file folder lists only its items, and moves an item to another folder;
  - the JSON lines files of one folder share its lock, and write their keys sorted.
Returns 1 if any case fails.
"""
import json
import multiprocessing
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import file_storage  # noqa: E402

FAILED: list[str] = []
WRITERS = 8
CHANGES = 20


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def count_up(path: Path, lock_path: Path) -> None:
    """Add 1 to the counter in the JSON file at `path` CHANGES times, with the lock."""
    counter = file_storage.JsonFile(path, lock=file_storage.FileLock(lock_path))
    for _ in range(CHANGES):
        counter.change(lambda value: {"n": (0 if value is None else value["n"]) + 1})


def append_rows(path: Path, writer: int) -> None:
    """Append CHANGES rows to the JSON lines file at `path`."""
    rows = file_storage.AppendOnlyJsonLinesFile(path)
    for i in range(CHANGES):
        rows.append_row({"writer": writer, "i": i, "text": "x" * 500})


def hold_lock(lock_path: Path, held: "multiprocessing.synchronize.Event",
              done: "multiprocessing.synchronize.Event") -> None:
    """Hold the FileLock at `lock_path` until `done` is set."""
    with file_storage.FileLock(lock_path).held():
        held.set()
        done.wait(30)


def hold_process_lock(lock_path: Path, held: "multiprocessing.synchronize.Event",
                      done: "multiprocessing.synchronize.Event") -> None:
    """Take the ProcessLock at `lock_path`, and end when `done` is set."""
    assert file_storage.ProcessLock(lock_path).take_for_this_process()
    held.set()
    done.wait(30)


def run_all(target: object, args_of_each: list[tuple[object, ...]]) -> None:
    """Run `target` in one process for each argument tuple, and wait for all of them."""
    procs = [multiprocessing.Process(target=target, args=args) for args in args_of_each]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0, f"a writer process ended with {p.exitcode}"


def test_locked_changes(tmp: Path) -> None:
    """Processes that change one JSON file at once lose no change."""
    path, lock = tmp / "counter.json", tmp / "counter.lock"
    run_all(count_up, [(path, lock)] * WRITERS)
    value = json.loads(path.read_text())
    expect("FileLock: every change of every process is in the file",
           value == {"n": WRITERS * CHANGES}, repr(value))


def test_lock_held_by_another_process(tmp: Path) -> None:
    """A lock that another process holds is not free; it is free after the process ends."""
    lock_path = tmp / "other.lock"
    held, done = multiprocessing.Event(), multiprocessing.Event()
    proc = multiprocessing.Process(target=hold_lock, args=(lock_path, held, done))
    proc.start()
    held.wait(30)
    with file_storage.FileLock(lock_path).held_if_free() as free:
        expect("FileLock: a lock that another process holds is not free", not free)
    done.set()
    proc.join(30)
    with file_storage.FileLock(lock_path).held_if_free() as free:
        expect("FileLock: the lock is free after the other process ends", free)


def test_process_lock(tmp: Path) -> None:
    """A ProcessLock gives the pid of its holder while it runs, and None after."""
    lock_path = tmp / "daemon.lock"
    lock = file_storage.ProcessLock(lock_path)
    expect("ProcessLock: no holder before any process takes it", lock.holder_pid() is None)
    held, done = multiprocessing.Event(), multiprocessing.Event()
    proc = multiprocessing.Process(target=hold_process_lock, args=(lock_path, held, done))
    proc.start()
    held.wait(30)
    expect("ProcessLock: the holder's pid while it runs", lock.holder_pid() == proc.pid,
           f"{lock.holder_pid()} != {proc.pid}")
    expect("ProcessLock: another process cannot take it",
           not file_storage.ProcessLock(lock_path).take_for_this_process())
    done.set()
    proc.join(30)
    expect("ProcessLock: no holder after the process ends", lock.holder_pid() is None)


def test_atomically_written_file(tmp: Path) -> None:
    """A write keeps the old mode or takes the given one, and leaves no temporary file."""
    path = tmp / "kept" / "file.txt"
    file_storage.AtomicallyWrittenFile(path).write_text("one")
    path.chmod(0o640)
    file_storage.AtomicallyWrittenFile(path).write_text("two")
    expect("AtomicallyWrittenFile: the text is replaced", path.read_text() == "two")
    expect("AtomicallyWrittenFile: the mode of the old file stays",
           path.stat().st_mode & 0o777 == 0o640, oct(path.stat().st_mode))
    script = tmp / "kept" / "script.sh"
    file_storage.AtomicallyWrittenFile(script, mode=0o755).write_text("#!/bin/sh\n")
    expect("AtomicallyWrittenFile: a given mode is used",
           script.stat().st_mode & 0o777 == 0o755, oct(script.stat().st_mode))
    expect("AtomicallyWrittenFile: no temporary file stays",
           sorted(p.name for p in path.parent.iterdir()) == ["file.txt", "script.sh"],
           repr(sorted(p.name for p in path.parent.iterdir())))


def test_json_lines(tmp: Path) -> None:
    """A partial last line is left out; a damaged line raises or is reported."""
    path = tmp / "rows.jsonl"
    rows = file_storage.AppendOnlyJsonLinesFile(path, sort_keys=True)
    expect("AppendOnlyJsonLinesFile: no rows when there is no file", rows.read_rows() == [])
    rows.append_row({"b": 1, "a": 2})
    expect("AppendOnlyJsonLinesFile: sort_keys writes the keys sorted",
           path.read_text() == '{"a": 2, "b": 1}\n', repr(path.read_text()))
    with open(path, "a") as fh:
        fh.write('{"a": 3')
    expect("AppendOnlyJsonLinesFile: a last line with no newline is left out",
           rows.read_rows() == [{"a": 2, "b": 1}], repr(rows.read_rows()))
    with open(path, "a") as fh:
        fh.write('\n[1]\n{"a": 4}\n')
    try:
        rows.read_rows()
        expect("AppendOnlyJsonLinesFile: a damaged line raises", False, "nothing raised")
    except file_storage.DamagedJsonLineError as e:
        expect("AppendOnlyJsonLinesFile: a damaged line raises, with its line number",
               e.line_number == 2, str(e))
    damaged: list[int] = []
    good = rows.read_rows_with_line_numbers(lambda e: damaged.append(e.line_number))
    expect("AppendOnlyJsonLinesFile: the other rows are read, with their line numbers",
           good == [(1, {"a": 2, "b": 1}), (4, {"a": 4})] and damaged == [2, 3],
           f"{good} {damaged}")


def test_many_appenders(tmp: Path) -> None:
    """Rows that several processes append at once are all there, each on its own line."""
    path = tmp / "many.jsonl"
    run_all(append_rows, [(path, w) for w in range(WRITERS)])
    rows = file_storage.AppendOnlyJsonLinesFile(path).read_rows()
    expect("AppendOnlyJsonLinesFile: every row of every process, none mixed",
           sorted((r["writer"], r["i"]) for r in rows)
           == [(w, i) for w in range(WRITERS) for i in range(CHANGES)], f"{len(rows)} rows")


def test_json_file_folder(tmp: Path) -> None:
    """A folder lists only its items, and moves one to another folder."""
    inbox = file_storage.JsonFileFolder(tmp / "inbox")
    delivered = file_storage.JsonFileFolder(tmp / "inbox" / "delivered")
    expect("JsonFileFolder: no items when there is no folder", inbox.names() == [])
    inbox.write("2-b.json", {"n": 2})
    inbox.write("1-a.json", {"n": 1})
    (tmp / "inbox" / ".3-c.json.tmp").write_text("{")      # a write not in place yet
    expect("JsonFileFolder: the items, sorted, and no file of a write in progress",
           inbox.names() == ["1-a.json", "2-b.json"], repr(inbox.names()))
    inbox.move_to("1-a.json", delivered)
    expect("JsonFileFolder: a moved item is in the other folder only",
           inbox.names() == ["2-b.json"] and delivered.read("1-a.json") == {"n": 1})


def test_json_lines_folder(tmp: Path) -> None:
    """The files of a folder share its lock, and each row has its keys sorted."""
    folder = file_storage.AppendOnlyJsonLinesFolder(tmp / "store",
                                                    file_storage.FileLock(tmp / "store.lock"))
    first, second = folder.file("a.jsonl"), folder.file("b.jsonl")
    expect("AppendOnlyJsonLinesFolder: every file has the lock of the folder",
           first.lock is folder.lock and second.lock is folder.lock)
    with folder.holding_lock(), file_storage.FileLock(tmp / "store.lock").held_if_free() as free:
        expect("AppendOnlyJsonLinesFolder: the lock is held in the block", not free)
    first.append_row({"b": 1, "a": 2})
    expect("AppendOnlyJsonLinesFolder: a row has its keys sorted",
           (tmp / "store" / "a.jsonl").read_text() == '{"a": 2, "b": 1}\n')


def main() -> int:
    """Run every case in a new temporary folder."""
    multiprocessing.set_start_method("spawn")
    for case in (test_locked_changes, test_lock_held_by_another_process, test_process_lock,
                 test_atomically_written_file, test_json_lines, test_many_appenders,
                 test_json_file_folder, test_json_lines_folder):
        print(case.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            start = time.time()
            case(Path(tmp))
            assert time.time() - start < 120, f"{case.__name__} took too long"
    if FAILED:
        print(f"\n{len(FAILED)} case(s) failed: {FAILED}")
        return 1
    print("\nAll file storage cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
