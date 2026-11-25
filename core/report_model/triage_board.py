"""The triage board: every finding of a run, sorted onto the axes of core/report_model/axis.py."""
from __future__ import annotations

from collections.abc import Mapping

from core.report_model.axis import Axis
from core.report_model.checks_not_run import ChecksNotRunCollector
from core.report_model.issue_collector import IssueCollector
from schema import findings
from schema.primitives import read_only, record


@record
class TriageBoardEntry:
    """One finding on the board: its code, and what it is about and what it is.

    `lines` is the detail as one line for each step, for a reader who sees the board
    as a list: what provenance and then recompute found for a rejected claim. It is
    empty when the detail is one line.
    """
    code: str
    subject: str
    detail: str                                 # the subject, then what the finding is
    lines: tuple[str, ...] = ()


@record
class TriageBoard:
    """The findings of one run on the six axes, each axis in board order."""
    entries_by_axis: Mapping[Axis, tuple[TriageBoardEntry, ...]]

    def entries_on_axis(self, axis: Axis) -> tuple[TriageBoardEntry, ...]:
        """The entries on one axis, in board order."""
        return self.entries_by_axis[axis]

    def to_json(self) -> dict[str, list[dict[str, str]]]:
        """The board as a dict of axes, each a list of {code, detail}, for static-check --json."""
        return {axis.value: [{"code": e.code, "detail": e.detail} for e in entries]
                for axis, entries in self.entries_by_axis.items()}


class TriageBoardBuilder:
    """Puts the issues of a Findings, and the claim checks that did not run, on the axes.

    The board and the issue list of the report come from one IssueCollector, so they
    cannot disagree on what a run found.
    """

    def build_board(self, tree: findings.Findings) -> TriageBoard:
        """Each issue on its axis, then one coverage line for each claim with checks not run.

        On a coverage line, the checks that did not run for the same reason are named
        together.
        """
        out: dict[Axis, list[TriageBoardEntry]] = {axis: [] for axis in Axis.in_board_order()}
        collector = IssueCollector()
        for located in collector.collect_issues(tree):
            issue, place = located.issue, located.place
            gate_failed = place is not None and place.claim is not None and place.check is None
            lines = (tuple(f"{step}: {text}" for step, text in collector.gate_steps(
                         tree.claims[place.claim], tree.inputs.claim_register_base))
                     if gate_failed and place is not None and place.claim is not None else ())
            out[issue.axis].append(TriageBoardEntry(
                code=issue.code, subject=issue.subject,
                detail=f"{issue.subject}: {issue.detail}", lines=lines))
        reasons_by_claim: dict[str, dict[str, list[str]]] = {}
        for n in ChecksNotRunCollector.checks_not_run_per_claim(tree):
            reasons_by_claim.setdefault(n.claim_id, {}).setdefault(n.reason, []).append(
                n.check_name)
        for cid, reasons in reasons_by_claim.items():
            parts = "; ".join(f"{', '.join(names)} ({reason})"
                              for reason, names in reasons.items())
            out[Axis.CAVEAT].append(TriageBoardEntry(
                code="not-checked", subject=cid, detail=f"{cid}: not checked -- {parts}"))
        return TriageBoard(entries_by_axis=read_only(
            {axis: tuple(entries) for axis, entries in out.items()}))
