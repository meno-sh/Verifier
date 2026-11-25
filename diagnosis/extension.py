"""Disputes and diagnoses as an extension of the real-time tools (see core/registry/extensions.py).

A person or the worker's agent disputes flags of the report with `verifier dispute`.
The report then strikes them out, and gives the verdict without them
(core/report_model/disputed_flags.py).
In its next round, the auditing agent finds why the Verifier raised each flag, and writes
a diagnosis for the developers. `verifier diagnosis . --share` puts the written diagnoses
in one file, in full, for the person to read and send. The core does not import
this package.
"""
from __future__ import annotations

from pathlib import Path

from core import cli_command
from core.analysis import verdict_policy
from core.registry.extensions import EventDescriber, Extension
from core.report_model import report_header
from core.report_model.disputed_flags import DisputedFlag, DisputedFlagSet
from diagnosis import agent_events, diagnosis_file_check
from diagnosis.commands import CheckDiagnosisCommand, DiagnosisCommand, DisputeCommand
from diagnosis.dispute_states import DisputeStateResolver
from diagnosis.dispute_store import DisputeStore
from runtime.agent import inbox, rounds
from runtime.project import paths
from schema import findings

SHARE_COMMAND = agent_events.SHARE_COMMAND
# The guide the agent reads before it writes a diagnosis, as a path in $VERIFIER_HOME.
GUIDE_PATH = "diagnosis/guide.md"


# The guide for the worker's agent.


# The auditing agent's rules.


class DiagnosisExtension(Extension):
    """Disputes and diagnoses: disputed flags, report notices, commands and agent events."""

    def disputed_flags(self, root: Path, state_dir: Path) -> list[DisputedFlag]:
        """Each flag of each dispute that is not withdrawn."""
        return [flag for d in DisputeStore(state_dir).read_ledger().active_disputes()
                for flag in d.disputed_flags_for_report()]

    def notices(self, tree: findings.Findings, root: Path,
                state_dir: Path) -> list[report_header.ReportHeaderRow]:
        """One row when flags of this report are disputed."""
        flags = DisputedFlagSet(self.disputed_flags(root, state_dir),
                                verdict_policy.VerdictPolicy())
        n = len(flags.places_in_findings(tree))
        return ([report_header.ReportHeaderRow(
            name="DisputedFlag", text=f"{n} flag{'' if n == 1 else 's'}. Please run "
                                      f"`{SHARE_COMMAND}`, read the file to double-check its "
                                      "anonymity, and send it to verifier@meno.sh.")]
                if n else [])

    def status_lines(self, root: Path, state_dir: Path) -> list[str]:
        """One line when disputes wait for a diagnosis."""
        n = DisputeStateResolver(state_dir).count_waiting_for_diagnosis()
        return [f"disputes waiting for a diagnosis: {n}"] if n else []

    def verifier_commands(self) -> dict[str, cli_command.CLICommand]:
        """`dispute` and `diagnosis`, for a person or the worker's agent."""
        return {"dispute": DisputeCommand(), "diagnosis": DiagnosisCommand()}

    def vcheck_commands(self) -> dict[str, cli_command.CLICommand]:
        """`check-diagnosis`, for the auditing agent."""
        return {"check-diagnosis": CheckDiagnosisCommand()}

    def event_describers(self) -> dict[str, EventDescriber]:
        """How a dispute is described to the auditing agent."""
        return {agent_events.DISPUTE_EVENT_KIND: self.describe_dispute_event}

    @staticmethod
    def describe_dispute_event(event: dict[str, object], root: Path) -> str:
        """The line of the round prompt for one dispute event."""
        n = event["flag_count"]
        return (f"a person or the worker's agent disputed {n} flag{'' if n == 1 else 's'} in "
                f"{event['dispute_id']}; write the diagnosis as AGENTS.md says (dispute)")

    def after_round_warnings(self, root: Path, state_dir: Path) -> list[str]:
        """One line for each dispute with no event, then for each one the last round missed.

        `verifier dispute` leaves an event for each dispute. A dispute with no event was
        written some other way, for example by the agent, or the command could not write
        the event.
        """
        told = agent_events.dispute_ids_with_event(inbox.AgentInbox(state_dir / paths.INBOX))
        disputes = DisputeStateResolver(state_dir).states_of_all_disputes()
        out = [f"dispute {d.dispute.dispute_id} has no event: it was written without "
               "`verifier dispute`, or that command could not write its event; running the "
               "same command again writes it"
               for d in disputes if d.dispute.dispute_id not in told]
        last = rounds.RoundHistory(paths.StatePaths(root=state_dir.parent)).last_round()
        n, last_events = (last.n, last.events) if last is not None else (0, [])
        sent = {e.get("dispute_id") for e in last_events
                if e.get("kind") == agent_events.DISPUTE_EVENT_KIND}
        out += [f"round {n} got dispute {d.dispute.dispute_id} and wrote no complete diagnosis"
                for d in disputes
                if d.dispute.dispute_id in sent and d.state.is_waiting]
        return out

    def worker_instructions(self) -> str:
        """The section of WORKER.md on diagnoses."""
        return "\n".join([
            "## 8. Diagnoses", "",
            "After a dispute, the auditing agent writes a diagnosis for the developers of the",
            "Verifier. `verifier diagnosis .` lists the disputes and their diagnoses.", "",
            "- When a diagnosis is written, tell the person once.",
            f"- Tell the person to run `{SHARE_COMMAND}`, to read the file to double-check its",
            "  anonymity, and to send it to verifier@meno.sh.",
            ""])

    def agent_instructions(self) -> str:
        """The "dispute" scenario of AGENTS.md."""
        return "\n".join([
            "### dispute", "",
            "The line: `a person or the worker's agent disputed N flags in D-N; ...`", "",
            "A person says that some flags of the report are false. Find why the Verifier raised",
            "each flag, and write a diagnosis. The developers of the Verifier read the diagnosis",
            "to make the Verifier better. The diagnosis does not change the report.", "",
            "Input:",
            "- `./diagnoses/disputes.jsonl`: the dispute D-N, with its flags and its reason.",
            f"- `$VERIFIER_HOME/{GUIDE_PATH}`: how the Verifier finds a flag, the logs that",
            "  record each step, how to read each log, and an example diagnosis. Read it first.",
            "- The logs in `.` that the guide names.",
            "- `./claim_register.json`, `./methods/`, `./checks/`, and the code of the checks in",
            "  `$VERIFIER_HOME/checks/`.", "",
            "Output:",
            "1. Find the cause of each flag. One cause can give many flags.",
            "2. Decide if each flag is false. You can disagree with the dispute.",
            "3. Write `./diagnoses/D-N.md` with these five headings, in this order:", "",
            "       ## Flags",
            "           each flag: its id, type, subject and detail",
            "       ## Why the Verifier flagged them",
            "           the cause, from the claim, its quotes, its source, its method and the",
            "           check",
            "       ## Is each flag false",
            "           one line for each flag: - `F-3a9c1e`: yes, no or partly, then the evidence",
            "       ## Cause",
            "           one of: " + ", ".join(diagnosis_file_check.CAUSES),
            "       ## What to change in the Verifier",
            "           the change to make in the Verifier", "",
            "   `auditor-agent` is your work or these instructions. `the-checks` is a check.",
            "   `deterministic-core` is the code with no model. `eval` is the evaluation code.",
            "4. Write the diagnosis in ASD Simplified Technical English (ASD-STE100):",
            "   - one idea in each sentence, and at most 25 words in each sentence;",
            "   - the active voice and simple tenses only;",
            "   - one word for one meaning, and common words;",
            "   - exact technical names unchanged.",
            "5. Run `vcheck check-diagnosis D-N`. If it prints `FAILED`, fix each problem that it",
            "   prints, and run it again until it prints `PASSED`.", "",
            "Change no other file because of a dispute. Never edit `./diagnoses/disputes.jsonl`",
            "or `./diagnoses/withdrawals.jsonl`.", ""])
