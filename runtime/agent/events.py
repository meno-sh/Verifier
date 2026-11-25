"""How the daemon describes each kind of event to the agent, in the prompt for a round.

The runtime's own events are below. Extensions add theirs through `event_describers()`
(see core/registry/extensions.py), so the daemon has no branch per event kind.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.registry import extensions
from runtime.project import git


def _commit(e: dict, root: Path) -> str:
    subject = git.output(root, "log", "-1", "--format=%s", e["sha"]) or "(not found)"
    body = git.output(root, "log", "-1", "--format=%b", e["sha"]).splitlines()
    more = f" [... {len(body)} more lines in the message cut; run git show]" if body else ""
    return f"commit {e['sha']}: {subject}{more}"


def _branch_reset(e: dict, root: Path) -> str:
    return (f"branch_reset: the tracked branch is now at {e['sha']} and no longer contains "
            f"{e['previous']}, the commit checked last")


def _job_ended(e: dict, root: Path) -> str:
    how = ("was stopped with `vjob stop`, so its output is not complete" if e.get("stopped")
           else "was killed" if e.get("killed") else f"exited with {e.get('exit_code')}")
    return f"job {e['job']} {how}. Last lines of its log:\n{e.get('tail', '')}"


def _job_checkin(e: dict, root: Path) -> str:
    return (f"job {e['job']} still running after {e['running_for']} s. "
            f"Last lines of its log:\n{e.get('tail', '')}")


BUILT_IN: dict[str, extensions.EventDescriber] = {
    "commit": _commit,
    "startup": lambda e, root: "startup: build the claim register for this working directory",
    "branch_reset": _branch_reset,
    "job_ended": _job_ended,
    "job_checkin": _job_checkin,
    "timer": lambda e, root: f"your reminder: {e['message']}",
}


def describe(e: dict, root: Path, texts: dict[str, extensions.EventDescriber]) -> str:
    """One event as the agent reads it. An event of an unknown kind is shown as its JSON."""
    render = texts.get(e.get("kind"))
    return render(e, root) if render else json.dumps(e)
