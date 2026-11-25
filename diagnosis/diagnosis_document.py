"""A diagnosis file (`diagnoses/D-N.md`), read once into its sections."""
from __future__ import annotations

import re

from schema.primitives import record

HEADING_FLAGS = "Flags"
HEADING_WHY_FLAGGED = "Why the Verifier flagged them"
HEADING_IS_EACH_FLAG_FALSE = "Is each flag false"
HEADING_CAUSE = "Cause"
HEADING_WHAT_TO_CHANGE = "What to change in the Verifier"
HEADINGS = (HEADING_FLAGS, HEADING_WHY_FLAGGED, HEADING_IS_EACH_FLAG_FALSE, HEADING_CAUSE,
            HEADING_WHAT_TO_CHANGE)
# One answer line: "- `F-3a9c1e`: yes", then the evidence.
ANSWER_LINE = re.compile(r"^- `(F-[0-9a-f]+)`: (\w+)\b", re.MULTILINE)


@record
class DiagnosisDocument:
    """One diagnosis file, read once: its level-2 sections in order, with their bodies."""
    text: str
    sections: tuple[tuple[str, str], ...]          # (heading, body), in order

    @classmethod
    def from_text(cls, text: str) -> DiagnosisDocument:
        """Read the level-2 sections of a diagnosis."""
        parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
        return cls(text=text, sections=tuple((parts[k].strip(), parts[k + 1].strip())
                                             for k in range(1, len(parts), 2)))

    @property
    def headings(self) -> list[str]:
        """The headings, in order."""
        return [h for h, _ in self.sections]

    def section(self, heading: str) -> str:
        """The body of the section with this heading (the last one, when it repeats)."""
        return dict(self.sections)[heading]

    def answers_by_flag_id(self) -> dict[str, list[str]]:
        """Each answer line of "Is each flag false", by flag id, in order."""
        answers: dict[str, list[str]] = {}
        for flag_id, answer in ANSWER_LINE.findall(self.section(HEADING_IS_EACH_FLAG_FALSE)):
            answers.setdefault(flag_id, []).append(answer)
        return answers

    def cause(self) -> str:
        """The cause, without code marks and a full stop."""
        return self.section(HEADING_CAUSE).strip("` .")
