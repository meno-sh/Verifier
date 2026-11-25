"""The report files of a watched project, in `.verifier/reports/`."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

from core.report_output.saved_report import SavedReport
from utils import file_storage, timestamps


class ReportStore:
    """Writes and reads the report files of one state folder.

    Each report is saved as Markdown, Typst markup, PDF and JSON, under its commit and as
    "latest". A reader never sees half a file: each file is written to a temporary name,
    then renamed.
    """
    REPORTS_DIR: ClassVar[str] = "reports"
    # The file of the layout data of a report, after the commit or "latest".
    LAYOUT_SUFFIX: ClassVar[str] = ".layout.json"

    def __init__(self, state_dir: Path) -> None:
        """Keep the reports of `state_dir`."""
        self.state_dir = state_dir
        self.folder = state_dir / self.REPORTS_DIR

    @property
    def pdf_title(self) -> str:
        """The title of the PDF: the name of the project, the folder that holds the state."""
        return self.state_dir.parent.name

    def save_report_files(self, commit: str | None, markdown: str, typst: str, pdf: bytes,
                          saved: SavedReport, layout_data: Mapping[str, object]) -> None:
        """Write the report as Markdown, Typst, PDF and JSON, for the commit and as the latest.

        `layout_data` is the data block of the Typst markup. It is also saved as JSON in
        `<name>.layout.json`, for the local page of `verifier web`.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        stem = commit or timestamps.utc_now_for_file_name()
        data = json.dumps(saved.to_json(), indent=2, sort_keys=True)
        layout = json.dumps(layout_data)
        for name in (stem, "latest"):
            for path, content in ((self.folder / f"{name}.md", markdown),
                                  (self.folder / f"{name}.typ", typst),
                                  (self.folder / f"{name}.pdf", pdf),
                                  (self.folder / f"{name}.json", data + "\n"),
                                  (self.folder / f"{name}{self.LAYOUT_SUFFIX}", layout + "\n")):
                file_storage.AtomicallyWrittenFile(path).write_bytes(
                    content if isinstance(content, bytes) else content.encode())

    def load_saved_report(self, name: str) -> SavedReport | None:
        """The saved report of a commit, or of "latest"; None if there is no such report.

        Raises:
            SavedReportError: the file is not a saved report with the keys `vcheck run` writes.
        """
        path = self.folder / f"{name}.json"
        if not path.exists():
            return None
        return SavedReport.from_json(json.loads(path.read_text()))

    def load_layout_data(self, name: str) -> dict[str, object] | None:
        """The layout data of a report, or None for a report saved before the layout file."""
        path = self.folder / f"{name}{self.LAYOUT_SUFFIX}"
        if not path.exists():
            return None
        found = json.loads(path.read_text())
        assert isinstance(found, dict), f"{path} holds {type(found).__name__}, not an object"
        return found

    def load_report_text(self, name: str, suffix: str) -> str | None:
        """The Markdown (".md") or the Typst markup (".typ") of a report, or None."""
        path = self.folder / f"{name}{suffix}"
        return path.read_text() if path.exists() else None
