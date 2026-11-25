"""What the model review checks share: reading a reply.

checks/model_review and checks/setup_review each ask one model to read the whole paper and
reply with a JSON list of findings. A check may not import another check
(tests/test_layers.py), so the parts they share live here. Each check keeps its own
prompt, finding codes and run options.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping

from schema.primitives import (
    CheckResult,
    Location,
    Problem,
    ProblemKind,
    Skipped,
    passed_or_flagged,
)
from utils import models

# the fields of a finding, each text or missing
TEXT_FIELDS = ("code", "where", "quote", "why")


@dataclasses.dataclass(frozen=True)
class FindingsReader:
    """Reads a review's reply into one result, with the check's finding codes.

    `codes` maps each code the prompt asks for to its kind; `unusable` is the kind of a
    finding the tool cannot use.
    """
    codes: Mapping[str, ProblemKind]
    unusable: ProblemKind

    def from_frozen(self, path: str) -> CheckResult:
        """Load a previously saved reading, so a verdict can be recomputed without a model.

        This is what makes the check reproducible: the same frozen reading always yields
        the same verdict, and a person can edit the reading if the model got it wrong.
        """
        with open(path) as fh:
            return self.read_reply(fh.read())

    def read_reply(self, reply: str) -> CheckResult:
        """A saved reading as one result; Skipped when it is not a reading.

        A saved reading is a file a person can edit, so one that cannot be read is a
        caveat in the report, not an error in the verifier.
        """
        try:
            return self.parse_reply(reply)
        except models.BadReply as e:
            return Skipped(because=f"the model's reply could not be read: {e}")

    def parse_reply(self, reply: str) -> CheckResult:
        """Turn the model's JSON reply into one result, with one Problem per finding.

        The text must be one JSON object with a `findings` list (models.read_json).
        Within the list, a finding the tool cannot use (not an object, a field that is
        not text, or a code the prompt does not ask for) is kept as the unusable kind, so
        it blocks until a person reads it, and the usable findings next to it are kept.

        Raises:
            models.BadReply: the text is not one JSON object with a list of findings.
        """
        data = models.read_json(reply)
        found = data.get("findings") if isinstance(data, dict) else None
        if not isinstance(found, list):
            raise models.BadReply("the reply is not an object with a list of findings")
        return passed_or_flagged(self._problem(f) for f in found)

    def _problem(self, finding: object) -> Problem:
        """One model finding as a Problem, or as an unusable finding for a person to read."""
        if not isinstance(finding, dict):
            return self._unusable(f"a finding is not an object: {finding!r}")
        fields = {key: finding.get(key) for key in TEXT_FIELDS}
        wrong = [key for key, value in fields.items()
                 if value is not None and not isinstance(value, str)]
        if wrong:
            return self._unusable(f"a finding has fields that are not text "
                                  f"({', '.join(wrong)}): {finding!r}")
        code, why = fields["code"] or "", fields["why"] or ""
        where = Location(section=fields["where"], excerpt=fields["quote"] or "")
        if code in self.codes:
            return Problem(kind=self.codes[code], detail=why, where=where)
        return self._unusable(
            f"a finding has a code the prompt does not ask for ({code!r}): {why}", where)

    def _unusable(self, detail: str, where: Location | None = None) -> Problem:
        """A finding the tool cannot classify, kept so the verdict blocks until read."""
        return Problem(kind=self.unusable, detail=detail, where=where)
