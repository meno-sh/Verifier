"""The shape of a diagnosis (`diagnoses/D-N.md`), which `vcheck check-diagnosis` checks.

A diagnosis has five sections, each once, in this order. "Is each flag false" has one
line for each disputed flag. "Cause" names one of the PR change labels of the
repository, without the prefix `changes-`. "What to change in the Verifier" must not be
empty.
"""
from __future__ import annotations

from core.report_output.saved_report import SavedFlag
from diagnosis import diagnosis_document
from diagnosis.diagnosis_document import DiagnosisDocument

CAUSES = ("auditor-agent", "the-checks", "deterministic-core", "eval")
ANSWERS = ("yes", "no", "partly")
FLAGS = diagnosis_document.HEADING_FLAGS
IS_EACH_FLAG_FALSE = diagnosis_document.HEADING_IS_EACH_FLAG_FALSE
CAUSE = diagnosis_document.HEADING_CAUSE
WHAT_TO_CHANGE = diagnosis_document.HEADING_WHAT_TO_CHANGE


class DiagnosisFileChecker:
    """Finds what is wrong with a diagnosis of the flags of one dispute."""

    def problems_of_diagnosis(self, document: DiagnosisDocument,
                              flags: tuple[SavedFlag, ...]) -> list[str]:
        """Each problem of the diagnosis, as a line for the auditing agent; none when complete."""
        if document.headings != list(diagnosis_document.HEADINGS):
            return ["The headings must be these, each once, in this order: "
                    + ", ".join(f"## {h}" for h in diagnosis_document.HEADINGS)
                    + f". Found: {document.headings or 'none'}."]
        ids = [f.flag_id for f in flags]
        out = [f"## {FLAGS} does not name the flag {i}." for i in ids
               if i not in document.section(FLAGS)]
        out += self.problems_of_answers(document.answers_by_flag_id(), ids)
        if document.cause() not in CAUSES:
            out.append(f"## {CAUSE} must be one of: {', '.join(CAUSES)}. "
                       f"Found: {document.section(CAUSE)!r}.")
        if not document.section(WHAT_TO_CHANGE):
            out.append(f"## {WHAT_TO_CHANGE} is empty.")
        return out

    @staticmethod
    def problems_of_answers(answers: dict[str, list[str]], ids: list[str]) -> list[str]:
        """The problems of "Is each flag false": one answer of yes, no or partly for each flag."""
        out = []
        for i in ids:
            given = answers.get(i, [])
            if len(given) != 1:
                out.append(f"## {IS_EACH_FLAG_FALSE} must have one line for {i}: "
                           f"- `{i}`: yes, no or partly.")
            elif given[0] not in ANSWERS:
                out.append(f"## {IS_EACH_FLAG_FALSE}: the answer for {i} is {given[0]!r}; "
                           "use yes, no or partly.")
        out += [f"## {IS_EACH_FLAG_FALSE} names {i}, which this dispute does not hold."
                for i in answers if i not in ids]
        return out
