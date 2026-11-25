"""The command-line channel: the text of a request, and the walk of `verifier review`."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from review.recorder import ReviewRequestRecorder
from review.types import ReviewDecision, ReviewRequest

VIA = "cli"


class InteractiveReviewSession:
    """Shows each waiting request and records the answers that a person types."""

    def __init__(self, recorder: ReviewRequestRecorder, root: Path,
                 ask: Callable[[str], str] = input,
                 say: Callable[[str], None] = print) -> None:
        """Record with `recorder`, and show evidence from `root`.

        Read the answers with `ask`, and show the text with `say`.
        """
        self.recorder = recorder
        self.root = root
        self.ask = ask
        self.say = say

    def review_waiting_requests(self) -> list[ReviewDecision]:
        """Walk through every waiting request and record the answers given. Returns them.

        It first repairs a change of the claim register that failed before.
        """
        made: list[ReviewDecision] = []
        self.recorder.repair_failed_kind_changes()
        requests = self.recorder.states.requests_waiting_for_answer(
            self.recorder.store.read_ledger())
        if not requests:
            self.say("Nothing is waiting for review.")
            return made
        for request in requests:
            decision = self.review_one_request(request)
            if decision is not None:
                made.append(decision)
        return made

    def review_one_request(self, request: ReviewRequest) -> ReviewDecision | None:
        """Show one request and record the answer typed, or None when the person skips it."""
        kind = self.recorder.kinds.kind_named(request.kind)
        self.say(self.text_for_person(request))
        keys = {option[0]: option for option in kind.options}
        prompt = "  " + "  ".join(f"[{k}]{o[1:]}" for k, o in keys.items()) + "  [s]kip > "
        while True:
            answer = self.ask(prompt).strip().lower()
            if answer in ("s", "skip", ""):
                self.say(f"Skipped {request.id}.\n")
                return None
            option = keys.get(answer[:1]) if answer[:1] in keys else None
            if option is None:
                self.say("  Please answer with one of the letters shown.")
                continue
            note = self.ask("  Note (optional): ").strip()
            decision = self.recorder.record_decision(request.id, option, note, VIA)
            self.say(f"Recorded: {request.id} {option}.\n")
            return decision

    def text_for_person(self, request: ReviewRequest) -> str:
        """One request as a person sees it before answering.

        The text has the question, the evidence, who asked and why, and the options.
        """
        kind = self.recorder.kinds.kind_named(request.kind)
        lines = [f"{request.id}  {request.question}", "",
                 *(f"  {line}" for line in kind.evidence_for_person(request,
                                                                    self.root).splitlines()),
                 f"  Asked by the {request.raised_by}, because: {request.reason}", "",
                 *(f"  {option:<9}{effect}" for option, effect in kind.option_effects.items())]
        return "\n".join(lines)
