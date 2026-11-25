"""The verdict: one outcome per run. core/analysis/verdict_policy.py computes it."""
from __future__ import annotations

from enum import Enum

from schema.primitives import record


@record
class VerdictStyle:
    """How a verdict shows: its colours, and its next-step lines."""
    terminal_colour: str            # the ANSI code of the verdict in the terminal
    report_colour: str              # the colour of the verdict in the PDF report
    action: str                     # the next step, for the triage board's lists
    report_action: str              # the next step, for the sections of the report


class Verdict(Enum):
    """The one outcome of a run, worst first."""
    MISSING_INPUTS = "MISSING INPUTS"
    REJECT = "REJECT"
    BLOCK_UNTIL_FIXED = "BLOCK UNTIL FIXED"
    PASS_WITH_DISCLOSURE = "PASS WITH DISCLOSURE"
    PASS_WITH_CAVEATS = "PASS WITH CAVEATS"
    PASS = "PASS"

    @property
    def exit_code(self) -> int:
        """The exit code of static-check and `vcheck run`: 2 REJECT, 1 a block or MISSING INPUTS."""
        return EXIT_CODES.get(self, 0)

    @property
    def style(self) -> VerdictStyle:
        """How this verdict shows in the terminal and in the PDF report."""
        return VERDICT_STYLES[self]


# One style per verdict, so the terminal and the PDF report tell all six apart.
VERDICT_STYLES = {
    Verdict.MISSING_INPUTS: VerdictStyle(
        terminal_colour="\033[45m\033[97m", report_colour="#6941c6",
        action="Supply what the run was missing, then run it again.",
        report_action="Supply what the run was missing, then run it again."),
    Verdict.REJECT: VerdictStyle(
        terminal_colour="\033[41m\033[97m", report_colour="#b42318",
        action="Fix or withdraw each number on the integrity list before relying on it.",
        report_action="Fix or withdraw each number that cannot be verified."),
    Verdict.BLOCK_UNTIL_FIXED: VerdictStyle(
        terminal_colour="\033[43m\033[30m", report_colour="#b54708",
        action="Fix each problem on the soundness and presentation lists, then run it again.",
        report_action="Fix each issue that blocks the verdict, then run it again."),
    Verdict.PASS_WITH_DISCLOSURE: VerdictStyle(
        terminal_colour="\033[36m", report_colour="#0e7090",
        action="Add each weakness on the strength list to the text.",
        report_action="Add each weakness that must be disclosed to the text."),
    Verdict.PASS_WITH_CAVEATS: VerdictStyle(
        terminal_colour="\033[33m", report_colour="#a15c07",
        action="Read the not-checked list to see what the run cannot confirm.",
        report_action="Read the caveats to see what the run cannot confirm."),
    Verdict.PASS: VerdictStyle(terminal_colour="\033[32m", report_colour="#067647",
                               action="Nothing to do.", report_action="Nothing to do."),
}
if set(VERDICT_STYLES) != set(Verdict):
    raise RuntimeError("VERDICT_STYLES must give every verdict a style")


# The exit code of each verdict that is not 0.
EXIT_CODES = {Verdict.REJECT: 2, Verdict.BLOCK_UNTIL_FIXED: 1, Verdict.MISSING_INPUTS: 1}
