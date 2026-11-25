"""Human review as an extension of the real-time tools (see core/registry/extensions.py).

The core and the schema do not import this package.
It adds claim checks, report notices, commands, status lines and agent events.
"""
from __future__ import annotations

from pathlib import Path

from core import cli_command
from core.registry import checks
from core.registry.extensions import EventDescriber, Extension
from core.report_model import report_header
from review.agent_events import DECISION_EVENT_KIND
from review.claim_checks import ReviewClaimCheck
from review.commands import (
    DecideCommand,
    RaiseReviewRequestCommand,
    ReviewCommand,
    WithdrawReviewRequestCommand,
)
from review.kinds import REVIEW_KINDS
from review.states import ReviewStateResolver
from review.store import ReviewStore
from runtime.agent import inbox
from runtime.project import paths
from schema import findings

# The guide for the worker's agent.


# The agent's rules.


class ReviewExtension(Extension):
    """Human review: claim checks, report notices, commands, status lines and agent events."""

    def extend(self, reg: checks.CheckRegistry, root: Path, state_dir: Path) -> None:
        """Add one claim check per review kind to the run's registry."""
        ReviewClaimCheck.add_one_check_for_each_kind(reg, ReviewStore(state_dir), REVIEW_KINDS,
                                                     root)

    def notices(self, tree: findings.Findings, root: Path,
                state_dir: Path) -> list[report_header.ReportHeaderRow]:
        """The "Waiting for you" row under the verdict, or none when nothing waits.

        Each request is a detail of depth 1, and what it says and its options are details
        of depth 2.
        """
        requests = ReviewStateResolver(REVIEW_KINDS, root).requests_waiting_for_answer(
            ReviewStore(state_dir).read_ledger())
        if not requests:
            return []
        claims = [c.claim for c in tree.claims]
        details: list[tuple[int, str]] = []
        for q in requests:
            kind = REVIEW_KINDS.kind_named(q.kind)
            n = len(kind.ids_of_claims_it_bears_on(q.facts, claims, root))
            details.append((1, f"{q.id}  {q.question}"))
            details.append((2, f"Raised by the {q.raised_by} at {q.raised_in[:12]}: {q.reason}"))
            if n:
                details.append((2, f"{n} claim(s) rest on it and are reported as waiting "
                                   "until you answer."))
            details += [(2, f"{option:<9}{effect}")
                        for option, effect in kind.option_effects.items()]
        return [report_header.ReportHeaderRow(
            name="Waiting for you", text=f"{len(requests)} review(s). Run `verifier review .`.",
            details=tuple(details))]

    def status_lines(self, root: Path, state_dir: Path) -> list[str]:
        """One line when requests are waiting. It reads only."""
        waiting = ReviewStateResolver(REVIEW_KINDS, root).requests_waiting_for_answer(
            ReviewStore(state_dir).read_ledger())
        n = len(waiting)
        return [f"reviews waiting for you: {n}; run `verifier review .`"] if n else []

    def vcheck_commands(self) -> dict[str, cli_command.CLICommand]:
        """`ask` and `withdraw`, for the auditing agent."""
        return {"ask": RaiseReviewRequestCommand(), "withdraw": WithdrawReviewRequestCommand()}

    def verifier_commands(self) -> dict[str, cli_command.CLICommand]:
        """`review` and `decide`, for a person."""
        return {"review": ReviewCommand(), "decide": DecideCommand()}

    def event_describers(self) -> dict[str, EventDescriber]:
        """How a decision is described to the agent."""
        return {DECISION_EVENT_KIND: self.describe_decision_event}

    @staticmethod
    def describe_decision_event(event: dict[str, object], root: Path) -> str:
        """The line of the round prompt for one decision event."""
        note = f": {event['note']}" if event.get("note") else ""
        return f"a person answered review {event['request']} with {event['option']}{note}"

    def after_round_warnings(self, root: Path, state_dir: Path) -> list[str]:
        """A warning per decision not made through `verifier review` or `verifier decide`.

        Both commands leave a decision event, so a decision with no event was written some
        other way, for example by the agent.
        """
        told = {e.get("request") for e in inbox.AgentInbox(state_dir / paths.INBOX).all_events()
                if e.get("kind") == DECISION_EVENT_KIND}
        return [f"decision on {d.request} was not recorded through verifier review or "
                "verifier decide"
                for d in ReviewStore(state_dir).read_ledger().decisions if d.request not in told]

    def worker_instructions(self) -> str:
        """The section of WORKER.md on the auditing agent's questions for the person."""
        return "\n".join([
            "## 7. Questions for the person", "",
            "The auditing agent can ask the person a question, for example to confirm a data",
            "file that changed. `verifier status .` says how many questions wait.", "",
            "- Tell the person about each waiting question. `verifier review .` shows them.",
            "- Record an answer with `verifier decide . R-N OPTION` only with the words of the",
            "  person.", ""])

    def agent_instructions(self) -> str:
        """The review scenarios of AGENTS.md: the requests of a commit, and an answer.

        Each review kind adds one item to the commit scenario.
        """
        return "\n".join([
            "### commit: review requests", "",
            "This adds steps to \"commit\". Some decisions are not yours: a person makes them.",
            "", "Output, after the steps of \"commit\":",
            "1. Raise a review request for each case below. A request with the same facts as an",
            "   open request gives the open request again.", "",
            *["   " + line if line else "" for kind in REVIEW_KINDS.agent_guide_text()
              for line in kind.splitlines()], "",
            "2. If this commit restores a file of an open request R-N, run",
            "   `vcheck withdraw R-N --reason \"<why>\"`.", "",
            "Never answer a request yourself. Never run `verifier review` or `verifier decide`.",
            "Never change a file in `./reviews/`.", "",
            "### answer to a review request", "",
            "The line: `a person answered review R-N with OPTION: NOTE`", "",
            "Output: do nothing.", ""])
