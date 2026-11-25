#!/usr/bin/env python3
"""Assert that the real-time runtime delivers events as designed, with no model and no OpenCode.

Covers:
  - the reference-transaction hook: writes one event each time the tracked branch
    moves (a commit, a merge, a forced move from another branch) and none for other
    branches, returns quickly, keeps an existing hook and runs it first, still lets it
    refuse a transaction, replaces the post-commit hook earlier versions installed, and
    keeps .verifier/ out of the worker's git, and refuses a hooks folder inside the
    project's own files;
  - jobs and timers: an ended job, a killed job, a check-in and a due timer each
    become one inbox event, and a job that `vjob stop` ends is reported as stopped;
  - the commands: every command in pyproject.toml names a function that exists, and
    the launchers the agent runs work from a project whose own `checks/` folder would
    shadow the verifier's;
  - the locked environment: built from the project's uv.lock without its own code, and
    removed, with the report saying so, when there is no uv.lock or it does not build;
  - the daemon, against a stub of OpenCode's HTTP API: commits made during a round
    wait and arrive together as the next round, nothing is sent while the agent is
    busy, a round past its timeout is aborted, a restarted daemon picks up the round
    in flight, and a finished round leaves a report and a state commit;
  - the alert log: a failed round is an `error`, a project whose inputs cannot be
    read gets a BLOCK UNTIL FIXED report and no record, and a vcheck run with no
    verdict stops the daemon with one `fatal` record.
  - the tracked branch: commits on other branches are ignored, a merge brings every
    commit it adds in parent-first order, a move made while the worker is on another
    branch is checked in the daemon's checkout rather than the worker's tree, a reset
    is reported, a stale event sends no round, and a job runs in its own checkout.
  - each start writes WORKER.md, the guide for the worker's agent, with the section of
    each loaded extension;
  - a short failure of the first OpenCode start is tried again, and a lasting one stops
    the daemon.

Returns 1 if any case fails, so it can gate a commit.
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.project import agent_session, watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from runtime import daemon  # noqa: E402
from runtime.agent import agent_launchers, inbox, jobs, opencode, rounds  # noqa: E402
from runtime.agent import events as agent_events  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project import hook, project_preparation, project_status  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import alerts, claim_files, structure  # noqa: E402

HERE = Path(__file__).resolve().parent
FAILED = []
METHOD_ENV = watched_project.WatchedProject.METHOD_ENV
OPTIONS_FILE = watched_project.WatchedProject.OPTIONS_FILE


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


def repo(tmp: Path) -> StatePaths:
    """A git repo holding a copy of the fixture working directory, with one commit."""
    root = tmp / "w"
    shutil.copytree(HERE / "fixtures" / "workdir", root)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    paths = StatePaths(root=root)
    paths.ensure()
    paths.branch_file.write_text("main\n")
    claim_files.save_scope(paths.state, (structure.WHOLE,))
    # claim_uncertainty calls models, which these tests do not cover
    (paths.state / OPTIONS_FILE).write_text(json.dumps({"want_claim_uncertainty": False}))
    return paths


def commit(paths: StatePaths, message: str, tree: Path | None = None,
           note_name: str = "notes.md") -> str:
    """Make one commit touching a note in `tree` (default: the worker's), and return its sha."""
    tree = tree or paths.root
    note = tree / note_name
    with open(note, "a") as fh:
        fh.write(message + "\n")
    git(tree, "add", note_name)
    git(tree, "commit", "-q", "-m", message)
    return git(tree, "rev-parse", "HEAD")


def events_now(paths: StatePaths) -> list[dict]:
    """The events waiting in the inbox."""
    return [inbox.AgentInbox.read_event(p) for p in inbox.AgentInbox(paths.inbox).pending_files()]


def test_hook(tmp: Path) -> None:
    """One event per move of the tracked branch, quickly, chaining an existing hook."""
    paths = repo(tmp)
    hooks = Path(git(paths.root, "rev-parse", "--git-path", "hooks"))
    hooks = hooks if hooks.is_absolute() else paths.root / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    existing = hooks / hook.HOOK_NAME
    existing.write_text("#!/bin/sh\n[ -e \"$(git rev-parse --show-toplevel)/refuse\" ] "
                        "&& [ \"$1\" = prepared ] && exit 1\n"
                        "cat >> \"$(git rev-parse --show-toplevel)/old-hook-saw\"\n")
    existing.chmod(0o755)
    legacy = hooks / hook.REPLACED_HOOK_NAME
    legacy.write_text(f"#!/bin/sh\n{hook.MARKER}\n")
    legacy.chmod(0o755)
    hook.install(paths.root, "main")
    hook.install(paths.root, "main")
    expect("hook: the post-commit hook of earlier versions is removed", not legacy.exists())
    started = time.time()
    sha = commit(paths, "first")
    took = time.time() - started
    expect("hook: a commit on the tracked branch gives one event with its sha",
           events_now(paths) == [{"kind": "branch_moved", "sha": sha}], repr(events_now(paths)))
    expect("hook: the commit returns in under a second", took < 1.0, f"{took:.2f} s")
    expect("hook: the existing hook is kept and still gets the transaction",
           f"{sha} refs/heads/main" in (paths.root / "old-hook-saw").read_text())
    inbox.AgentInbox(paths.inbox).mark_delivered(inbox.AgentInbox(paths.inbox).pending_files())

    other = tmp / "other"
    git(paths.root, "worktree", "add", "-q", "-b", "feature", str(other))
    commit(paths, "on a feature branch", other)
    expect("hook: a commit on another branch gives no event", events_now(paths) == [],
           repr(events_now(paths)))
    git(paths.root, "merge", "-q", "--no-edit", "feature")
    merged = git(paths.root, "rev-parse", "HEAD")
    expect("hook: a merge into the tracked branch gives one event",
           events_now(paths) == [{"kind": "branch_moved", "sha": merged}], repr(events_now(paths)))
    inbox.AgentInbox(paths.inbox).mark_delivered(inbox.AgentInbox(paths.inbox).pending_files())
    git(paths.root, "switch", "-q", "-c", "elsewhere")
    moved_to = commit(paths, "more on the feature branch", other)
    git(other, "branch", "-f", "main", "feature")
    expect("hook: the tracked branch moved from another worktree gives one event",
           [e["sha"] for e in events_now(paths)] == [moved_to], repr(events_now(paths)))

    (paths.root / "refuse").touch()
    before = git(paths.root, "rev-parse", "elsewhere")
    try:
        commit(paths, "refused")
        refused = False
    except subprocess.CalledProcessError:
        refused = True
    expect("hook: the existing hook can still refuse a transaction",
           refused and git(paths.root, "rev-parse", "elsewhere") == before)
    (paths.root / "refuse").unlink()

    exclude = Path(git(paths.root, "rev-parse", "--git-path", "info/exclude"))
    exclude = exclude if exclude.is_absolute() else paths.root / exclude
    expect("hook: .verifier/ is excluded once, and the worker's status stays clean",
           exclude.read_text().count("/.verifier/") == 1
           and ".verifier" not in git(paths.root, "status", "--porcelain"))
    hook.uninstall(paths.root)
    expect("hook: uninstall puts the old hook back",
           "old-hook-saw" in existing.read_text() and hook.MARKER not in existing.read_text())


def test_jobs(tmp: Path) -> None:
    """Ended, killed and checked-in jobs, and a due timer, each give one event."""
    paths = repo(tmp)
    started_on = git(paths.root, "rev-parse", "HEAD")
    runtime_git.check_out(paths.root, paths.checkout, started_on)
    paths.link_outputs(paths.checkout)          # as the daemon does when it checks out
    paths.checked_file.write_text(started_on + "\n")
    (paths.state / "ends.sh").write_text("echo hello; git rev-parse HEAD; "
                                         "mkdir -p .verifier/outputs/ends; "
                                         "echo 0.5 > .verifier/outputs/ends/score.txt; exit 3\n")
    jobs.start(paths, "ends", ["bash", "../../ends.sh"], checkin=None)
    jobs.start(paths, "killed", ["sleep", "60"], checkin=None)
    jobs.start(paths, "stopped", ["sleep", "60"], checkin=None)
    jobs.start(paths, "slow", ["sleep", "60"], checkin=1)
    jobs.set_timer(paths, 0, "look again")
    time.sleep(1.5)
    record = json.loads((paths.jobs / "killed.json").read_text())
    os.killpg(os.getpgid(record["pid"]), 9)
    jobs.stop(paths, "stopped")
    time.sleep(0.5)
    jobs.collect(paths)
    events = [inbox.AgentInbox.read_event(p) for p in inbox.AgentInbox(paths.inbox).pending_files()]
    by_kind = {}
    for e in events:
        by_kind.setdefault((e["kind"], e.get("job")), e)
    ended = by_kind.get(("job_ended", "ends"), {})
    expect("jobs: an ended job reports its exit code and log",
           ended.get("exit_code") == 3 and "hello" in ended.get("tail", ""), repr(ended))
    expect("jobs: a job runs in its own checkout of the commit being checked",
           started_on in ended.get("tail", "")
           and git(paths.jobs / "ends.tree", "rev-parse", "HEAD") == started_on, repr(ended))
    written = paths.checkout / ".verifier" / "outputs" / "ends" / "score.txt"
    expect("jobs: what a job writes under .verifier/outputs/NAME/ is read at the same path "
           "in the daemon's checkout, and kept in the state's outputs folder",
           written.exists() and written.read_text() == "0.5\n"
           and (paths.outputs / "ends" / "score.txt").exists())
    try:
        jobs.start(paths, "ends", ["true"], checkin=None)
        reused = True
    except ValueError:
        reused = False
    expect("jobs: a name whose checkout is still there is refused", not reused)
    expect("jobs: a killed job is reported as killed",
           by_kind.get(("job_ended", "killed"), {}).get("killed") is True)
    stopped = by_kind.get(("job_ended", "stopped"), {})
    expect("jobs: a job that `vjob stop` ends is reported as stopped, not as a success",
           stopped.get("stopped") is True
           and "was stopped" in agent_events.describe(stopped, paths.root,
                                                      agent_events.BUILT_IN), repr(stopped))
    expect("jobs: a running job with a check-in interval checks in",
           ("job_checkin", "slow") in by_kind)
    expect("jobs: a due timer becomes one event",
           [e["message"] for e in events if e["kind"] == "timer"] == ["look again"])
    inbox.AgentInbox(paths.inbox).mark_delivered(inbox.AgentInbox(paths.inbox).pending_files())
    jobs.collect(paths)
    again = [inbox.AgentInbox.read_event(p)["kind"]
             for p in inbox.AgentInbox(paths.inbox).pending_files()]
    expect("jobs: an ended job is reported once", "job_ended" not in again, repr(again))
    jobs.stop(paths, "slow")
    slow = json.loads((paths.jobs / "slow.json").read_text())
    deadline = time.time() + 5
    while jobs.process_is_running(slow["pid"]) and time.time() < deadline:  # its EXIT trap
        time.sleep(0.1)
    time.sleep(0.2)


def test_commands(tmp: Path) -> None:
    project = tmp / "project"
    (project / "checks").mkdir(parents=True)
    (project / "checks" / "__init__.py").write_text("raise SystemExit('shadowed')\n")
    scripts = tomllib.loads((HERE.parent / "pyproject.toml").read_text())["project"]["scripts"]
    for name, target in scripts.items():
        module, function = target.split(":")
        expect(f"pyproject command {name} exists",
               callable(getattr(importlib.import_module(module), function, None)))
    folder = agent_launchers.write_launchers(tmp / "bin")
    expect("the launcher folder holds only vcheck, vjob and vwake",
           sorted(p.name for p in folder.iterdir()) == ["vcheck", "vjob", "vwake"])
    before = (folder / "vcheck").stat().st_mtime_ns
    agent_launchers.write_launchers(folder)
    expect("an unchanged launcher is not rewritten",
           (folder / "vcheck").stat().st_mtime_ns == before)
    env = {"PATH": f"{folder}:/usr/bin:/bin", "HOME": str(tmp)}
    for name in agent_launchers.CALLS:
        out = subprocess.run([name, "--help"], cwd=project, env=env, capture_output=True, text=True)
        expect(f"launcher {name} runs from a project with checks/", out.returncode == 0,
               out.stderr[-300:])
    out = subprocess.run([sys.executable, "-m", "runtime.cli", "--help"], cwd=tmp,
                         env={**os.environ, "PYTHONPATH": str(HERE.parent)},
                         capture_output=True, text=True)
    listed = re.search(r"\{([a-z,-]+)\}", out.stdout)
    expect("verifier --help lists the commands of the default extensions in a folder that "
           "the Verifier did not prepare",
           out.returncode == 0 and listed is not None
           and {"dispute", "diagnosis", "review", "decide"} <= set(listed.group(1).split(",")),
           out.stdout[-600:] + out.stderr[-300:])
    switched_off = tmp / "no-extensions"
    StatePaths(root=switched_off).state.mkdir(parents=True)
    extensions.ExtensionSet.save_names(StatePaths(root=switched_off).state, ())
    out = subprocess.run([sys.executable, "-m", "runtime.cli", "dispute", "--flag", "F-3a9c1e",
                          str(switched_off)], cwd=tmp,
                         env={**os.environ, "PYTHONPATH": str(HERE.parent)},
                         capture_output=True, text=True)
    expect("verifier refuses an extension command in a folder that does not load its extension",
           out.returncode == 1 and "does not load the extension of `dispute`" in out.stderr,
           out.stderr[-600:])


def test_environment(tmp: Path) -> None:
    paths = repo(tmp)
    alerts.configure(paths.alerts_log, "test", paths.root)
    root, venv = paths.root, paths.state / METHOD_ENV
    daemon.sync_environment(root, paths.state)
    expect("environment: no uv.lock means none, and the report says so",
           not venv.exists() and "(no uv.lock)" in watched_project.WatchedProject.load(
               root, paths.state).method_environment_row().text)
    (root / "src" / "w").mkdir(parents=True)
    (root / "src" / "w" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "w"\nversion = "0"\nrequires-python = ">=3.10"\n'
        'dependencies = []\n'
        '[build-system]\nrequires = ["uv_build"]\nbuild-backend = "uv_build"\n')
    subprocess.run(["uv", "lock", "--offline", "--directory", str(root)], check=True,
                   capture_output=True)
    daemon.sync_environment(root, paths.state)
    expect("environment: a uv.lock gives a venv, without the project's own code",
           (venv / "bin" / "python").exists()
           and not list(venv.glob("lib/python*/site-packages/w*")))
    expect("environment: the report names the uv.lock's checksum",
           "uv.lock pins (sha256" in watched_project.WatchedProject.load(
               root, paths.state).method_environment_row().text)
    (root / "uv.lock").write_text("not a lockfile\n")
    daemon.sync_environment(root, paths.state)
    try:
        watched_project.WatchedProject.load(root, paths.state).method_environment_row()
        stopped = ""
    except watched_project.MethodEnvironmentError as e:
        stopped = str(e)
    expect("environment: a uv.lock that does not build removes it, and the run cannot start",
           not venv.exists() and "did not build" in stopped, stopped)
    failed = [r for r in alerts.read(paths.alerts_log) if r["key"] == "daemon.uv_lock_failed"]
    expect("environment: uv's whole output is an `error` record in the alert log",
           len(failed) == 1 and failed[0]["level"] == "error"
           and "TOML parse error" in failed[0]["details"]["stderr"], repr(failed))


class StubOpenCode:
    """The four OpenCode endpoints the daemon calls, with a busy flag the test sets."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.prompt_sessions: list[str] = []      # the session of each prompt
        self.aborts = 0
        self.busy = False
        self.error: str | None = None     # set to make the next round's model call fail
        self.finish: str | None = None    # the finish reason of the agent's last step
        self.message_reads = 0            # fetches of the session's messages
        self.sessions = {"ses_1"}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, code: int, body: object | None = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/global/health":
                    return self._send(200, {"healthy": True})
                if self.path == "/session/status":
                    return self._send(200, {s: {"type": "busy"} for s in stub.sessions}
                                      if stub.busy else {})
                if self.path.endswith("/message"):
                    stub.message_reads += 1
                    if stub.finish is not None:
                        return self._send(200, [{"info": {
                            "role": "assistant", "time": {"created": 10**15},
                            "finish": stub.finish}}])
                    if stub.error is None:
                        return self._send(200, [])
                    return self._send(200, [{"info": {
                        "role": "assistant", "time": {"created": 10**15},
                        "error": {"name": "APIError", "data": {"message": stub.error}}}}])
                if self.path.startswith("/session/"):
                    sid = self.path.split("/")[2]
                    return self._send(200 if sid in stub.sessions else 404, {"id": sid})
                self._send(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/session":
                    sid = f"ses_{len(stub.sessions) + 1}"
                    stub.sessions.add(sid)
                    return self._send(200, {"id": sid})
                if self.path.endswith("/prompt_async"):
                    stub.prompts.append(json.loads(body)["parts"][0]["text"])
                    stub.prompt_sessions.append(self.path.split("/")[2])
                    stub.busy = True
                    return self._send(204)
                if self.path.endswith("/abort"):
                    stub.aborts += 1
                    stub.busy = False
                    return self._send(200, True)
                self._send(404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def claim_state(paths: StatePaths, register: str) -> None:
    """Write the five claim files: the register as given, and the other four empty."""
    for name in claim_files.FILES:
        (paths.state / name).write_text(register if name == claim_files.REGISTER else "{}")


class StubbedDaemon(daemon.Daemon):
    """The daemon with its OpenCode server replaced by the stub.

    By default code runs the claim steps, which call a model; a test daemon runs none
    unless the test sets `run_claim_steps` (tests/test_round_steps.py, with a fake model).
    """
    run_claim_steps = False

    def __init__(self, root: Path, stub: StubOpenCode, **kw) -> None:
        super().__init__(root, model="stub/none", **kw)
        self.stub = stub

    def do_claim_steps(self, n: int) -> list[dict[str, object]]:
        return super().do_claim_steps(n) if self.run_claim_steps else []

    def ensure_server(self) -> None:
        if self.client is None:         # the server keeps the session the daemon makes
            self.server_info = opencode.ServerInfo(port=self.stub.port, password="pw",
                                                   session_id="ses_1")
            self.client = opencode.Client(self.stub.port, "pw")


def test_daemon(tmp: Path) -> None:
    """Rounds are batched, never sent while busy, aborted on timeout, and resumed."""
    paths = repo(tmp)
    claim_state(paths, (paths.root / "claims.json").read_text())
    hook.install(paths.root, "main")
    stub = StubOpenCode()
    d = StubbedDaemon(paths.root, stub, round_timeout=100)
    d.prepare()
    d.ensure_server()

    first = commit(paths, "one")
    d.tick(now=1000)
    expect("daemon: an idle agent gets the commit as round 1",
           len(stub.prompts) == 1 and first in stub.prompts[0] and "Round 1." in stub.prompts[0])
    shas = [commit(paths, f"during {i}") for i in range(5)]
    d.tick(now=1001)
    expect("daemon: nothing is sent while the agent is busy", len(stub.prompts) == 1)
    stub.busy = False
    d.tick(now=1002)
    expect("daemon: the five commits made mid-round arrive together as round 2",
           len(stub.prompts) == 2 and all(s in stub.prompts[1] for s in shas)
           and "Round 2." in stub.prompts[1], stub.prompts[-1][:300])
    r1 = json.loads((paths.rounds / "1.json").read_text())
    expect("daemon: round 1 has a verdict from vcheck, and its report exists",
           r1["verdict"] == "MISSING INPUTS" and (paths.state / "reports" / f"{first}.md").exists(),
           repr(r1))
    expect("daemon: the state directory is committed after a round",
           "round 1" in git(paths.state, "log", "--format=%s"))

    d.tick(now=1200)
    expect("daemon: a round past its timeout is aborted", stub.aborts == 1)
    d.tick(now=1201)
    commit(paths, "after abort")
    d.tick(now=1202)
    expect("daemon: the next round says the previous one was stopped",
           len(stub.prompts) == 3 and "stopped for running too long" in stub.prompts[2])

    restarted = StubbedDaemon(paths.root, stub)
    restarted.ensure_server()
    restarted.resume_round()
    expect("daemon: a restarted daemon picks up the round in flight",
           restarted.round is not None and restarted.round.n == 3)
    commit(paths, "while down")
    stub.busy = False
    restarted.tick(now=2000)
    expect("daemon: after restart, the finished round closes and waiting commits are sent",
           len(stub.prompts) == 4 and "Round 4." in stub.prompts[3])

    stub.error = "API key expired."
    stub.busy = False
    restarted.tick(now=2001)
    r4 = json.loads((paths.rounds / "4.json").read_text())
    expect("daemon: a failed model call marks the round failed and puts its events back",
           r4["error"] == "APIError: API key expired." and r4["verdict"] is None
           and len(inbox.AgentInbox(paths.inbox).pending_files()) == 1, repr(r4))
    keys = [r["key"] for r in alerts.read(paths.alerts_log) if r["level"] == "log"]
    expect("daemon: each round sent, ended and resumed is a `log` record",
           {"daemon.round_sent", "daemon.round_ended", "daemon.round_resumed",
            "daemon.round_aborted"} <= set(keys), repr(keys))
    failed = [r for r in alerts.read(paths.alerts_log) if r["key"] == "daemon.round_failed"]
    expect("daemon: the failed round is an error in the alert log",
           len(failed) == 1 and failed[0]["level"] == "error"
           and failed[0]["details"]["round_error"] == "APIError: API key expired.", repr(failed))
    restarted.tick(now=2010)
    expect("daemon: a failed round is not sent again before the wait", len(stub.prompts) == 4)
    stub.error = None
    restarted.tick(now=2001 + daemon.RETRY_FIRST_SECONDS + 1)
    expect("daemon: after the wait, the failed round's events are sent again",
           len(stub.prompts) == 5 and "while down" in stub.prompts[4], stub.prompts[-1][:200])
    stub.httpd.shutdown()


def test_daemon_vcheck(tmp: Path) -> None:
    """A project whose inputs cannot be read gets a report; a vcheck that fails stops the daemon."""
    paths = repo(tmp)
    claim_state(paths, "{not json")
    hook.install(paths.root, "main")
    stub = StubOpenCode()
    d = StubbedDaemon(paths.root, stub)
    d.prepare()
    d.ensure_server()
    sha = commit(paths, "one")
    d.tick(now=1000)
    d.tick(now=1001)                    # the daemon sees the agent busy
    stub.busy = False
    d.tick(now=1002)
    report = (paths.state / "reports" / "latest.md").read_text()
    data = json.loads((paths.state / "reports" / "latest.json").read_text())
    expect("daemon: a claim register that is not JSON gives a BLOCK UNTIL FIXED report",
           report.startswith(f"Verdict: BLOCK UNTIL FIXED  (commit {sha[:12]})")
           and "The run could not start: ClaimRegisterError" in report
           and data["run_error"].startswith("ClaimRegisterError") and data["findings"] is None,
           report)
    serious = [r for r in alerts.read(paths.alerts_log) if r["level"] != "log"]
    expect("daemon: the round has that verdict, and the alert log has only `log` records",
           json.loads((paths.rounds / "1.json").read_text())["verdict"] == "BLOCK UNTIL FIXED"
           and serious == [], repr(serious))

    commit(paths, "two")
    d.tick(now=1003)
    d.tick(now=1004)
    stub.busy = False
    # vcheck cannot start
    extensions.ExtensionSet.save_names(paths.state, ("no_such_extension:Missing",))
    try:
        d.tick(now=1005)
        expect("daemon: a vcheck run with no verdict stops the daemon", False, "no error raised")
    except alerts.Fatal:
        records = [r for r in alerts.read(paths.alerts_log) if r["level"] != "log"]
        expect("daemon: a vcheck run with no verdict stops the daemon, with one fatal record "
               "that holds vcheck's whole stderr, after the error of reorder-claims, which cannot "
               "start either",
               [(r["level"], r["key"]) for r in records]
               == [("error", "daemon.link_step_failed"), ("fatal", "daemon.vcheck_failed")]
               and "ModuleNotFoundError: No module named 'no_such_extension'"
               in records[1]["details"]["stderr"]
               and records[1]["details"]["exit_code"] == 1, repr(records))
    finally:
        (paths.state / extensions.EXTENSIONS_FILE).unlink()


def test_agent_sessions(tmp: Path) -> None:
    """The agent session policy decides which rounds start in a new session."""
    policy = agent_session.AgentSessionPolicy
    expect("sessions: the first round of a session never needs a new one",
           not policy(agent_session.FRESH_EACH_ROUND).needs_fresh_session(0, 10**9))
    expect("sessions: one-session never needs a new one",
           not policy(agent_session.ONE_SESSION).needs_fresh_session(5, 10**9))
    limit = policy(agent_session.FRESH_AFTER_TOKEN_LIMIT, token_limit=1000)
    expect("sessions: fresh-after-token-limit needs a new one at the limit only",
           limit.needs_fresh_session(3, 1000) and not limit.needs_fresh_session(3, 999))
    for options, name in (({"agent_session_policy": "every-day"}, "an unknown policy"),
                          ({"agent_session_token_limit": 0}, "a token limit of 0")):
        try:
            policy.from_options(options)
            expect(f"sessions: {name} is refused", False, "nothing raised")
        except agent_session.AgentSessionError:
            expect(f"sessions: {name} is refused", True)

    for kind, sessions in ((agent_session.ONE_SESSION, ["ses_1"] * 3),
                           (agent_session.FRESH_EACH_ROUND, ["ses_1", "ses_2", "ses_3"])):
        paths = repo(tmp / kind)
        claim_state(paths, (paths.root / "claims.json").read_text())
        hook.install(paths.root, "main")
        options = json.loads((paths.state / OPTIONS_FILE).read_text())
        (paths.state / OPTIONS_FILE).write_text(json.dumps(
            {**options, "agent_session_policy": kind}))
        stub = StubOpenCode()
        d = StubbedDaemon(paths.root, stub, round_timeout=100)
        d.prepare()
        d.ensure_server()
        for i in range(3):
            commit(paths, f"commit {i}")
            d.tick(now=1000 + 10 * i)
            end_round(d, stub, 1001 + 10 * i)
        expect(f"sessions: {kind}: the rounds are sent in the sessions {sessions}",
               stub.prompt_sessions == sessions, repr(stub.prompt_sessions))
        saved = [json.loads((paths.rounds / f"{n}.json").read_text())["session_id"]
                 for n in (1, 2, 3)]
        expect(f"sessions: {kind}: each round file names its session", saved == sessions,
               repr(saved))
        briefed = ["This is a new session." in p for p in stub.prompts]
        expect(f"sessions: {kind}: the briefing starts each new session after round 1",
               briefed == [False] + [kind == agent_session.FRESH_EACH_ROUND] * 2,
               repr(briefed))
        if kind == agent_session.FRESH_EACH_ROUND:
            expect("sessions: the briefing gives the last report, and no claim stage",
                   "The last report is for " in stub.prompts[2] and "Round 3." in stub.prompts[2]
                   and "vcheck stage" not in stub.prompts[2]
                   and "The new candidates" not in stub.prompts[2], stub.prompts[2][:600])
            expect("sessions: the briefing gives the commit with its files and the pinned "
                   "files it changed",
                   "(`git show --stat`):" in stub.prompts[2]
                   and "commit 2" in stub.prompts[2]
                   and "pinned file" in stub.prompts[2], stub.prompts[2][:3000])
            info = opencode.ServerInfo.read(paths.server_file)
            expect("sessions: the server file holds the session and its first round",
                   info is not None and (info.session_id, info.session_first_round)
                   == ("ses_3", 3), repr(info))
            paths.server_file.write_text(json.dumps(
                {"port": 1, "password": "pw", "session_id": "ses_old"}))
            try:
                opencode.ServerInfo.read(paths.server_file)
                expect("sessions: a server file of an older Verifier is refused", False,
                       "nothing raised")
            except opencode.ServerFileError as e:
                expect("sessions: a server file of an older Verifier is refused",
                       "session_first_round" in str(e), str(e))


def end_round(d: daemon.Daemon, stub: StubOpenCode, now: float) -> None:
    """Let the round in flight be seen busy, then end it."""
    d.tick(now=now)
    stub.busy = False
    d.tick(now=now)


def test_tracked_branch(tmp: Path) -> None:
    """Only the tracked branch is checked, in the daemon's own checkout of it."""
    paths = repo(tmp)
    claim_state(paths, (paths.root / "claims.json").read_text())
    hook.install(paths.root, "main")
    stub = StubOpenCode()
    d = StubbedDaemon(paths.root, stub, round_timeout=100)
    d.prepare()
    d.ensure_server()
    first = commit(paths, "one")
    d.tick(now=1000)
    end_round(d, stub, now=1001)
    expect("branch: the daemon's checkout reaches the outputs folder",
           (paths.checkout / ".verifier" / "outputs").resolve() == paths.outputs.resolve())
    expect("branch: the first round checks the tip in the daemon's checkout",
           git(paths.checkout, "rev-parse", "HEAD") == first
           and (paths.state / "reports" / f"{first}.md").exists())

    other = tmp / "other"
    git(paths.root, "worktree", "add", "-q", "-b", "feature", str(other))
    a = commit(paths, "feature a", other, "feature.md")
    b = commit(paths, "feature b", other, "feature.md")
    d.tick(now=1002)
    expect("branch: commits on another branch send no round", len(stub.prompts) == 1)
    on_main = commit(paths, "main moves on")
    git(paths.root, "merge", "-q", "--no-edit", "feature")
    merge = git(paths.root, "rev-parse", "HEAD")
    d.tick(now=1003)
    r2 = json.loads((paths.rounds / "2.json").read_text())
    got = [e.get("sha") for e in r2["events"]]
    expect("branch: a merge sends every commit it brings, each parent before its child",
           sorted(got) == sorted([on_main, a, b, merge])
           and got.index(a) < got.index(b) < got.index(merge)
           and got.index(on_main) < got.index(merge)
           and all(e["kind"] == "commit" for e in r2["events"]), repr(r2["events"]))
    end_round(d, stub, now=1004)

    git(paths.root, "switch", "-q", "-c", "work")
    (paths.root / "only-on-work.md").write_text("not on main\n")
    git(paths.root, "add", "only-on-work.md")
    git(paths.root, "commit", "-q", "-m", "work only")
    git(other, "merge", "-q", "--no-edit", "main")
    brought = commit(paths, "merged without switching", other, "feature.md")
    git(other, "fetch", "-q", ".", "feature:main")
    d.tick(now=1005)
    expect("branch: main moved while the worker is on another branch is sent as a round",
           len(stub.prompts) == 3 and brought in stub.prompts[2], stub.prompts[-1][:300])
    expect("branch: the checkout follows main, not the worker's checked-out branch",
           git(paths.checkout, "rev-parse", "HEAD") == brought
           and not (paths.checkout / "only-on-work.md").exists()
           and git(paths.root, "rev-parse", "--abbrev-ref", "HEAD") == "work")
    end_round(d, stub, now=1006)
    expect("branch: the round's report is for main's tip",
           (paths.state / "reports" / f"{brought}.md").exists())

    git(other, "branch", "-f", "main", first)
    d.tick(now=1007)
    r4 = json.loads((paths.rounds / "4.json").read_text())
    expect("branch: a reset of the tracked branch is reported, with nothing new to check",
           r4["events"] == [{"kind": "branch_reset", "sha": first, "previous": brought}]
           and git(paths.checkout, "rev-parse", "HEAD") == first, repr(r4["events"]))
    end_round(d, stub, now=1008)
    inbox.AgentInbox(paths.inbox).write_event("branch_moved", sha=first)
    d.tick(now=1009)
    expect("branch: an event for a commit already checked sends no round",
           len(stub.prompts) == 4 and not inbox.AgentInbox(paths.inbox).pending_files())
    expect("branch: the state repo does not take in the checkout",
           "checkout" not in git(paths.state, "ls-files"))
    stub.httpd.shutdown()


def test_hooks_path(tmp: Path) -> None:
    """A hooks folder among a checkout's files is refused, unless git ignores it."""
    paths = repo(tmp)
    other = tmp / "other"
    git(paths.root, "worktree", "add", "-q", "-b", "feature", str(other))
    inside = {"a relative path": ".githooks", "a path in this checkout": str(paths.root / "h"),
              "a path in another checkout": str(other / "hooks")}
    for name, value in inside.items():
        git(paths.root, "config", "core.hooksPath", value)
        problem = hook.hooks_problem(paths.root)
        expect(f"hooks path: {name} is refused",
               problem is not None and "core.hooksPath" in problem, repr(problem))
    try:
        hook.install(paths.root, "main")
        refused = False
    except RuntimeError as e:
        refused = "core.hooksPath" in str(e)
    expect("hooks path: install refuses it too, and writes nothing", refused
           and not (other / "hooks").exists())
    (paths.root / ".gitignore").write_text("ignored-hooks/\n")
    allowed = {"a folder git ignores": str(paths.root / "ignored-hooks"),
               "a folder outside every checkout": str(tmp / "hooks"),
               "a folder in the git folder": str(paths.root / ".git" / "my-hooks")}
    for name, value in allowed.items():
        git(paths.root, "config", "core.hooksPath", value)
        expect(f"hooks path: {name} is used",
               hook.install(paths.root, "main") == Path(value).resolve() / hook.HOOK_NAME)


def test_worker_guide(tmp: Path) -> None:
    """Each start writes WORKER.md, with the section of each loaded extension."""
    for names, wanted, unwanted in (
            (",".join(extensions.DEFAULT_EXTENSIONS),
             ["## 7. Questions for the person", "## 8. Diagnoses"], []),
            ("", [], ["## 7.", "## 8."])):
        paths = repo(tmp / (names or "none"))
        extensions.ExtensionSet.save_names(paths.state, 
            extensions.extension_names_from_argument(names))
        daemon.Daemon(paths.root).prepare()
        text = (paths.state / daemon.WORKER_GUIDE_FILE).read_text()
        expect(f"guide: with extensions {names or 'none'}, WORKER.md has the right sections",
               text.startswith("# The Verifier: a guide for the worker's agent")
               and "## 6. How to share the report with the person" in text
               and all(w in text for w in wanted) and not any(u in text for u in unwanted),
               text[-400:])
    state = tmp / "extensions-state"
    expect("extensions: a state folder with no extensions.json loads none",
           extensions.ExtensionSet.saved_names(state) == ()
           and not extensions.ExtensionSet.names_are_saved(state))
    extensions.ExtensionSet.save_names(
        state, extensions.extension_names_from_argument(" a.b:C, ,d.e:F"))
    expect("extensions: --extensions is split at commas, and the names are saved in order",
           extensions.ExtensionSet.saved_names(state) == ("a.b:C", "d.e:F"),
           repr(extensions.ExtensionSet.saved_names(state)))
    for name in ("review.extension", "core.registry.checks:CheckRegistry"):
        try:
            extensions.ExtensionSet.load_named([name])
            expect(f"extensions: the name {name} is refused", False)
        except extensions.ExtensionsFileError:
            expect(f"extensions: the name {name} is refused", True)
    loaded = extensions.ExtensionSet.load_named(extensions.DEFAULT_EXTENSIONS)
    expect("extensions: the default names load one Extension each",
           [type(e).__name__ for e in loaded.extensions]
           == ["ReviewExtension", "DiagnosisExtension"])
    (state / extensions.EXTENSIONS_FILE).write_text('{"extensions": "review.extension"}')
    try:
        extensions.ExtensionSet.saved_names(state)
        expect("extensions: an extensions.json that is not a list is refused", False)
    except extensions.ExtensionsFileError:
        expect("extensions: an extensions.json that is not a list is refused", True)
    message = project_preparation.worker_guide_message(StatePaths(root=Path("/w")))
    expect("guide: start and init give one line: follow the guide, or import it",
           message == "Instruct your coding agent to follow /w/.verifier/WORKER.md, or add "
                      "this line to its instruction file (CLAUDE.md or AGENTS.md): "
                      "@.verifier/WORKER.md", message)


class FirstStartFails(daemon.Daemon):
    """A daemon whose OpenCode start fails the first `failures` times."""

    def __init__(self, root: Path, failures: int) -> None:
        super().__init__(root, model="stub/none")
        self.failures, self.tries = failures, 0

    def ensure_server(self) -> None:
        self.tries += 1
        if self.tries <= self.failures:
            raise opencode.OpenCodeError("opencode serve did not answer within 60 s")


def test_first_start_retries(tmp: Path) -> None:
    """A short failure of the first OpenCode start is tried again; a lasting one stops."""
    paths = repo(tmp)
    wait, daemon.TICK_SECONDS = daemon.TICK_SECONDS, 0
    try:
        flaky = FirstStartFails(paths.root, failures=daemon.FIRST_START_ATTEMPTS - 1)
        flaky.start_server_with_retries()
        expect("first start: a failure that does not last is tried again, and OpenCode starts",
               flaky.tries == daemon.FIRST_START_ATTEMPTS, str(flaky.tries))
        broken = FirstStartFails(paths.root, failures=daemon.FIRST_START_ATTEMPTS)
        try:
            broken.start_server_with_retries()
            stopped = False
        except opencode.OpenCodeError:
            stopped = True
        expect("first start: after the last try, the error stops the daemon",
               stopped and broken.tries == daemon.FIRST_START_ATTEMPTS, str(broken.tries))
    finally:
        daemon.TICK_SECONDS = wait


def test_requeue_writes_events_once(tmp: Path) -> None:
    """A failed cost read in a requeue is an alert; the events go back to the inbox once."""
    paths = repo(tmp)
    d = FirstStartFails(paths.root, failures=0)
    d.prepare()
    events = [{"kind": "timer", "message": "look again"}]
    failed = rounds.Round(n=1, events=events, sent_at=0.0, error="the model failed")
    d.round = failed                     # requeue acts on the round in flight

    def broken(*args: object) -> object:
        raise opencode.OpenCodeError("the database is not in the layout it knows")
    real, project_status.Spending.of_project = project_status.Spending.of_project, broken
    try:
        d.requeue(failed, now=1.0)
    finally:
        project_status.Spending.of_project = real
    unread = [r for r in alerts.read(paths.alerts_log) if r["key"] == "daemon.round_cost_unread"]
    expect("requeue: a failed cost read is an alert, and the events go back to the inbox once",
           len(inbox.AgentInbox(paths.inbox).pending_files()) == 1 and len(unread) == 1
           and failed.spending is None and failed.events == events,
           f"{inbox.AgentInbox(paths.inbox).pending_files()} {unread}")


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_hook, test_hooks_path, test_jobs, test_commands, test_environment,
                  test_daemon, test_daemon_vcheck, test_tracked_branch, test_agent_sessions,
                  test_worker_guide, test_first_start_retries,
                  test_requeue_writes_events_once):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll runtime cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
