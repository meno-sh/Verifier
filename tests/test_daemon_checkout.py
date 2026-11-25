#!/usr/bin/env python3
"""Check the daemon and checkout fixes, one case or more for each issue of the v10 issue list.

The cases, by issue number:
  - 42: before the first checkout, `verifier status` calls no extension and an
    extension command is refused with a clear message;
  - 43: a checkout that git cannot move stops the daemon with a clear fatal record,
    and a restart does not add the waiting events again;
  - 44: a renamed tracked branch stops the daemon with a fatal record that says what
    to do, and `verifier status` shows it;
  - 45: the agent's git rules refuse `bisect` and `git -P`, and a checkout whose HEAD
    the agent moved is moved back with no commit sent again;
  - 46: a job's $VERIFIER_ROOT is its own checkout;
  - 47: the daemon builds the methods environment from the checkout, not the worker's tree;
  - 51: `verifier status` shows why a stopped daemon stopped;
  - 90: `verifier start` and the daemon read the tracked branch with the same git call.
Returns 1 if any case fails.
"""
import argparse
import contextlib
import io
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_bash_rules  # noqa: E402
import test_runtime as rt  # noqa: E402

from core.registry import extensions  # noqa: E402
from runtime import cli, daemon  # noqa: E402
from runtime.agent import jobs, opencode  # noqa: E402
from runtime.commands import status as status_command  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project import hook, project_preparation  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import alerts  # noqa: E402

FAILED: list[str] = []


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def started(tmp: Path) -> tuple[StatePaths, rt.StubOpenCode, rt.StubbedDaemon, str]:
    """A project whose first round has ended, with its daemon and OpenCode stub."""
    paths = rt.repo(tmp)
    rt.claim_state(paths, (paths.root / "claims.json").read_text())
    hook.install(paths.root, "main")
    stub = rt.StubOpenCode()
    d = rt.StubbedDaemon(paths.root, stub, round_timeout=100)
    d.prepare()
    d.ensure_server()
    first = rt.commit(paths, "one")
    d.tick(now=1000)
    rt.end_round(d, stub, now=1001)
    return paths, stub, d, first


def fatal_key(call) -> str | None:
    """The key of the fatal error that `call` raises, or None if it raises none."""
    try:
        call()
    except alerts.Fatal as e:
        return str(e).split(":")[0]
    return None


def status_text(paths: StatePaths) -> str:
    """What `verifier status` prints."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status_command.VerifierStatusCommand().execute(
            argparse.Namespace(json=False, root=paths.root))
    return out.getvalue()


def test_checked_commit(tmp: Path) -> None:
    """Issues 45 and 43: the checked commit is recorded, and events are not added twice."""
    paths, stub, d, first = started(tmp)
    expect("45: the daemon records the checked commit", paths.checked() == first)
    second = rt.commit(paths, "two")
    d.tick(now=1002)
    rt.end_round(d, stub, now=1003)
    rt.git(paths.checkout, "checkout", "-q", "--detach", first)     # what the agent can do
    d.take_in_branch()
    expect("45: a moved checkout HEAD sends no commit again",
           rt.events_now(paths) == [], repr(rt.events_now(paths)))
    expect("45: a moved checkout HEAD is moved back to the checked commit",
           rt.git(paths.checkout, "rev-parse", "HEAD") == second)

    third = rt.commit(paths, "three")
    (paths.checkout / "notes.md").write_text("changed in the checkout\n")
    key = fatal_key(d.take_in_branch)
    expect("43: a checkout that git cannot move stops the daemon with a clear record",
           key == "daemon.checkout_failed", repr(key))
    waiting = rt.events_now(paths)
    fatal_key(d.take_in_branch)                                        # what a restart does
    expect("43: a restart does not add the waiting events again",
           rt.events_now(paths) == waiting == [{"kind": "commit", "sha": third}],
           repr(rt.events_now(paths)))
    expect("43: the checked commit stays the last one the checkout reached",
           paths.checked() == second)
    stub.httpd.shutdown()


def test_branch_gone(tmp: Path) -> None:
    """Issues 44 and 51: a renamed tracked branch stops the daemon, and status says why."""
    paths, stub, d, _ = started(tmp)
    rt.git(paths.root, "branch", "-m", "main", "trunk")
    key = fatal_key(d.take_in_branch)
    expect("44: a renamed tracked branch stops the daemon with a clear record",
           key == "daemon.branch_missing", repr(key))
    text = status_text(paths)
    expect("51: `verifier status` shows why the daemon stopped",
           "daemon stopped at" in text and "'main' is gone" in text, text)
    alerts.notify("log", "daemon.opencode_started", "opencode serving on port 1", port=1)
    expect("51: after a new start, `verifier status` shows no old stop",
           "daemon stopped at" not in status_text(paths))
    stub.httpd.shutdown()


def test_git_rules(tmp: Path) -> None:
    """Issue 45: the agent's git rules refuse bisect and the -P and -p options."""
    rules = opencode.bash_rules()
    expect("45: `git bisect start` is refused",
           test_bash_rules.verdict(rules, "git bisect start") == "deny")
    expect("45: `git -P checkout main` is refused",
           test_bash_rules.verdict(rules, "git -P checkout main") == "deny")
    expect("45: `git -p -C . reset` is refused",
           test_bash_rules.verdict(rules, "git -p -C . reset") == "deny")


def test_job_root(tmp: Path) -> None:
    """Issue 46: a job reads its own checkout through $VERIFIER_ROOT."""
    paths, stub, d, first = started(tmp)
    (paths.state / "where.sh").write_text('echo "$VERIFIER_ROOT"\n')
    jobs.start(paths, "where", ["bash", "../../where.sh"], checkin=None)
    deadline = time.time() + 10
    while time.time() < deadline and jobs.exit_code(paths, "where") is None:
        time.sleep(0.1)
    expect("46: a job's $VERIFIER_ROOT is its own checkout",
           jobs.tail(paths, "where").strip() == str(paths.jobs / "where.tree"),
           jobs.tail(paths, "where"))
    stub.httpd.shutdown()


def test_environment_source(tmp: Path) -> None:
    """Issue 47: the methods environment is built from the checkout."""
    paths, stub, d, _ = started(tmp)
    seen: list[Path] = []
    real = daemon.sync_environment
    daemon.sync_environment = lambda root, state: seen.append(root)
    try:
        d.prepare()
    finally:
        daemon.sync_environment = real
    expect("47: prepare() builds the environment from the checkout",
           seen == [paths.checkout], repr(seen))
    stub.httpd.shutdown()


def test_before_first_checkout(tmp: Path) -> None:
    """Issues 42 and 90: no extension reads a checkout that does not exist yet."""
    paths = rt.repo(tmp)
    extensions.ExtensionSet.save_names(paths.state, ("review.extension:ReviewExtension",))
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["review", str(paths.root)])
        expect("42: an extension command before the first checkout is refused",
               code == 1 and "nothing is checked yet" in err.getvalue(), err.getvalue())
        text = status_text(paths)
        expect("42: `verifier status` before the first checkout calls no extension",
               "checked up to nothing yet" in text, text)
    finally:
        (paths.state / extensions.EXTENSIONS_FILE).unlink()
    expect("90: `verifier start` accepts a branch that the daemon can read",
           project_preparation.branch_problem(paths, "main") is None
           and runtime_git.has_branch(paths.root, "main"))
    paths.branch_file.unlink()          # a first start, before any branch is recorded
    problem = project_preparation.branch_problem(paths, "gone") or ""
    expect("90: `verifier start` refuses a missing branch", "no branch 'gone'" in problem, problem)
    rt.git(paths.root, "tag", "v1")
    expect("90: a tag is not a branch", not runtime_git.has_branch(paths.root, "v1"))


def main() -> int:
    """Run every group of cases, each in a fresh temporary directory."""
    for group in (test_checked_commit, test_branch_gone, test_git_rules, test_job_root,
                  test_environment_source, test_before_first_checkout):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            group(Path(tmp))
    print(json.dumps({"failed": FAILED}) if FAILED else "\nAll daemon and checkout cases passed.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
