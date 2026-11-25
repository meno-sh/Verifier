"""The runs of `verifier web`: one folder for each upload, checked by the `verifier` commands.

Each run folder holds `run.json`, which the page reads, and `project/`, a new git repository
with the uploaded files in one commit. The page changes a project only through the
`verifier` commands that a person can type, so the page and the terminal give the same result.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from core.report_output import report_store
from runtime.project.paths import StatePaths
from schema.errors import VerifierError
from utils import alerts, extract, file_storage, timestamps

# The branch of the new repository, which the verifier checks.
BRANCH = "main"
# Who the commit of the uploaded files is by.
UPLOAD_IDENTITY = ("-c", "user.name=verifier web", "-c", "user.email=verifier@localhost")
# Folders of an uploaded zip that are not copied: an old repository, an old state folder,
# and the resource folders that macOS adds to a zip.
SKIPPED_ZIP_FOLDERS = frozenset({".git", ".verifier", "__MACOSX"})
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
# The setup states in run.json.
PREPARING, READY, SETUP_FAILED = "preparing", "ready", "setup failed"


# The largest upload that the page accepts, and the largest total size of the files in a zip.
# The page runs on the user's own computer; the limits stop a wrong file from filling its disk.
MAX_UPLOAD_BYTES = 1024 ** 3
MAX_UNZIPPED_BYTES = 4 * 1024 ** 3


class UploadError(VerifierError, ValueError):
    """The uploaded files cannot become a project to check."""


class RunIsBeingPrepared(VerifierError):
    """A run cannot be deleted while prepare_run works on it."""


class CommandFailed(VerifierError):
    """A `verifier` command gave an exit code that is not 0. The message is its error output."""


@dataclass(frozen=True)
class UploadedFile:
    """One file of an upload: the name on the sender's machine, and its bytes."""
    name: str
    data: bytes


class VerifierCommandLine:
    """Runs `verifier` commands in a child process, as a person runs them in a terminal."""

    def __init__(self, model: str) -> None:
        """Run the auditing agent with `model`, in each run that the page starts."""
        self.model = model

    def run(self, *args: str) -> str:
        """Run `verifier` with `args` and return its output.

        Raises:
            CommandFailed: the command gave an exit code that is not 0.
        """
        out = subprocess.run([sys.executable, "-m", "runtime.cli", *args],
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise CommandFailed(out.stderr.strip() or out.stdout.strip()
                                 or f"`verifier {args[0]}` gave exit code {out.returncode}")
        return out.stdout

    def run_and_read_json(self, *args: str) -> dict[str, object]:
        """Run a `verifier` command with `--json`, and return the object that it prints."""
        found = json.loads(self.run(*args, "--json"))
        assert isinstance(found, dict), f"`verifier {args[0]} --json` printed {found!r}"
        return found

    def init(self, project: Path) -> None:
        """Prepare the project for the verifier, with every note as a source of claims."""
        self.run("init", str(project), "--branch", BRANCH, "--claims-from", ".",
                 "--model", self.model)

    def start_if_pending(self, projects: Sequence[Path]) -> None:
        """Start the daemon of each project where work waits. The daemon stops when idle."""
        if projects:
            self.run("start-if-pending", *map(str, projects), "--model", self.model)


class WebRun:
    """One upload: its folder, the project in it, and the record that the page reads."""

    def __init__(self, folder: Path) -> None:
        """The run in `folder`."""
        self.folder = folder
        self.run_id = folder.name
        self.project = folder / "project"
        self.record_file = folder / "run.json"
        self.share_file = folder / "verifier-diagnoses.md"

    def record(self) -> dict[str, object]:
        """The name, the files, the time of the upload, and the setup state of the run."""
        found = json.loads(self.record_file.read_text())
        assert isinstance(found, dict), f"{self.record_file} holds {found!r}"
        return found

    def write_record(self, **changes: object) -> None:
        """Change some fields of the record, and keep the other fields."""
        record = self.record() if self.record_file.exists() else {}
        record.update(changes)
        file_storage.AtomicallyWrittenFile(self.record_file).write_text(json.dumps(record))

    def report_pdf(self) -> Path:
        """The PDF of the latest report. The file is not there before the first report."""
        state = StatePaths(root=self.project).state
        return report_store.ReportStore(state).folder / "latest.pdf"


class RunLibrary:
    """The runs in one folder, newest first."""

    def __init__(self, folder: Path, verifier_commands: VerifierCommandLine) -> None:
        """Keep the runs in `folder`, and check each with `verifier_commands`."""
        self.folder = folder
        self.verifier_commands = verifier_commands
        self.last_start_if_pending_error: str | None = None
        # the ids of the runs whose preparation started and has not ended, in this process
        self.runs_being_prepared: set[str] = set()
        # held while a run is deleted, while the pending work is started, and while the set
        # above changes, so that none of them acts on a run that another one changes
        self.runs_lock = threading.RLock()

    def runs(self) -> list[WebRun]:
        """Every run, newest first."""
        if not self.folder.is_dir():
            return []
        found = [WebRun(p) for p in self.folder.iterdir()
                 if RUN_ID.fullmatch(p.name) and (p / "run.json").exists()]
        return sorted(found, key=lambda r: r.run_id, reverse=True)

    def run_with_id(self, run_id: str) -> WebRun | None:
        """The run with this id, or None when there is no such run."""
        if not RUN_ID.fullmatch(run_id) or not (self.folder / run_id / "run.json").exists():
            return None
        return WebRun(self.folder / run_id)

    def create_run(self, uploads: Sequence[UploadedFile]) -> WebRun:
        """Make a run folder and put the uploaded files in its project.

        The setup state is PREPARING. prepare_run does the slow part.

        Raises:
            UploadError: the files are not one zip, or one or more paper files, or they hold
                no paper file.
        """
        stamp = timestamps.utc_now_for_file_name()[:15].replace("T", "-")
        run = WebRun(self.folder / f"{stamp}-{secrets.token_hex(3)}")
        run.project.mkdir(parents=True)
        try:
            write_uploaded_files_into_project(run.project, uploads)
        except BaseException:
            # any failure, also one that no code expects, leaves no broken run folder
            shutil.rmtree(run.folder)
            raise
        names = [os.path.basename(u.name.replace("\\", "/")) for u in uploads]
        run.write_record(name=names[0] if len(names) == 1 else f"{names[0]} and "
                         f"{len(names) - 1} more", files=names,
                         uploaded_at=timestamps.utc_now_iso(), setup=PREPARING,
                         setup_error=None)
        return run

    def start_preparation(self, run: WebRun) -> None:
        """Mark the run as being prepared, then prepare it in a thread of its own.

        The mark is set before this returns, so delete_run refuses the run from the moment
        that the page gets its id.
        """
        with self.runs_lock:
            self.runs_being_prepared.add(run.run_id)
        threading.Thread(target=self.prepare_run, args=(run,), daemon=True).start()

    def prepare_run(self, run: WebRun) -> None:
        """Commit the files, run `verifier init`, and start the first round.

        A failure is written to the record, for the page to show. start_preparation marks
        the run as being prepared; this removes the mark when it ends.
        """
        try:
            self._prepare_run(run)
        finally:
            with self.runs_lock:
                self.runs_being_prepared.discard(run.run_id)

    def _prepare_run(self, run: WebRun) -> None:
        """The work of prepare_run."""
        try:
            git = ["git", "-C", str(run.project)]
            subprocess.run([*git, "init", "-q", "-b", BRANCH], check=True, capture_output=True)
            subprocess.run([*git, "add", "-A"], check=True, capture_output=True)
            subprocess.run([*git, *UPLOAD_IDENTITY, "commit", "-q", "-m", "The uploaded files"],
                           check=True, capture_output=True)
            self.verifier_commands.init(run.project)
        except subprocess.CalledProcessError as e:
            run.write_record(setup=SETUP_FAILED,
                             setup_error=f"git failed: {e.stderr.decode(errors='replace')}")
            return
        except CommandFailed as e:
            run.write_record(setup=SETUP_FAILED, setup_error=str(e))
            return
        run.write_record(setup=READY)
        self.start_pending_work([run])

    def start_pending_work(self, runs: Sequence[WebRun] | None = None) -> None:
        """Start the daemon of each ready run where work waits, such as a new dispute.

        A run whose daemon stopped on an error stays stopped; its status gives the error.
        """
        with self.runs_lock:
            self._start_pending_work(runs)

    def _start_pending_work(self, runs: Sequence[WebRun] | None) -> None:
        """The work of start_pending_work. The caller holds runs_lock."""
        # a run whose record is gone was deleted since `runs()` listed it
        ready = [r for r in (self.runs() if runs is None else runs)
                 if r.record_file.exists() and r.record()["setup"] == READY]
        try:
            self.verifier_commands.start_if_pending([r.project for r in ready])
        except CommandFailed as e:
            # the page shows the error of each run from `verifier status`; the log gets
            # each new error once, and not every time the same error comes back
            if str(e) != self.last_start_if_pending_error:
                alerts.notify("warn", "web.start_if_pending", str(e))
            self.last_start_if_pending_error = str(e)

    def delete_run(self, run: WebRun) -> None:
        """Stop the daemon of the run, and delete its folder.

        Raises:
            RunIsBeingPrepared: prepare_run works on the run now. A delete then would let
                the preparation write the run again and start its daemon.
        """
        with self.runs_lock:
            if run.run_id in self.runs_being_prepared:
                raise RunIsBeingPrepared("this run is being prepared; delete it when it is "
                                         "ready")
            if StatePaths(root=run.project).branch_file.exists():
                self.verifier_commands.run("stop", str(run.project))
            shutil.rmtree(run.folder)

    def page_view_of_run(self, run: WebRun) -> dict[str, object]:
        """What the page shows for one run: the record, the state, and what the commands print.

        The report, the disputes and the questions are there only after the first round.
        """
        record = run.record()
        view: dict[str, object] = {"id": run.run_id, **record, "status": None, "report": None,
                                   "disputes": [], "requests": [],
                                   "has_pdf": run.report_pdf().exists(),
                                   "share_file": (run.share_file.read_text()
                                                  if run.share_file.exists() else None)}
        if record["setup"] != READY:
            view["state"] = record["setup"]
            return view
        project = str(run.project)
        status = self.verifier_commands.run_and_read_json("status", project)
        view["status"] = status
        view["state"] = state_word_of_ready_run(status)
        report = self.verifier_commands.run_and_read_json("report", project)["report"]
        if report is None:
            return view
        assert isinstance(report, dict), f"`verifier report --json` gave {report!r}"
        # the page shows the verdicts, the issues and the Markdown text, not the raw findings
        view["report"] = {key: value for key, value in report.items()
                          if key not in ("findings", "typst")}
        commands = self.verifier_commands
        view["disputes"] = commands.run_and_read_json("diagnosis", project)["disputes"]
        view["requests"] = commands.run_and_read_json("review", project)["requests"]
        return view

    def list_entry_of_run(self, run: WebRun) -> dict[str, object]:
        """What the list of runs shows for one run: the record, the state and the verdict."""
        record = run.record()
        if record["setup"] != READY:
            return {"id": run.run_id, **record, "state": record["setup"], "verdict": None}
        status = self.verifier_commands.run_and_read_json("status", str(run.project))
        return {"id": run.run_id, **record, "state": state_word_of_ready_run(status),
                "verdict": status["verdict"]}


def state_word_of_ready_run(status: dict[str, object]) -> str:
    """One word for the state of a ready run, from `verifier status --json`."""
    if status["stopped_because"]:
        return "stopped on an error"
    if status["running"] or status["waiting_events"]:
        return "running"
    return "done" if status["verdict"] else "waiting to start"


def write_uploaded_files_into_project(project: Path, uploads: Sequence[UploadedFile]) -> None:
    """Put one zip, or one or more paper files, into the empty folder `project`.

    Raises:
        UploadError: the upload mixes a zip with other files, a file is not a paper file,
            or no paper file is in the result.
    """
    if not uploads:
        raise UploadError("No file was uploaded.")
    zips = [u for u in uploads if u.name.lower().endswith(".zip")]
    if zips and len(uploads) > 1:
        raise UploadError("Upload one .zip file, or one or more paper files, not both.")
    if zips:
        extract_zip(zips[0].data, project)
    else:
        for upload in uploads:
            try:
                extract.paper_format(upload.name)
            except extract.UnknownPaperFormat as e:
                raise UploadError(f"{e}, or a .zip file") from e
        save_uploaded_paper_files(uploads, project)
    if not any(is_paper_file(f) for _, _, files in os.walk(project) for f in files):
        raise UploadError("The upload holds no paper file (.tex or .md).")


def is_paper_file(name: str) -> bool:
    """True when the extension of `name` is one of a paper file that the verifier reads."""
    try:
        extract.paper_format(name)
    except extract.UnknownPaperFormat:
        return False
    return True


def extract_zip(data: bytes, project: Path) -> None:
    """Extract a zip into `project`, without SKIPPED_ZIP_FOLDERS.

    A zip whose files are all in one folder gives the contents of that folder.

    Raises:
        UploadError: the file is not a zip, or its files are larger than MAX_UNZIPPED_BYTES.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UploadError(f"The .zip file cannot be read: {e}") from e
    members = [m for m in archive.infolist()
               if not SKIPPED_ZIP_FOLDERS & set(Path(m.filename).parts)]
    unzipped = sum(m.file_size for m in members)
    if unzipped > MAX_UNZIPPED_BYTES:
        raise UploadError(f"The .zip file holds {unzipped:,} bytes of files. The limit is "
                          f"{MAX_UNZIPPED_BYTES:,} bytes.")
    # extractall drops "..", and the first "/", from each name, so no file leaves `project`
    archive.extractall(project, members)
    entries = list(project.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        # first a name that no child has, so that a child with the folder's own name
        # ("paper/paper/") can move up
        inner = entries[0].rename(project / f".unzipped-{secrets.token_hex(8)}")
        for child in list(inner.iterdir()):
            child.rename(project / child.name)
        inner.rmdir()


def save_uploaded_paper_files(uploads: Sequence[UploadedFile], folder: Path) -> None:
    """Write each uploaded paper file into `folder`.

    Each file keeps its own name, made safe to use as a path, because the extension decides
    how the file is read. Two files with the same name, from different folders on the
    sender's machine, are both kept: the later one gets a number.
    """
    for upload in uploads:
        name = re.sub(r"[^A-Za-z0-9._-]", "_",
                      os.path.basename(upload.name.replace("\\", "/"))).lstrip(".")
        stem, ext = os.path.splitext(name)
        path, n = folder / name, 1
        while path.exists():
            n += 1
            path = folder / f"{stem}-{n}{ext}"
        path.write_bytes(upload.data)
