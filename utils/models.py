"""Calls to a model, with the retries every caller shares.

Every model name is URL#MODEL. utils/model_keys.json gives the key variable of each
host (api_key); a host not in it is refused. ask() returns the reply to a prompt;
next_token_logprobs() returns the most likely first tokens of a reply. Both retry a
failed call that a later call can fix; ask_and_read() also asks again when the caller's
reader refuses the reply. Each failed attempt is a `warn` in the alert log. When the
attempts or the time limit run out, the error is raised. The usage of every reply goes in
the spending log (utils/spending.py). label() is the part of a model name that saved
results may keep. $VERIFIER_MODEL_BACKEND=claude-cli runs ask() as `claude -p` (BACKEND_ENV).
"""
from __future__ import annotations

import concurrent.futures
import email.utils
import functools
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar

from utils import alerts, spending

# Each model host, and the environment variable that holds its key; "" for no key.
KEYS_FILE = Path(__file__).with_name("model_keys.json")
# For each OpenCode provider: a URL whose host gives the key (KEYS_FILE), and a URL that
# answers a GET with a valid key with 200.
AGENT_PROVIDERS = {"openrouter": ("https://openrouter.ai/api/v1/chat/completions",
                                  "https://openrouter.ai/api/v1/key")}
CHAT = "/chat/completions"        # the path that ends every model's URL
ATTEMPTS = 4                # calls in all, for a failure that a later call can fix
READ_ATTEMPTS = 2           # replies in all, for a reply the caller's reader refuses
FIRST_WAIT = 4.0            # seconds before the second call; it doubles each time,
MAX_WAIT = 60.0             # up to this, also when the provider asks for a longer wait
TIME_LIMIT = 2              # one call, with every retry and re-ask, takes at most this
                            # many times the timeout of one reply
OWN_SERVER = "own-server"   # label() gives this for a host with no key, such as vLLM
RETRY_CODES = frozenset({408, 429})     # and every 5xx
EFFORTS = ("max", "xhigh", "high", "medium", "low", "minimal", "none")   # OpenRouter's list
# The effort of the one new call after a reply that the output limit cut (ask()).
EFFORT_AFTER_CUT_REPLY = "low"
# The cache mark of a shared prefix (ask's `shared_prefix_for_cache`): the provider keeps the
# prefix for 1 hour after its last use. A write of the prefix costs 2 times the input price
# on Anthropic models; a read costs 0.1 times.
SHARED_PREFIX_CACHE_MARK = {"type": "ephemeral", "ttl": "1h"}
SHARED_PREFIX_CACHE_SECONDS = 3600
# A call that starts later than this after the last finished call with the same prefix and
# model goes alone again, because the provider may have dropped the prefix by then.
SHARED_PREFIX_SURELY_CACHED_SECONDS = SHARED_PREFIX_CACHE_SECONDS - 300

# Which route the model calls take. With "claude-cli", every ask() runs `claude -p` (the
# Claude Code CLI) on the Claude account whose OAuth token is in CLI_TOKEN, and an
# OpenRouter model name is mapped to a Claude model (cli_model). A name CLI#MODEL, such
# as "claude-cli#claude-opus-5-5", takes that route under either backend.
BACKEND_ENV = "VERIFIER_MODEL_BACKEND"
OPENROUTER, CLI = "openrouter", "claude-cli"
BACKENDS = (OPENROUTER, CLI)
CLI_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"      # the Claude account's token, which `claude` reads
# Set in the auditing agent's shell (runtime/daemon.py). CLI_TOKEN is kept out of that
# shell, so the agent's model cannot read the Claude token; a CLI call there says why.
AGENT_SHELL_ENV = "VERIFIER_AGENT_SHELL"
CLI_BINARY_ENV = "VERIFIER_CLAUDE"         # the claude program; "claude" on PATH by default
CLI_DIR_ENV = "VERIFIER_CLI_DIR"           # its empty working directory; a new temp dir by default
CLI_CONCURRENCY_ENV = "VERIFIER_CLI_CONCURRENCY"   # CLI calls at once in one process
CLI_MAX_OUTPUT_TOKENS_ENV = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"   # `claude` reads its output limit here
CLI_CONCURRENCY = 12             # 12 at once gave no rate-limit error (24 Haiku calls)
# The one table of the CLI backend: which Claude model, at which effort, answers each
# call. A call goes by the spending purpose its caller names (CLI_PURPOSE_ROLES); a call
# with another purpose goes by the model it asks for: a Haiku model is mechanical, any
# other model (Sonnet, DeepSeek Chat, Kimi, ...) is judgement. A judgement call asks
# Opus at medium effort, whatever effort it asks for; a mechanical call asks Haiku at
# the effort it asks for. Each model and the judgement effort can be changed with its
# variable. A model named CLI#MODEL is called as it is, with the effort it asks for.
JUDGEMENT, MECHANICAL = "judgement", "mechanical"
CLI_PURPOSE_ROLES = {
    "model_review": JUDGEMENT,      # checks/model_review.py: reads the paper, flags problems
    "setup_review": JUDGEMENT,      # checks/setup_review: the paper and its setup register
    "quantifier": JUDGEMENT,        # checks/quantifier.py: the judge scores an attack
    "red_team": JUDGEMENT,          # checks/red_team/prompted.py: writes attacks on a claim
    "scenario": JUDGEMENT,          # checks/scenario.py: writes the gap of the worst attack
    "method_writer": JUDGEMENT,     # core/methods/method_writer.py: writes recomputation methods
    "label": MECHANICAL,            # utils/labeller.py: stage 2, keep or drop each piece
    "duplicates": MECHANICAL,       # utils/duplicates.py: stage 3, grouping proposals
    "claim_reordering": MECHANICAL,     # core/claim_steps/claim_reordering.py: sorts the claims
    "full_paper_linker": MECHANICAL,    # core/claim_steps/full_paper_linker.py: stage 5, links
}
CLI_ROLE_SETTINGS = {   # role: (model variable, default model, effort variable, default effort)
    JUDGEMENT: ("VERIFIER_CLI_JUDGEMENT_MODEL", "claude-opus-5-5",
                "VERIFIER_CLI_JUDGEMENT_EFFORT", "medium"),
    MECHANICAL: ("VERIFIER_CLI_MECHANICAL_MODEL", "claude-haiku-5-5", None, None),
}
CLI_SETTINGS = (CLI_BINARY_ENV, CLI_DIR_ENV, CLI_CONCURRENCY_ENV,
                *(v for model_var, _, effort_var, _ in CLI_ROLE_SETTINGS.values()
                  for v in (model_var, effort_var) if v is not None))
CLI_EFFORTS = ("low", "medium", "high", "xhigh", "max")      # what `claude --effort` takes
CLI_COST_BASIS = "list price; the Claude account pays by subscription"
# A failed CLI reply whose text matches is the account's usage limit: no retry can fix it.
USAGE_LIMIT = re.compile(r"hit your [\w' ]{0,40}limit|usage limit|limit reached|"
                         r"out of (extra )?usage|spend limit", re.I)
# A failed CLI reply whose text matches can be fixed by a later call.
CLI_RETRY = re.compile(r"rate.?limit|overloaded|\b(429|500|502|503|504|529)\b|"
                       r"timed? ?out|ECONNRESET|socket hang up|network", re.I)


def backend() -> str:
    """The route of the model calls: $VERIFIER_MODEL_BACKEND, or "openrouter" when unset.

    Raises:
        ValueError: the variable is set to a name not in BACKENDS.
    """
    name = os.environ.get(BACKEND_ENV) or OPENROUTER
    if name not in BACKENDS:
        raise ValueError(f"${BACKEND_ENV} must be one of {', '.join(BACKENDS)}, not {name!r}")
    return name


# A reply that is one JSON value inside one ```json fence, with nothing else around it.
FENCED = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.S)

# Added to the prompt after a reply the reader refused.
RETRY_NOTE = ("\n\nYOUR LAST REPLY BROKE A RULE: {error}\n"
              "Reply again, and follow every rule above.")


class ProviderError(Exception):
    """The model gave no usable answer: no key, a refused call, or a cut or empty reply."""


class ReplyCutAtLengthLimit(ProviderError):
    """The reply stopped at the output limit of the model, before its end."""


class BadReply(ValueError):
    """The reply breaks a rule of the caller's reader. The message names the rule."""


class preset:  # noqa: N801 - a namespace, read as models.preset.SONNET
    """The default models for ask() and next_token_logprobs().

    Other modules name a model only with these, or with a name a person gives, so that no
    other module knows the form of a model name or its provider.
    """
    SONNET: ClassVar[str] = "https://openrouter.ai/api/v1/chat/completions#anthropic/claude-sonnet-5.5"
    HAIKU: ClassVar[str] = "https://openrouter.ai/api/v1/chat/completions#anthropic/claude-haiku-5.5"
    OPUS: ClassVar[str] = "https://openrouter.ai/api/v1/chat/completions#anthropic/claude-opus-5.5"


class agent_preset:  # noqa: N801 - a namespace, read as models.agent_preset.KIMI
    """The default models of the auditing agent, in OpenCode's form (agent_key_problem)."""
    KIMI: ClassVar[str] = "openrouter/moonshotai/kimi-k3"


class KeyMissing(ProviderError, ValueError):
    """The host of a model needs a key, and its variable is not set.

    A ValueError too, so that a run option rule can raise it before the run.
    """


class UsageLimit(ProviderError):
    """The Claude account hit its usage limit; no call can work until it resets.

    call_all_at_once() stops the calls on it, whatever the caller tolerates.
    """


def load_keys(path: Path = KEYS_FILE) -> dict[str, str | None]:
    """Each host in the file, and the variable that holds its key, or None for no key.

    Raises:
        ValueError: the file is not one JSON object of host names to variable names.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or not all(
            isinstance(k, str) and k and isinstance(v, str) for k, v in raw.items()):
        raise ValueError(f"{path} must be one JSON object that maps each host to the name "
                         f'of its key variable, or to "" for a host with no key')
    return {host: name or None for host, name in raw.items()}


KEYS = load_keys()


class _Retryable(ProviderError):
    """A failure that a later call can fix, such as HTTP 429 or a timeout."""

    def __init__(self, message: str, wait: float | None = None) -> None:
        """Keep the message, and the wait the provider asked for, if it did."""
        super().__init__(message)
        self.wait = wait


def ask(prompt: str, model: str, timeout: int = 300, *, system: str | None = None,
        shared_prefix_for_cache: str | None = None, cache_system_after_shared_prefix: bool = False,
        earlier_turns: Sequence[tuple[str, str]] = (),
        reasoning: str | None = None, max_output_tokens: int | None = None,
        attempts: int = ATTEMPTS, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic, deadline: float | None = None) -> str:
    """Send one prompt to `model`, and return the text of the reply.

    A call that fails with HTTP 408, 429 or 5xx, a timeout or a broken connection is
    made again, up to `attempts` calls in all. Each failure is a `warn` in the alert log.
    All the calls together take at most TIME_LIMIT times `timeout`, or end at `deadline`.
    A host with no key is a server of our own, such as vLLM. It gets the reasoning as
    `enable_thinking` for the chat template, which a template with no thinking ignores.

    Args:
        prompt: the user part.
        model: URL#MODEL, with a URL that ends in "/chat/completions".
        timeout: seconds to wait for one reply.
        system: static instructions; they go first, or after `shared_prefix_for_cache`.
        shared_prefix_for_cache: text that many calls of many callers start with, such as
            the full paper (utils/shared_paper_prefix.py). It goes first, and it is the
            only part marked for the provider's cache. The first call with a prefix and
            a model goes alone (FirstCallAloneGate).
        cache_system_after_shared_prefix: also mark `system` for the cache, for a caller
            whose many calls repeat a long `system` after the shared prefix.
        earlier_turns: (user, assistant) pairs of the same conversation, before `prompt`.
        reasoning: a reasoning effort, such as "xhigh"; None for no reasoning.
        max_output_tokens: the most tokens the reply may have, with its reasoning; None
            for the provider's default. A caller whose model reasons at length gives a
            high limit, so the reasoning does not use up the output tokens.
        attempts: calls in all, at least 1.
        sleep: waits between calls; tests give one that does not wait.
        clock: the time in seconds, for the time limit; tests give a fake one.
        deadline: the `clock` time at which the calls stop; ask_and_read() gives it.

    A reply that the output limit cut, at an effort above EFFORT_AFTER_CUT_REPLY, is asked
    for once more at EFFORT_AFTER_CUT_REPLY, because the reasoning used up the output
    tokens. The cut is a `warn` in the alert log.

    Raises:
        ValueError: `model` is not URL#MODEL, or its URL is not a chat-completions URL.
        ReplyCutAtLengthLimit: the output limit cut the reply, and no lower effort is left.
        ProviderError: there is no key, the provider refused the call, the reply is
            empty, the last of `attempts` calls failed, or the time ran out.
    """
    call = functools.partial(_ask_at_effort, prompt, model, timeout, system=system,
                             shared_prefix_for_cache=shared_prefix_for_cache,
                             cache_system_after_shared_prefix=cache_system_after_shared_prefix,
                             earlier_turns=earlier_turns, max_output_tokens=max_output_tokens,
                             attempts=attempts, sleep=sleep, clock=clock,
                             deadline=_deadline(clock, timeout, deadline))
    try:
        return call(reasoning=reasoning)
    except ReplyCutAtLengthLimit as e:
        if not effort_is_above(reasoning, EFFORT_AFTER_CUT_REPLY):
            raise
        alerts.notify("warn", "models.cut_reply", f"{model}: {e}; asking again at effort "
                      f"{EFFORT_AFTER_CUT_REPLY}", error=e, model=model, effort=reasoning)
        return call(reasoning=EFFORT_AFTER_CUT_REPLY)


def effort_is_above(effort: str | None, other: str) -> bool:
    """True when `effort` is a reasoning effort above `other` in EFFORTS; None is no reasoning."""
    return effort is not None and EFFORTS.index(effort) < EFFORTS.index(other)


def _ask_at_effort(prompt: str, model: str, timeout: int, *, system: str | None,
                   shared_prefix_for_cache: str | None, cache_system_after_shared_prefix: bool,
                   earlier_turns: Sequence[tuple[str, str]], reasoning: str | None,
                   max_output_tokens: int | None, attempts: int,
                   sleep: Callable[[float], None], clock: Callable[[], float],
                   deadline: float) -> str:
    """One ask() at one effort, with the retries of _call(); the arguments are ask()'s."""
    if uses_cli(model):
        if earlier_turns:
            raise ValueError(f"{model}: the claude CLI route takes one prompt, so it cannot "
                             "send earlier turns of a conversation")
        cli_system = (system if shared_prefix_for_cache is None
                      else "\n\n".join(p for p in (shared_prefix_for_cache, system)
                                       if p is not None))
        return FIRST_CALL_ALONE.call(
            model, shared_prefix_for_cache,
            lambda waited: _ask_cli(prompt, model, timeout, cli_system, reasoning,
                                    max_output_tokens, attempts, sleep, clock,
                                    deadline + waited))
    url, name = endpoint(model)
    if not url.endswith(CHAT):
        raise ValueError(f"ask() needs a URL that ends in {CHAT}, got {model!r}")
    key = api_key(url)
    payload = request_body(prompt, name, system=system,
                           shared_prefix_for_cache=shared_prefix_for_cache,
                           cache_system_after_shared_prefix=cache_system_after_shared_prefix,
                           earlier_turns=earlier_turns, reasoning=reasoning,
                           max_output_tokens=max_output_tokens)
    if key is None:
        payload["chat_template_kwargs"] = {"enable_thinking": reasoning is not None}
    body = json.dumps(payload).encode()
    return FIRST_CALL_ALONE.call(
        model, shared_prefix_for_cache,
        lambda waited: _call(lambda seconds: _post(url, body, key, seconds), model, timeout,
                             attempts, sleep, lambda reply: reply_text(reply, model),
                             deadline + waited, clock))


class FirstCallAloneGate:
    """Makes the first call with a shared prefix and a model go alone, before the others.

    A provider writes a prefix to its cache only after it has read a request. Calls that
    start at the same time all miss the cache, and each one pays for a write of the
    whole prefix. So the first call with a (model, prefix) pair goes alone, and the
    calls with the same pair wait until it has finished. If it fails, the next waiting
    call goes alone. A pair whose last call finished more than
    SHARED_PREFIX_SURELY_CACHED_SECONDS ago goes alone again. The gate works inside one
    process; calls of two processes do not wait for each other.

    The time that a call waits here is not part of its time limit: `send` gets the waited
    seconds, and the caller moves its deadline by them. Otherwise a slow first call uses
    up the time of the waiting calls, and they fail with "no time is left".
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """An empty gate that reads the time from `clock`; tests give a fake one."""
        self.clock = clock
        self.lock = threading.Lock()
        self.running_first_call: dict[tuple[str, str], threading.Event] = {}
        self.last_finished_call: dict[tuple[str, str], float] = {}

    def call[T](self, model: str, shared_prefix: str | None,
                send: Callable[[float], T]) -> T:
        """`send(waited_seconds)`, alone if it is the first call with `shared_prefix` and `model`.

        With no shared prefix, `send(0.0)` runs at once. Otherwise `send` gets the seconds
        that this call waited for a first call.
        """
        if shared_prefix is None:
            return send(0.0)
        pair = (model, hashlib.sha256(shared_prefix.encode()).hexdigest())
        started = time.monotonic()
        first_call_done = self._wait_for_turn(pair)
        try:
            result = send(time.monotonic() - started)
        except BaseException:
            self._finish(pair, first_call_done, succeeded=False)
            raise
        self._finish(pair, first_call_done, succeeded=True)
        return result

    def _wait_for_turn(self, pair: tuple[str, str]) -> threading.Event | None:
        """Wait while a first call with `pair` runs; an Event when this call goes alone."""
        while True:
            with self.lock:
                running = self.running_first_call.get(pair)
                if running is None:
                    last = self.last_finished_call.get(pair)
                    if (last is not None
                            and self.clock() - last < SHARED_PREFIX_SURELY_CACHED_SECONDS):
                        return None
                    done = threading.Event()
                    self.running_first_call[pair] = done
                    return done
            running.wait()

    def _finish(self, pair: tuple[str, str], first_call_done: threading.Event | None,
                succeeded: bool) -> None:
        """Note when a call with `pair` finished, and let the waiting calls go."""
        with self.lock:
            if succeeded:
                self.last_finished_call[pair] = self.clock()
            if first_call_done is not None:
                del self.running_first_call[pair]
                first_call_done.set()


FIRST_CALL_ALONE = FirstCallAloneGate()


def next_token_logprobs(messages: list[dict[str, str]], model: str, top: int,
                        timeout: int = 300, *, attempts: int = ATTEMPTS,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic) -> dict[str, float]:
    """The log probabilities of the `top` most likely first tokens of the reply to `messages`.

    The request asks for one output token at temperature 0, with no reasoning. Tokens
    that are the same text without white space at either end (" True" and "True") are
    one entry, with their probabilities added. Failed calls are retried as in ask().

    Args:
        messages: the chat, as {"role": ..., "content": ...} items; the last is the user's.
        model: URL#MODEL, with a URL that ends in "/chat/completions".
        top: how many tokens the provider returns, at least 1.
        timeout: seconds to wait for one reply.
        attempts: calls in all, at least 1.
        sleep: waits between calls; tests give one that does not wait.
        clock: the time in seconds, for the time limit; tests give a fake one.

    Raises:
        ValueError: `model` is not URL#MODEL, or its URL is not a chat-completions URL.
        ProviderError: as ask() raises it, or the reply has no usable log probabilities.
    """
    if top < 1:
        raise ValueError(f"top must be at least 1, got {top}")
    if uses_cli(model):
        raise ProviderError(
            f"{model}: next-token log probabilities cannot be read through the claude CLI "
            f"({BACKEND_ENV}={backend()}); the coherence quantifier with a coherence model "
            f"needs an OpenRouter or own-server model, so run it with {BACKEND_ENV}="
            f"{OPENROUTER}, or use the judge quantifier or a coherence command")
    url, name = endpoint(model)
    if not url.endswith(CHAT):
        raise ValueError(f"next_token_logprobs() needs a URL that ends in {CHAT}, got {model!r}")
    key = api_key(url)
    payload: dict = {"model": name, "messages": messages, "max_tokens": 1, "temperature": 0,
                     "logprobs": True, "top_logprobs": top, "reasoning": {"enabled": False},
                     # OpenRouter sends the request only to providers that return logprobs
                     "provider": {"require_parameters": True}}
    if key is None:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    return _call(lambda seconds: _post(url, body, key, seconds), model, timeout, attempts,
                 sleep, lambda reply: top_logprobs(reply, model),
                 _deadline(clock, timeout, None), clock)


def _deadline(clock: Callable[[], float], timeout: int, deadline: float | None) -> float:
    """The `clock` time at which a call and its retries stop: `deadline`, or TIME_LIMIT timeouts."""
    return clock() + TIME_LIMIT * timeout if deadline is None else deadline


def _call[T](send: Callable[[int], object], model: str, timeout: int, attempts: int,
             sleep: Callable[[float], None], read: Callable[[object], T], deadline: float,
             clock: Callable[[], float]) -> T:
    """`send` one request and `read` the parsed reply, calling again after a retryable failure.

    `send(seconds)` makes one call that waits at most that long, and returns the parsed
    reply (_post for an HTTP model, _run_cli for a CLI model). Each call waits at most
    `timeout`, or the time left before `deadline` if that is less. A retry whose wait
    ends at or after `deadline` is not made. The usage of every reply goes in the
    spending log under `model`.

    Raises:
        ProviderError: the last of `attempts` calls failed, the time ran out, or a failure
            no call can fix.
    """
    if attempts < 1:
        raise ValueError(f"attempts must be at least 1, got {attempts}")
    for attempt in range(1, attempts + 1):
        left = deadline - clock()
        if left <= 0:
            raise ProviderError(f"{model}: no time is left for attempt {attempt} of {attempts}")
        try:
            payload = send(min(timeout, math.ceil(left)))
            if isinstance(payload, dict):
                spending.record(model, payload)     # a refused reply costs money too
            return read(payload)
        except _Retryable as e:
            if attempt == attempts:
                raise ProviderError(f"{model}: {e} (attempt {attempt} of {attempts})") from e
            wait = min(e.wait if e.wait is not None else FIRST_WAIT * 2 ** (attempt - 1),
                       MAX_WAIT)
            if clock() + wait >= deadline:
                raise ProviderError(f"{model}: {e} (attempt {attempt} of {attempts}; the "
                                    f"time limit ends before the next attempt)") from e
            alerts.notify("warn", "models.retry", f"{model}: {e}", error=e, model=model,
                          attempt=attempt, of=attempts, wait_s=wait)
            sleep(wait)
    raise AssertionError("unreachable: the loop returns or raises")


def ask_and_read[T](prompt: str, model: str, read: Callable[[str], T], timeout: int = 300, *,
                    system: str | None = None, shared_prefix_for_cache: str | None = None,
                    cache_system_after_shared_prefix: bool = False,
                    earlier_turns: Sequence[tuple[str, str]] = (),
                    reasoning: str | None = None,
                    attempts: int = ATTEMPTS, read_attempts: int = READ_ATTEMPTS,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> T:
    """ask(), then `read` the reply; a reply `read` refuses is asked for again.

    `read` turns the text into a value, or raises BadReply. The prompt is sent again
    with the error added and the same system part and shared prefix, up to
    `read_attempts` replies in all. Each refused reply is a `warn` in the alert log, with
    the whole reply. Every call, retry and re-ask together takes at most TIME_LIMIT times
    `timeout`.

    Raises:
        BadReply: the last of `read_attempts` replies was refused.
        ProviderError: as ask() raises it, also when the time runs out.
    """
    if read_attempts < 1:
        raise ValueError(f"read_attempts must be at least 1, got {read_attempts}")
    deadline = _deadline(clock, timeout, None)
    sent = prompt
    for attempt in range(1, read_attempts + 1):
        text = ask(sent, model, timeout, system=system,
                   shared_prefix_for_cache=shared_prefix_for_cache,
                   cache_system_after_shared_prefix=cache_system_after_shared_prefix,
                   earlier_turns=earlier_turns, reasoning=reasoning,
                   attempts=attempts, sleep=sleep, clock=clock, deadline=deadline)
        try:
            return read(text)
        except BadReply as e:
            alerts.notify("warn", "models.bad_reply", f"{model}: {e}", error=e, model=model,
                          attempt=attempt, of=read_attempts, reply=text)
            if attempt == read_attempts:
                raise
            sent = prompt + RETRY_NOTE.format(error=e)
    raise AssertionError("unreachable: the loop returns or raises")


def call_all_at_once[T](calls: Sequence[Callable[[], T]], workers: int,
                        on_done: Callable[[int, T], None],
                        tolerated: tuple[type[Exception], ...] = (),
                        finish_running: bool = False) -> list[int]:
    """Run the calls in `workers` threads, and give each result to `on_done` as it comes.

    A call that raises an error of a `tolerated` type is counted as failed, and the
    others go on. KeyMissing is never tolerated. Any other error stops the calls: the
    calls not started are cancelled, and the error is raised. Without `finish_running`
    it is raised at once, without waiting for the calls already running. With it, those
    calls are waited for first, and each one that returns still goes to `on_done`, so a
    caller keeps the answers it paid for. An interrupt (KeyboardInterrupt) never waits.

    A UsageLimit stops the calls the same way, whatever is tolerated: no call can work
    until the account's limit resets.

    Args:
        calls: the calls, each with no arguments.
        workers: how many calls run at once.
        on_done: gets the index of a call in `calls` and its result.
        tolerated: the error types that only fail their own call.
        finish_running: wait for the running calls before an error is raised.

    Returns:
        The indexes of the failed calls, sorted.
    """
    failed: list[int] = []
    failure: Exception | None = None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(call): k for k, call in enumerate(calls)}
        for future in concurrent.futures.as_completed(futures):
            if future.cancelled():
                continue
            k = futures[future]
            try:
                result = future.result()
            except Exception as e:
                if isinstance(e, tolerated) and not isinstance(e, KeyMissing | UsageLimit):
                    failed.append(k)
                    continue
                if not finish_running:
                    raise
                if failure is None:
                    failure = e
                    for other in futures:
                        other.cancel()
                continue
            on_done(k, result)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    if failure is not None:
        raise failure
    return sorted(failed)


def settings_problem(model: str, effort: str, timeout: int,
                     names: tuple[str, str, str] = ("the model", "the effort", "the timeout"),
                     no_effort: str | None = None) -> str | None:
    """What is wrong with a model caller's model, effort and timeout, or None.

    The model must be a model id, the effort one of EFFORTS (or `no_effort`, a caller's
    word for none, such as "off"), and the timeout at least one second. `names` are the
    three settings' names as the message gives them.
    """
    model_name, effort_name, timeout_name = names
    if not model.strip():
        return f"{model_name} must be a model id, not empty"
    if effort != no_effort and effort not in EFFORTS:
        either = f"{no_effort} or " if no_effort is not None else ""
        return f"{effort_name} must be {either}one of {', '.join(EFFORTS)}, not {effort!r}"
    if timeout < 1:
        return f"{timeout_name} must be at least 1 second, got {timeout}"
    return None


def read_json(text: str) -> object:
    """The one JSON value a reply holds, for every reader of a model's JSON reply.

    The reply must be the JSON value alone, or the JSON value alone inside one
    ```json fence. White space around it is allowed; any other text is not.

    Raises:
        BadReply: there is other text, or the JSON does not parse.
    """
    body = text.strip()
    fenced = FENCED.fullmatch(body)
    if fenced:
        body = fenced.group(1)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise BadReply("the reply is not one JSON value, alone or in one ```json fence, "
                       f"with nothing else ({e})") from e


def request_body(prompt: str, model: str, *, system: str | None = None,
                 shared_prefix_for_cache: str | None = None,
                 cache_system_after_shared_prefix: bool = False,
                 earlier_turns: Sequence[tuple[str, str]] = (),
                 reasoning: str | None = None, max_output_tokens: int | None = None) -> dict:
    """The JSON body of one chat completion request.

    With no reasoning, the temperature is 0. With reasoning, the body has no
    temperature (thinking models accept none) and no token budget. With
    `max_output_tokens`, the body has it as `max_tokens`, the limit of the reply with its
    reasoning; the provider then gives the effort its share of that limit.

    With a shared prefix, the system message starts with it, with a cache mark of 1 hour
    (SHARED_PREFIX_CACHE_MARK); `system` follows it, with a cache mark of 5 minutes only
    with `cache_system_after_shared_prefix`. With no shared prefix, the system part has a
    cache mark of 5 minutes at its end, so the requests of one caller that share it can
    read it from the cache. The `earlier_turns` come after the system message, before
    `prompt`.

    Raises:
        ValueError: `cache_system_after_shared_prefix` without a shared prefix and a system,
            or a `max_output_tokens` below 1.
    """
    if max_output_tokens is not None and max_output_tokens < 1:
        raise ValueError(f"max_output_tokens must be at least 1, got {max_output_tokens}")
    if cache_system_after_shared_prefix and (shared_prefix_for_cache is None or system is None):
        raise ValueError("cache_system_after_shared_prefix needs a shared prefix and a system")
    messages: list[dict] = []
    if shared_prefix_for_cache is not None:
        blocks = [{"type": "text", "text": shared_prefix_for_cache,
                   "cache_control": dict(SHARED_PREFIX_CACHE_MARK)}]
        if system is not None:
            blocks.append({"type": "text", "text": system})
            if cache_system_after_shared_prefix:
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
        messages.append({"role": "system", "content": blocks})
    elif system is not None:
        messages.append({"role": "system", "content": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]})
    for user, assistant in earlier_turns:
        messages += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    messages.append({"role": "user", "content": prompt})
    body: dict = {"model": model, "messages": messages}
    if reasoning is None:
        body["temperature"] = 0
    else:
        body["reasoning"] = {"effort": reasoning}
    if max_output_tokens is not None:
        body["max_tokens"] = max_output_tokens
    return body


def endpoint(model: str) -> tuple[str, str]:
    """The URL and the model name of a model name URL#MODEL.

    Raises:
        ValueError: the name does not start with http:// or https://, or has no "#MODEL" part.
    """
    url, sep, name = model.partition("#")
    if not url.startswith(("http://", "https://")) or not sep or not name:
        raise ValueError(f"a model must be URL#MODEL, such as "
                         f"'https://openrouter.ai/api/v1/chat/completions#anthropic/"
                         f"claude-sonnet-5.5', got {model!r}")
    return url, name


def label(model: str, purpose: str | None = None) -> str:
    """The host and the model of URL#MODEL, for a version or a saved result.

    The label has no path, port, user name or password, because a saved result can be
    committed. A host with no key is a server of our own, so its label is OWN_SERVER and
    the model: a new GPU host for the same model does not change the label. Through the
    claude CLI it names the Claude model a call gets, which depends on the call's spending
    purpose (cli_role): `purpose` is the one the calls to `model` run under, and None
    takes the purpose this code runs in.

    Raises:
        ValueError: `model` is not URL#MODEL, or `purpose` is not a purpose name.
    """
    if uses_cli(model):
        if purpose is None:
            return f"{CLI}#{cli_model(model)}"
        with spending.purpose(purpose):
            return f"{CLI}#{cli_model(model)}"
    url, name = endpoint(model)
    host = urllib.parse.urlsplit(url).hostname or ""
    return f"{host if _key_name(url) is not None else OWN_SERVER}#{name}"


def key_name(model: str) -> str | None:
    """The environment variable that holds the key for `model`, or None for a host with no key.

    Raises:
        ValueError: `model` is not URL#MODEL.
    """
    if uses_cli(model):
        cli_model(model)
        return CLI_TOKEN
    return _key_name(endpoint(model)[0])


def check_key(model: str) -> None:
    """Refuse `model` before any call, as a call to it would be refused (api_key).

    Raises:
        ValueError: the name is not URL#MODEL, or its host is not in KEYS_FILE.
        KeyMissing: the host's key variable is not set; for a CLI model (uses_cli),
            CLI_TOKEN is not set.
    """
    if uses_cli(model):
        cli_model(model)
        if not os.environ.get(CLI_TOKEN):
            raise cli_token_missing(model)
        if shutil.which(cli_binary()) is None:
            raise ValueError(f"{model} runs through the claude CLI, and {cli_binary()!r} is "
                             f"not a program on PATH; set {CLI_BINARY_ENV}")
        return
    api_key(endpoint(model)[0])


def cli_token_missing(model: str) -> KeyMissing:
    """The error of a call to `model` through the claude CLI when CLI_TOKEN is not set.

    In the auditing agent's shell (AGENT_SHELL_ENV), the token is left out on purpose:
    the agent's model could read it there. The error then says so, and which steps run
    with the token: those the daemon runs itself.
    """
    if os.environ.get(AGENT_SHELL_ENV):
        return KeyMissing(
            f"{model} runs through the claude CLI ({BACKEND_ENV}=claude-cli), and the Claude "
            f"token is kept out of the agent's shell, so the agent's model cannot read it. "
            "Only the steps the daemon runs itself call the claude CLI: the claim steps "
            "before the round and "
            "`vcheck run` after it. This command cannot call a model here.")
    return KeyMissing(f"{CLI_TOKEN} is not set, and {model} runs through the claude CLI "
                      f"({BACKEND_ENV}={backend()}), which needs it")


def _key_name(url: str) -> str | None:
    """The environment variable that holds the key for the host of `url`, or None for no key.

    Raises:
        ValueError: the host is not in KEYS_FILE.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    if host not in KEYS:
        raise ValueError(f"the model host {host!r} is not in {KEYS_FILE}; add it with the "
                         f'name of its key variable, or with "" for a host with no key')
    return KEYS[host]


def key_variables() -> tuple[str, ...]:
    """Every variable a child process needs for its model calls, to pass them on.

    The key variables KEYS_FILE names and BACKEND_ENV; with the CLI backend, also
    CLI_TOKEN and the CLI settings (CLI_SETTINGS). The auditing agent's shell gets them
    without CLI_TOKEN (runtime/daemon.py).

    Raises:
        ValueError: BACKEND_ENV is set to a name not in BACKENDS.
    """
    names = {name for name in KEYS.values() if name is not None} | {BACKEND_ENV}
    if backend() == CLI:
        names |= {CLI_TOKEN, *CLI_SETTINGS}
    return tuple(sorted(names))


def agent_key_problem(model: str) -> str | None:
    """Why the auditing agent cannot call `model`, or None.

    `model` is in OpenCode's PROVIDER/MODEL form. Only a provider in AGENT_PROVIDERS is
    checked; OpenCode reads the key of any other provider from its own configuration.
    The key is read with api_key, then sent to the provider's check URL.
    """
    provider = model.partition("/")[0]
    if provider not in AGENT_PROVIDERS:
        return None
    chat, check = AGENT_PROVIDERS[provider]
    try:
        key = api_key(chat)
    except ValueError as e:
        return str(e)
    if key is None:
        return None
    req = urllib.request.Request(check, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return None
    except urllib.error.HTTPError as e:
        return (f"{urllib.parse.urlsplit(check).hostname} rejected {_key_name(chat)} "
                f"(HTTP {e.code}); it may have expired")
    except (urllib.error.URLError, OSError) as e:
        return f"could not reach {urllib.parse.urlsplit(check).hostname} to check the key: {e}"


def api_key(url: str) -> str | None:
    """The key for `url`, read from the environment, or None for a host with no key.

    Every model call and every check of a key before a run use this function.

    Raises:
        ValueError: the host is not in KEYS_FILE.
        KeyMissing: the host has a key variable, and it is not set.
    """
    name = _key_name(url)
    if name is None:
        return None
    key = os.environ.get(name)
    if not key:
        raise KeyMissing(f"{name} is not set, and the model host "
                         f"{urllib.parse.urlsplit(url).hostname} needs it")
    return key


def _post(url: str, body: bytes, key: str | None, timeout: int) -> object:
    """POST one request to `url` and return the parsed reply; `key` is None for a GPU server.

    Raises:
        _Retryable: HTTP 408, 429 or 5xx, a timeout or a broken connection.
        ProviderError: any other HTTP error, or a body that is not JSON.
    """
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        message = f"the provider answered HTTP {e.code} {e.reason}: {detail}"
        if e.code in RETRY_CODES or e.code >= 500:
            raise _Retryable(message, _retry_after(e.headers.get("Retry-After"))) from e
        raise ProviderError(message) from e
    except (TimeoutError, ConnectionError, http.client.HTTPException,
            urllib.error.URLError) as e:
        raise _Retryable(f"the call failed: {type(e).__name__}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProviderError(f"the reply body is not JSON ({e}): "
                            f"{raw.decode(errors='replace')}") from e


def _retry_after(value: str | None) -> float | None:
    """The seconds a Retry-After header asks for, or None when there is no usable header.

    A header that is neither a number of seconds nor a date is a `warn` in the alert log,
    with its text, and the caller's own wait is used.
    """
    if value is None:
        return None
    if value.strip().isdigit():
        return float(value.strip())
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError) as e:
        alerts.notify("warn", "models.retry_after_unreadable",
                      f"Retry-After is neither seconds nor a date: {value!r}", error=e,
                      header=value)
        return None
    return max(0.0, when.timestamp() - time.time())


def _choice(payload: object, model: str) -> dict:
    """The one choice of a reply.

    Raises:
        _Retryable: the reply is an error the provider marks 408, 429 or 5xx.
        ProviderError: any other error reply, or not exactly one choice.
    """
    if not isinstance(payload, dict):
        raise ProviderError(f"{model} gave a reply that is not an object: {payload!r}")
    error = payload.get("error")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, int) and (code in RETRY_CODES or code >= 500):
            raise _Retryable(f"{model} answered with an error: {error!r}")
        raise ProviderError(f"{model} answered with an error: {error!r}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError(f"{model} did not give exactly one choice: {payload!r}")
    return choices[0]


def reply_text(payload: object, model: str) -> str:
    """The text of a chat completion reply.

    Raises:
        _Retryable: the reply is an error the provider marks 408, 429 or 5xx.
        ReplyCutAtLengthLimit: the reply stopped at the output limit.
        ProviderError: any other error reply, a refusal, no text, no output tokens, or a
            reply that stopped before its end for another reason.
    """
    choice = _choice(payload, model)
    if choice.get("finish_reason") == "length":
        raise ReplyCutAtLengthLimit(f"{model} stopped at its output limit, not at its end")
    if choice.get("finish_reason") != "stop":
        raise ProviderError(f"{model} stopped with {choice.get('finish_reason')!r}, not 'stop'")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderError(f"{model} gave a choice with no message: {choice!r}")
    if message.get("refusal"):
        raise ProviderError(f"{model} refused: {message['refusal']!r}")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError(f"{model} gave no text")
    usage = payload.get("usage")
    if usage is not None:
        tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        if not isinstance(tokens, int) or tokens < 1:
            raise ProviderError(f"{model} reports {tokens!r} output tokens for a reply with text")
    return text


def top_logprobs(payload: object, model: str) -> dict[str, float]:
    """The first-token log probabilities of a chat completion reply, merged by stripped text.

    The reply's `logprobs.content` holds one item for each output token, with its
    `top_logprobs` as {"token": ..., "logprob": ...} items; the first output token is read.
    Tokens that are the same text without white space at either end are one entry, with
    their probabilities added.

    Raises:
        _Retryable: the reply is an error the provider marks 408, 429 or 5xx.
        ProviderError: any other error reply, or no finite log probabilities.
    """
    choice = _choice(payload, model)
    logprobs = choice.get("logprobs")
    content = logprobs.get("content") if isinstance(logprobs, dict) else None
    first = content[0] if isinstance(content, list) and content else None
    tops = first.get("top_logprobs") if isinstance(first, dict) else None
    if not isinstance(tops, list) or not tops:
        raise ProviderError(f"{model} gave no first-token log probabilities: {choice!r}")
    merged: dict[str, float] = {}
    for item in tops:
        token = item.get("token") if isinstance(item, dict) else None
        value = item.get("logprob") if isinstance(item, dict) else None
        if not isinstance(token, str) or not isinstance(value, (int, float)) \
                or isinstance(value, bool) or not math.isfinite(value) or value > 0:
            raise ProviderError(f"{model} gave a log probability that is not a token and a "
                                f"number from -inf to 0: {item!r}")
        text = token.strip()
        merged[text] = value if text not in merged else _log_add(merged[text], value)
    return merged


def _log_add(a: float, b: float) -> float:
    """log(exp(a) + exp(b)), without overflow."""
    high, low = max(a, b), min(a, b)
    return high + math.log1p(math.exp(low - high))


# ---------------------------------------------------------------- the claude CLI backend

_cli_slots: threading.BoundedSemaphore | None = None
_cli_dir: Path | None = None
_cli_lock = threading.Lock()


def uses_cli(model: str) -> bool:
    """True when a call to `model` runs `claude -p`: a CLI#MODEL name, or the CLI backend."""
    return model.startswith(CLI + "#") or backend() == CLI


def cli_role(model: str) -> str:
    """JUDGEMENT or MECHANICAL: by the call's spending purpose, else by `model`'s name."""
    role = CLI_PURPOSE_ROLES.get(spending.current_purpose() or "")
    if role is not None:
        return role
    name = model.partition("#")[2]
    return MECHANICAL if "haiku" in name.lower() else JUDGEMENT


def cli_call(model: str, reasoning: str | None) -> tuple[str, str | None]:
    """The Claude model id and the `--effort` (None: no thinking) of a call to `model`.

    CLI#MODEL is called as it is. Any other model is mapped by its role (cli_role) with
    CLI_ROLE_SETTINGS. OpenRouter's "minimal" is the CLI's "low", and "none" is no
    thinking.

    Raises:
        ValueError: `model` is neither CLI#MODEL nor URL#MODEL, the effort is not one of
            EFFORTS, or a judgement effort variable is not one of CLI_EFFORTS.
    """
    if reasoning is not None and reasoning not in EFFORTS:
        raise ValueError(f"the effort must be one of {', '.join(EFFORTS)}, not {reasoning!r}")
    effort = None if reasoning is None else {"minimal": "low", "none": None}.get(reasoning,
                                                                               reasoning)
    if model.startswith(CLI + "#"):
        name = model[len(CLI) + 1:]
        if not name.strip():
            raise ValueError(f"a CLI model must be {CLI}#MODEL, got {model!r}")
        return name, effort
    endpoint(model)
    model_var, default_model, effort_var, default_effort = CLI_ROLE_SETTINGS[cli_role(model)]
    name = os.environ.get(model_var) or default_model
    if effort_var is not None:
        effort = os.environ.get(effort_var) or default_effort
        if effort not in CLI_EFFORTS:
            raise ValueError(f"${effort_var} must be one of {', '.join(CLI_EFFORTS)}, "
                             f"not {effort!r}")
    return name, effort


def cli_model(model: str) -> str:
    """The Claude model id a call to `model` gets now (cli_call)."""
    return cli_call(model, None)[0]


def cli_binary() -> str:
    """The claude program: $VERIFIER_CLAUDE, or `claude` on PATH."""
    return os.environ.get(CLI_BINARY_ENV) or "claude"


def _slots() -> threading.BoundedSemaphore:
    """The process's CLI call slots: $VERIFIER_CLI_CONCURRENCY (CLI_CONCURRENCY) at once.

    Raises:
        ValueError: the variable is not a whole number of at least 1.
    """
    global _cli_slots
    with _cli_lock:
        if _cli_slots is None:
            text = os.environ.get(CLI_CONCURRENCY_ENV) or str(CLI_CONCURRENCY)
            if not text.isdigit() or int(text) < 1:
                raise ValueError(f"${CLI_CONCURRENCY_ENV} must be a whole number of at "
                                 f"least 1, not {text!r}")
            _cli_slots = threading.BoundedSemaphore(int(text))
        return _cli_slots


def _workdir() -> Path:
    """The empty directory `claude` runs in: $VERIFIER_CLI_DIR, or one temp dir a process.

    `claude` keeps its own configuration under config/ there (CLAUDE_CONFIG_DIR), so no
    user setting, memory or CLAUDE.md reaches the call.
    """
    global _cli_dir
    with _cli_lock:
        if _cli_dir is None:
            given = os.environ.get(CLI_DIR_ENV)
            _cli_dir = Path(given) if given else Path(tempfile.mkdtemp(prefix="verifier-cli-"))
            (_cli_dir / "config").mkdir(parents=True, exist_ok=True)
        return _cli_dir


def cli_command(name: str, effort: str | None, system_file: Path | None) -> list[str]:
    """The `claude -p` command of one call: no tools, no settings, one JSON result."""
    cmd = [cli_binary(), "-p", "--model", name, "--tools", "", "--strict-mcp-config",
           "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
           "--output-format", "json"]
    if system_file is not None:
        cmd += ["--system-prompt-file", str(system_file)]
    if effort is not None:
        cmd += ["--effort", effort]
    return cmd


def cli_env(effort: str | None, workdir: Path,
            max_output_tokens: int | None = None) -> dict[str, str]:
    """The environment of `claude`: ours, with no API key, so it uses CLI_TOKEN.

    With `max_output_tokens`, CLI_MAX_OUTPUT_TOKENS_ENV gives `claude` that output limit.
    """
    drop = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            *key_variables_of_hosts()}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(CLAUDE_CONFIG_DIR=str(workdir / "config"), DISABLE_AUTOUPDATER="1",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    if effort is None:
        env["CLAUDE_CODE_DISABLE_THINKING"] = "1"
    else:
        env.pop("CLAUDE_CODE_DISABLE_THINKING", None)
    if max_output_tokens is not None:
        env[CLI_MAX_OUTPUT_TOKENS_ENV] = str(max_output_tokens)
    return env


def key_variables_of_hosts() -> set[str]:
    """The key variables KEYS_FILE names, such as OPENROUTER_API_KEY."""
    return {name for name in KEYS.values() if name is not None}


def _ask_cli(prompt: str, model: str, timeout: int, system: str | None,
             reasoning: str | None, max_output_tokens: int | None, attempts: int,
             sleep: Callable[[float], None],
             clock: Callable[[], float], deadline: float) -> str:
    """ask() through `claude -p`: the prompt on stdin, the system part in a file.

    The usage of each reply goes in the spending log under label(model), with the CLI's
    total_cost_usd as the cost, at list price (CLI_COST_BASIS). At most
    $VERIFIER_CLI_CONCURRENCY calls run at once in a process; a call waits for a slot.

    Raises:
        KeyMissing: CLI_TOKEN is not set.
        UsageLimit: the account hit its usage limit.
        ProviderError: as ask() raises it.
    """
    if not os.environ.get(CLI_TOKEN):
        raise cli_token_missing(model)
    name, effort = cli_call(model, reasoning)
    workdir = _workdir()
    env = cli_env(effort, workdir, max_output_tokens)
    tag = f"{CLI}#{name}"
    system_file = None
    if system is not None:
        fd, path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=workdir)
        with os.fdopen(fd, "w") as fh:
            fh.write(system)
        system_file = Path(path)
    cmd = cli_command(name, effort, system_file)
    try:
        return _call(lambda seconds: _run_cli(cmd, prompt, env, workdir, seconds, deadline,
                                              clock),
                     tag, timeout, attempts, sleep, lambda reply: cli_reply_text(reply, tag),
                     deadline, clock)
    finally:
        if system_file is not None:
            system_file.unlink(missing_ok=True)


def _run_cli(cmd: list[str], prompt: str, env: dict[str, str], cwd: Path, seconds: int,
             deadline: float, clock: Callable[[], float]) -> dict:
    """Run one `claude -p` call, once a slot is free; its JSON result in chat-reply form.

    The time spent waiting for a slot is taken from `seconds`, so the call still ends
    by `deadline`. The result keeps the CLI's own fields (is_error, result, stop_reason,
    api_error_status), with `usage` as spending.record() reads it.

    Raises:
        _Retryable: no slot in time, a timeout, or a crash with no JSON result.
        ProviderError: the program cannot be run.
    """
    slots = _slots()
    if not slots.acquire(timeout=max(0.0, deadline - clock())):
        raise _Retryable(f"no claude CLI slot came free within {seconds} s")
    try:
        left = min(seconds, math.ceil(deadline - clock()))
        if left <= 0:
            raise _Retryable("the time ran out while waiting for a claude CLI slot")
        try:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd,
                                  env=env, timeout=left, check=False)
        except subprocess.TimeoutExpired as e:
            raise _Retryable(f"claude -p did not answer within {left} s") from e
        except OSError as e:
            raise ProviderError(f"{cmd[0]} cannot be run ({e}); set {CLI_BINARY_ENV}") from e
    finally:
        slots.release()
    try:
        out = json.loads(done.stdout)
    except json.JSONDecodeError:
        text = (done.stdout + done.stderr).strip()[-500:]
        if USAGE_LIMIT.search(text):
            return {"is_error": True, "result": text}
        raise _Retryable(f"claude -p exited {done.returncode} with no JSON result: "
                         f"{text!r}") from None
    if not isinstance(out, dict):
        raise ProviderError(f"claude -p gave a result that is not an object: {out!r}")
    u = out.get("usage") if isinstance(out.get("usage"), dict) else {}
    out["usage"] = {
        "prompt_tokens": sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        "completion_tokens": int(u.get("output_tokens") or 0),
        "cost": out.get("total_cost_usd"), "cost_basis": CLI_COST_BASIS}
    return out


def cli_reply_text(payload: object, model: str) -> str:
    """The text of a `claude -p` result (_run_cli).

    Raises:
        UsageLimit: the account hit its usage limit (USAGE_LIMIT).
        _Retryable: a failure a later call can fix (CLI_RETRY, or a 429 or 5xx status).
        ReplyCutAtLengthLimit: the output limit cut the reply.
        ProviderError: any other failure, or no text.
    """
    if not isinstance(payload, dict):
        raise ProviderError(f"{model} gave a reply that is not an object: {payload!r}")
    text = payload.get("result")
    text = text if isinstance(text, str) else ""
    if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
        if USAGE_LIMIT.search(text):
            raise UsageLimit(f"{model}: the Claude account hit its usage limit; the run "
                             f"stops until it resets: {text[:300]!r}")
        status = payload.get("api_error_status")
        if CLI_RETRY.search(text) or (isinstance(status, int)
                                      and (status in RETRY_CODES or status >= 500)):
            raise _Retryable(f"claude -p failed: {text[:300]!r}")
        raise ProviderError(f"{model}: claude -p failed: {text[:300] or payload!r}")
    if payload.get("stop_reason") == "max_tokens":
        raise ReplyCutAtLengthLimit(f"{model} stopped at its output limit, not at its end")
    if payload.get("stop_reason") == "refusal":
        raise ProviderError(f"{model} refused: {text[:300]!r}")
    if not text.strip():
        raise ProviderError(f"{model} gave no text")
    return text
