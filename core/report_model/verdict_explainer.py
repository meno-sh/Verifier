"""The sentence that explains a verdict to the reader."""
from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from core.report_model import report_header
from core.report_model.axis import Axis
from core.report_model.issue_collector import IssueCollector
from core.report_model.triage_board import TriageBoard
from schema import findings
from schema.primitives import read_only
from schema.verdict import Verdict


class VerdictExplainer:
    """Gives the sentence that explains a verdict, from the triage board."""
    # Why the run cannot pass, for each code the board puts on the missing-inputs axis.
    MISSING_INPUT_REASONS: ClassVar[Mapping[str, str]] = read_only({
        "unresolved-input": "files the paper depends on were not submitted",
        "claim-unreadable": "entries of the claim register could not be read",
        IssueCollector.CLAIM_CHECKS_NOT_RUN: report_header.CLAIM_CHECKS_NOT_RUN,
    })
    # What each blocking axis means, for the BLOCK UNTIL FIXED sentence.
    BLOCKING_REASONS: ClassVar[Mapping[Axis, str]] = read_only({
        Axis.SOUNDNESS: "a number may not measure what its claim says, for example because the run "
                     "behind it was not the one described",
        Axis.PRESENTATION: "the paper misdescribes its own contents",
    })

    @classmethod
    def explain_verdict(cls, outcome: Verdict, board: TriageBoard,
                        tree: findings.Findings) -> str:
        """The sentence that explains the verdict to the reader, from the board of `tree`."""
        match outcome:
            case Verdict.MISSING_INPUTS:
                # several documents can put the same code on the axis; give its reason once
                codes = dict.fromkeys(e.code for e in board.entries_on_axis(Axis.MISSING_INPUTS))
                suffix = IssueCollector.NOT_RUN_SUFFIX
                reasons = [cls.MISSING_INPUT_REASONS.get(code) or
                           f"{code.removesuffix(suffix).replace('-', ' ')} was asked for "
                           f"but gave no result"
                           for code in codes]
                return (f"{' and '.join(reasons)}, so the run cannot pass. "
                        f"This is not a judgment on the paper.")
            case Verdict.REJECT:
                rest = len(board.entries_on_axis(Axis.INTEGRITY)) - 1
                more = f", and {rest} more" if rest else ""
                return (f"rejected because a claim cannot be verified at all: "
                        f"{cls.first_reason_to_reject(tree)}{more}. No other result offsets this.")
            case Verdict.BLOCK_UNTIL_FIXED:
                return "; and ".join(reason for axis, reason in cls.BLOCKING_REASONS.items()
                                     if board.entries_on_axis(axis))
            case Verdict.PASS_WITH_DISCLOSURE:
                return ("no integrity, soundness or presentation problem; the flagged weaknesses "
                        "must appear in the paper, not be removed from it")
            case Verdict.PASS_WITH_CAVEATS:
                return ("nothing failed; some checks could not run, so the run cannot confirm "
                        "the parts they cover")
        return "no flags"

    @staticmethod
    def first_reason_to_reject(tree: findings.Findings) -> str:
        """What the first claim that cannot be verified says, in one phrase.

        That is the first step that failed on the first claim whose gate failed.
        """
        c = next(c for c in tree.claims if c.gate_failed)
        broken = c.provenance if c.provenance.is_broken else c.recompute
        said = broken.describe_with_paths_relative_to(tree.inputs.claim_register_base)
        return f"{c.claim.id}: {said}"
