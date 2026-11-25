"""The event that tells the auditing agent about a review decision."""
from __future__ import annotations

from review.types import ReviewDecision
from runtime.agent import inbox

DECISION_EVENT_KIND = "decision"


def write_decision_event(agent_inbox: inbox.AgentInbox, decision: ReviewDecision) -> None:
    """Leave a decision event, so the agent hears the answer in its next round."""
    agent_inbox.write_event(DECISION_EVENT_KIND, request=decision.request,
                            option=decision.option, note=decision.note)
