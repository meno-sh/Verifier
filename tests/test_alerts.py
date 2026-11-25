#!/usr/bin/env python3
"""Check the alert log (utils/alerts.py) and the shared model call with retries (utils/models.py).

Covers:
  - a record: every field, kept whole; the line on stderr; refused levels, keys,
    messages and details, which write nothing;
  - reading: a partial last line is left out, any other bad line raises;
  - many processes writing at once: every line is whole;
  - a process that ends with an exception: one fatal record with its traceback, and
    one record only for an error that fail() already wrote;
  - the model call: HTTP 429 and 5xx, an error body and a timeout are retried with
    the waits asked for, and raised after the last attempt; HTTP 4xx, a cut reply, a
    refusal and a reply with no output tokens are raised at once; a reply the reader
    refuses is asked for again, then raised;
  - `verifier alerts`: `warn` and worse by default, every record with `--level log`;
  - reading a JSON reply: alone or in one ```json fence, and nothing else.
The provider is a fake, so no model is called. Returns 1 if any case fails.
"""
import contextlib
import datetime
import email.message
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime import cli as runtime_cli  # noqa: E402
from utils import alerts, models  # noqa: E402

HOME = Path(__file__).resolve().parent.parent
URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = URL + "#m"                  # a model the fake provider answers for
KEY = models.KEYS["openrouter.ai"]
FAILED: list[str] = []


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


def test_record(folder: Path) -> None:
    """One record has every field, kept whole, and bad input writes nothing."""
    log = alerts.configure(folder / "runtime" / "alerts.jsonl", "test", folder)
    long = "x" * 50_000
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            raise ValueError("a bad value")
        except ValueError as e:
            alerts.notify("warn", "test.case", "something went wrong", error=e, long=long,
                          nested={"a": [1, 2.5, None, True]})
    records = alerts.read(log.path)
    r = records[0] if records else {}
    expect("the record has exactly the fields, in order", tuple(r) == alerts.FIELDS, repr(r))
    expect("the record names the process, project and level",
           (r.get("level"), r.get("key"), r.get("process"), r.get("project"), r.get("pid"))
           == ("warn", "test.case", "test", str(folder), os.getpid()), repr(r))
    expect("the time is ISO UTC and agrees with the epoch milliseconds",
           r.get("time", "").endswith("Z") and abs(
               datetime.datetime.fromisoformat(r["time"]).timestamp() * 1000
               - r["ts"]) < 1, repr((r.get("time"), r.get("ts"))))
    expect("the traceback is whole, and a long detail is not cut",
           r.get("error_type") == "ValueError" and "Traceback (most recent call last)"
           in r.get("traceback", "") and "a bad value" in r["traceback"]
           and r["details"]["long"] == long
           and r["details"]["nested"] == {"a": [1, 2.5, None, True]}, repr(r)[:300])
    expect("one line on stderr names the level, key and the log",
           err.getvalue() == f"[WARN] test.case: something went wrong (see {log.path})\n",
           err.getvalue())
    expect("the verifier's commit is named when its code is a git checkout",
           r.get("verifier_commit") is None if not (HOME / ".git").exists()
           else len(r.get("verifier_commit") or "") == 40, repr(r.get("verifier_commit")))
    for name, call, error in [
            ("an unknown level", lambda: alerts.notify("info", "test.case", "m"), ValueError),
            ("a key with no dot", lambda: alerts.notify("warn", "nodot", "m"), ValueError),
            ("a key in capitals", lambda: alerts.notify("warn", "Test.Case", "m"), ValueError),
            ("an empty message", lambda: alerts.notify("warn", "test.case", " "), ValueError),
            ("a detail that is not JSON",
             lambda: alerts.notify("warn", "test.case", "m", path=Path("/x")), TypeError),
            ("a detail that is NaN",
             lambda: alerts.notify("warn", "test.case", "m", v=float("nan")), ValueError),
            ("an error that is not an exception",
             lambda: alerts.notify("warn", "test.case", "m", error="text"), TypeError)]:
        raises(f"notify refuses {name}", error, call)
    expect("a refused record writes nothing", len(alerts.read(log.path)) == 1)
    raises("configure refuses a relative path", ValueError,
           lambda: alerts.configure(Path("alerts.jsonl"), "test", folder))


def test_read(folder: Path) -> None:
    """A partial last line is left out; any other bad line raises."""
    log = alerts.configure(folder / "alerts.jsonl", "test", folder)
    with contextlib.redirect_stderr(io.StringIO()):
        alerts.notify("error", "test.one", "one")
        alerts.notify("fatal", "test.two", "two")
    whole = log.path.read_bytes()
    log.path.write_bytes(whole + b'{"ts": 1, "lev')
    expect("a partial last line, still being written, is left out",
           [r["key"] for r in alerts.read(log.path)] == ["test.one", "test.two"])
    lines = whole.split(b"\n")
    for name, data in [("a line that is not JSON", lines[0] + b"\nnot json\n" + lines[1] + b"\n"),
                       ("an empty line", lines[0] + b"\n\n" + lines[1] + b"\n"),
                       ("a record with a field missing",
                        json.dumps({"ts": 1}).encode() + b"\n" + lines[1] + b"\n")]:
        log.path.write_bytes(data)
        raises(f"reading refuses {name}", alerts.AlertError, lambda: alerts.read(log.path))
    expect("a log that does not exist has no records",
           alerts.read(folder / "none.jsonl") == [])


def test_many_writers(folder: Path) -> None:
    """Four processes that each write 200 records at once leave 800 whole lines."""
    path = folder / "alerts.jsonl"
    script = textwrap.dedent(f"""
        import contextlib, io, sys
        from pathlib import Path
        sys.path.insert(0, {str(HOME)!r})
        from utils import alerts
        alerts.configure(Path({str(path)!r}), "writer", Path({str(folder)!r}))
        with contextlib.redirect_stderr(io.StringIO()):
            for i in range(200):
                alerts.notify("warn", "test.many", "record " + str(i), text="y" * 5000)
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(4)]
    codes = [p.wait() for p in procs]
    records = alerts.read(path)
    expect("every writer ends well, and all 800 records are whole",
           codes == [0] * 4 and len(records) == 800
           and len({(r["pid"], r["message"]) for r in records}) == 800,
           f"exit codes {codes}, {len(records)} records")


def test_process_end(folder: Path) -> None:
    """An exception that ends a process is one fatal record; one fail() wrote is not two."""
    path = folder / "alerts.jsonl"

    def end_with(body: str) -> subprocess.CompletedProcess:
        script = textwrap.dedent(f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(HOME)!r})
            from utils import alerts
            alerts.configure(Path({str(path)!r}), "ender", Path({str(folder)!r}))
        """) + textwrap.dedent(body)
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    out = end_with("raise KeyError('no such claim')\n")
    records = alerts.read(path)
    expect("an uncaught exception is one fatal record with its traceback, and the process "
           "still prints it and exits 1",
           [(r["level"], r["key"]) for r in records] == [("fatal", "process.crashed")]
           and "KeyError: 'no such claim'" in records[0]["traceback"]
           and "KeyError: 'no such claim'" in out.stderr and out.returncode == 1,
           repr(records) + out.stderr)
    path.unlink()
    out = end_with("raise alerts.fail('test.stop', 'it must stop', why='a test')\n")
    records = alerts.read(path)
    expect("an error from fail() is written once, with its details",
           [(r["level"], r["key"]) for r in records] == [("fatal", "test.stop")]
           and records[0]["details"] == {"why": "a test"} and out.returncode == 1,
           repr(records))
    path.unlink()
    out = end_with("import threading\n"
                   "t = threading.Thread(target=lambda: 1 / 0, name='worker')\n"
                   "t.start(); t.join()\n")
    records = alerts.read(path)
    expect("an exception that ends a thread is one fatal record that names the thread",
           [(r["level"], r["key"]) for r in records] == [("fatal", "process.thread_crashed")]
           and records[0]["details"] == {"thread": "worker"}
           and "ZeroDivisionError" in records[0]["traceback"], repr(records))
    out = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HOME)!r})
        from utils import alerts
        alerts.notify("warn", "test.case", "m")
    """)], capture_output=True, text=True)
    expect("notify before configure raises", "AlertError" in out.stderr and out.returncode == 1,
           out.stderr[-300:])


# The model call, against a fake provider.

class Reply(io.BytesIO):
    """A response body, as urlopen returns it."""
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def chat(content: str, finish: str = "stop", tokens: int = 5, **message: object) -> bytes:
    """A chat completion reply."""
    return json.dumps({"choices": [{"finish_reason": finish,
                                    "message": {"content": content, **message}}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": tokens}}).encode()


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    """An HTTP error with a body, and Retry-After when given."""
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "status", headers,
                                  io.BytesIO(b'{"error": "from the provider"}'))


class Provider:
    """A fake urlopen that gives the answers in order, and records each request."""

    def __init__(self, *answers: object) -> None:
        self.answers, self.sent = list(answers), []

    def __call__(self, req: urllib.request.Request, timeout: float) -> Reply:
        self.sent.append(json.loads(req.data))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return Reply(answer)


def test_model_call(folder: Path) -> None:
    """Retries for a failure a later call can fix; at once for the rest; then raised."""
    log = alerts.configure(folder / "alerts.jsonl", "test", folder)
    saved = urllib.request.urlopen, os.environ.get(KEY)
    os.environ[KEY] = "test"
    waits: list[float] = []

    def call(provider: Provider, **kw: object) -> str:
        urllib.request.urlopen = provider
        return models.ask("q", MODEL, sleep=waits.append, **kw)

    def keys() -> list[tuple[str, str]]:
        return [(r["level"], r["key"]) for r in alerts.read(log.path)]

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            p = Provider(http_error(429, "7"), http_error(503), TimeoutError("timed out"),
                         chat("ok"))
            got = call(p)
            expect("HTTP 429, HTTP 503 and a timeout are retried, and the fourth call answers",
                   got == "ok" and len(p.sent) == 4 and waits == [7.0, 8.0, 16.0],
                   f"{got!r}, {len(p.sent)} calls, waits {waits}")
            expect("each failed call is a warn, with its attempt and wait",
                   keys() == [("warn", "models.retry")] * 3
                   and [r["details"]["attempt"] for r in alerts.read(log.path)] == [1, 2, 3]
                   and alerts.read(log.path)[0]["details"]["wait_s"] == 7.0, repr(keys()))

            waits.clear()
            p = Provider(*[http_error(500)] * 4)
            e = raises("four HTTP 500s are raised after the fourth call", models.ProviderError,
                       lambda: call(p))
            expect("the error names the last attempt, and the waits double",
                   e is not None and "attempt 4 of 4" in str(e) and waits == [4.0, 8.0, 16.0]
                   and len(p.sent) == 4, f"{e}, waits {waits}")

            waits.clear()
            p = Provider(json.dumps({"error": {"code": 502, "message": "bad gateway"}}).encode(),
                         chat("ok"))
            expect("an error body marked 502 is retried",
                   call(p) == "ok" and len(p.sent) == 2 and waits == [4.0])

            waits.clear()
            p = Provider(*[http_error(429)] * 30)
            for _ in range(3):
                with contextlib.suppress(models.ProviderError):
                    call(p, attempts=1)
            expect("one attempt in all means no retry and no wait",
                   len(p.sent) == 3 and waits == [], f"{len(p.sent)} calls, waits {waits}")

            before = len(keys())
            for name, answer in [
                    ("HTTP 401", http_error(401)),
                    ("HTTP 400", http_error(400)),
                    ("an error body with no code", json.dumps({"error": "busy"}).encode()),
                    ("a reply cut at the token limit", chat("half", finish="length")),
                    ("a refusal", chat("text", refusal="I will not")),
                    ("a reply with no text", chat(" ")),
                    ("a reply with no output tokens", chat("text", tokens=0)),
                    ("two choices", json.dumps({"choices": [{}, {}]}).encode()),
                    ("a body that is not JSON", b"<html>")]:
                p = Provider(answer, chat("never"))
                e = raises(f"{name} is raised at once", models.ProviderError, lambda p=p: call(p))
                expect(f"{name}: one call, and no retry record",
                       len(p.sent) == 1 and len(keys()) == before, f"{len(p.sent)} calls; {e}")
            key = os.environ.pop(KEY)
            raises("no key is raised before any call", models.ProviderError,
                   lambda: call(Provider()))
            os.environ[KEY] = key

            def read(text: str) -> int:
                if not text.isdigit():
                    raise models.BadReply(f"{text!r} is not a whole number")
                return int(text)

            def call_read(provider: Provider) -> int:
                urllib.request.urlopen = provider
                return models.ask_and_read("give a number", MODEL, read, system="rules",
                                           sleep=waits.append)

            log.path.unlink()
            p = Provider(chat("seven"), chat("7"))
            expect("a refused reply is asked for again, and the good one is read",
                   call_read(p) == 7 and len(p.sent) == 2)
            expect("the second request keeps the system part and adds the error",
                   p.sent[0]["messages"][0] == p.sent[1]["messages"][0]
                   and p.sent[1]["messages"][-1]["content"]
                   == "give a number" + models.RETRY_NOTE.format(
                       error="'seven' is not a whole number"), repr(p.sent[1]))
            expect("the refused reply is a warn with the whole reply",
                   keys() == [("warn", "models.bad_reply")]
                   and alerts.read(log.path)[0]["details"]["reply"] == "seven")
            p = Provider(chat("seven"), chat("eight"), chat("9"))
            raises("two refused replies are raised", models.BadReply, lambda: call_read(p))
            expect("and there is no third call", len(p.sent) == 2)
    finally:
        urllib.request.urlopen = saved[0]
        if saved[1] is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = saved[1]


def test_command(folder: Path) -> None:
    """`verifier alerts` shows `warn` and worse by default, and every record with `log`."""
    root = folder / "w"
    log = alerts.configure(alerts.project_log(root / ".verifier"), "test", root)
    with contextlib.redirect_stderr(io.StringIO()):
        alerts.notify("log", "test.routine", "a round was sent")
        alerts.notify("warn", "test.retry", "a call was retried", attempt=1)
        alerts.notify("fatal", "other.crash", "it stopped")

    def shown(*flags: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = runtime_cli.main(["alerts", str(root), *flags])
        return out.getvalue() if code == 0 else f"exit {code}"

    default, everything = shown(), shown("--level", "log")
    expect("by default, `log` records are left out, and the newest comes first",
           default.startswith(f"2 record(s) in {log.path}") and "test.routine" not in default
           and default.index("other.crash") < default.index("test.retry"), default)
    expect("--level log shows every record", everything.startswith("3 record(s)")
           and "a round was sent" in everything, everything)
    only = shown("--level", "warn", "--key", "test")
    expect("--key keeps the records whose key starts with it",
           only.startswith("1 record(s)") and '"attempt": 1' in only, only)


def test_read_json(folder: Path) -> None:
    """Every JSON reply is read by models.read_json: alone, or in one ```json fence."""
    for name, text, want in [
            ("the JSON alone", '{"a": 1}', {"a": 1}),
            ("the JSON with white space around it", '\n  {"a": 1}\n', {"a": 1}),
            ("one ```json fence around the whole reply", '```json\n{"a": [1, 2]}\n```',
             {"a": [1, 2]}),
            ("a fence with white space around it", '\n```json \n{"a": 1}\n```\n', {"a": 1}),
            ("a fence whose JSON holds ``` in a string",
             '```json\n{"code": "```py\\nx\\n```"}\n```', {"code": "```py\nx\n```"})]:
        got = models.read_json(text)
        expect(f"read_json reads {name}", got == want, repr(got))
    for name, text in [("text before the JSON", 'Here it is: {"a": 1}'),
                       ("text after the fence", '```json\n{"a": 1}\n```\nDone.'),
                       ("a fence with no language", '```\n{"a": 1}\n```'),
                       ("two fences", '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```'),
                       ("a fence that is not closed", '```json\n{"a": 1}'),
                       ("no JSON", "no JSON here")]:
        raises(f"read_json refuses {name}", models.BadReply,
               lambda text=text: models.read_json(text))


def main() -> int:
    """Run every group of cases, each in a fresh temporary folder."""
    for group in (test_record, test_read, test_many_writers, test_process_end,
                  test_model_call, test_command, test_read_json):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp).resolve())
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll alert log and model call cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
