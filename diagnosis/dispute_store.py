"""Disputes, their withdrawals and their diagnoses, in `.verifier/diagnoses/`.

    disputes.jsonl      one dispute per line: id, commit, flags, reason, at
    withdrawals.jsonl   one withdrawal per line: id, at
    D-N.md              the diagnosis of dispute D-N, which the auditing agent writes

The two JSON lines files are never rewritten. The state of a dispute is computed
from them and from its diagnosis (diagnosis/dispute_states.py).
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from core.report_output.saved_report import SavedFlag
from diagnosis.diagnosis_document import DiagnosisDocument
from diagnosis.records import Dispute, DisputeError, DisputeWithdrawal
from schema.primitives import record
from utils import file_storage, structure, timestamps

DIAGNOSES_DIR = "diagnoses"
DISPUTES, WITHDRAWALS = "disputes.jsonl", "withdrawals.jsonl"
# Held while a command reads the store and appends to it, so two at once cannot both
# give out the same id. It is in runtime/, which the state's git history leaves out.
LOCK_FILE = Path(structure.RUNTIME_DIR) / "diagnoses.lock"


@record
class DisputeLedger:
    """All disputes and withdrawals, read once, oldest first."""
    disputes: tuple[Dispute, ...]
    withdrawals: tuple[DisputeWithdrawal, ...]

    def dispute_with_id(self, dispute_id: str) -> Dispute | None:
        """The dispute with this id, or None."""
        return next((d for d in self.disputes if d.dispute_id == dispute_id), None)

    def withdrawal_for(self, dispute_id: str) -> DisputeWithdrawal | None:
        """The first withdrawal of a dispute, or None."""
        return next((w for w in self.withdrawals if w.dispute_id == dispute_id), None)

    def active_disputes(self) -> list[Dispute]:
        """Every dispute that is not withdrawn, oldest first."""
        return [d for d in self.disputes if self.withdrawal_for(d.dispute_id) is None]

    def active_dispute_of_same_flags(self, flags: list[SavedFlag]) -> Dispute | None:
        """The active dispute whose flags are exactly `flags`, by identity, or None."""
        wanted = {f.identity() for f in flags}
        return next((d for d in self.active_disputes()
                     if {f.identity() for f in d.flags} == wanted), None)


class DisputeStore:
    """Reads and appends the two files of `.verifier/diagnoses/`, and reads each diagnosis.

    It knows no rule about states (diagnosis/dispute_states.py).
    """

    def __init__(self, state_dir: Path) -> None:
        """Open the disputes in `state_dir`."""
        self.dir = state_dir / DIAGNOSES_DIR
        self.folder = file_storage.AppendOnlyJsonLinesFolder(
            self.dir, file_storage.FileLock(state_dir / LOCK_FILE))
        self.disputes_file = self.folder.file(DISPUTES)
        self.withdrawals_file = self.folder.file(WITHDRAWALS)

    def read_ledger(self) -> DisputeLedger:
        """Every dispute and withdrawal."""
        return DisputeLedger(
            disputes=tuple(Dispute.from_json(row) for row in self.disputes_file.read_rows()),
            withdrawals=tuple(DisputeWithdrawal.from_json(row)
                              for row in self.withdrawals_file.read_rows()))

    @staticmethod
    def diagnosis_name_in_state_folder(dispute_id: str) -> str:
        """Where the diagnosis of a dispute is, from the state folder: `diagnoses/D-N.md`."""
        return f"{DIAGNOSES_DIR}/{dispute_id}.md"

    def read_diagnosis(self, dispute_id: str) -> DiagnosisDocument | None:
        """The diagnosis of a dispute, or None when it is not written."""
        path = self.dir.parent / self.diagnosis_name_in_state_folder(dispute_id)
        return DiagnosisDocument.from_text(path.read_text()) if path.exists() else None

    @contextlib.contextmanager
    def holding_lock(self) -> Iterator[None]:
        """Hold the store lock for the block. A method that locks must not run inside it."""
        with self.folder.holding_lock():
            yield

    def raise_dispute(self, flags: list[SavedFlag], reason: str, commit: str) -> Dispute:
        """Record a new dispute of `flags` (issues of a saved report) and return it.

        Raises:
            DisputeError: as raise_dispute_while_locked.
        """
        with self.holding_lock():
            return self.raise_dispute_while_locked(flags, reason, commit)

    def raise_dispute_while_locked(self, flags: list[SavedFlag], reason: str,
                                   commit: str) -> Dispute:
        """Record a new dispute of `flags` and return it. The caller holds the store lock.

        Raises:
            DisputeError: no flag, no reason, or a flag that an active dispute already names.
        """
        if not flags:
            raise DisputeError("name at least one flag with --flag")
        if len({f.flag_id for f in flags}) != len(flags):
            raise DisputeError("a flag is named twice; give each --flag once")
        if not reason.strip():
            raise DisputeError("give the reason with --why")
        ledger = self.read_ledger()
        named = {f.identity(): d.dispute_id for d in ledger.active_disputes() for f in d.flags}
        for f in flags:
            if f.identity() in named:
                raise DisputeError(f"{f.flag_id} is already disputed in {named[f.identity()]}")
        dispute = Dispute(dispute_id=f"D-{len(ledger.disputes) + 1}", commit=commit,
                          reason=reason.strip(), raised_at=timestamps.utc_now_iso(),
                          flags=tuple(flags))
        self.disputes_file.append_row(dispute.to_json())
        return dispute

    def withdraw_dispute(self, dispute_id: str) -> None:
        """Withdraw an active dispute. Its flags count again in the next report.

        Raises:
            DisputeError: there is no such dispute, or it is withdrawn already.
        """
        with self.holding_lock():
            ledger = self.read_ledger()
            if ledger.dispute_with_id(dispute_id) is None:
                raise DisputeError(f"there is no dispute {dispute_id}")
            if ledger.withdrawal_for(dispute_id) is not None:
                raise DisputeError(f"{dispute_id} is withdrawn already")
            self.withdrawals_file.append_row(
                DisputeWithdrawal(dispute_id=dispute_id, at=timestamps.utc_now_iso()).to_json())
