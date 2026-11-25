#!/usr/bin/env python3
"""Check the claude CLI backend of utils/models.py with a fake `claude` program.

The fake (written below into a temp dir) records its arguments, its stdin and a few
variables of its environment, and answers as `claude -p --output-format json` does. No
real model is called. The cases:

  - the prompt goes on stdin, the system part in --system-prompt-file, with no tools;
  - a shared paper prefix goes first in the system file, a blank line before the system
    part; earlier turns of a conversation are refused before any call;
  - each call maps to Opus at medium (judgement) or Haiku at its own effort (mechanical),
    by its spending purpose, else by its model name; the variables change each choice;
  - no effort turns thinking off;
  - the reply's text is read, and its usage goes in the spending log at list price;
  - no OpenRouter key reaches the program;
  - a usage limit stops at once, with no retry; a rate limit is retried;
  - at most $VERIFIER_CLI_CONCURRENCY calls run at once;
  - logprobs fail loudly.

Returns 1 if any case fails.
"""
import contextlib
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))
from checks.uncertainty import quantifier, uncertainty  # noqa: E402
from checks.uncertainty.red_team import prompted  # noqa: E402
from core.claim_steps import labeller  # noqa: E402
from utils import alerts, models, shared_paper_prefix, spending  # noqa: E402

HAIKU_CLI = "claude-cli#claude-haiku-5-5"

FAILED: list[str] = []
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions#"
PAPER = "\\documentclass{article}\n\\section{Results}\nA beats B by 3 points.\n"

FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time, uuid
    from pathlib import Path
    log = Path(os.environ["FAKE_LOG"])
    me = log / ("run-" + uuid.uuid4().hex)
    me.touch()
    running = len(list(log.glob("run-*")))
    peak = log / "peak"
    try:
        old = int(peak.read_text())
    except (OSError, ValueError):
        old = 0
    if running > old:
        peak.write_text(str(running))
    args = sys.argv[1:]
    system = None
    if "--system-prompt-file" in args:
        system = Path(args[args.index("--system-prompt-file") + 1]).read_text()
    record = {{"args": args, "stdin": sys.stdin.read(), "system": system, "cwd": os.getcwd(),
              "env": {{k: os.environ.get(k) for k in (
                  "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                  "CLAUDE_CODE_DISABLE_THINKING", "CLAUDE_CONFIG_DIR")}}}}
    with open(log / "calls.jsonl", "a") as fh:
        fh.write(json.dumps(record) + "\\n")
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    me.unlink()
    mode = os.environ.get("FAKE_MODE", "ok")
    calls = sum(1 for _ in open(log / "calls.jsonl"))
    if mode == "usage_limit":
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                          "result": "You've hit your limit · resets 5pm",
                          "usage": {{"input_tokens": 0, "output_tokens": 0}},
                          "total_cost_usd": 0}}))
        sys.exit(1)
    if mode == "rate_once" and calls == 1:
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                          "result": "API Error: 529 overloaded", "api_error_status": 529}}))
        sys.exit(1)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": "pong", "stop_reason": "end_turn",
                      "total_cost_usd": 0.0012,
                      "usage": {{"input_tokens": 10, "cache_creation_input_tokens": 100,
                                "cache_read_input_tokens": 1000, "output_tokens": 7}}}}))
''')


def expect(name: str, ok: bool, detail: object = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def calls(log: Path) -> list[dict]:
    """The fake's recorded calls, oldest first."""
    f = log / "calls.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def fresh(log: Path, **env: str) -> None:
    """Empty the fake's log, set its behaviour, and reset the module's CLI slots."""
    for f in log.iterdir():
        f.unlink()
    roles = [v for m, _, e, _ in models.CLI_ROLE_SETTINGS.values() for v in (m, e) if v]
    for k in ("FAKE_MODE", "FAKE_SLEEP", models.CLI_CONCURRENCY_ENV, *roles):
        os.environ.pop(k, None)
    os.environ.update(env)
    models._cli_slots = None


def arg(args: list[str], flag: str) -> str | None:
    """The value after `flag`, or None."""
    return args[args.index(flag) + 1] if flag in args else None


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        log, bindir, work = tmp / "log", tmp / "bin", tmp / "work"
        for d in (log, bindir, work):
            d.mkdir()
        fake = bindir / "claude"
        fake.write_text(FAKE.format(python=sys.executable))
        fake.chmod(0o755)
        os.environ.update({"FAKE_LOG": str(log), models.CLI_BINARY_ENV: str(fake),
                           models.CLI_DIR_ENV: str(work), models.CLI_TOKEN: "fake-token",
                           "OPENROUTER_API_KEY": "fake-openrouter-key",
                           "ANTHROPIC_API_KEY": "fake-api-key",
                           models.BACKEND_ENV: models.CLI})
        alerts.configure(tmp / "alerts.jsonl", "test", tmp)
        no_wait = lambda s: None  # noqa: E731

        print("prompt, system, model, effort, reply, spending:")
        fresh(log)
        text = models.ask("ping?", OPENROUTER + "anthropic/claude-haiku-5.5", system="Be brief.",
                          sleep=no_wait)
        (c,) = calls(log)
        expect("the reply's text is returned", text == "pong", text)
        expect("the prompt goes on stdin", c["stdin"] == "ping?", c["stdin"])
        expect("the system part goes in --system-prompt-file", c["system"] == "Be brief.",
               c["system"])
        expect("the system file is removed after the call",
               not any(work.glob("system-*.txt")), list(work.iterdir()))
        expect("-p with no tools, no settings, JSON out",
               c["args"][0] == "-p" and arg(c["args"], "--tools") == ""
               and arg(c["args"], "--setting-sources") == ""
               and "--strict-mcp-config" in c["args"]
               and "--no-session-persistence" in c["args"]
               and arg(c["args"], "--output-format") == "json", c["args"])
        expect("anthropic/claude-haiku-5.5 maps to claude-haiku-5-5",
               arg(c["args"], "--model") == "claude-haiku-5-5", c["args"])
        expect("no effort: no --effort, thinking off",
               "--effort" not in c["args"] and c["env"]["CLAUDE_CODE_DISABLE_THINKING"] == "1",
               c)
        expect("no OpenRouter or Anthropic API key reaches claude",
               c["env"]["OPENROUTER_API_KEY"] is None and c["env"]["ANTHROPIC_API_KEY"] is None
               and c["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-token", c["env"])
        expect("claude runs in the empty work dir, with its own config dir",
               c["cwd"] == str(work.resolve()) or c["cwd"] == str(work),
               c["cwd"])
        expect("CLAUDE_CONFIG_DIR is under the work dir",
               c["env"]["CLAUDE_CONFIG_DIR"] == str(work / "config"), c["env"])
        entries = spending.read(spending.log_path(tmp / "alerts.jsonl"))
        e = entries[-1] if entries else {}
        expect("spending: one record, tokens and list-price cost",
               len(entries) == 1 and e["model"] == "claude-cli#claude-haiku-5-5"
               and e["prompt_tokens"] == 1110 and e["completion_tokens"] == 7
               and e["cost"] == 0.0012 and "list price" in e.get("cost_basis", ""), entries)

        print("shared paper prefix:")
        fresh(log)
        prefix = shared_paper_prefix.shared_paper_prefix_for_model_calls(PAPER)
        models.ask("ping?", HAIKU_CLI, system="Be brief.", shared_prefix_for_cache=prefix,
                   sleep=no_wait)
        models.ask("ping?", HAIKU_CLI, shared_prefix_for_cache=prefix, sleep=no_wait)
        with_system, alone = calls(log)
        expect("the prefix goes first in the system file, a blank line before the system part",
               with_system["system"] == prefix + "\n\nBe brief."
               and with_system["stdin"] == "ping?", with_system["system"])
        expect("with no system part, the system file is the prefix alone",
               alone["system"] == prefix, alone["system"])
        fresh(log)
        try:
            models.ask("ping?", HAIKU_CLI, earlier_turns=(("hi", "hello"),), sleep=no_wait)
            expect("earlier turns are refused on the CLI route", False, "no error")
        except ValueError as err:
            expect("earlier turns are refused on the CLI route, before any call",
                   "earlier turns" in str(err) and not calls(log), err)

        fresh(log)
        sonnet = OPENROUTER + "anthropic/claude-sonnet-5.5"
        deepseek = OPENROUTER + "deepseek/deepseek-v4.1-flash"     # any non-Haiku model
        haiku = OPENROUTER + "anthropic/claude-haiku-5.5"
        cases = [("red_team", sonnet, "xhigh"), ("model_review", deepseek, None),
                 ("quantifier", sonnet, None), ("scenario", sonnet, None),
                 ("method_writer", sonnet, None), ("label", haiku, None),
                 ("duplicates", haiku, "medium"), ("duplicates", sonnet, "xhigh"),
                 ("other", sonnet, None), ("other", haiku, "minimal"),
                 ("other", OPENROUTER + "moonshotai/kimi-k3", None),
                 ("full_paper_linker", sonnet, None)]
        for purpose, model, effort in cases:
            with spending.purpose(purpose):
                models.ask("x", model, reasoning=effort, sleep=no_wait)
        got = [(arg(c["args"], "--model"), arg(c["args"], "--effort")) for c in calls(log)]
        opus = ("claude-opus-5-5", "medium")
        want = [opus, opus, opus, opus, opus, ("claude-haiku-5-5", None),
                ("claude-haiku-5-5", "medium"), ("claude-haiku-5-5", "xhigh"), opus,
                ("claude-haiku-5-5", "low"), opus, ("claude-haiku-5-5", None)]
        for (purpose, model, effort), g, w in zip(cases, got, want, strict=True):
            expect(f"{purpose} asking {model.partition('#')[2]} at {effort} -> {w}", g == w, g)
        fresh(log, VERIFIER_CLI_JUDGEMENT_MODEL="claude-sonnet-5-5",
              VERIFIER_CLI_JUDGEMENT_EFFORT="high",
              VERIFIER_CLI_MECHANICAL_MODEL="claude-sonnet-5-5")
        with spending.purpose("model_review"):
            models.ask("x", deepseek, sleep=no_wait)
        with spending.purpose("label"):
            models.ask("x", haiku, sleep=no_wait)
        models.ask("x", "claude-cli#claude-opus-5-5", reasoning="low", sleep=no_wait)
        a = [c["args"] for c in calls(log)]
        expect("the judgement variables change its model and effort",
               (arg(a[0], "--model"), arg(a[0], "--effort")) == ("claude-sonnet-5-5", "high"),
               a[0])
        expect("the mechanical variable changes its model",
               arg(a[1], "--model") == "claude-sonnet-5-5", a[1])
        expect("claude-cli#MODEL is called as it is, at its own effort",
               (arg(a[2], "--model"), arg(a[2], "--effort")) == ("claude-opus-5-5", "low"),
               a[2])
        expect("no system part: no --system-prompt-file", "--system-prompt-file" not in a[0],
               a[0])
        fresh(log)

        print("labels and keys:")
        expect("label of a mapped model names the CLI model",
               models.label(OPENROUTER + "anthropic/claude-haiku-5.5")
               == "claude-cli#claude-haiku-5-5"
               and models.label(OPENROUTER + "anthropic/claude-sonnet-5.5")
               == "claude-cli#claude-opus-5-5")
        haiku = OPENROUTER + "anthropic/claude-haiku-5.5"
        judge = quantifier.JudgeQuantifier(haiku, uncertainty.CUTOFF.default)
        coherence = quantifier.CoherenceQuantifier(None, judge, model=haiku)
        red = prompted.PromptRedTeam(haiku)
        expect("the judge's, the coherence model's and the red team's versions name the "
               "model their purpose calls (Opus), not the one the model name alone gives",
               "/claude-cli#claude-opus-5-5/" in judge.version
               and coherence.version.split("/")[2] == "claude-cli#claude-opus-5-5"
               and "/claude-cli#claude-opus-5-5/" in red.version,
               (judge.version, coherence.version, red.version))
        fresh(log)
        with contextlib.suppress(models.BadReply):   # the fake answers "pong", no number
            judge.number("Rate it.", "believability", (uncertainty.paper_premise(PAPER),))
        called = {arg(c["args"], "--model") for c in calls(log)}
        expect("the judge calls the model its version names",
               called and all(f"claude-cli#{m}/" in judge.version for m in called), called)
        expect("the judge's system file is the paper prefix, then its own system part",
               calls(log) and all(c["system"] == prefix + "\n\n" + quantifier.AFTER_PAPER_SYSTEM
                                  for c in calls(log)),
               [c["system"] for c in calls(log)])
        fresh(log)
        item = labeller.items({"c1": {"kind": "sentence", "text": "A beats B."}})[0]
        settings = labeller.Settings(model=haiku)
        before = labeller.cache_key(item, settings)
        fresh(log, VERIFIER_CLI_MECHANICAL_MODEL="claude-sonnet-5-5")
        after = labeller.cache_key(item, settings)
        fresh(log)
        expect("the label cache key changes with the Claude model the CLI calls",
               before != after)
        os.environ[models.BACKEND_ENV] = models.OPENROUTER
        plain = labeller.cache_key(item, settings)
        os.environ[models.BACKEND_ENV] = models.CLI
        expect("... and through OpenRouter it is the key the model name alone gives",
               plain not in (before, after))
        expect("key_name of a CLI model is the OAuth token variable",
               models.key_name("claude-cli#claude-opus-5-5") == models.CLI_TOKEN)
        expect("key_variables passes the token and the backend on",
               {models.CLI_TOKEN, models.BACKEND_ENV} <= set(models.key_variables()))
        saved = os.environ.pop(models.CLI_TOKEN)
        try:
            models.check_key("claude-cli#claude-opus-5-5")
            expect("check_key refuses a CLI model with no token", False, "no error")
        except models.KeyMissing as err:
            expect("check_key refuses a CLI model with no token", models.CLI_TOKEN in str(err))
        os.environ[models.CLI_TOKEN] = saved
        models.check_key("claude-cli#claude-opus-5-5")
        expect("check_key passes with the token and the program", True)

        print("failures:")
        fresh(log, FAKE_MODE="usage_limit")
        slept: list[float] = []
        try:
            models.ask("x", HAIKU_CLI, sleep=slept.append)
            expect("a usage limit raises UsageLimit", False, "no error")
        except models.ProviderError as err:
            expect("a usage limit raises UsageLimit",
                   isinstance(err, models.UsageLimit) and "usage limit" in str(err), err)
        expect("a usage limit is not retried", len(calls(log)) == 1 and not slept,
               (len(calls(log)), slept))
        try:
            models.call_all_at_once([lambda: models.ask("x", HAIKU_CLI)] * 3, 1,
                                    lambda k, r: None, tolerated=(models.ProviderError,),
                                    finish_running=True)
            expect("call_all_at_once stops on a usage limit even when it tolerates errors",
                   False, "no error")
        except models.UsageLimit:
            expect("call_all_at_once stops on a usage limit even when it tolerates errors",
                   True)

        fresh(log, FAKE_MODE="rate_once")
        slept = []
        text = models.ask("x", HAIKU_CLI, sleep=slept.append)
        expect("an overloaded reply is retried after a wait, then answers",
               text == "pong" and len(calls(log)) == 2 and slept == [models.FIRST_WAIT],
               (text, len(calls(log)), slept))

        fresh(log)
        try:
            models.next_token_logprobs([{"role": "user", "content": "x"}],
                                       OPENROUTER + "anthropic/claude-sonnet-5.5", 5)
            expect("logprobs on the CLI backend fail loudly", False, "no error")
        except models.ProviderError as err:
            expect("logprobs on the CLI backend fail loudly",
                   "log probabilities" in str(err) and not calls(log), err)

        print("concurrency:")
        fresh(log, FAKE_SLEEP="0.4", **{models.CLI_CONCURRENCY_ENV: "2"})
        got: dict[int, str] = {}
        models.call_all_at_once([lambda: models.ask("x", HAIKU_CLI)] * 8, 8, got.__setitem__)
        out = [got[k] for k in range(8)]
        peak = int((log / "peak").read_text())
        expect("8 calls in 8 threads, at most 2 claude programs at once",
               out == ["pong"] * 8 and peak == 2, (out, peak))

    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll claude CLI cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
