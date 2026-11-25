#!/usr/bin/env python3
"""Check the coherence quantifier's breakage from a model's next-token log probabilities.

Covers:
  - the chat: the scoring server's questions and answers, item for item;
  - breakage: P("False") / (P("True") + P("False")), with tokens such as " True" and
    "True" added up, from one chat request with the key of the URL's host;
  - a reply without both answers: the strict question once more, then the upper bound of
    the breakage, which is a `warn`; with neither answer, a provider error;
  - a reply with no usable log probabilities is a provider error;
  - no key is a provider error before any call; the key is never in the version;
  - a failed call is retried as every model call is;
  - model names: URL#MODEL only, with a chat-completions URL;
  - the options rules: exactly one of coherence_command and coherence_model.
The provider is a fake, so no model is called. Returns 1 if any case fails.
"""
import email.message
import io
import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from checks.uncertainty import quantifier, uncertainty  # noqa: E402
from utils import alerts, models  # noqa: E402

# the made-up model hosts of these tests need no key
models.KEYS.update(dict.fromkeys(("gpu", "gpu-host", "127.0.0.1", "10.0.0.5", "10.0.0.9",
                                  "example.invalid"), None))

FAILED: list[str] = []
URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = URL + "#deepseek/deepseek-v4.1-flash-1017"
JUDGE = URL + "#anthropic/claude-sonnet-5.5"
KEY = models.KEYS["openrouter.ai"]

CLAIM = uncertainty.Premise(id="c", statement="Retrieval helps every model size.", value=None)
PREMISES = (uncertainty.Premise(
                id="p1", value=68.4,
                statement="With retrieval, the 7B model answers 68.4% of questions."),
            uncertainty.Premise(id="p2", statement="Retrieval adds 4.1 points at 13B.", value=None))
ATTACK = uncertainty.Attack(hypotheticals=(
    uncertainty.Hypothetical(design="the 70B model on the same data",
                             result="retrieval lowers accuracy by 2 points"),
    uncertainty.Hypothetical(design="the 1B model", result="no change, within 1e-6")))
# The scoring server's chat for this attack: build_messages_for_question of
# project-coherence at 117328e (src/generation.py), with the questions of
# src/claim_stress/attack.py and scoring.py.
PREMISE_Q = "What is an established result of the study?"
SERVER_MESSAGES = [
    {"role": "user", "content": PREMISE_Q},
    {"role": "assistant",
     "content": "With retrieval, the 7B model answers 68.4% of questions. (value: 68.4)"},
    {"role": "user", "content": PREMISE_Q},
    {"role": "assistant", "content": "Retrieval adds 4.1 points at 13B."},
    {"role": "user",
     "content": "A follow-up experiment: the 70B model on the same data. What is its result?"},
    {"role": "assistant", "content": "retrieval lowers accuracy by 2 points"},
    {"role": "user", "content": "A follow-up experiment: the 1B model. What is its result?"},
    {"role": "assistant", "content": "no change, within 1e-6"},
    {"role": "user", "content": "Claim: Retrieval helps every model size.\n"
                                "Is the claim true, given all the results above? "
                                "Answer True or False."}]


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def raises(name: str, error: type[BaseException], call: object) -> BaseException | None:
    """Record whether `call()` raises `error`, and return the error."""
    try:
        call()
    except error as e:
        expect(name, True)
        return e
    expect(name, False, "no error raised")
    return None


class Reply(io.BytesIO):
    """A response body, as urlopen returns it."""
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def completion(top: dict[str, object] | None) -> bytes:
    """A chat completion reply with one output token and its most likely tokens."""
    logprobs = None if top is None else {"content": [{
        "token": next(iter(top)), "logprob": next(iter(top.values())),
        "top_logprobs": [{"token": t, "logprob": v} for t, v in top.items()]}]}
    return json.dumps({"choices": [{"index": 0, "finish_reason": "length",
                                    "message": {"role": "assistant", "content": "True"},
                                    "logprobs": logprobs}]}).encode()


class Provider:
    """A fake urlopen that gives the answers in order, and records each request."""

    def __init__(self, *answers: object) -> None:
        self.answers, self.sent = list(answers), []

    def __call__(self, req: urllib.request.Request, timeout: float) -> Reply:
        self.sent.append({"url": req.full_url, "key": req.get_header("Authorization"),
                          "body": json.loads(req.data)})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return Reply(answer)


def coherence(model: str = MODEL) -> quantifier.CoherenceQuantifier:
    """The coherence quantifier with `model` for breakage and a judge that is never called."""
    judge = quantifier.JudgeQuantifier(JUDGE, uncertainty.CUTOFF.default)
    return quantifier.CoherenceQuantifier(command=None, judge=judge, model=model)


def with_provider(provider: Provider, call: object, key: str | None = "test-key") -> object:
    """Run `call()` with `provider` as urlopen and `key` as the OpenRouter key; restore both."""
    saved = urllib.request.urlopen, os.environ.get(KEY)
    urllib.request.urlopen = provider
    if key is None:
        os.environ.pop(KEY, None)
    else:
        os.environ[KEY] = key
    try:
        return call()
    finally:
        urllib.request.urlopen = saved[0]
        if saved[1] is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = saved[1]


def test_messages() -> None:
    """The chat is the scoring server's, item for item."""
    got = quantifier.coherence_messages(CLAIM, PREMISES, ATTACK)
    expect("the chat is the scoring server's chat", got == SERVER_MESSAGES, repr(got))


def test_breakage() -> None:
    """One chat request, and breakage from the True and False tokens."""
    top = {" True": -0.5, "True": -2.0, " False": -1.0, " The": -3.0}
    p = Provider(completion(top))
    got = with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK))
    true, false = math.exp(-0.5) + math.exp(-2.0), math.exp(-1.0)
    expect("breakage is P(False) / (P(True) + P(False)), with ' True' and 'True' added up",
           isinstance(got, float) and abs(got - false / (true + false)) < 1e-12, repr(got))
    sent = p.sent[0] if p.sent else {}
    expect("one request to the OpenRouter URL, with the OpenRouter key",
           len(p.sent) == 1 and sent["url"] == URL and sent["key"] == "Bearer test-key",
           repr(sent))
    expect("the request asks for one token at temperature 0, its top 20, and no reasoning",
           sent.get("body") == {"model": "deepseek/deepseek-v4.1-flash-1017",
                                "messages": SERVER_MESSAGES, "max_tokens": 1,
                                "temperature": 0, "logprobs": True, "top_logprobs": 20,
                                "reasoning": {"enabled": False},
                                "provider": {"require_parameters": True}},
           repr(sent.get("body")))
    for name, answer in [("a reply with no log probabilities", completion(None)),
                         ("a log probability above 0", completion({" True": 0.5, " False": -1.0})),
                         ("a log probability that is not a number",
                          completion({" True": "high", " False": -1.0}))]:
        p = Provider(answer)
        raises(f"{name} is a provider error", models.ProviderError,
               lambda p=p: with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK)))
    expect("the answers merge whatever white space is around them",
           models.top_logprobs(json.loads(completion({" False": -1.0, "False\n": -1.0})),
                               "m") == {"False": -1.0 + math.log(2)})


def test_missing_answer(folder: Path) -> None:
    """A missing answer: the strict question, then the upper bound, then a provider error."""
    log = alerts.configure(folder / "alerts.jsonl", "test", folder)
    no_false = completion({" True": -0.5, " The": -1.5})
    both = completion({" True": -1.0, " False": -2.0})
    p = Provider(no_false, both)
    got = with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK))
    last = p.sent[1]["body"]["messages"][-1]["content"] if len(p.sent) == 2 else ""
    expect("without 'False', the model gets the strict question, and its answer is used",
           len(p.sent) == 2 and abs(got - 1 / (1 + math.e)) < 1e-12
           and last == quantifier.STRICT_QUESTION.format(claim=CLAIM.statement)
           and p.sent[1]["body"]["messages"][:-1] == SERVER_MESSAGES[:-1], f"{got}; {last!r}")
    expect("an answer that is not used is not a warn", alerts.read(log.path) == [])

    p = Provider(no_false, no_false)
    got = with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK))
    rest = 1 - math.exp(-0.5) - math.exp(-1.5)
    records = alerts.read(log.path)
    expect("still without 'False', the breakage is rest / (P(True) + rest)",
           len(p.sent) == 2 and abs(got - rest / (math.exp(-0.5) + rest)) < 1e-12, repr(got))
    expect("and the bound is a warn in the alert log",
           [(r["level"], r["key"]) for r in records] == [("warn", "coherence.bound")],
           repr(records))

    no_true = completion({" False": -0.2, " The": -2.0})
    p = Provider(no_true, no_true)
    expect("without 'True', the bound is 1",
           with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK)) == 1.0)

    neither = completion({" The": -0.2, " Based": -2.0})
    p = Provider(neither, neither)
    raises("with neither answer after the strict question, it is a provider error",
           models.ProviderError,
           lambda: with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK)))
    expect("the bound needs exactly one answer",
           quantifier.breakage_bound({"True": -1.0, "False": -1.0}) is None
           and quantifier.breakage_bound({"The": -1.0}) is None)


def test_key(folder: Path) -> None:
    """No key fails before any call; the key is never in the version; retries as ask() does."""
    p = Provider()
    e = raises("no OpenRouter key is a provider error", models.ProviderError,
               lambda: with_provider(p, lambda: coherence().breakage(CLAIM, PREMISES, ATTACK),
                                     key=None))
    expect("the error names the variable, and no call was made",
           e is not None and KEY in str(e) and not p.sent, f"{e}; {len(p.sent)} calls")
    a, b = coherence(MODEL), coherence(URL + "#deepseek/deepseek-v4-pro")
    expect("the coherence model is part of the version, and the key is not",
           a.version != b.version and "test-key" not in a.version
           and models.label(MODEL) in a.version,
           a.version)
    alerts.configure(folder / "alerts.jsonl", "test", folder)
    headers = email.message.Message()
    busy = urllib.error.HTTPError(URL, 429, "busy", headers, io.BytesIO(b"{}"))
    waits: list[float] = []
    p = Provider(busy, completion({" True": -1.0, " False": -1.0}))
    got = with_provider(p, lambda: models.next_token_logprobs(SERVER_MESSAGES, MODEL, 20,
                                                              sleep=waits.append))
    expect("HTTP 429 is retried, and the second call answers",
           got == {"True": -1.0, "False": -1.0} and len(p.sent) == 2 and waits == [4.0],
           f"{got}, {len(p.sent)} calls, waits {waits}")


def test_model_names() -> None:
    """URL#MODEL only, with a chat-completions URL; the URL's host gives the key."""
    raises("a model name with no URL is refused", ValueError,
           lambda: models.endpoint("deepseek/deepseek-v4.1-flash"))
    raises("a URL that is not a chat-completions URL is refused", ValueError,
           lambda: models.next_token_logprobs(SERVER_MESSAGES,
                                              "https://openrouter.ai/api/v1/completions#m", 20))
    gpu = "http://gpu-host:8000/v1/chat/completions#Qwen/Qwen3-8B"
    expect("the OpenRouter key is named, and any other host has none",
           models.key_name(JUDGE) == KEY and models.key_name(gpu) is None)
    p = Provider(completion({" True": -1.0, " False": -2.0}))
    with_provider(p, lambda: models.next_token_logprobs(SERVER_MESSAGES, gpu, 20), key=None)
    expect("a host with no key is called without one, with Qwen3 thinking off",
           len(p.sent) == 1 and p.sent[0]["key"] is None
           and p.sent[0]["body"].get("chat_template_kwargs") == {"enable_thinking": False},
           repr(p.sent))


def test_options() -> None:
    """The coherence quantifier needs exactly one of its command and its model."""
    want = {uncertainty.WANT.key: True,
            uncertainty.QUANTIFIER.key: quantifier.CoherenceQuantifier.name}
    for name, extra in [("both the command and the model", {"coherence_command": "score-it",
                                                            "coherence_model": MODEL}),
                        ("neither the command nor the model", {})]:
        raises(f"{name} is refused", ValueError,
               lambda extra=extra: quantifier.coherence_needs_one_source({**want, **extra}))
    for name, extra in [("only the command", {"coherence_command": "score-it"}),
                        ("only the model", {"coherence_model": MODEL})]:
        try:
            quantifier.coherence_needs_one_source({**want, **extra})
            expect(f"{name} is accepted", True)
        except ValueError as e:
            expect(f"{name} is accepted", False, str(e))
    raises("a coherence model that is not URL#MODEL is refused before the run", ValueError,
           lambda: quantifier.models_are_reachable(
               {**want, "judge_model": JUDGE, "coherence_model": "deepseek/deepseek-v4.1-flash"}))
    built = uncertainty.QUANTIFIERS["coherence"]({"coherence_model": MODEL,
                                                  "judge_model": JUDGE})
    expect("the quantifier is built with the model the options name, and the default cutoff",
           isinstance(built, quantifier.CoherenceQuantifier) and built.model == MODEL
           and built.command is None and built.judge.model == JUDGE
           and built.judge.cutoff == uncertainty.CUTOFF.default == 0.59)


def main() -> int:
    """Run every group of cases."""
    for group in (test_messages, test_breakage, test_model_names, test_options):
        print(group.__name__)
        group()
    for group in (test_missing_answer, test_key):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp).resolve())
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll coherence API cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
