"""The inbox: events waiting for the agent's next round, one JSON file each.

The git hook, the job watcher, the timers and the extensions all write here. A file
appears whole or not at all (utils/file_storage.py).
File names start with a nanosecond timestamp, so sorting them gives arrival order.

The hook writes its file in shell, not through this module (see runtime/project/hook.py),
in the same format: {"kind": "branch_moved", "sha": ...}.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import ClassVar

from runtime.project import paths
from utils import file_storage


class AgentInbox:
    """The inbox of one state folder: the events for the next round of the agent.

    Delivered events move to the `delivered` folder inside it, and stay there for the
    record.
    """
    # the last nanosecond timestamp a file name got in this process, so names written
    # at once keep the order they were written in
    _last_ns: ClassVar[int] = 0

    def __init__(self, folder: Path) -> None:
        """The inbox in `folder`."""
        self.folder = folder
        self.delivered_folder = folder / paths.DELIVERED
        self.pending = file_storage.JsonFileFolder(folder)
        self.delivered = file_storage.JsonFileFolder(self.delivered_folder)

    def write_event(self, kind: str, **payload: object) -> Path:
        """Add one event to the inbox and return its file.

        Events written by one process get strictly increasing names, so several written at
        once keep the order they were written in.
        """
        AgentInbox._last_ns = max(time.time_ns(), AgentInbox._last_ns + 1)
        name = f"{AgentInbox._last_ns}-{kind}-{uuid.uuid4().hex[:8]}.json"
        return self.pending.write(name, {"kind": kind, **payload})

    def pending_files(self) -> list[Path]:
        """Every event waiting in the inbox, oldest first.

        An inbox folder that does not exist yet holds no events; write_event makes it.
        """
        return [self.folder / name for name in self.pending.names()]

    @staticmethod
    def read_event(path: Path) -> dict[str, object]:
        """One event. A file that is not valid JSON is returned as an `unreadable` event."""
        try:
            event = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            return {"kind": "unreadable", "file": path.name, "error": str(e)}
        return event if isinstance(event, dict) else {"kind": "unreadable", "file": path.name}

    def mark_delivered(self, files: list[Path]) -> None:
        """Move delivered events out of the inbox, keeping them for the record."""
        for p in files:
            self.pending.move_to(p.name, self.delivered)

    def all_events(self) -> list[dict[str, object]]:
        """Every event, pending and delivered."""
        return [self.read_event(f) for f in [*self.pending_files(),
                                             *self.delivered_folder.glob("*.json")]]
