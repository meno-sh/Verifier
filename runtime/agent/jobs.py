"""Long-running processes that the agent starts, and one-time wake-ups that it sets.

The shell tool of the agent cannot keep `cmd &` alive: OpenCode kills it at the tool timeout.
`vjob start` runs a command in its own process group and returns at once.
The daemon wakes the agent when the job ends and at each check-in.
Each job runs in `jobs/NAME.tree/`, its own checkout of the commit being checked when it started.
$VERIFIER_ROOT in the job's environment names that checkout.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from runtime.agent import inbox, opencode
from runtime.project import git
from runtime.project.paths import StatePaths
from utils import alerts, file_storage, structure

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
TAIL_LINES = 50


@dataclasses.dataclass
class JobRecord:
    """One job the agent started, as jobs/NAME.json holds it."""
    name: str
    command: list[str]
    commit: str                     # the commit the job runs on, in its own checkout
    pid: int
    started_at: float
    checkin: int | None             # seconds between check-ins, or None for none
    last_checkin_at: float
    reported: bool                  # True once the agent was told that the job ended

    @classmethod
    def from_json(cls, data: object) -> JobRecord:
        """Read a job record file."""
        assert isinstance(data, dict), f"a job record is not a JSON object: {data!r}"
        return cls(**data)

    def to_json(self) -> dict[str, object]:
        """The record as its file holds it."""
        return dataclasses.asdict(self)

    def is_running(self) -> bool:
        """True while the job's process is alive."""
        return process_is_running(self.pid)


def _lock(paths: StatePaths) -> file_storage.FileLock:
    """The lock of the job records: `start`, `stop` and `collect` each hold it."""
    return file_storage.FileLock(paths.runtime / "jobs.lock")


def start(paths: StatePaths, name: str, command: list[str], checkin: int | None) -> dict:
    """Start a job in its own process group and record it. Refuses a name already running."""
    with _lock(paths).held():
        return _start(paths, name, command, checkin)


def _start(paths: StatePaths, name: str, command: list[str], checkin: int | None) -> dict:
    """Start and record a job. The caller holds the lock of the job records."""
    if not NAME.fullmatch(name):
        raise ValueError(f"job name {name!r} must be letters, digits, '.', '_' or '-'")
    record_file = _records(paths).file(f"{name}.json")
    if record_file.exists() and JobRecord.from_json(record_file.read()).is_running():
        raise ValueError(f"job {name!r} is already running")
    tree = paths.jobs / f"{name}.tree"
    problem = runs_outside(paths, tree, command)
    if problem:
        raise ValueError(problem)
    if tree.exists():
        raise ValueError(f"{tree} is still there from an earlier job {name!r}; use another name")
    commit = paths.checked()
    if not commit:
        raise ValueError("no commit is checked yet, so a job has nothing to run on")
    git.check_out(paths.root, tree, commit)
    paths.link_outputs(tree)
    log, status = paths.jobs / f"{name}.log", paths.jobs / f"{name}.status"
    status.unlink(missing_ok=True)
    script = (f"trap 'echo $? > {shlex.quote(str(status))}' EXIT\n"
              f"cd {shlex.quote(str(tree))}\n{shlex.join(command)}\n")
    with open(log, "ab") as out:
        # the job reads its own checkout, which stays at its commit, not the daemon's
        proc = subprocess.Popen(["bash", "-c", script], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                env={**os.environ, "VERIFIER_ROOT": str(tree)})
    record = JobRecord(name=name, command=command, commit=commit, pid=proc.pid,
                       started_at=time.time(), checkin=checkin, last_checkin_at=time.time(),
                       reported=False)
    record_file.write(record.to_json())
    return record


def runs_outside(paths: StatePaths, cwd: Path, command: list[str]) -> str | None:
    """Why `command`, run in `cwd`, would run code outside `.verifier/`, or None if it does not.

    The same limits as the agent's shell rules (runtime/agent/opencode.py): no inline code, and
    no program or interpreted file outside the state directory. Paths count as they
    resolve from `cwd`, since a job runs in its own checkout.
    """
    words = command[2:] if command[:2] == ["uv", "run"] else command
    if not words:
        return "the job has no command"
    program = "python" if re.fullmatch(r"python[0-9.]*", words[0]) else words[0]
    if " ".join([program, *words[1:2]]) in opencode.INLINE_CODE:
        return f"the job runs inline code: {shlex.join(command)}"
    targets = [words[0]] if "/" in words[0] else []
    if program in opencode.INTERPRETERS:
        targets += [w for w in words[1:] if not w.startswith("-")][:1]
    state = paths.state.resolve()
    for target in targets:
        if not (cwd / target).resolve().is_relative_to(state):
            return f"the job runs {target}, which is outside {state}"
    return None


def _records(paths: StatePaths) -> file_storage.JsonFileFolder:
    """The folder of the job records, one JSON file for each job."""
    return file_storage.JsonFileFolder(paths.jobs, indent=2)


def records(paths: StatePaths) -> list[JobRecord]:
    """Every job record, oldest first."""
    folder = _records(paths)
    out = [JobRecord.from_json(folder.read(name)) for name in folder.names()]
    return sorted(out, key=lambda r: r.started_at)


def process_is_running(pid: int) -> bool:
    """True while the process `pid` is alive.

    A job runs as the user who started it. If the pid now belongs to another user's
    process, the job ended and the system gave its pid to that process.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:          # the job ended, and its process is gone
        return False
    except PermissionError:             # the job ended, and another user's process has its pid
        return False
    return _not_zombie(pid)


def _not_zombie(pid: int) -> bool:
    """False for a process that has exited but not been reaped, which kill(0) still finds.

    A system with no /proc (macOS) cannot tell, so a live pid counts as running there.
    """
    if not Path("/proc").is_dir():
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:           # it ended between kill(0) and this read
        return False
    state = stat.rsplit(")", 1)[-1].split()
    if not state:
        raise ValueError(f"/proc/{pid}/stat has no process state: {stat!r}")
    return state[0] != "Z"


def exit_code(paths: StatePaths, name: str) -> int | None:
    """The job's exit code, or None if it has not written one (it was killed).

    Raises:
        ValueError: the status file holds something other than a whole number.
    """
    status = paths.jobs / f"{name}.status"
    try:
        text = status.read_text()
    except FileNotFoundError:
        return None
    if not text.strip().lstrip("-").isdigit():
        raise ValueError(f"{status} holds {text!r}, not an exit code")
    return int(text.strip())


def stopped_file(paths: StatePaths, name: str) -> Path:
    """The file that `stop` writes, so that the end of the job is reported as stopped.

    The exit code of a stopped job can be 0, because the EXIT trap of its shell writes
    the code of the shell, so the code alone does not show the stop.
    """
    return paths.jobs / f"{name}.stopped"


def stop(paths: StatePaths, name: str) -> None:
    """Send SIGTERM to the job's whole process group, and record the stop."""
    with _lock(paths).held():
        record = JobRecord.from_json(_records(paths).read(f"{name}.json"))
        if record.is_running():
            stopped_file(paths, name).touch()
            os.killpg(os.getpgid(record.pid), signal.SIGTERM)


def tail(paths: StatePaths, name: str, lines: int = TAIL_LINES) -> str:
    """The last lines of the job's log."""
    log = paths.jobs / f"{name}.log"
    if not log.exists():
        return ""
    every = log.read_text(errors="replace").splitlines()
    cut = [f"[... {len(every) - lines} earlier lines cut]"] if len(every) > lines else []
    return "\n".join(cut + every[-lines:])


def collect(paths: StatePaths, now: float | None = None) -> int:
    """Turn ended jobs, due check-ins and due timers into inbox events. Returns how many.

    Called by the daemon on every tick, and by `verifier start-if-pending`. A job's end is
    reported once, because both hold the lock of the job records.
    """
    with _lock(paths).held():
        return _collect(paths, time.time() if now is None else now)


def _collect(paths: StatePaths, now: float) -> int:
    """Turn ended jobs, check-ins and timers into events. The caller holds the lock."""
    added = 0
    for record in records(paths):
        if record.reported:
            continue
        name = record.name
        if not record.is_running():
            code = exit_code(paths, name)
            inbox.AgentInbox(paths.inbox).write_event(
                "job_ended", job=name, exit_code=code, killed=code is None,
                stopped=stopped_file(paths, name).exists(), tail=tail(paths, name))
            record.reported = True
            _records(paths).write(f"{name}.json", record.to_json())
            added += 1
        elif record.checkin and now - record.last_checkin_at >= record.checkin:
            inbox.AgentInbox(paths.inbox).write_event(
                "job_checkin", job=name, running_for=int(now - record.started_at),
                tail=tail(paths, name))
            record.last_checkin_at = now
            _records(paths).write(f"{name}.json", record.to_json())
            added += 1
    timers = file_storage.JsonFileFolder(paths.timers)
    for timer in timers.names():
        data = timers.read(timer)
        if data["due_at"] <= now:
            inbox.AgentInbox(paths.inbox).write_event("timer", message=data["message"])
            timers.remove(timer)
            added += 1
    return added


def set_timer(paths: StatePaths, seconds: int, message: str) -> Path:
    """Wake the agent once, after `seconds`, with `message`."""
    due = time.time() + seconds
    return file_storage.JsonFileFolder(paths.timers).write(
        f"{int(due * 1000)}.json", {"due_at": due, "message": message})


# Command lines for the agent: vjob and vwake.

def _paths(process: str) -> StatePaths:
    """The state paths from $VERIFIER_STATE, which the daemon sets for the agent.

    Also sends this process's error records to the project's alert log.
    """
    state = Path(os.environ.get("VERIFIER_STATE", structure.STATE_DIR_NAME)).resolve()
    paths = StatePaths(root=state.parent)
    alerts.configure(paths.alerts_log, process, paths.root)
    paths.ensure()
    return paths


def vjob_main(argv: list[str] | None = None) -> int:
    """Run the `vjob` command."""
    ap = argparse.ArgumentParser(prog="vjob", description="Run and watch long processes.")
    sub = ap.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("name")
    p_start.add_argument("--checkin", type=int, help="also wake me every N seconds while it runs")
    p_start.add_argument("cmd", nargs=argparse.REMAINDER)
    sub.add_parser("list")
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("name")
    args = ap.parse_args(argv)
    paths = _paths("vjob")
    if args.command == "start":
        cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
        if not cmd:
            ap.error("give the command after --")
        try:
            record = start(paths, args.name, cmd, args.checkin)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"started {args.name} (pid {record.pid}); output in "
              f"{paths.jobs / (args.name + '.log')}. You will be woken when it ends.")
    elif args.command == "list":
        for r in records(paths):
            state = ("running" if r.is_running()
                     else f"ended, exit {exit_code(paths, r.name)}")
            print(f"{r.name}: {state}; {shlex.join(r.command)}")
    else:
        stop(paths, args.name)
        print(f"sent SIGTERM to {args.name}")
    return 0


def vwake_main(argv: list[str] | None = None) -> int:
    """Run the `vwake` command."""
    ap = argparse.ArgumentParser(prog="vwake", description="Wake me once, later.")
    ap.add_argument("seconds", type=int)
    ap.add_argument("message", nargs="+")
    args = ap.parse_args(argv)
    set_timer(_paths("vwake"), args.seconds, " ".join(args.message))
    print(f"you will be woken in {args.seconds} seconds")
    return 0
