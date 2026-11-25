"""The share file of `verifier diagnosis . --share`.

It holds the written diagnoses in full, for the person to read and then send to the
developers.
"""
from __future__ import annotations

import os
from pathlib import Path

from diagnosis import agent_events
from diagnosis.dispute_states import DisputeStateResolver, DisputeWithState
from diagnosis.records import DisputeDiagnosisWritten
from runtime.agent import rounds
from runtime.project import git, paths
from schema.errors import VerifierError
from utils import timestamps, version


class ShareFileError(VerifierError):
    """The share file cannot be written where the person asked."""


class DiagnosisShareFileWriter:
    """Writes the share file.

    The file gives the Verifier version, then each written diagnosis with all its sections,
    with the reason of the dispute and the model of the round that wrote it.
    """

    def __init__(self, state_dir: Path) -> None:
        """The diagnoses of `state_dir`."""
        self.state_dir = state_dir
        self.states = DisputeStateResolver(state_dir)
        self.history = rounds.RoundHistory(paths.StatePaths(root=state_dir.parent))

    def write_share_file(self, output: str | None) -> None:
        """Write the share file, print its path, and name the disputes that are not in it.

        Args:
            output: the folder or the file to write, as the person typed it; the project
                folder if None. A text that ends with a slash names a folder.

        Raises:
            ShareFileError: the output names a folder that does not exist, or a file in a
                folder that does not exist.
        """
        disputes = self.states.states_of_all_disputes()
        written = [d for d in disputes if isinstance(d.state, DisputeDiagnosisWritten)]
        waiting = [d.dispute.dispute_id for d in disputes if d.state.is_waiting]
        if not written:
            print("No diagnosis is written yet, so there is nothing to share."
                  + (f" Waiting: {', '.join(waiting)}." if waiting else ""))
            return
        name = f"verifier-diagnoses-{timestamps.utc_now_for_file_name()}.md"
        if output is not None and output.endswith(("/", os.sep)) and not Path(output).is_dir():
            raise ShareFileError(f"the folder {output} does not exist. Make it first, or give "
                                 "a folder that exists")
        target = Path(output) if output is not None else self.state_dir.parent
        path = target / name if target.is_dir() else target
        if not path.parent.is_dir():
            raise ShareFileError(f"{path.parent} is not a folder, so {path} cannot be written. "
                                 "Give a folder that exists, or a file in one")
        path.write_text(self.share_file_text(written))
        print(f"Wrote {path} with {', '.join(d.dispute.dispute_id for d in written)}.")
        if waiting:
            print(f"Not in the file, because they wait for a diagnosis: {', '.join(waiting)}.")
        print("Read the file to double-check its anonymity before you send it to "
              "verifier@meno.sh. Do not commit this file.")

    def share_file_text(self, written: list[DisputeWithState]) -> str:
        """The share file text, of the disputes whose diagnosis is written.

        Each section gives the model of the round that wrote it, the reason of the dispute,
        the type and axis of each flag, and then every section of the diagnosis one level
        down. Nothing is left out, so the person must read the file for private facts of the
        project before they send it.
        """
        lines = ["# Verifier diagnoses", "",
                 f"- Verifier version: {self.verifier_code_version()}",
                 f"- Diagnoses: {len(written)}"]
        models = self.history.model_of_last_clean_round_for_each_event_value(
            agent_events.DISPUTE_EVENT_KIND, "dispute_id")
        for d in written:
            assert isinstance(d.state, DisputeDiagnosisWritten), \
                f"{d.dispute.dispute_id} has a written diagnosis"
            document = d.state.diagnosis
            dispute_id = d.dispute.dispute_id
            lines += ["", f"## {dispute_id}", "",
                      f"Model of the auditing agent: {models.get(dispute_id) or 'not recorded'}",
                      "", f"Reason of the dispute: {d.dispute.reason or 'none given'}",
                      "", "| Flag | Flag type | Axis |", "|---|---|---|"]
            lines += [f"| `{f.flag_id}` | `{f.code}` | {f.axis} |" for f in d.dispute.flags]
            for heading, body in document.sections:
                lines += ["", f"### {heading}", "", body]
        return "\n".join(lines) + "\n"

    @staticmethod
    def verifier_code_version(home: Path = version.VERIFIER_HOME) -> str:
        """The short commit of the Verifier code in `home`.

        A note follows the commit when there are changes that are not committed.
        """
        sha = git.output(home, "rev-parse", "HEAD")
        if not sha:
            return "not a git checkout"
        dirty = git.output(home, "status", "--porcelain")
        return sha[:12] + (" with changes that are not committed" if dirty else "")
