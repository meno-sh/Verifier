#!/usr/bin/env python3
"""Check the model calls, the red team rounds and the alert log setup of the model-calls PR.

The cases are named by the issue of the v10 issue list that each one covers:

  - 11: git that cannot name the verifier's commit gives a `warn`, not a stopped command;
  - 14: a long Retry-After is cut to MAX_WAIT, and one that cannot be read is a `warn`;
  - 15: one model call, with its retries and re-asks, ends at its time limit;
  - 17: the red team's budget rule runs only when the check is asked for;
  - 22: a server of our own gets the caller's reasoning, for every model family;
  - 23: a version holds no URL, user name, password or GPU host;
  - 66: the help of --red-team-proposals names the rounds;
  - 67: the feedback of three rounds builds up round by round;
  - 97: vcheck records the project, not its checkout, in the alert log;
  - cut reply: a reply that the output limit cut is asked for once more at effort low.

The provider, the clock and git are fakes, so no network is used and no model is paid.
Issues 18 and 99 are in test_score_cap.py and test_uncertainty.py. Returns 1 if any case
fails, so it can gate a commit.
"""
import contextlib
import email.message
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from checks.uncertainty import quantifier, uncertainty  # noqa: E402
from checks.uncertainty.red_team import prompt, prompted, reply  # noqa: E402
from core import cli  # noqa: E402
from utils import alerts, models, version  # noqa: E402

# the made-up model hosts of these tests need no key
models.KEYS.update(dict.fromkeys(("gpu", "gpu-host", "127.0.0.1", "10.0.0.5", "10.0.0.9",
                                  "example.invalid"), None))

FAILED: list[str] = []
URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = URL + "#anthropic/claude-sonnet-5.5"
GPU = "http://user:secret@10.0.0.5:8000/v1/chat/completions#Qwen/Qwen3-32B"
KEY = "OPENROUTER_API_KEY"
CLAIM = uncertainty.Premise(id="claim", statement="Retrieval helps on every task.", value=None)
# the full paper, the one premise the check gives the red team (uncertainty.paper_premise)
PREMISES = (uncertainty.paper_premise("Retrieval adds 4 points on QA."),)


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


class Clock:
    """A fake clock. Each wait and each call moves it forward."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Reply(io.BytesIO):
    """A response body, as urlopen returns it."""
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def chat(content: str) -> bytes:
    """A chat completion reply."""
    return json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 5,
                                 "cost": 0.001}}).encode()


def cut_chat() -> bytes:
    """A chat completion reply that the output limit cut."""
    return json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "pa"}}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 65536,
                                 "cost": 0.3}}).encode()


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    """An HTTP error with a body, and Retry-After when given."""
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "status", headers, io.BytesIO(b'{"error": "busy"}'))


class Provider:
    """A fake urlopen: each call takes `takes` seconds of `clock`, up to its timeout.

    A call that needs more than its timeout raises TimeoutError. Otherwise it gives the
    next answer. Each request and its timeout are recorded.
    """

    def __init__(self, *answers: object, clock: Clock | None = None, takes: float = 0.0) -> None:
        self.answers, self.clock, self.takes = list(answers), clock, takes
        self.sent: list[dict] = []
        self.timeouts: list[float] = []

    def __call__(self, req: urllib.request.Request, timeout: float) -> Reply:
        self.sent.append(json.loads(req.data))
        self.timeouts.append(timeout)
        if self.clock is not None:
            self.clock.now += min(self.takes, timeout)
            if self.takes > timeout:
                raise TimeoutError("timed out")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return Reply(answer)


@contextlib.contextmanager
def provider(p: Provider):
    """Use `p` as urlopen, with a key set, and keep stderr quiet."""
    saved = urllib.request.urlopen, os.environ.get(KEY)
    urllib.request.urlopen, os.environ[KEY] = p, "test-key"
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield p
    finally:
        urllib.request.urlopen = saved[0]
        if saved[1] is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = saved[1]


def records(log: alerts.Log, key: str) -> list[dict]:
    """The records of the alert log with this key."""
    return [r for r in alerts.read(log.path) if r["key"] == key]


def test_retry_after(folder: Path) -> None:
    """Issue 14."""
    log = alerts.configure(folder / "alerts.jsonl", "test", folder)
    waits: list[float] = []
    with provider(Provider(http_error(429, "3600"), chat("ok"))):
        got = models.ask("q", MODEL, sleep=waits.append)
    expect("14: a Retry-After of 3600 s waits only MAX_WAIT",
           got == "ok" and waits == [models.MAX_WAIT], f"{got!r}, waits {waits}")
    waits.clear()
    with provider(Provider(http_error(429, "soon"), chat("ok"))):
        got = models.ask("q", MODEL, sleep=waits.append)
    warned = records(log, "models.retry_after_unreadable")
    expect("14: a Retry-After that cannot be read is retried after the usual wait",
           got == "ok" and waits == [models.FIRST_WAIT], f"{got!r}, waits {waits}")
    expect("14: ... and it is a warn that holds the header",
           len(warned) == 1 and warned[0]["level"] == "warn"
           and warned[0]["details"]["header"] == "soon", repr(warned))


def test_cut_reply(folder: Path) -> None:
    """A cut reply is asked for again at effort low, once; with no lower effort it fails."""
    log = alerts.configure(folder / "alerts.jsonl", "test", folder)
    with provider(Provider(cut_chat(), chat("ok"))) as p:
        got = models.ask("q", MODEL, reasoning="xhigh", sleep=lambda s: None)
    expect("cut reply: the second call goes at effort low and its reply is used",
           got == "ok" and [b.get("reasoning") for b in p.sent]
           == [{"effort": "xhigh"}, {"effort": models.EFFORT_AFTER_CUT_REPLY}], repr(p.sent))
    warned = records(log, "models.cut_reply")
    expect("cut reply: the cut is a warn with the first effort",
           len(warned) == 1 and warned[0]["level"] == "warn"
           and warned[0]["details"]["effort"] == "xhigh", repr(warned))
    with provider(Provider(cut_chat(), cut_chat())) as p:
        try:
            models.ask("q", MODEL, reasoning="high", sleep=lambda s: None)
            raised = False
        except models.ReplyCutAtLengthLimit:
            raised = True
    expect("cut reply: a second cut reply raises ReplyCutAtLengthLimit, after 2 calls",
           raised and len(p.sent) == 2, f"raised {raised}, {len(p.sent)} calls")
    for effort in (None, "low", "minimal"):
        with provider(Provider(cut_chat())) as p:
            try:
                models.ask("q", MODEL, reasoning=effort, sleep=lambda s: None)
                raised = False
            except models.ReplyCutAtLengthLimit:
                raised = True
        expect(f"cut reply: at effort {effort} there is no lower effort, so 1 call fails",
               raised and len(p.sent) == 1, f"raised {raised}, {len(p.sent)} calls")
    expect("cut reply: it is a ProviderError, so every caller that tolerates one goes on",
           issubclass(models.ReplyCutAtLengthLimit, models.ProviderError))


def test_time_limit(folder: Path) -> None:
    """Issue 15."""
    alerts.configure(folder / "alerts.jsonl", "test", folder)
    clock = Clock()
    p = Provider(*[chat("never")] * 4, clock=clock, takes=1000)
    with provider(p):
        try:
            models.ask("q", MODEL, timeout=100, sleep=clock.sleep, clock=clock)
            expect("15: calls that time out stop at the time limit", False, "no error raised")
        except models.ProviderError as e:
            expect("15: calls that time out stop at the time limit, before 4 attempts",
                   len(p.sent) == 2 and "time limit" in str(e)
                   and clock.now <= models.TIME_LIMIT * 100, f"{e}; {len(p.sent)} calls, "
                   f"{clock.now} s")
            expect("15: the second call waits only for the time that is left",
                   p.timeouts == [100, 96], repr(p.timeouts))

    clock = Clock()
    p = Provider(*[chat("not a number")] * 3, clock=clock, takes=90)

    def read(text: str) -> float:
        raise models.BadReply("the reply must be a number")
    with provider(p):
        try:
            models.ask_and_read("q", MODEL, read, timeout=100, read_attempts=3,
                                sleep=clock.sleep, clock=clock)
            expect("15: re-asks share the one time limit", False, "no error raised")
        except models.ProviderError as e:
            expect("15: re-asks share the one time limit, so the third ask runs out of time",
                   p.timeouts == [100, 100, 20] and "time limit" in str(e)
                   and clock.now <= models.TIME_LIMIT * 100,
                   f"{e}; timeouts {p.timeouts}, {clock.now} s")


def test_budget_rule() -> None:
    """Issue 17."""
    small = {"max_uncertainty_evals_per_claim": 4}
    prompted.options_fit({**small, "want_claim_uncertainty": False})
    expect("17: a cap of 4 passes when the check is turned off", True)
    try:
        prompted.options_fit({**small, "want_claim_uncertainty": True})
        expect("17: a cap of 4 is refused when the check is asked for", False, "no error")
    except ValueError as e:
        expect("17: a cap of 4 is refused when the check is asked for", "16 score calls" in str(e),
               str(e))


def test_own_server() -> None:
    """Issue 22."""
    for model, reasoning, want in [
            ("http://gpu:8000/v1/chat/completions#Qwen/Qwen3-32B", None, False),
            ("http://gpu:8000/v1/chat/completions#Qwen/Qwen3-32B", "xhigh", True),
            ("http://gpu:8000/v1/chat/completions#deepseek/r1", "high", True)]:
        p = Provider(chat("ok"))
        with provider(p):
            models.ask("q", model, reasoning=reasoning, sleep=lambda s: None)
        expect(f"22: {model.split('#')[1]} with reasoning {reasoning}: enable_thinking is {want}",
               p.sent[0].get("chat_template_kwargs") == {"enable_thinking": want},
               repr(p.sent[0]))
    p = Provider(chat("ok"))
    with provider(p):
        models.ask("q", MODEL, reasoning="xhigh", sleep=lambda s: None)
    expect("22: a host with a key gets no chat template setting",
           "chat_template_kwargs" not in p.sent[0], repr(p.sent[0]))


def test_versions() -> None:
    """Issue 23."""
    expect("23: a host with a key keeps its host and model, with no path, user or password",
           models.label("https://user:pw@openrouter.ai:443/api/v1/chat/completions#a/b")
           == "openrouter.ai#a/b")
    expect("23: two GPU hosts that serve one model have one label",
           models.label(GPU) == models.label("http://10.0.0.9:9000/v1/chat/completions"
                                             "#Qwen/Qwen3-32B") == "own-server#Qwen/Qwen3-32B")
    judge = quantifier.JudgeQuantifier(GPU, uncertainty.CUTOFF.default)
    versions = [judge.version, prompted.PromptRedTeam(model=GPU).version,
                quantifier.CoherenceQuantifier(command=None, judge=judge, model=GPU).version]
    expect("23: no version holds the GPU host, the user name or the password",
           not any(s in v for v in versions for s in ("10.0.0.5", "user", "secret", "8000")),
           repr(versions))
    expect("23: each version holds the model", all("Qwen/Qwen3-32B" in v for v in versions),
           repr(versions))


def test_help() -> None:
    """Issue 66."""
    expect("66: the help of --red-team-proposals names the rounds",
           "--red-team-iterations" in prompted.PROPOSALS.help, prompted.PROPOSALS.help)


def test_three_rounds() -> None:
    """Issue 67."""
    def round_reply(n: int) -> str:
        return json.dumps({"proposals": [
            {"hypotheticals": [{"design": f"Round {n} design {k}.",
                                "result": f"Round {n} result {k}."}]} for k in (1, 2)]})
    asked: list[str] = []

    def ask_and_read(text, model, read, timeout=300, *, system=None, reasoning=None, **kw):
        asked.append(text)
        return read(round_reply(len(asked)))
    scored: list[uncertainty.Attack] = []

    def score(attack: uncertainty.Attack) -> uncertainty.Assessment:
        scored.append(attack)
        return uncertainty.Assessment(believability=0.5, breakage=len(scored) / 10)
    team = prompted.PromptRedTeam(model=MODEL, proposals=2, results=1, iterations=3,
                                  ask_and_read=ask_and_read)
    got = team.attack(CLAIM, PREMISES, score)
    rounds = [reply.read(round_reply(n), 2, 1) for n in (1, 2, 3)]
    expect("67: three rounds give the proposals of every round, in order",
           got == rounds[0] + rounds[1] + rounds[2], repr(got))
    expect("67: the proposals of the first two rounds are scored for feedback, each once",
           scored == list(rounds[0] + rounds[1]), repr(scored))
    feedback = tuple((a, uncertainty.Assessment(believability=0.5, breakage=k / 10))
                     for k, a in enumerate(rounds[0] + rounds[1], start=1))
    expect("67: round 2 is shown round 1, and round 3 is shown rounds 1 and 2",
           asked[1] == prompt.user_text(CLAIM, PREMISES, feedback[:2], 2, 3)
           and asked[2] == prompt.user_text(CLAIM, PREMISES, feedback, 3, 3), asked[2])


def test_commit_unknown(folder: Path) -> None:
    """Issue 11."""
    home = folder / "verifier"
    (home / ".git").mkdir(parents=True)
    saved_home, saved_run = version.VERIFIER_HOME, alerts.subprocess.run
    try:
        version.VERIFIER_HOME = home
        with contextlib.redirect_stderr(io.StringIO()):
            log = alerts.configure(folder / "a.jsonl", "test", folder)
        warned = records(log, "alerts.commit_unknown")
        expect("11: git that refuses the checkout gives no commit, and the command goes on",
               log.verifier_commit is None and len(warned) == 1
               and warned[0]["level"] == "warn", repr(warned))

        def no_git(*args, **kwargs):
            raise FileNotFoundError("git")
        alerts.subprocess.run = no_git
        with contextlib.redirect_stderr(io.StringIO()):
            log = alerts.configure(folder / "b.jsonl", "test", folder)
        warned = records(log, "alerts.commit_unknown")
        expect("11: with no git installed, the command goes on, with a warn that says why",
               log.verifier_commit is None and len(warned) == 1
               and "git cannot run" in warned[0]["message"], repr(warned))
    finally:
        version.VERIFIER_HOME, alerts.subprocess.run = saved_home, saved_run


def test_vcheck_project(folder: Path) -> None:
    """Issue 97."""
    project, checkout = folder / "project", folder / "project" / ".verifier" / "checkout"
    checkout.mkdir(parents=True)
    with (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
          contextlib.suppress(SystemExit)):
        cli.main(["--state", str(project / ".verifier"), "--root", str(checkout), "fields"])
    expect("97: vcheck in a checkout records the project in the alert log",
           alerts.configured().project == project.resolve(), str(alerts.configured().project))


def main() -> int:
    """Run every group of cases and report the failures."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for name in ("14", "15", "11", "97", "cut"):
            (folder / name).mkdir()
        for group in (lambda: test_retry_after(folder / "14"),
                      lambda: test_time_limit(folder / "15"),
                      lambda: test_cut_reply(folder / "cut"), test_budget_rule,
                      test_own_server, test_versions, test_help, test_three_rounds,
                      lambda: test_commit_unknown(folder / "11"),
                      lambda: test_vcheck_project(folder / "97")):
            group()
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll model-calls cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
