"""The commands of the diagnosis extension.

They are `verifier dispute`, `verifier diagnosis`, and `vcheck check-diagnosis` for the
auditing agent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import ClassVar

from core import cli_command
from core.report_output import report_store, saved_report
from diagnosis import agent_events, dispute_store
from diagnosis.diagnosis_file_check import DiagnosisFileChecker
from diagnosis.dispute_states import DisputeStateResolver, DisputeWithState
from diagnosis.records import DisputeDiagnosisWritten, DisputeError
from diagnosis.share_file import DiagnosisShareFileWriter, ShareFileError
from runtime.agent import agent_launchers, inbox
from runtime.project import git, paths


class DisputeCommand(cli_command.CLICommand):
    """Runs `verifier dispute`.

    It raises a dispute of flags of a saved report, or withdraws one, then refreshes the report.
    """
    HELP: ClassVar[str] = "mark flags of the report as false, or withdraw a dispute"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the options of `verifier dispute`."""
        p.add_argument("--flag", action="append", default=[], metavar="ID",
                       help="the id of a flag in the report, such as F-3a9c1e; give it once "
                            "for each flag")
        p.add_argument("--why", dest="reason", help="why the flags are false")
        p.add_argument("--commit", help="the commit of the report the flags are in (default: "
                                        "the latest report)")
        p.add_argument("--withdraw", metavar="D-N", help="withdraw this dispute instead")

    def execute(self, args: argparse.Namespace) -> int:
        """Record a dispute of the named flags, or withdraw a dispute, then update the report."""
        root: Path = args.root
        state_dir: Path = args.state
        store = dispute_store.DisputeStore(state_dir)
        try:
            if args.withdraw:
                if args.flag or args.reason:
                    raise DisputeError("give --withdraw alone")
                store.withdraw_dispute(args.withdraw)
                print(f"withdrawn: {args.withdraw}. Its flags count again.")
            else:
                self.raise_dispute(args, root, state_dir, store)
        except DisputeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        agent_launchers.ReportRefresher(root, state_dir).refresh_report_and_print_verdicts()
        return 0

    def raise_dispute(self, args: argparse.Namespace, root: Path, state_dir: Path,
                      store: dispute_store.DisputeStore) -> None:
        """Record the dispute of the named flags, and leave an event for the auditing agent.

        The store lock is held until the event is written. When an active dispute has no
        event, and the same flags, commit and reason, the same command recorded it and
        could not write its event. Then only the event is written now. Any other active
        dispute of a flag refuses the new one.

        Raises:
            DisputeError: the dispute is refused, or its event could not be written.
        """
        report = report_store.ReportStore(state_dir).load_saved_report(args.commit or "latest")
        if report is None:
            raise DisputeError(f"there is no report for {args.commit or 'any commit'}")
        flags = [self.issue_with_flag_id(report, flag_id).flag()
                 for flag_id in args.flag]
        agent_inbox = inbox.AgentInbox(state_dir / paths.INBOX)
        commit = report.commit or git.head(root)
        with store.holding_lock():
            earlier = (store.read_ledger().active_dispute_of_same_flags(flags) if flags
                      else None)
            told = agent_events.dispute_ids_with_event(agent_inbox)
            same_command = (earlier is not None and earlier.dispute_id not in told
                            and earlier.commit == commit
                            and earlier.reason == (args.reason or "").strip())
            if same_command:
                assert earlier is not None
                dispute, new = earlier, False
            else:
                dispute = store.raise_dispute_while_locked(flags, args.reason or "", commit)
                new = True
            try:
                agent_events.write_dispute_event(agent_inbox, dispute)
            except OSError as e:
                raise DisputeError(
                    f"{dispute.dispute_id} is recorded, but the auditing agent could not be "
                    f"told about it ({e}). Run the same command again to tell it") from e
        n = len(flags)
        if new:
            print(f"raised {dispute.dispute_id} ({n} flag{'' if n == 1 else 's'}). "
                  "The auditing agent writes its diagnosis in its next round.")
        else:
            print(f"{dispute.dispute_id} was recorded before; the auditing agent is told about it "
                  "now, and writes its diagnosis in its next round.")

    @staticmethod
    def issue_with_flag_id(report: saved_report.SavedReport,
                           flag_id: str) -> saved_report.SavedIssue:
        """The issue of the report with this flag id.

        Raises:
            DisputeError: the report has no such flag, or nobody can dispute it.
        """
        found = report.issue_with_flag_id(flag_id)
        if found is None:
            raise DisputeError(f"the report of {report.commit or 'the latest commit'} has "
                               f"no flag {flag_id}; `verifier report . --json` lists the flags")
        if not found.disputable:
            raise DisputeError(f"{flag_id} ({found.code}) cannot be disputed. A flag "
                               "that something could not be read or checked is not a claim "
                               "the Verifier got wrong")
        return found


class DiagnosisCommand(cli_command.CLICommand):
    """Runs `verifier diagnosis`: lists the disputes, prints one diagnosis, or shares them."""
    HELP: ClassVar[str] = "list your disputes and why each flag was raised; --share writes them"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the options of `verifier diagnosis`."""
        p.add_argument("dispute", nargs="?", metavar="D-N", help="print this diagnosis")
        p.add_argument("--share", action="store_true",
                       help="write the written diagnoses to one file, in full, to check "
                            "and send")
        p.add_argument("-o", "--output",
                       help="with --share: the folder or the file to write (default: the "
                            "project)")
        p.add_argument("--json", action="store_true",
                       help="print every dispute, its state and its diagnosis as one JSON object")

    def execute(self, args: argparse.Namespace) -> int:
        """List the disputes, print one diagnosis, or write the share file."""
        state_dir: Path = args.state
        if args.share:
            try:
                DiagnosisShareFileWriter(state_dir).write_share_file(args.output)
            except ShareFileError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            return 0
        reader = DisputeStateResolver(state_dir)
        if args.json:
            print(json.dumps({"disputes": [self.dispute_json(d)
                                           for d in reader.states_of_all_disputes()]}))
            return 0
        if args.dispute:
            found = reader.state_of_dispute_with_id(args.dispute)
            if found is None:
                print(f"error: there is no dispute {args.dispute}", file=sys.stderr)
                return 1
            print(found.state.diagnosis.text if isinstance(found.state, DisputeDiagnosisWritten)
                  else f"{found.dispute.dispute_id}: {found.state.state_word}")
            return 0
        disputes = reader.states_of_all_disputes()
        if not disputes:
            print("no disputes")
        for d in disputes:
            n = len(d.dispute.flags)
            flags = f"{n} flag{' ' if n == 1 else 's'}"
            print(f"{d.dispute.dispute_id:<6} {flags}  {d.state.state_word}")
        return 0


    @staticmethod
    def dispute_json(found: DisputeWithState) -> dict[str, object]:
        """One dispute, its state, and the text of its diagnosis when it is written."""
        dispute = found.dispute
        return {"id": dispute.dispute_id, "commit": dispute.commit, "reason": dispute.reason,
                "at": dispute.raised_at, "flags": [f.flag_id for f in dispute.flags],
                "state": found.state.state_word,
                "diagnosis": (found.state.diagnosis.text
                              if isinstance(found.state, DisputeDiagnosisWritten) else None)}


class CheckDiagnosisCommand(cli_command.CLICommand):
    """Runs `vcheck check-diagnosis D-N` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the argument of `vcheck check-diagnosis`."""
        p.add_argument("dispute", metavar="D-N")

    def execute(self, args: argparse.Namespace) -> int:
        """Check the shape of the diagnosis of one dispute."""
        state_dir: Path = args.state
        store = dispute_store.DisputeStore(state_dir)
        dispute = store.read_ledger().dispute_with_id(args.dispute)
        if dispute is None:
            print(f"FAILED. There is no dispute {args.dispute} in "
                  f"{dispute_store.DIAGNOSES_DIR}/{dispute_store.DISPUTES}.")
            return 1
        document = store.read_diagnosis(dispute.dispute_id)
        found = (["Write the file first: "
                  + store.diagnosis_name_in_state_folder(dispute.dispute_id)]
                 if document is None
                 else DiagnosisFileChecker().problems_of_diagnosis(document, dispute.flags))
        if found:
            print(f"FAILED. {len(found)} problem{'' if len(found) == 1 else 's'}:")
            print("\n".join(f"  - {p}" for p in found))
            return 1
        print(f"PASSED. The diagnosis of {dispute.dispute_id} is complete. End the round.")
        return 0


