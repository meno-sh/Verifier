#!/usr/bin/env python3
"""Check that a missing or wrong setting stops a run before it starts, with a clear error.

Covers:
  - utils/model_keys.json gives each host's key variable; a host not in it is refused,
    and a host whose variable is not set is refused, both before any call;
  - the model review's settings are run options: a watched project can turn it on, and
    a model review with no key, or with both a call and a saved reading, is refused;
  - the options of a paper check group are not checked when there is no document;
  - `verifier start-if-pending` refuses a project whose models have no key;
  - a uv.lock that did not build stops `vcheck run` with BLOCK UNTIL FIXED.
Returns 1 if any case fails, so it can gate a commit.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from checks.model_review import model_review  # noqa: E402
from checks.uncertainty import uncertainty  # noqa: E402
from core import cli as core_cli  # noqa: E402
from core.analysis import analysis_request, findings_analyzer  # noqa: E402
from core.project import watched_project
from core.registry import extensions
from runtime import cli as runtime_cli  # noqa: E402
from runtime.commands import start_if_pending  # noqa: E402
from runtime.project import daemon_process  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import models  # noqa: E402

BUILT_IN_CHECKS = extensions.builtin_check_registry()   # the registry of the built-in checks

FAILED: list[str] = []
KEY = "OPENROUTER_API_KEY"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions#m"
PAPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "broken_paper.tex")
OPTIONS_FILE = watched_project.WatchedProject.OPTIONS_FILE


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def error(call: object) -> str:
    """The message of the ValueError that `call` raises, or "" when it raises none."""
    try:
        call()
    except ValueError as e:
        return str(e)
    return ""


@contextlib.contextmanager
def environment(**values: str | None):
    """Set or remove environment variables for the block, then restore them."""
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_keys(tmp: Path) -> None:
    """The key file, and the one function that reads a key."""
    expect("keys: the file maps openrouter.ai to OPENROUTER_API_KEY",
           models.load_keys()["openrouter.ai"] == KEY)
    bad = tmp / "keys.json"
    bad.write_text(json.dumps({"openrouter.ai": 1}))
    expect("keys: a file whose values are not names is refused",
           "maps each host" in error(lambda: models.load_keys(bad)))
    no_key = tmp / "no-key.json"
    no_key.write_text(json.dumps({"gpu-box": ""}))
    expect("keys: an empty name means a host with no key",
           models.load_keys(no_key) == {"gpu-box": None})
    unknown = error(lambda: models.check_key("http://typo-host:8000/v1/chat/completions#m"))
    expect("keys: a host that is not in the file is refused, and the error names the file",
           "typo-host" in unknown and "model_keys.json" in unknown, unknown)
    with environment(**{KEY: None}):
        try:
            models.ask("q", OPENROUTER, sleep=lambda s: None)
            got = ""
        except models.KeyMissing as e:
            got = str(e)
        expect("keys: a model call with no key stops before it calls, with the same error",
               got == error(lambda: models.check_key(OPENROUTER)) and KEY in got, got)
    with environment(**{KEY: "test-key"}):
        expect("keys: with the variable set, the key is the variable's value",
               models.api_key("https://openrouter.ai/api/v1/chat/completions") == "test-key")


def test_model_review(tmp: Path) -> None:
    """The model review's settings are run options, with a rule that runs before the run."""
    expect("model review: its four settings are run options",
           {model_review.WANT.key, model_review.MODEL.key, model_review.OUT.key,
            model_review.FROZEN.key} <= set(BUILT_IN_CHECKS.run_option_catalog.options))
    with environment(**{KEY: None}):
        missing = error(lambda: BUILT_IN_CHECKS.refuse_options_that_do_not_fit(
            {model_review.WANT.key: True, uncertainty.WANT.key: False}))
    expect("model review: asked for with no key, the run is refused", KEY in missing, missing)
    saved = tmp / "reading.txt"
    saved.write_text('{"findings": []}')
    both = {model_review.WANT.key: True, model_review.FROZEN.key: str(saved)}
    expect("model review: a model call and a saved reading together are refused",
           "cannot both be set" in error(lambda: model_review.options_fit(both)))
    gone = {model_review.FROZEN.key: str(tmp / "gone.txt")}
    expect("model review: a saved reading that is not a file is refused",
           "is not a file" in error(lambda: model_review.options_fit(gone)))
    model_review.options_fit({model_review.FROZEN.key: str(saved)})
    expect("model review: a saved reading alone passes", True)
    with environment(**{KEY: None}):
        tree = findings_analyzer.FindingsAnalyzer(BUILT_IN_CHECKS).analyse(
            analysis_request.AnalysisRequest(run_options={model_review.WANT.key: True}))
    expect("model review: with no document, its options are not checked", not tree.papers)
    state = tmp / "w" / ".verifier"
    state.mkdir(parents=True)
    (state / OPTIONS_FILE).write_text(json.dumps({model_review.WANT.key: True}))
    options = watched_project.WatchedProject.load(state.parent, state).run_options(BUILT_IN_CHECKS)
    expect("model review: a watched project turns it on in .verifier/options.json",
           options[model_review.WANT.key] is True
           and options[model_review.OUT.key] == str(state / "readings" / "model_review.txt"),
           repr({k: options[k] for k in (model_review.WANT.key, model_review.OUT.key)}))
    argv = sys.argv
    sys.argv = ["verifier", "static-check", "--paper", PAPER, "--frozen-model-review", str(saved),
                "--json"]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            runtime_cli.main(sys.argv[1:])
    finally:
        sys.argv = argv
    result = json.loads(out.getvalue())["findings"]["papers"][PAPER]["checks"]["model_review"]
    expect("model review: static-check --frozen-model-review reads the saved reading",
           result["type"] == "Passed", repr(result))


def test_start(tmp: Path) -> None:
    """`verifier start-if-pending` checks the keys, as `verifier start` does."""
    paths = StatePaths(root=tmp / "w")
    paths.ensure()
    paths.branch_file.write_text("main\n")
    (paths.inbox / "0-startup.json").write_text(json.dumps({"kind": "startup"}))
    args = type("Args", (), {"roots": [paths.root], "model": "openrouter/m/m",
                             "round_timeout": 60.0})()
    err = io.StringIO()
    with environment(**{KEY: None}), contextlib.redirect_stderr(err):
        code = start_if_pending.VerifierStartIfPendingCommand().execute(args)
    expect("start-if-pending: a project with no key is refused, and no daemon starts",
           code == 1 and KEY in err.getvalue() and daemon_process.daemon_pid(paths) is None,
           err.getvalue())


def test_environment(tmp: Path) -> None:
    """A uv.lock that did not build stops the run."""
    root = tmp / "w"
    state = root / ".verifier"
    state.mkdir(parents=True)
    (root / "uv.lock").write_text("not a lockfile\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = core_cli.main(["--state", str(state), "--root", str(root), "run",
                              "--commit", "abcdef1234567890"])
    expect("environment: vcheck run gives BLOCK UNTIL FIXED and names uv.lock",
           code == 1 and out.getvalue().startswith("Verdict: BLOCK UNTIL FIXED")
           and "uv.lock did not build" in out.getvalue(), out.getvalue())
    (root / "uv.lock").unlink()
    expect("environment: with no uv.lock, methods use the standard library",
           "(no uv.lock)" in watched_project.WatchedProject.load(
               root, state).method_environment_row().text)


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_keys, test_model_review, test_start, test_environment):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    if FAILED:
        print(f"\nFAILED: {len(FAILED)} case(s).")
        return 1
    print("\nAll loud-settings cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
