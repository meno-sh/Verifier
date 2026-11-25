"""What the auditing agent is told about disputes.

That is the event of a dispute, and the command that shares the diagnoses.
"""
from __future__ import annotations

from diagnosis.records import Dispute
from runtime.agent import inbox

DISPUTE_EVENT_KIND = "dispute"
SHARE_COMMAND = "verifier diagnosis . --share"


def write_dispute_event(agent_inbox: inbox.AgentInbox, dispute: Dispute) -> None:
    """Leave a dispute event, so the agent diagnoses the dispute in its next round."""
    agent_inbox.write_event(DISPUTE_EVENT_KIND, dispute_id=dispute.dispute_id,
                            flag_count=len(dispute.flags))


def dispute_ids_with_event(agent_inbox: inbox.AgentInbox) -> set[object]:
    """The ids of the disputes that have an event in the inbox, pending or delivered."""
    return {e.get("dispute_id") for e in agent_inbox.all_events()
            if e.get("kind") == DISPUTE_EVENT_KIND}
