"""The OpenCode server the agent runs in: starting it, and the few HTTP calls the daemon makes.

Only the documented session API is used (/session, /session/status, prompt_async,
abort); the newer /api/session API keeps a separate history for the same session.

The server runs with its working directory in `.verifier/`, which is its own git
repo, so OpenCode takes it as the project root and loads only `.verifier/AGENTS.md`.
Its data, config and cache directories are under `.verifier/runtime/`, so nothing
from the user's own OpenCode setup reaches the agent. `spending` reads what the agent's
model calls cost from OpenCode's database there.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from utils import file_storage, text

PINNED_VERSION = "1.19.0"
USERNAME = "opencode"
# Every git subcommand that can change a repository, its refs, its index or its files.
GIT_WRITE_COMMANDS = ("add", "am", "apply", "bisect", "branch", "checkout", "cherry-pick",
                      "clean", "clone", "commit", "config", "fetch", "filter-branch", "gc", "init",
                      "merge", "mv", "notes", "prune", "pull", "push", "rebase", "replace",
                      "reset", "restore", "revert", "rm", "sparse-checkout", "stash",
                      "submodule", "switch", "symbolic-ref", "tag", "update-index",
                      "update-ref", "worktree")
# Programs that run code from a file or from their arguments.
INTERPRETERS = ("python", "python3", "uv run", "node", "bash", "sh", "perl", "ruby", "Rscript")
INLINE_CODE = ("python -c", "python3 -c", "node -e", "sh -c", "bash -c", "perl -e", "ruby -e")


@dataclasses.dataclass
class AgentSpending:
    """The agent's model calls in a span of time, and what OpenCode says they cost."""
    calls: int = 0
    cost: float = 0.0               # dollars, from OpenCode's price for the model
    input_tokens: int = 0
    output_tokens: int = 0          # without the reasoning tokens
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def tokens_read(self) -> int:
        """The tokens the calls read: their input tokens and their cache read tokens."""
        return self.input_tokens + self.cache_read_tokens


@dataclasses.dataclass(frozen=True)
class ServerInfo:
    """How to reach the OpenCode server of a daemon: its port, its password and its session.

    The daemon saves it in StatePaths.server_file, which only its owner can read, because
    it holds the password. `verifier start` and `verifier attach` read it.
    """
    port: int
    password: str
    session_id: str                 # empty until the daemon has a session
    # the first round sent in the session; None before a round is sent in it
    session_first_round: int | None = None

    @classmethod
    def read(cls, path: Path) -> ServerInfo | None:
        """The saved server info, or None before the daemon has written it.

        Raises:
            ServerFileError: the file lacks a key; an older Verifier wrote it.
        """
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        missing = sorted({"port", "password", "session_id", "session_first_round"} - set(data))
        if missing:
            raise ServerFileError(f"{path} has no {', '.join(missing)}; an older Verifier "
                                  "wrote it, and the daemon writes it again when it starts")
        return cls(port=int(data["port"]), password=str(data["password"]),
                   session_id=str(data["session_id"]),
                   session_first_round=data["session_first_round"])

    def save(self, path: Path) -> None:
        """Save the server info, readable by its owner only."""
        file_storage.JsonFile(path, mode=0o600).write(dataclasses.asdict(self))

    def client(self) -> Client:
        """A client of the server."""
        return Client(self.port, self.password)


class ServerFileError(ValueError):
    """The server file of the daemon is not in the form that ServerInfo.save writes."""


@dataclasses.dataclass(frozen=True)
class TurnOutcome:
    """How the agent's turn went, from the session's messages (Client.turn_outcome)."""
    error: str | None               # the error of the first failed assistant message
    finish: str | None              # why the agent's last step ended ("stop", "length", ...)


class OpenCodeError(Exception):
    """The OpenCode server refused a request or did not answer."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep the message, and the HTTP status when the server answered with one."""
        super().__init__(message)
        self.status = status


class Client:
    """HTTP calls to one OpenCode server."""

    def __init__(self, port: int, password: str, timeout: float = 30.0) -> None:
        """Set up calls to the server on `port`. The calls log in with `password`."""
        self.base = f"http://127.0.0.1:{port}"
        token = base64.b64encode(f"{USERNAME}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self.timeout = timeout

    def _call(self, method: str, path: str, body: object | None = None) -> object:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise OpenCodeError(f"{method} {path}: {e}", status=e.code) from e
        except (urllib.error.URLError, OSError) as e:
            raise OpenCodeError(f"{method} {path}: {e}") from e
        return json.loads(raw) if raw else None

    def healthy(self) -> bool:
        """True if the server answers its health check."""
        try:
            self._call("GET", "/global/health")
        except OpenCodeError:
            return False
        return True

    def create_session(self, title: str) -> str:
        """Create a session and return its id."""
        out = self._call("POST", "/session", {"title": title})
        return out["id"]

    def session_exists(self, session_id: str) -> bool:
        """True if the server knows the session, False if it answers 404.

        Any other failure raises, so a server that is down is never taken as a lost
        session, which would start a new conversation and drop the agent's history.

        Raises:
            OpenCodeError: the server did not answer, or answered with another error.
        """
        try:
            self._call("GET", f"/session/{session_id}")
        except OpenCodeError as e:
            if e.status == 404:
                return False
            raise
        return True

    def busy(self, session_id: str) -> bool:
        """True while the session is running a turn. An idle session is absent from the status."""
        status = self._call("GET", "/session/status") or {}
        return session_id in status

    def prompt(self, session_id: str, text: str) -> None:
        """Send one message and return at once; the turn runs in the background."""
        self._call("POST", f"/session/{session_id}/prompt_async",
                   {"parts": [{"type": "text", "text": text}]})

    def turn_outcome(self, session_id: str, error_since: float,
                     finish_since: float | None = None) -> TurnOutcome:
        """The turn's first error and last finish reason, from one fetch of the messages.

        `error` is that of the first failed assistant message at or after `error_since`
        (epoch time), or None. `finish` is why the last assistant step at or after
        `finish_since` (`error_since` when not given) ended, or None if there is none.
        OpenCode records the model's finish reason on each assistant message: "stop" for
        a turn that ended, "tool-calls" for a step that runs tools, and "length" for a
        step cut by the output limit.
        """
        finish_since = error_since if finish_since is None else finish_since
        messages = self._call("GET", f"/session/{session_id}/message") or []
        error = finish = None
        for m in messages:
            info = m.get("info", {})
            if info.get("role") != "assistant":
                continue
            created = info.get("time", {}).get("created", 0)
            if error is None and created >= error_since * 1000 and info.get("error"):
                err = info["error"]
                error = f"{err.get('name')}: {err.get('data', {}).get('message', '')}".strip()
            if created >= finish_since * 1000:
                finish = info.get("finish")
        return TurnOutcome(error, finish)

    def abort(self, session_id: str) -> None:
        """Stop the session's current turn."""
        self._call("POST", f"/session/{session_id}/abort")


def bash_rules() -> dict[str, str]:
    """OpenCode's bash permission: allow everything, then deny what writes or runs outside.

    Denied: each git command that writes, an interpreter given a file outside
    `.verifier/` (`../` or an absolute path), inline code (`python -c`), and a command
    that starts a file outside `.verifier/` directly.

    OpenCode applies the last rule that matches, so the denials come after "*". It
    matches each command of a compound line (`cd DIR && git commit`) on its own. A
    `*` matches any text, spaces too, and a pattern ending in ` *` also matches the
    command without that part (tests/test_bash_rules.py, `pattern_matches`). Five rules
    cover each write subcommand, here `commit`:

    - `git commit *`: right after `git`;
    - `git -* * commit *`: after a short option and its value, and any options after
      them (`git -C DIR commit`, `git -c KEY=VALUE --no-pager commit`);
    - `git --* commit *`: after a long option (`git --no-pager commit`);
    - `git -p commit *` and `git -P commit *`: after the two short options that take
      no value (--paginate and --no-pager).

    A short option must have a value before the subcommand, so `git -C checkout log`
    runs although `checkout` is a write subcommand's name: the agent's checkout folder
    is named `checkout`. The rules cannot tell -p and -P from an option with a value, so
    `git -P -C checkout log` is refused; `git -C checkout log` runs.

    OpenCode echoes every shell rule to the agent when it refuses a command, so the
    rules are kept few: about two hundred rules make a refusal of about 14,000
    characters, where all pairs of options made 2,260 rules and 157,000 characters.

    Since `*` matches spaces, a later word that is a write subcommand's name is also
    refused when the command starts with an option and the word is the last one or has
    a space after it. For example, `git -C checkout log -- notes` is refused, because it
    matches `git -* * notes *`. `git -C checkout log -- ./notes` is not refused.

    OpenCode refuses these before they run. The rules match the command's text, so they
    stop a plain `git commit` or `cd checkout && git -C . reset`, not a program that runs
    git itself, and not a git alias. If the checkout's HEAD moves anyway, the daemon moves
    it back to the checked commit before it uses it (runtime/daemon.py).
    """
    rules = {"*": "allow"}
    for name in GIT_WRITE_COMMANDS:
        for before in ("", "-* * ", "--* ", "-p ", "-P "):
            rules[f"git {before}{name} *"] = "deny"
    for program in INTERPRETERS:
        rules[f"{program} ../*"] = "deny"
        rules[f"{program} /*"] = "deny"
    for flag in INLINE_CODE:
        rules[f"{flag} *"] = "deny"
    rules["../*"] = rules["/*"] = "deny"
    return rules


def free_port() -> int:
    """A TCP port nothing is listening on right now."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


INSTALLED = (Path.home() / ".local" / "share" / "verifier" / f"opencode-{PINNED_VERSION}"
             / "opencode")


def binary() -> str:
    """The OpenCode binary that install.sh installed, pinned to PINNED_VERSION.

    Raises:
        OpenCodeError: it is not installed.
    """
    if not INSTALLED.exists():
        raise OpenCodeError(f"OpenCode {PINNED_VERSION} is not installed in {INSTALLED.parent}; "
                            "run install.sh from the Verifier's folder")
    return str(INSTALLED)


def version() -> str:
    """The installed OpenCode version."""
    out = subprocess.run([binary(), "--version"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def database(runtime_dir: Path) -> Path:
    """OpenCode's database for the server that start_server() runs with `runtime_dir`."""
    return runtime_dir / "opencode" / "data" / "opencode" / "opencode.db"


def spending(runtime_dir: Path, since_ms: int = 0, session_id: str | None = None
             ) -> AgentSpending:
    """The agent's model calls that began at or after `since_ms`, from OpenCode's database.

    Each assistant message is one model call, with its cost and tokens. Before the
    server's first start there is no database, and no call. With `session_id`, only the
    calls of that session count.

    Raises:
        OpenCodeError: the database or a message is not in the layout of OpenCode
            PINNED_VERSION, so the cost cannot be read.
    """
    db = database(runtime_dir)
    out = AgentSpending()
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = (con.execute("select data from message").fetchall() if session_id is None
                    else con.execute("select data from message where session_id = ?",
                                     (session_id,)).fetchall())
        finally:
            con.close()
    except sqlite3.Error as e:
        raise OpenCodeError(f"the OpenCode database {db} cannot be read: {e}") from e
    for (data,) in rows:
        m = json.loads(data)
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        created = (m.get("time") or {}).get("created")
        if not isinstance(created, int):
            raise OpenCodeError(f"{db}: an assistant message has no creation time: "
                                f"{text.first_part(data, 300)}")
        if created < since_ms:
            continue
        cost, tokens = m.get("cost"), m.get("tokens")
        counts = (tokens.get("input"), tokens.get("output"), tokens.get("reasoning"),
                  (tokens.get("cache") or {}).get("read")) if isinstance(tokens, dict) else ()
        if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0 \
                or len(counts) != 4 or not all(isinstance(n, int) and n >= 0 for n in counts):
            raise OpenCodeError(f"{db}: an assistant message has no usable cost and tokens: "
                                f"{text.first_part(data, 300)}")
        out.calls += 1
        out.cost += cost
        out.input_tokens += counts[0]
        out.output_tokens += counts[1]
        out.reasoning_tokens += counts[2]
        out.cache_read_tokens += counts[3]
    return out


def start_server(state_dir: Path, runtime_dir: Path, port: int, env: dict[str, str],
                 log_path: Path) -> subprocess.Popen:
    """Start `opencode serve` for this working directory and wait until it answers."""
    xdg = {f"XDG_{kind}_HOME": str(runtime_dir / "opencode" / kind.lower())
           for kind in ("DATA", "CONFIG", "CACHE", "STATE")}
    full_env = {**env, **xdg, "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    log = open(log_path, "ab")  # noqa: SIM115 - the server writes to this file
    proc = subprocess.Popen([binary(), "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                            cwd=state_dir, env=full_env, stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    client = Client(port, env["OPENCODE_SERVER_PASSWORD"])
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise OpenCodeError(f"opencode serve exited with {proc.returncode}; see {log_path}")
        if client.healthy():
            return proc
        time.sleep(0.5)
    proc.terminate()
    raise OpenCodeError(f"opencode serve did not answer within 60 s; see {log_path}")
