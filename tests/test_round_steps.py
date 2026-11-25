#!/usr/bin/env python3
"""Check the daemon's extra steps in a round, against the stub of OpenCode in test_runtime.py.

Covers:
  - a turn cut by the model's output limit: the daemon asks the agent to go on, at
    most twice in a round, and only then ends the round;
  - the claim steps before the agent's round: the rules, the labeller, grouping and the
    claims in code, in that order, so every stage has passed when the agent starts; a
    failed step is an error record, and the steps after it do not run; the agent is not
    woken, the report checks the papers only and flags that no claim was checked, and the
    events are sent again after the wait of a failed round, when every step runs again;
  - the note on an aborted round: papers-only rounds after it are skipped;
  - the same steps on a note with a table: the table is one quote with no value, outside
    grouping, in a class of its own; its claim states the table's name and columns, and
    check-register passes.
    The steps run in this process with a fake model (test_labeller.FakeModel), no network.

Returns 1 if any case fails.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_claim_rounds as tcr  # noqa: E402
import test_labeller as tl  # noqa: E402
import test_runtime as rt  # noqa: E402

from core import cli  # noqa: E402
from core.project import agent_session, watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from core.report_output import report_store  # noqa: E402
from runtime import daemon  # noqa: E402
from runtime.agent import agent_launchers, inbox, rounds  # noqa: E402
from runtime.project import hook, project_preparation  # noqa: E402
from utils import alerts, claim_files, extract, file_storage, models  # noqa: E402

NOTE = """# Results

With retrieval, the 7B model answers 68.4% of the questions correctly.
Retrieval raises the accuracy of the 7B model by 7.4 points.
The 7B model gains 7.4 points from retrieval.
We thank Jane Doe for helpful discussions.
"""
TABLE_NOTE = NOTE + """
The 7B model reaches 81.25 on the held-out split.

| Model | Held-out | Steps |
|---|---|---|
| 7B | 81.25 | 1000 |
| 13B | 84.50 | 1000 |
"""
ALL_STEPS = ["drop-non-claims", "label-candidates", "check-quotes", "propose-classes",
             "accept-classes", "fill-register"]

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def started(tmp: Path) -> tuple:
    """A working directory with claim files, a stubbed daemon and its stub, after one commit."""
    paths = rt.repo(tmp)
    rt.claim_state(paths, (paths.root / "claims.json").read_text())
    hook.install(paths.root, "main")
    stub = rt.StubOpenCode()
    d = rt.StubbedDaemon(paths.root, stub, round_timeout=1000)
    d.prepare()
    d.ensure_server()
    rt.commit(paths, "one")
    return paths, stub, d


def test_output_limit(tmp: Path) -> None:
    """A turn that ends on the output limit gets a follow-up prompt, at most twice a round."""
    paths, stub, d = started(tmp)
    d.tick(now=1000)
    d.tick(now=1001)                     # the agent is busy
    stub.finish = "length"
    stub.busy = False
    reads = stub.message_reads
    d.tick(now=1025)
    expect("output limit: the agent is asked to go on in smaller batches",
           len(stub.prompts) == 2 and stub.prompts[1] == daemon.CONTINUE_PROMPT
           and d.round is not None and d.round.continued == 1, repr(stub.prompts[1:]))
    expect("output limit: the error and the finish reason come from one fetch of the messages",
           stub.message_reads - reads == 1, f"{stub.message_reads - reads} fetches")
    stub.busy = False                    # not seen busy yet after the follow-up
    d.tick(now=1040)
    expect("output limit: the start grace counts from the follow-up, so the round goes on "
           "while the agent has not started on it",
           len(stub.prompts) == 2 and d.round is not None and d.round.continued == 1)
    d.tick(now=1060)
    expect("output limit: a second cut turn gets a second follow-up",
           len(stub.prompts) == 3 and d.round is not None and d.round.continued == 2)
    stub.busy = False
    d.tick(now=1100)
    r1 = json.loads((paths.rounds / "1.json").read_text())
    expect("output limit: after two follow-ups the round ends, and its record keeps the count",
           len(stub.prompts) == 3 and r1["ended_at"] is not None and r1["continued"] == 2,
           repr(r1))

    rt.commit(paths, "two")
    stub.finish = "stop"
    d.tick(now=1200)
    d.tick(now=1201)
    stub.busy = False
    d.tick(now=1202)
    r2 = json.loads((paths.rounds / "2.json").read_text())
    expect("output limit: a turn that ends normally ends the round with no follow-up",
           len(stub.prompts) == 4 and r2["ended_at"] is not None and r2["continued"] == 0,
           repr(r2))


class FakeModels:
    """models.ask_and_read for both claim steps: test_labeller's fake answers the labeller's
    calls, and test_claim_rounds' fake the shortlist calls of propose-classes."""

    def __init__(self, fail: bool = False) -> None:
        self.label, self.group = tl.FakeModel(fail=fail), tcr.FakeModel()

    def __call__(self, prompt: str, model: str, read, timeout: int = 300, **kw) -> object:
        target = self.label if prompt.startswith(("Piece 1", "These pieces")) else self.group
        return target(prompt, model, read, timeout, **kw)


class InProcessVcheck:
    """agent_launchers.run_vcheck for the claim steps: vcheck in this process, with a fake model.

    `vcheck run` goes to the real command. Every claim step's arguments are recorded.
    """

    def __init__(self, ask: object) -> None:
        self.ask, self.steps, self.real = ask, [], agent_launchers.run_vcheck
        self.timeouts: list[object] = []
        self.envs: list[dict] = []      # the environment each claim step was given
        self.on_step = None             # called with the step's arguments, before it runs

    def __call__(self, args: list[str], env: dict, timeout: float | None = None
                 ) -> subprocess.CompletedProcess:
        if args[0] == "run":
            return self.real(args, env)
        self.steps.append(args[0])
        self.timeouts.append(timeout)
        self.envs.append(dict(env))
        if self.on_step is not None:
            self.on_step(args)
        out, err = io.StringIO(), io.StringIO()
        real_ask = models.ask_and_read
        models.ask_and_read = self.ask
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli.main(["--state", env["VERIFIER_STATE"], "--root", env["VERIFIER_ROOT"],
                               *args])
        finally:
            models.ask_and_read = real_ask
        return subprocess.CompletedProcess(args, rc, out.getvalue(), err.getvalue())


@contextlib.contextmanager
def code_steps(ask: object, on_step: object = None) -> Iterator[InProcessVcheck]:
    """Run the claim steps in this process with `ask` as the model."""
    fake = InProcessVcheck(ask)
    fake.on_step = on_step
    agent_launchers.run_vcheck = fake
    try:
        yield fake
    finally:
        agent_launchers.run_vcheck = fake.real


def first_round(tmp: Path, ask: object, options: dict[str, object], on_step: object = None,
                setup: object = None, note: str = NOTE) -> tuple:
    """Deliver the first round of a project with no claim files; return what it needs.

    `options` go in options.json, as `verifier init --option` writes them.
    `on_step(daemon, args)` runs before each claim step; `setup(daemon)` before delivery.
    `note` is the text of the one note committed.
    """
    paths = rt.repo(tmp)
    hook.install(paths.root, "main")
    stub = rt.StubOpenCode()
    d = rt.StubbedDaemon(paths.root, stub, round_timeout=1000)
    d.run_claim_steps = True
    d.prepare()
    d.ensure_server()
    if options:
        file_storage.JsonFile(paths.state / watched_project.WatchedProject.OPTIONS_FILE,
                              indent=2).write(options)
    (paths.root / "notes.md").write_text(note)
    rt.git(paths.root, "add", "notes.md")
    rt.git(paths.root, "commit", "-q", "-m", "results")
    if setup is not None:
        setup(d)
    step = None if on_step is None else (lambda args: on_step(d, args))
    with code_steps(ask, step) as fake:
        d.tick(now=1000)
    return paths, stub, d, fake


def test_claim_steps(tmp: Path) -> None:
    """Code answers the candidates, makes the classes and writes the claims first."""
    paths, stub, d, fake = first_round(tmp / "b", FakeModels(), {})
    expect("claim steps: rules, labels, grouping, then the claims, in that order",
           fake.steps == ALL_STEPS, repr(fake.steps))
    files, r = claim_files.loaded(paths.state, paths.checkout)
    expect("claim steps: every stage has passed when the agent starts",
           not claim_files.quote_problems(files, r) and not claim_files.class_problems(files, r)
           and len(files.register) == len(files.classes) > 0, json.dumps(files.register))
    expect("claim steps: the rules dropped the thanks before the labeller",
           any(v["why"] == "auto:R4 acknowledgement" for v in files.dropped.values()),
           json.dumps(files.dropped))
    expect("claim steps: each step is on the round record with its exit code, and the agent "
           "is woken after them",
           [p["exit"] for p in d.round.claim_steps] == [0] * 6 and len(stub.prompts) == 1,
           repr(d.round.claim_steps))
    logged = [a for a in alerts.read(paths.alerts_log) if a["key"] == "daemon.claim_step"]
    expect("claim steps: each step is a log record", len(logged) == 6, repr(logged))

    paths, stub, d, fake = first_round(tmp / "c", FakeModels(fail=True), {})
    failed = [a for a in alerts.read(paths.alerts_log)
              if a["key"] == "daemon.claim_step_failed"]
    expect("claim steps: a failed label is an error record, the later steps do not run, and "
           "the agent is not woken",
           fake.steps == ["drop-non-claims", "label-candidates"]
           and len(failed) == 1 and failed[0]["level"] == "error"
           and failed[0]["details"]["step"] == "label-candidates"
           and not stub.prompts and d.round is None,
           repr(fake.steps) + repr(failed))
    r1 = json.loads((paths.rounds / "1.json").read_text())
    expect("claim steps: the round is saved as failed, with the verdict of the papers alone",
           r1["verdict"] == "MISSING INPUTS" and "vcheck label-candidates failed" in r1["error"]
           and r1["ended_at"] is not None and r1["papers_only"] is True, repr(r1))
    saved = report_store.ReportStore(paths.state).load_saved_report(paths.checked())
    flagged = [i for i in saved.issues if i.code == "claim-checks-not-run"] if saved else []
    expect("claim steps: the report flags that no claim was checked, with the failed step",
           len(flagged) == 1 and "vcheck label-candidates failed" in flagged[0].detail,
           repr(saved))
    expect("claim steps: the events stay in the inbox, and the state is committed",
           len(inbox.AgentInbox(paths.inbox).pending_files()) == 1
           and "a claim step failed" in rt.git(paths.state, "log", "-1", "--format=%s"),
           rt.git(paths.state, "log", "-1", "--format=%s"))
    with code_steps(FakeModels(), None) as again:
        # the wait counts from the end of the round, after the claim steps and the papers
        d.tick(now=r1["ended_at"] + daemon.RETRY_FIRST_SECONDS - 1)
        expect("claim steps: nothing is sent before the wait of a failed round ends",
               not again.steps and not stub.prompts, repr(again.steps))
        d.tick(now=r1["ended_at"] + daemon.RETRY_FIRST_SECONDS)
    expect("claim steps: after the wait, the next round runs every claim step again, and "
           "the agent gets the events",
           again.steps[:6] == ALL_STEPS and len(stub.prompts) == 1 and d.round.n == 2,
           repr(again.steps) + repr(stub.prompts))


def test_tables(tmp: Path) -> None:
    """The first round in code with a table: the table is one quote and one claim."""
    paths, stub, d, fake = first_round(tmp, FakeModels(), {}, note=TABLE_NOTE)
    expect("tables: label, check-quotes, propose-classes, accept-classes and fill-register "
           "all pass",
           fake.steps == ALL_STEPS
           and [p["exit"] for p in d.round.claim_steps] == [0] * 6 and len(stub.prompts) == 1,
           repr(fake.steps) + repr(d.round.claim_steps))
    files, r = claim_files.loaded(paths.state, paths.checkout)
    expect("tables: no candidate is a table row",
           {c["kind"] for c in files.candidates.values()} <= {"sentence", "table",
                                                              "commit message"},
           repr({c["kind"] for c in files.candidates.values()}))
    whole = [(qid, q) for qid, q in files.quotes.items()
             if any(files.candidates[c]["kind"] == "table" for c in q["candidates"])]
    expect("tables: the table is one quote of its whole text, with no value",
           len(whole) == 1 and whole[0][1]["value"] is None
           and "Model: 7B; Held-out: 81.25; Steps: 1000" in whole[0][1]["quote"],
           repr(whole))
    items = json.loads(claim_files.proposals_path(paths.state, r).read_text())["items"]
    expect("tables: the table quote is not sent to grouping",
           f"quote:{whole[0][0]}" not in items, repr(sorted(items)))
    home = {q: cid for cid, c in files.classes.items() for q in c["quotes"]}
    expect("tables: the table quote is in a class of its own",
           files.classes[home[whole[0][0]]]["quotes"] == [whole[0][0]], json.dumps(files.classes))
    expect("tables: every class has a claim, and every stage passed",
           len(files.register) == len(files.classes) > 0
           and not claim_files.quote_problems(files, r)
           and not claim_files.class_problems(files, r),
           repr(len(files.register)) + repr(len(files.classes)))
    said = [e["statement"] for e in files.register
            if [q for q, _ in e["quotes"]] == [whole[0][0]]]
    expect("tables: the table claim's statement is the table's name and columns, with no row",
           said == [extract.table_header(whole[0][1]["quote"])] and "Rows:" not in said[0],
           repr(said))
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = cli.main(["--state", str(paths.state), "--root", str(paths.checkout),
                       "check-register"])
    expect("tables: check-register passes on the register code wrote", rc == 0,
           out.getvalue())


class Clock:
    """A clock the claim steps move forward, as a slow model call would."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_step_time(tmp: Path) -> None:
    """The round's clock starts once the claim steps are done, and each step has a timeout."""
    clock = Clock(5000.0)

    def slow(d: object, args: list[str]) -> None:
        clock.now += 600                         # ten minutes a step

    paths, stub, d, fake = first_round(tmp / "a", FakeModels(), {}, on_step=slow,
                                       setup=lambda d: setattr(d, "clock", clock))
    expect("step time: the round is sent when the steps end, 60 minutes after they began",
           d.round is not None and d.round.sent_at == 1000 + 6 * 600, repr(d.round))
    stub.busy = False
    d.tick(now=1000 + 6 * 600 + 10)
    expect("step time: the 30 s start grace counts from the prompt, not from before the steps",
           d.round is not None and d.round.ended_at is None, repr(d.round))
    expect("step time: each claim step runs with the default timeout of 30 minutes",
           fake.timeouts == [agent_session.CLAIM_STEPS_TIMEOUT.default] * 6
           and agent_session.CLAIM_STEPS_TIMEOUT.default == 30 * 60, repr(fake.timeouts))

    paths, stub, d, fake = first_round(tmp / "b", FakeModels(),
                                       {"claim_steps_timeout": 7})
    expect("step time: the timeout is a run option", fake.timeouts == [7] * 6,
           repr(fake.timeouts))

    def hang(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd, kw["timeout"], output=b"", stderr=b"")

    real_run = agent_launchers.subprocess.run
    agent_launchers.subprocess.run = hang
    try:
        out = agent_launchers.run_vcheck(["label-candidates"], {}, timeout=5)
    finally:
        agent_launchers.subprocess.run = real_run
    expect("step time: a step past its timeout is a failed step, not an exception",
           out.returncode == agent_launchers.TIMEOUT_EXIT and "timed out after 5" in out.stderr,
           repr(out))

    def stop(d: object, args: list[str]) -> None:
        d.stopping = True                        # SIGTERM while the first step runs

    paths, stub, d, fake = first_round(tmp / "c", FakeModels(), {}, on_step=stop)
    expect("step time: a stop request between steps runs no further step and wakes no agent; "
           "the events wait in the inbox",
           fake.steps == ["drop-non-claims"] and stub.prompts == [] and d.round is None
           and inbox.AgentInbox(paths.inbox).pending_files(),
           repr(fake.steps) + repr(stub.prompts))


def test_options_mid_run(tmp: Path) -> None:
    """A changed guide is written again before the round, and a bad option stops the daemon."""
    paths, stub, d, fake = first_round(tmp / "a", FakeModels(), {})
    extensions.ExtensionSet.save_names(paths.state, extensions.DEFAULT_EXTENSIONS)
    d.write_agent_guide()
    guide = (paths.state / "AGENTS.md").read_text()
    sections = extensions.ExtensionSet.of_project(paths.state).agent_instructions()
    expect("the guide holds the template once, and each extension section once, after it",
           sections and guide.count(daemon.TEMPLATES.joinpath("AGENTS.md").read_text()) == 1
           and all(guide.count(s) == 1 for s in sections), guide[-400:])
    expect("the round line gives the time limit of the round",
           "The time limit of this round is 16.6667 minutes." in stub.prompts[0]
           and "is 120 minutes." in daemon.build_prompt(
               d.round, paths.checkout, False, extensions.ExtensionSet.load_named(()),
               round_timeout=daemon.ROUND_TIMEOUT_SECONDS), stub.prompts[0][:300])
    expect("an unchanged guide is not written again", d.write_agent_guide() is False)
    (paths.state / "AGENTS.md").write_text("an older guide")
    expect("a changed guide is written again", d.write_agent_guide() is True
           and (paths.state / "AGENTS.md").read_text() != "an older guide")
    try:
        first_round(tmp / "bad", FakeModels(), {"claim_steps_timeout": 0})
        stopped = ""
    except alerts.Fatal as e:
        stopped = str(e)
    expect("a claim_steps_timeout below 1 stops the daemon with options_invalid",
           stopped.startswith("daemon.options_invalid") and "claim_steps_timeout" in stopped,
           stopped)


def test_agent_env(tmp: Path) -> None:
    """The agent's shell never gets the Claude token; the daemon's own steps do."""
    saved = {k: os.environ.get(k) for k in (models.CLI_TOKEN, models.BACKEND_ENV,
                                            models.AGENT_SHELL_ENV)}
    os.environ.update({models.CLI_TOKEN: "secret-claude-token", models.BACKEND_ENV: models.CLI})
    try:
        paths, stub, d, fake = first_round(tmp / "a", FakeModels(), {})
        agent = d.env()
        expect("claude-cli backend: the agent's shell has no Claude token, and is marked",
               models.CLI_TOKEN not in agent and "secret-claude-token" not in agent.values()
               and agent.get(models.AGENT_SHELL_ENV) == "1", repr(sorted(agent)))
        expect("... and the claim steps the daemon runs get it, unmarked",
               fake.envs and all(e.get(models.CLI_TOKEN) == "secret-claude-token"
                                 and models.AGENT_SHELL_ENV not in e for e in fake.envs),
               repr([sorted(e) for e in fake.envs][:1]))
        os.environ.pop(models.CLI_TOKEN)
        os.environ[models.AGENT_SHELL_ENV] = "1"
        try:
            models.check_key(models.preset.HAIKU)
            said = ""
        except models.KeyMissing as e:
            said = str(e)
        expect("a model call through the CLI in the agent's shell fails and says why",
               "kept out of the agent's shell" in said, said)
        os.environ.pop(models.AGENT_SHELL_ENV)
        os.environ[models.BACKEND_ENV] = "claude_cli"
        try:
            d.prepare()
            stopped = ""
        except alerts.Fatal as e:
            stopped = str(e)
        expect("a typo in the backend variable stops the daemon at start with options_invalid",
               stopped.startswith("daemon.options_invalid") and "claude_cli" in stopped,
               stopped)
        project = watched_project.WatchedProject.load(paths.checkout, paths.state)
        problem = project_preparation.options_problem(project) or ""
        expect("... and `verifier start` names it", "claude_cli" in problem, problem)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_aborted_note(tmp: Path) -> None:
    """The note on an aborted round skips the papers-only rounds after it."""
    paths = rt.repo(tmp)
    d = rt.StubbedDaemon(paths.root, rt.StubOpenCode())
    history = rounds.RoundHistory(paths)

    def saved(n: int, **fields: object) -> rounds.Round:
        r = rounds.Round(n=n, events=[], sent_at=0, ended_at=1, **fields)
        history.save(r)
        return r

    saved(1, aborted=True)
    saved(2, error="a claim step failed", papers_only=True)
    saved(3, error="a claim step failed", papers_only=True)
    d.round = saved(4)
    expect("aborted note: the agent is told of an aborted round before papers-only rounds",
           d.last_aborted())
    saved(5)
    d.round = saved(6)
    expect("aborted note: an agent round after the aborted round clears the note",
           not d.last_aborted())
    saved(7)
    # a papers-only round is never aborted; this one is, to show that it is skipped
    saved(8, papers_only=True, aborted=True)
    d.round = saved(9)
    expect("aborted note: the note reads the agent round before the papers-only round",
           not d.last_aborted())
    old = json.loads((paths.rounds / "7.json").read_text())
    old.pop("papers_only")
    expect("aborted note: a round file with no papers_only key reads as an agent round",
           rounds.Round.from_json(old).papers_only is False, repr(old))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_output_limit, test_claim_steps, test_tables, test_step_time,
                  test_options_mid_run, test_agent_env, test_aborted_note):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll round step cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
