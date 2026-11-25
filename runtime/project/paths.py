"""Where everything lives in a working directory's `.verifier/` state directory."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from runtime.project import git
from utils import alerts, structure

INBOX = "inbox"
DELIVERED = "delivered"
# Never linked into a checkout: tool folders, which are large or machine-specific.
UNLINKED = {".git", structure.STATE_DIR_NAME, ".venv", "venv", "node_modules", "__pycache__"}


@dataclasses.dataclass(frozen=True)
class StatePaths:
    """The paths inside one working directory's state directory."""

    root: Path                   # the worker's working directory

    @property
    def state(self) -> Path:
        """The state directory itself."""
        return self.root / structure.STATE_DIR_NAME

    @property
    def branch_file(self) -> Path:
        """Holds the name of the tracked branch."""
        return self.state / "branch"

    @property
    def checkout(self) -> Path:
        """The tracked branch at the commit being checked, which the agent and the checks read."""
        return self.state / "checkout"

    @property
    def checked_file(self) -> Path:
        """Holds the commit the daemon last moved the checkout to.

        The daemon reads the commit from here, not from the checkout's HEAD, because
        the agent works in the checkout and can move its HEAD.
        """
        return self.runtime / structure.CHECKED_COMMIT_FILE

    def checked(self) -> str:
        """The commit the daemon last moved the checkout to, or "" before the first one."""
        return structure.checked_commit(self.state)

    @property
    def inbox(self) -> Path:
        """Events waiting for the next round."""
        return self.state / INBOX

    @property
    def delivered(self) -> Path:
        """Events already sent to the agent, kept for the record."""
        return self.inbox / DELIVERED

    @property
    def jobs(self) -> Path:
        """One record, log and status file per long-running process."""
        return self.state / "jobs"

    @property
    def timers(self) -> Path:
        """One file per pending wake-up."""
        return self.state / "timers"

    @property
    def rounds(self) -> Path:
        """One record per round."""
        return self.state / "rounds"

    @property
    def runtime(self) -> Path:
        """Daemon and OpenCode files that are never committed."""
        return self.state / structure.RUNTIME_DIR

    @property
    def server_file(self) -> Path:
        """The OpenCode server's port, password and session id (mode 600)."""
        return self.runtime / "server.json"

    @property
    def lock(self) -> Path:
        """Held by the daemon for its whole life, so only one runs per working directory."""
        return self.runtime / "daemon.lock"

    @property
    def daemon_log(self) -> Path:
        """The daemon's raw stdout and stderr; its records are in the alert log too.

        It also holds what the daemon prints before its alert log is set up, such as an
        import error.
        """
        return self.runtime / "daemon.log"

    @property
    def alerts_log(self) -> Path:
        """The alert log, for the verifier's developers (utils/alerts.py)."""
        return alerts.project_log(self.state)

    @property
    def outputs(self) -> Path:
        """What the agent's long jobs wrote, one folder per job."""
        return self.state / "outputs"

    def link_outputs(self, tree: Path) -> None:
        """Make the outputs folder and the project's gitignored files reachable in a checkout.

        A job runs in its own checkout and writes under `.verifier/outputs/NAME/`; the claims
        on that output are read in the daemon's checkout. Both then reach `outputs`.

        A checkout holds only committed files, and projects often gitignore their data and
        results. So each gitignored path of the project is linked in at the same relative
        path, leading to the project's current copy. Tool folders and `.env*` files are left
        out, so credentials stay away from the agent. A path the checkout already has is
        kept as it is.

        An ignored folder becomes a real folder holding one link per entry, not one link.
        A pattern such as `runs/` matches only folders, so git would see a link named
        `runs` as an untracked file and refuse to move the checkout over it. An entry
        added to the project's folder later appears the next time this runs.
        """
        link = tree / structure.STATE_DIR_NAME / "outputs"
        if not link.is_symlink():
            self.outputs.mkdir(parents=True, exist_ok=True)
            link.parent.mkdir(exist_ok=True)
            link.symlink_to(self.outputs, target_is_directory=True)
        for rel in self.ignored():
            source = (self.root / rel).absolute()
            if not source.is_dir():
                _link(source, tree / rel)
                continue
            place = tree / rel
            if os.path.lexists(place) and (place.is_symlink() or not place.is_dir()):
                continue
            place.mkdir(parents=True, exist_ok=True)
            for entry in source.iterdir():
                _link(entry, place / entry.name)

    def ignored(self) -> list[str]:
        """The project's gitignored paths to link into a checkout, relative to the project.

        A wholly ignored folder is one path. Tool folders, `.env*` files and anything
        inside either are left out.
        """
        out = git.run(self.root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                      "--directory")
        keep = []
        for rel in out.split("\0"):
            parts = Path(rel).parts
            if rel and not any(p in UNLINKED or p.startswith(".env") for p in parts):
                keep.append(str(Path(*parts)))
        return keep

    def branch(self) -> str:
        """The tracked branch, as `verifier start` recorded it. Raises if it never did."""
        name = self.branch_file.read_text().strip()
        if not name:
            raise ValueError(f"{self.branch_file} is empty")
        return name

    def ensure(self) -> None:
        """Create every directory the runtime writes to."""
        for d in (self.inbox, self.delivered, self.jobs, self.timers, self.rounds, self.runtime,
                  self.outputs):
            d.mkdir(parents=True, exist_ok=True)


def _link(source: Path, place: Path) -> None:
    """Make `place` a link to `source`, unless `place` already exists."""
    if os.path.lexists(place):
        return
    place.parent.mkdir(parents=True, exist_ok=True)
    place.symlink_to(source, target_is_directory=source.is_dir())
