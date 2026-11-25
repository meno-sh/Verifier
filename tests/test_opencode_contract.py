#!/usr/bin/env python3
"""Assert that the daemon works with the real OpenCode binary, using a fake model.

Needs OpenCode 1.19.0, installed by install.sh. No API
key is used: the model is tests/fakes/fake_model.py, and the agent's config points
OpenCode at it.

Covers:
  - the agent loads `.verifier/AGENTS.md` and never the worker's own AGENTS.md;
  - a commit reaches the agent as a round, and the round is seen to end;
  - `vjob start` through the agent's shell tool returns at once, and the job keeps
    running after the round ends;
  - after the OpenCode server is killed, the daemon restarts it on the same session;
  - a job ending wakes the agent with a new round;
  - the agent's shell runs git commands that read, and OpenCode refuses the ones that
    write, alone, after another command or after options, and the checkout is left as
    it was; the message of a refusal is short.

Prints SKIP and returns 0 when OpenCode is not installed. Returns 1 if any case fails.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime import daemon  # noqa: E402
from runtime.agent import jobs, opencode  # noqa: E402
from runtime.project import hook  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import claim_files, structure  # noqa: E402

HERE = Path(__file__).resolve().parent
FAILED = []
AGENT_MARKER = "You are working for the verifier system of a research project"
WORKER_MARKER = "WORKER-INSTRUCTIONS-MUST-NOT-REACH-THE-VERIFIER"
ROUND_WAIT_SECONDS = 120
# OpenCode's message for a refused command lists every shell rule (runtime/agent/opencode.py),
# about 66 characters each: 13,759 characters for the 208 rules, 156,616 for 2,260.
MAX_REFUSAL = 16000


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def git(root: Path, *args: str) -> str:
    """Run git with a fixed identity."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def commit(root: Path, message: str) -> str:
    """Commit one change to a note and return the sha."""
    with open(root / "notes.md", "a") as fh:
        fh.write(message + "\n")
    git(root, "add", "notes.md")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def run_until_round_ends(d: daemon.Daemon, n: int) -> float:
    """Tick the daemon until round n has ended. Returns how long it took."""
    started = time.time()
    while time.time() - started < ROUND_WAIT_SECONDS:
        d.tick()
        path = d.paths.rounds / f"{n}.json"
        if path.exists() and json.loads(path.read_text())["ended_at"] is not None:
            return time.time() - started
        time.sleep(1)
    raise TimeoutError(f"round {n} did not end within {ROUND_WAIT_SECONDS} s")


def requests(log: Path) -> list[dict]:
    """Every request the fake model received."""
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def main() -> int:
    """Run the contract against real OpenCode in a temporary working directory."""
    try:
        found = opencode.version()
    except opencode.OpenCodeError as e:
        print(f"SKIP: {e}")
        return 0
    if found != opencode.PINNED_VERSION:
        print(f"SKIP: OpenCode {found} is installed, the contract is for {opencode.PINNED_VERSION}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        return contract(Path(tmp))


def contract(tmp: Path) -> int:
    """The cases, in one working directory, sharing one daemon."""
    root = tmp / "w"
    shutil.copytree(HERE / "fixtures" / "workdir", root)
    (root / "AGENTS.md").write_text(WORKER_MARKER + "\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    paths = StatePaths(root=root)
    paths.ensure()
    paths.branch_file.write_text("main\n")
    claim_files.save_scope(paths.state, (structure.WHOLE,))
    # one session, so the restart below can show that the session is kept
    (paths.state / "options.json").write_text(json.dumps(
        {"agent_session_policy": "one-session"}))
    hook.install(root, "main")

    log = tmp / "model.log"
    port = opencode.free_port()
    fake = subprocess.Popen([sys.executable, str(HERE / "fakes" / "fake_model.py"), str(port),
                             str(log)])
    config = json.loads((daemon.TEMPLATES / "opencode.json").read_text())
    config["provider"] = {"fake": {"npm": "@ai-sdk/openai-compatible", "name": "fake",
                                   "options": {"baseURL": f"http://127.0.0.1:{port}/v1",
                                               "apiKey": "unused"},
                                   "models": {"m1": {"name": "m1", "tool_call": True}}}}
    (paths.state / "opencode.json").write_text(json.dumps(config))
    for name in claim_files.FILES:     # the register, and the other claim files empty
        (paths.state / name).write_text((root / "claims.json").read_text()
                                        if name == claim_files.REGISTER else "{}")
    d = daemon.Daemon(root, model="fake/m1", round_timeout=600)
    d.do_claim_steps = lambda n: []    # the claim steps call a model; this tests OpenCode
    try:
        d.prepare()
        d.ensure_server()
        session = d.server_info.session_id

        sha = commit(root, "VJOB start the long run")
        took = run_until_round_ends(d, 1)
        reqs = requests(log)
        first = next((r for r in reqs if sha in r["last"]), None)
        expect("contract: the commit reached the model as a round", first is not None)
        expect("contract: the agent's instructions are .verifier/AGENTS.md, never the worker's",
               first is not None and AGENT_MARKER in first["system"]
               and WORKER_MARKER not in json.dumps(reqs))
        record = json.loads((paths.jobs / "sleeper.json").read_text()) \
            if (paths.jobs / "sleeper.json").exists() else None
        expect("contract: vjob through the shell tool returned and the round ended quickly",
               record is not None and took < 60, f"took {took:.1f} s, record {record}")
        expect("contract: the job keeps running after the round ended",
               record is not None and jobs.process_is_running(record["pid"]))

        d.server.kill()
        d.server.wait()
        sha2 = commit(root, "a second commit")
        run_until_round_ends(d, 2)
        expect("contract: after the server was killed, it restarted on the same session",
               d.server_info.session_id == session)
        expect("contract: the second commit reached the model after the restart",
               any(sha2 in r["last"] for r in requests(log)))

        jobs.stop(paths, "sleeper")
        run_until_round_ends(d, 3)
        r3 = json.loads((paths.rounds / "3.json").read_text())
        expect("contract: the job ending woke the agent with a new round",
               [e["kind"] for e in r3["events"]] == ["job_ended"], repr(r3["events"]))

        commit(root, "BASH: git -C checkout log -1 --format=READ-OK-%h")
        run_until_round_ends(d, 4)
        expect("contract: the agent's shell runs a git command that reads",
               any(r["last_role"] == "tool" and "READ-OK-" in r["last"] for r in requests(log)))
        writes = ["git -C checkout commit -q --allow-empty -m sneaky",
                  "cd checkout && git reset -q --hard HEAD~1",
                  "git -C checkout checkout -q --detach HEAD~1",
                  "git --no-pager -C checkout reset -q --hard HEAD~1"]
        for n, command in enumerate(writes, start=5):
            before = len(requests(log))
            commit(root, f"BASH: {command}")
            run_until_round_ends(d, n)
            tip = git(root, "rev-parse", "main")
            expect(f"contract: OpenCode refuses `{command}`, and the checkout is unchanged",
                   git(paths.checkout, "rev-parse", "HEAD") == tip
                   and "sneaky" not in git(root, "log", "--all", "--format=%s").splitlines()
                   and any(r["last_role"] == "tool" and "prevents you from using" in r["last"]
                           for r in requests(log)[before:]),
                   json.dumps(requests(log)[before:])[-600:])
            refusals = [r for r in requests(log)[before:]
                        if r["last_role"] == "tool" and "prevents you from using" in r["last"]]
            expect(f"contract: the refusal of `{command}` is short, since it echoes every "
                   f"shell rule (at most {MAX_REFUSAL} characters)",
                   bool(refusals) and all(r["last_length"] <= MAX_REFUSAL for r in refusals),
                   repr([r["last_length"] for r in refusals]))
    finally:
        if d.server is not None:
            d.server.terminate()
        fake.terminate()
        for record in jobs.records(paths) if paths.jobs.exists() else []:
            if record.is_running():
                jobs.stop(paths, record.name)
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll OpenCode contract cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
