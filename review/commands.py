"""The commands of the review extension.

`vcheck ask` and `vcheck withdraw` are for the auditing agent. `verifier review` and
`verifier decide` are for a person.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.project import watched_project
from review import agent_events
from review.channels.cli import VIA, InteractiveReviewSession
from review.kinds import REVIEW_KINDS, ReviewKindRegistry
from review.recorder import ReviewRequestRecorder
from review.store import ReviewError, ReviewStore
from review.types import ReviewDecision
from runtime.agent import agent_launchers, inbox
from runtime.project import git, paths


def review_recorder(root: Path, state_dir: Path,
                    kinds: ReviewKindRegistry = REVIEW_KINDS) -> ReviewRequestRecorder:
    """The recorder of the reviews of `state_dir`, with these kinds."""
    return ReviewRequestRecorder(ReviewStore(state_dir), kinds, root, state_dir)


class ReviewCommandBase(cli_command.CLICommand):
    """A review command, with the kinds it uses."""

    def __init__(self, kinds: ReviewKindRegistry = REVIEW_KINDS) -> None:
        """Use these kinds; the registered kinds by default."""
        self.kinds = kinds


class RaiseReviewRequestCommand(ReviewCommandBase):
    """Runs `vcheck ask`: raises a review request, or reports the open one with the same facts."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck ask`."""
        p.add_argument("kind", choices=sorted(k.name for k in self.kinds.all_kinds()))
        p.add_argument("facts", nargs="*", metavar="key=value")
        p.add_argument("--reason", required=True, help="why a person should decide this")
        p.add_argument("--commit", help="the commit being checked (default: HEAD)")

    def execute(self, args: argparse.Namespace) -> int:
        """Raise a review request, or report the open one with the same facts."""
        root: Path = args.root
        state_dir: Path = args.state
        pairs = dict(f.split("=", 1) for f in args.facts if "=" in f)
        if len(pairs) != len(args.facts):
            print("error: give each fact as key=value", file=sys.stderr)
            return 1
        kind = self.kinds.kind_named(args.kind)
        project = watched_project.WatchedProject.load(root, state_dir)
        register = project.claim_register(project.check_registry())
        try:
            facts, question = kind.build_request_facts(root, register.claims, pairs)
            commit = args.commit or git.head(root)
            request, new = review_recorder(root, state_dir, self.kinds).raise_request(
                args.kind, facts, question, args.reason, "agent", commit)
        except ReviewError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"{'raised' if new else 'already open'}: {request.id}  {request.question}")
        return 0


class WithdrawReviewRequestCommand(ReviewCommandBase):
    """Runs `vcheck withdraw`: withdraws a request nobody has answered."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck withdraw`."""
        p.add_argument("request")
        p.add_argument("--reason", required=True)

    def execute(self, args: argparse.Namespace) -> int:
        """Withdraw a request nobody has answered, and undo what raising it changed."""
        root: Path = args.root
        state_dir: Path = args.state
        try:
            review_recorder(root, state_dir, self.kinds).withdraw_request(args.request, args.reason)
        except ReviewError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"withdrawn: {args.request}")
        return 0


def tell_agent_about_decisions(root: Path, state_dir: Path,
                                  decisions: list[ReviewDecision]) -> None:
    """Leave a decision event for each decision, so the agent hears the answers."""
    agent_inbox = inbox.AgentInbox(state_dir / paths.INBOX)
    for decision in decisions:
        agent_events.write_decision_event(agent_inbox, decision)


class ReviewCommand(ReviewCommandBase):
    """Runs `verifier review`: walks through the waiting requests and records the answers."""
    HELP: ClassVar[str] = "answer the questions that the auditing agent asks you"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the option of `verifier review`."""
        p.add_argument("--json", action="store_true",
                       help="print the waiting requests as one JSON object, and ask nothing")

    def execute(self, args: argparse.Namespace) -> int:
        """Walk through the waiting requests, then tell the agent and refresh the report.

        With --json, print the waiting requests and change no file.
        """
        root: Path = args.root
        state_dir: Path = args.state
        if args.json:
            print(json.dumps({"requests": self.waiting_requests_json(root, state_dir)}))
            return 0
        session = InteractiveReviewSession(review_recorder(root, state_dir, self.kinds), root)
        try:
            made = session.review_waiting_requests()
        except ReviewError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        tell_agent_about_decisions(root, state_dir, made)
        if made:
            agent_launchers.ReportRefresher(root, state_dir).refresh_report_and_print_verdicts()
        return 0

    def waiting_requests_json(self, root: Path, state_dir: Path) -> list[dict[str, object]]:
        """Each request waiting for an answer, with its evidence and the effect of each option."""
        recorder = review_recorder(root, state_dir, self.kinds)
        out: list[dict[str, object]] = []
        for request in recorder.states.requests_waiting_for_answer(recorder.store.read_ledger()):
            kind = self.kinds.kind_named(request.kind)
            out.append({"id": request.id, "kind": request.kind, "title": kind.title,
                        "question": request.question, "reason": request.reason,
                        "raised_by": request.raised_by,
                        "evidence": kind.evidence_for_person(request, root),
                        "options": dict(kind.option_effects)})
        return out


class DecideCommand(ReviewCommandBase):
    """Runs `verifier decide`: records one answer to one request."""
    HELP: ClassVar[str] = "answer one question of the auditing agent"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier decide`."""
        p.add_argument("request")
        p.add_argument("option")
        p.add_argument("--note", default="")

    def execute(self, args: argparse.Namespace) -> int:
        """Record one answer, then tell the agent and refresh the report."""
        root: Path = args.root
        state_dir: Path = args.state
        try:
            decision = review_recorder(root, state_dir, self.kinds).record_decision(
                args.request, args.option, args.note, VIA)
        except ReviewError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        tell_agent_about_decisions(root, state_dir, [decision])
        print(f"Recorded: {args.request} {args.option}.")
        agent_launchers.ReportRefresher(root, state_dir).refresh_report_and_print_verdicts()
        return 0
