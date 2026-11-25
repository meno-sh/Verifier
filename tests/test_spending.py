#!/usr/bin/env python3
"""Check the spending record, the agent's cost from OpenCode, and the cost estimate.

No model is called: a fake urlopen gives the replies, and a small SQLite file in the
layout of OpenCode 1.19 holds the agent's messages. The cases check that:
  - each reply's cost and tokens go in the spending log, with the purpose of the call,
    also from a pool thread, also for a reply the caller's reader refuses;
  - a call outside every purpose is recorded as unnamed, and a reply with no cost (a
    server of our own) counts as unknown;
  - a reply with a cost or a token count that is not a number raises;
  - the agent's calls and cost come from OpenCode's database, from a given time on,
    and a message with no cost raises; with a session id, only that session counts;
  - project_status.Spending adds the two, and `verifier status` prints the total and each part;
  - the estimate grows with the candidates, gives a range only for the uncertainty
    check, and refuses a rates file that is not in its form.
Returns 1 if any case fails.
"""
import concurrent.futures
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.agent import opencode  # noqa: E402
from runtime.project import first_round_estimate, project_status  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import alerts, models, spending  # noqa: E402

FAILED = []
MODEL = "https://openrouter.ai/api/v1/chat/completions#anthropic/claude-sonnet-5.5"
OWN = "http://gpu-host:8000/v1/chat/completions#Qwen/Qwen3-32B"


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


class Reply(io.BytesIO):
    """A response body, as urlopen returns it."""
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def chat(content: str, usage: dict | None) -> bytes:
    """A chat completion reply with this usage."""
    body = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body).encode()


def provider(*answers: bytes):
    """A fake urlopen that gives the answers in order."""
    left = list(answers)

    def fake(req: urllib.request.Request, timeout: float) -> Reply:
        return Reply(left.pop(0))
    return fake


def usage(cost: object, prompt: object = 100, completion: object = 10) -> dict:
    """A usage object."""
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cost": cost}


def test_record(tmp: Path) -> None:
    """Each reply's usage goes in the spending log, with its purpose."""
    log = alerts.configure(tmp / "runtime" / "alerts.jsonl", "vcheck", tmp)
    path = spending.log_path(log.path)
    real = urllib.request.urlopen
    os.environ["VERIFIER_TEST_KEY"] = "x"
    try:
        urllib.request.urlopen = provider(chat("a", usage(0.25)), chat("b", usage(0.5)))
        with spending.purpose("method_writer"):
            models.ask("q", MODEL, attempts=1)
        models.ask("q", MODEL, attempts=1)
        entries = spending.read(path)
        expect("record: the purpose and the cost of each reply are kept",
               [(e["purpose"], e["cost"]) for e in entries]
               == [("method_writer", 0.25), (spending.UNNAMED, 0.5)], str(entries))
        expect("record: the tokens and the process are kept",
               entries[0]["prompt_tokens"] == 100 and entries[0]["completion_tokens"] == 10
               and entries[0]["process"] == "vcheck", str(entries[0]))

        urllib.request.urlopen = provider(chat("not a number", usage(0.1)),
                                          chat("still not", usage(0.1)))
        refused = False
        with spending.purpose("quantifier"), contextlib.redirect_stderr(io.StringIO()):
            try:
                models.ask_and_read("q", MODEL, lambda t: models.read_json(t), attempts=1)
            except models.BadReply:
                refused = True
        expect("record: a reply the reader refuses is recorded too",
               refused and [e["purpose"] for e in spending.read(path)][-2:] == ["quantifier"] * 2)

        urllib.request.urlopen = provider(*(chat("x", usage(0.01)) for _ in range(3)))

        def one(_: int) -> None:
            with spending.purpose("duplicates"):
                models.ask("q", MODEL, attempts=1)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            list(pool.map(one, range(3)))
        totals = spending.totals(spending.read(path))
        expect("record: calls in pool threads keep their purpose",
               totals["duplicates"].calls == 3 and abs(totals["duplicates"].cost - 0.03) < 1e-9,
               str(totals))

        urllib.request.urlopen = provider(chat("x", {"prompt_tokens": 5,
                                                     "completion_tokens": 1}))
        models.ask("q", OWN, attempts=1)
        expect("record: a reply with no cost counts as unknown",
               spending.totals(spending.read(path))[spending.UNNAMED].unknown == 1)
        for bad, name in ((usage("0.1"), "a cost as text"), (usage(-1), "a negative cost"),
                          (usage(0.1, prompt=None), "no prompt tokens")):
            urllib.request.urlopen = provider(chat("x", bad))
            try:
                models.ask("q", MODEL, attempts=1)
                expect(f"record: {name} raises", False)
            except spending.SpendingError:
                expect(f"record: {name} raises", True)
    finally:
        urllib.request.urlopen = real
        del os.environ["VERIFIER_TEST_KEY"]


def opencode_db(runtime: Path, messages: list[dict], sessions: list[str] | None = None) -> None:
    """An OpenCode database with these messages, in the layout of OpenCode 1.19.

    `sessions` gives the session of each message; by default all are in one session.
    """
    db = opencode.database(runtime)
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("create table message (id text, session_id text, data text)")
    con.executemany("insert into message values (?, ?, ?)",
                    [(str(i), s, json.dumps(m)) for i, (m, s)
                     in enumerate(zip(messages, sessions or ["ses_a"] * len(messages),
                                      strict=True))])
    con.commit()
    con.close()


def message(created: int, cost: float) -> dict:
    """One assistant message, as OpenCode stores it."""
    return {"role": "assistant", "cost": cost, "time": {"created": created},
            "tokens": {"input": 10, "output": 5, "reasoning": 3, "cache": {"read": 100}}}


def test_agent_session(tmp: Path) -> None:
    """With a session id, only the calls of that session count."""
    paths = StatePaths(root=tmp)
    paths.ensure()
    opencode_db(paths.runtime, [message(1000, 0.5), message(2000, 0.25), message(3000, 1.0)],
                ["ses_a", "ses_b", "ses_b"])
    one = opencode.spending(paths.runtime, session_id="ses_b")
    expect("agent: only the calls of the given session",
           (one.calls, one.cost, one.tokens_read) == (2, 1.25, 220), str(one))
    expect("agent: a session with no call has no tokens",
           opencode.spending(paths.runtime, session_id="ses_c").tokens_read == 0)


def test_agent(tmp: Path) -> None:
    """The agent's calls and cost come from OpenCode's database."""
    paths = StatePaths(root=tmp)
    paths.ensure()
    expect("agent: before the first start, no call", opencode.spending(paths.runtime).calls == 0)
    opencode_db(paths.runtime, [{"role": "user", "time": {"created": 1}},
                                message(1000, 0.5), message(5000, 0.25)])
    every, later = opencode.spending(paths.runtime), opencode.spending(paths.runtime, 2000)
    expect("agent: every assistant message is one call, with its cost and tokens",
           (every.calls, every.cost, every.cache_read_tokens) == (2, 0.75, 200), str(every))
    expect("agent: only the calls from the given time on", (later.calls, later.cost) == (1, 0.25))
    expect("agent: the tokens read are the input and the cache read tokens",
           every.tokens_read == 220, str(every))

    alerts.configure(paths.alerts_log, "vcheck", paths.root)
    spending.log_path(paths.alerts_log).write_text(json.dumps(
        {"ts": 6000, "purpose": "red_team", "model": MODEL, "cost": 1.5, "prompt_tokens": 1,
         "completion_tokens": 1}) + "\n")
    spent = project_status.Spending.of_project(paths, since=2.0)
    expect("spent: the agent and the Verifier's own calls, from the given time on",
           abs(spent.cost - 1.75) < 1e-9 and spent.model_calls["red_team"].calls == 1,
           str(spent))
    line = project_status.Spending.of_project(paths).status_line()
    expect("status: one line with the total and each part",
           line.startswith("spent: $2.25 (agent $0.75 in 2 calls, red_team $1.50"), line)

    bad = tmp / "bad"
    opencode_db(bad, [{"role": "assistant", "time": {"created": 1}, "tokens": {}}])
    try:
        opencode.spending(bad)
        expect("agent: a message with no cost raises", False)
    except opencode.OpenCodeError:
        expect("agent: a message with no cost raises", True)


def test_estimate(tmp: Path) -> None:
    """The estimate grows with the candidates; only the uncertainty check is a range."""
    small, large = first_round_estimate.estimate(100), first_round_estimate.estimate(1000)
    expect("estimate: more candidates cost more and take longer",
           small.dollars[1] < large.dollars[1] and small.minutes[1] < large.minutes[1])
    expect("estimate: only the uncertainty check has a range",
           all(lo == hi for name, (lo, hi) in large.parts.items() if name != "claim_uncertainty")
           and large.parts["claim_uncertainty"][0] < large.parts["claim_uncertainty"][1])
    rates = json.loads(first_round_estimate.RATES_FILE.read_text())
    rates["parts"]["agent"]["fixed_dollars"] = "9"
    broken = tmp / "rates.json"
    broken.write_text(json.dumps(rates))
    try:
        first_round_estimate.estimate(10, broken)
        expect("estimate: a rate that is not a number is refused", False)
    except ValueError:
        expect("estimate: a rate that is not a number is refused", True)


def main() -> int:
    """Run every case and report."""
    keys = models.KEYS
    models.KEYS = {**keys, "openrouter.ai": "VERIFIER_TEST_KEY", "gpu-host": None}
    try:
        for case in (test_record, test_agent, test_agent_session, test_estimate):
            with tempfile.TemporaryDirectory() as tmp:
                case(Path(tmp))
    finally:
        models.KEYS = keys
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
