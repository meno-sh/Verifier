"""The report of the `verifier static-check` command in the terminal."""
from __future__ import annotations

from typing import ClassVar

from core.report_model import measured_numbers, verification_report
from core.report_model.axis import Axis
from schema import findings
from utils import text


class TerminalReportPrinter:
    """Prints a Findings to the terminal for the `verifier static-check` command.

    The gate and the claim checks are printed as two separate tables on purpose: the gate
    gives exactly one status per claim while claim checks give zero or many problems per
    claim, and a single table cannot carry both.
    """
    COLOURS: ClassVar[dict[str, str]] = {
        "PASS": "\033[32m", "INTACT": "\033[32m", "TRACED": "\033[36m",
        "DERIVED": "\033[36m", "SKIP": "\033[33m",
        "FAIL": "\033[31m", "BLOCK": "\033[41m\033[97m",
        "BOLD": "\033[1m", "OFF": "\033[0m"}

    def __init__(self, colour: bool = True) -> None:
        """Print with ANSI colours, or with none."""
        self.colour = colour

    def _c(self, key: str) -> str:
        """The ANSI code of `key`, or nothing when the colours are off."""
        return self.COLOURS.get(key, "") if self.colour else ""

    def print_report(self, summary: verification_report.VerdictSummary) -> None:
        """Print the findings, the triage board and the verdict."""
        C = self._c
        tree = summary.findings
        if not tree.papers:
            print(f"\n{C('BOLD')}PAPER CHECKS{C('OFF')}   off: no document was read")
        for path, paper in tree.papers.items():
            flagged = paper.problems()
            print(f"\n{C('BOLD')}PAPER CHECKS --- {path} against itself{C('OFF')}   "
                  + "".join(f"{n} {name.replace('_', ' ')}, " for name, n in paper.facts.items())
                  .removesuffix(", ") + f" -> {len(flagged)} problem(s)")
            for p in flagged:
                print(f"  [{p.kind.code}] {text.first_part(p.text_for_reader(), 110)}")
        if tree.claims or tree.unreadable_claims:
            self._print_gate_table(tree)
            self._print_claim_check_table(tree)
        if tree.superseded:
            print("\n  Superseded by a later claim, not checked: "
                  + ", ".join(c.superseded_label() for c in tree.superseded))
        board = summary.triage_board
        print(f"\n{C('BOLD')}TRIAGE BOARD{C('OFF')}   every finding, by what it means for the "
              "paper")
        for axis in Axis.in_board_order():
            items = board.entries_on_axis(axis)
            print(f"\n  {axis.board_heading}: {len(items)}")
            for e in items[:6]:
                if e.lines:
                    print(f"    - [{e.code}] {text.first_part(e.subject, 104)}")
                    for line in e.lines:
                        print(f"        {text.first_part(line, 104)}")
                else:
                    print(f"    - [{e.code}] {text.first_part(e.detail, 104)}")
            if len(items) > 6:
                print(f"    ... and {len(items) - 6} more")
        style = summary.verdict.style
        print(f"\n{C('BOLD')}VERDICT: {style.terminal_colour if self.colour else ''}"
              f"{summary.verdict.value}{C('OFF')}\n  {style.action}\n"
              f"  {summary.reason_for_verdict}\n")

    def _print_gate_table(self, tree: findings.Findings) -> None:
        """The provenance and recompute table, one row per claim, then the unreadable entries."""
        C = self._c
        ids = [c.claim.id for c in tree.claims] or ["claim"]
        width = max(len(i) for i in ids) + 2
        print(f"\n{C('BOLD')}PROVENANCE AND RECOMPUTATION --- each claim against its artifact"
              f"{C('OFF')}")
        print(f"  {'claim':<{width}} {'status':<7} {'recomputed':>10} {'n':>6}  note")
        print("  " + "-" * (width + 71))
        for c in tree.claims:
            st = c.gate_status_word()
            matched = c.recompute.recomputed_number()
            val = f"{matched.value:+.3f}" if matched else "--"
            n = str(matched.n) if matched else "--"
            print(f"  {c.claim.id:<{width}} {C(st)}{st:<7}{C('OFF')} {val:>10} {n:>6}  "
                  f"{'' if st == 'PASS' else c.gate_note(tree.inputs.claim_register_base)}")
        for entry in tree.unreadable_claims:
            label = entry.label()
            print(f"  {label:<{width}} {C('FAIL')}{'UNREAD':<7}{C('OFF')}  {entry.reason}")
        print("\n  PASS = recomputed and matches   TRACED = not recomputed on purpose")
        print("  DERIVED = inferred from other claims, so there is no file or number")
        print("  SKIP = no source, or traced only to the note the claim was written in")
        print("  INTACT = artifact present and unchanged, recomputation switched off")
        print("  BLOCK = file, anchor or setup gone or changed   FAIL = number disagrees")
        print("  UNREAD = the claim register entry could not be read")

    def _print_claim_check_table(self, tree: findings.Findings) -> None:
        """Every problem a claim check found, one line each, then the numbers they measured."""
        C = self._c
        print(f"\n{C('BOLD')}CLAIM CHECKS --- the problems they found{C('OFF')}")
        hits = [(c.claim.id, p) for c in tree.claims for p in c.problems()]
        if not hits:
            print("  none")
        width = max((len(c.claim.id) for c in tree.claims), default=0) + 2
        for cid, p in hits:
            print(f"  {cid:<{width}} {p.text_for_reader()}")
        measures = measured_numbers.MeasuredNumberCollector().collect_measured_numbers(tree)
        if measures:
            print(f"\n{C('BOLD')}CLAIM CHECKS --- the numbers they measured{C('OFF')}")
        for m in measures:
            print(f"  {m.claim_id:<{width}} {m.check_name}: {m.value:.3g}")
        print("\n  A claim check flagging a claim is not proof the claim is wrong.")
        print("  It means a person has to look, and says what to look at.")
