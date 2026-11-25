"""Review kind: was a pinned data file deliberately regenerated?

Raised by the agent when a file that claims rest on no longer matches its pin and the
worker says it was rerun. Raising it proposes the file's new checksum on every claim
that pins the file (core/gate/artifact_pins.py), so provenance finds those claims provisionally
intact and recomputes them: a claim whose number moved is rejected by recomputation,
and the rest count as a caveat. This kind's check adds a disclosure while the request
waits, and another after a decline. A confirm moves the pins to the new file. If the
file changes again, the request is superseded and the file matches neither checksum,
so its claims are rejected as resting on a changed file.
"""
from __future__ import annotations

import difflib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import ClassVar

from core.gate import artifact_pins
from core.project import watched_project
from core.registry import extensions
from review.kinds.base import ReviewKind
from review.store import ReviewError
from review.types import ReviewDecision, ReviewRequest, ReviewState
from runtime.project import git
from schema import inputs
from schema.inputs import Scalar
from schema.primitives import ClaimId, Problem, ProblemKind, Severity


class FileRegeneratedReviewKind(ReviewKind):
    """The kind "Was this pinned data file deliberately regenerated?"."""
    name = "file-regenerated"
    title = "Confirm a data file was deliberately regenerated"
    options = {"confirm": "positive", "decline": "negative"}
    option_effects = {
        "confirm": "the new file becomes the reference for every claim on it",
        "decline": "claims whose recomputed number moved stay rejected; the rest are disclosed",
    }
    raised_by = "agent"
    agent_guide = """\
- `file-regenerated`: the briefing gives the line "It changed these pinned files: ..."
  for each commit that changed a data file of an artifact source. For each such file,
  read what the worker says about it in the commit message and the notes. If the worker
  says that the file was made again on purpose, run:

      vcheck ask file-regenerated file=PATH --reason "<the words of the worker, and
      each suspicious detail>"

  Raise one request for each file. One request covers each claim of the file. Otherwise,
  do nothing about the file."""
    # The problem kinds this kind's check reports. Both are disclosures: whether the
    # number still holds is decided by recomputation, not by the person's answer.
    UNCONFIRMED: ClassVar[ProblemKind] = ProblemKind(code="regenerated-file-unconfirmed",
                                                     severity=Severity.STRENGTH)
    DECLINED: ClassVar[ProblemKind] = ProblemKind(code="regenerated-file-declined",
                                                  severity=Severity.STRENGTH)
    problem_kinds = (UNCONFIRMED, DECLINED)

    def build_request_facts(self, root: Path, claims: Iterable[inputs.Claim],
                            given: Mapping[str, str]) -> tuple[dict[str, Scalar], str]:
        """The facts for `file`: its path, the checksum claims pin, and its checksum now."""
        file = given.get("file", "")
        if not file:
            raise ReviewError("give the file: file=PATH (relative to the working directory)")
        if not (root / file).is_file():
            raise ReviewError(f"{file} is not a file in the working directory")
        new = artifact_pins.ArtifactPins.current_checksum(root, file)
        pinning = artifact_pins.ArtifactPins.claims_pinning_file(claims, root, file)
        old = {pinned.pin for _claim, pinned in pinning} - {new}
        if not old:
            raise ReviewError(f"no claim pins {file} at a checksum other than its current "
                                   "one, so there is nothing to confirm")
        if len(old) > 1:
            raise ReviewError(f"claims pin {file} at {len(old)} different checksums; "
                                   "re-pinning them all at once is not supported")
        facts: dict[str, Scalar] = {"file": file, "old_sha256": old.pop(), "new_sha256": new}
        return facts, f"Was {file} deliberately regenerated?"

    def facts_still_hold(self, facts: Mapping[str, Scalar], root: Path) -> bool:
        """The file still has the checksum the request was raised about."""
        file = str(facts["file"])
        return ((root / file).is_file()
                and artifact_pins.ArtifactPins.current_checksum(root, file) == facts["new_sha256"])

    def ids_of_claims_it_bears_on(self, facts: Mapping[str, Scalar],
                                  claims: Iterable[inputs.Claim],
                                  root: Path) -> tuple[ClaimId, ...]:
        """The claims that pin the file at its old checksum or propose its new one."""
        wanted = {facts["old_sha256"], facts["new_sha256"]}
        pinning = artifact_pins.ArtifactPins.claims_pinning_file(claims, root, str(facts["file"]))
        return tuple(c.id for c, pinned in pinning
                     if isinstance(c.source, inputs.ArtifactSource)
                     and wanted & {pinned.pin, (c.source.proposed_sha256 or {}).get(pinned.name)})

    def problems_for_claim(self, request: ReviewRequest,
                           state: ReviewState) -> tuple[Problem, ...]:
        """Waiting: disclosed. Declined: disclosed with the note. Confirmed: nothing."""
        file = request.facts["file"]
        if state.is_waiting:
            return (Problem(
                kind=self.UNCONFIRMED,
                detail=f"rests on {file}, which changed; waiting for a person to confirm it was "
                       f"regenerated ({request.id})"),)
        decision = state.decision
        if decision is not None and not self.option_is_positive(decision.option):
            note = f": {decision.note}" if decision.note else ""
            return (Problem(
                kind=self.DECLINED,
                detail=f"rests on {file}, which a person did not accept as regenerated "
                       f"({request.id}{note})"),)
        return ()

    def after_request_raised(self, request: ReviewRequest, root: Path, state_dir: Path) -> None:
        """Propose the file's new checksum on every claim that pins it."""
        self.change_pins(root, state_dir, lambda editor: editor.propose_checksum(
            str(request.facts["file"]), str(request.facts["new_sha256"])))

    def after_decision_recorded(self, request: ReviewRequest, decision: ReviewDecision,
                                root: Path, state_dir: Path) -> None:
        """A confirm moves the pins to the new checksum.

        A decline keeps the proposal, so the claims are still recomputed against the new file.
        """
        if self.option_is_positive(decision.option):
            self.change_pins(root, state_dir, lambda editor: editor.confirm_proposed_checksum(
                str(request.facts["file"]), str(request.facts["new_sha256"])))

    def after_request_withdrawn(self, request: ReviewRequest, root: Path,
                                state_dir: Path) -> None:
        """Withdrawing removes the proposal; the claims fall back to their pins."""
        self.change_pins(root, state_dir, lambda editor: editor.withdraw_proposed_checksum(
            str(request.facts["file"]), str(request.facts["new_sha256"])))

    def evidence_for_person(self, request: ReviewRequest, root: Path) -> str:
        """What changed in the file, from git, and the commit that changed it."""
        file = str(request.facts["file"])
        commit = git.output(root, "log", "-1", "--format=%H", "--", file)
        lines = []
        if commit:
            summary = git.output(root, "log", "-1", "--format=%h \"%s\" by %an", commit)
            lines.append("Commit:       " + summary)
            old_text = git.output(root, "show", f"{commit}^:{file}")
            new_text = (root / file).read_text(errors="replace")
            lines.append("What changed: " + self.describe_change(old_text, new_text))
        else:
            lines.append("What changed: the file is not committed, so git cannot show the change")
        return "\n".join(lines)

    @classmethod
    def change_pins(cls, root: Path, state_dir: Path,
                    change: Callable[[artifact_pins.ClaimRegisterPinEditor], list[str]]) -> None:
        """Make one change of the pins in the claim register of `state_dir`.

        Each review command first makes again the changes of the requests on record
        (ReviewRecorder.repair_failed_kind_changes_while_locked). So while a file of the
        project is broken, the command stops there, before it records anything.

        Raises:
            ReviewError: the extensions file, a method file, a plug-in check, the claim
                register or a run option of the project is broken.
        """
        try:
            change(cls.pin_editor(root, state_dir))
        except (*watched_project.PROJECT_INPUT_ERRORS, extensions.ExtensionsFileError) as e:
            raise ReviewError(
                f"the checksums in {watched_project.WatchedProject.REGISTER_FILE} cannot be "
                f"changed, because {e}. Fix this, then give the command again") from e

    @staticmethod
    def pin_editor(root: Path, state_dir: Path) -> artifact_pins.ClaimRegisterPinEditor:
        """The editor of the pins in the claim register of `state_dir`.

        Raises:
            The errors of watched_project.PROJECT_INPUT_ERRORS, and ExtensionsFileError:
                a file of the project is broken.
        """
        project = watched_project.WatchedProject.load(root, state_dir)
        return artifact_pins.ClaimRegisterPinEditor(
            state_dir / project.REGISTER_FILE, root,
            project.claim_register_parser(project.check_registry()))

    @staticmethod
    def describe_change(old_text: str, new_text: str) -> str:
        """How many lines differ. The file's format is the workspace's business, not ours."""
        old, new = old_text.splitlines(), new_text.splitlines()
        blocks = difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes()
        changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in blocks if tag != "equal")
        return f"{changed} line(s) differ; {len(old)} -> {len(new)} lines."
