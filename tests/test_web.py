#!/usr/bin/env python3
"""Assert that `verifier web` makes a run from an upload, and acts on it only through `verifier`.

Covers:
  - an upload of paper files keeps each name, and numbers a second file with the same name;
  - a zip whose files are in one folder gives the contents of that folder, without `.git`,
    `.verifier` and `__MACOSX`, and no file leaves the project;
  - a zip with other files, a file that is not a paper, a zip with no paper, and a file
    that is not a zip are refused;
  - a multipart body keeps the bytes of each file, also a last CRLF of the file;
  - the server serves the page; it refuses another host, and a change without its header;
  - a run whose `verifier init` fails shows the setup error, here because no key is set;
  - on a run with a report: the page gets the issues and the layout data of the PDF,
    without the raw findings and the markup; a dispute and a withdrawal go through
    `verifier dispute`, and the share request with no written diagnosis writes no file;
  - deleting a run removes its folder, and an unknown run is not found;
  - a zip with a folder inside a folder of the same name gives its contents;
  - a run that is being prepared cannot be deleted; after its preparation, it can.

Returns 1 if any case fails, so it can gate a commit.
"""
import contextlib
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_core  # noqa: E402

from core import cli as vcheck  # noqa: E402
from core.project import watched_project  # noqa: E402
from core.registry import extensions  # noqa: E402
from runtime.project import git as runtime_git  # noqa: E402
from runtime.project.paths import StatePaths  # noqa: E402
from utils import alerts, models  # noqa: E402
from web import run_library, server  # noqa: E402

FAILED: list[str] = []
WHY = "The method reads runs/old/; the notes use runs/fixed/."
PAPER = b"\\documentclass{article}\\begin{document}Hello.\\end{document}\n"


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(name)
        if detail:
            print(f"        {detail}")


def upload_error_message_of(call: object) -> str:
    """The message of the UploadError that `call` raises, or "" when it raises none."""
    assert callable(call)
    try:
        call()
    except run_library.UploadError as e:
        return str(e)
    return ""


def zip_bytes(files: dict[str, bytes]) -> bytes:
    """A zip that holds `files`, by their names in the zip."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


def files_under(folder: Path) -> list[str]:
    """Every file under `folder`, by its path relative to the folder."""
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def test_uploads(tmp: Path) -> None:
    """Paper files and zips become a project; other uploads are refused."""
    up = run_library.UploadedFile
    project = tmp / "papers"
    project.mkdir()
    write = run_library.write_uploaded_files_into_project
    write(project, [up("a/main.tex", PAPER), up("b\\main.tex", PAPER),
                    up("notes one.md", b"# Notes\n")])
    expect("uploads: each paper keeps its name, made safe, and a repeated name gets a number",
           files_under(project) == ["main-2.tex", "main.tex", "notes_one.md"],
           str(files_under(project)))

    project = tmp / "zipped"
    project.mkdir()
    write(project, [up("project.zip", zip_bytes({
        "paper/main.tex": PAPER, "paper/data/r.csv": b"a,b\n", "paper/.git/HEAD": b"x",
        "paper/.verifier/options.json": b"{}", "__MACOSX/paper/._main.tex": b"x",
        "paper/../../outside.tex": PAPER}))])
    expect("uploads: a zip in one folder gives its contents, without .git, .verifier and "
           "__MACOSX, and no file leaves the project",
           files_under(project) == ["data/r.csv", "main.tex", "outside.tex"]
           and not (tmp / "outside.tex").exists(), str(files_under(project)))

    project = tmp / "nested"
    project.mkdir()
    write(project, [up("project.zip", zip_bytes({
        "paper/paper/main.tex": PAPER, "paper/data.csv": b"a\n"}))])
    expect("uploads: a zip with a folder inside a folder of the same name gives its contents",
           files_under(project) == ["data.csv", "paper/main.tex"], str(files_under(project)))

    def message_of_refused_upload(uploads: list[run_library.UploadedFile]) -> str:
        return upload_error_message_of(lambda: write(Path(tempfile.mkdtemp(dir=tmp)), uploads))
    expect("uploads: a zip with another file is refused",
           "not both" in message_of_refused_upload(
               [up("p.zip", zip_bytes({"m.tex": PAPER})), up("m.tex", PAPER)]))
    expect("uploads: a file that is not a paper is refused, and the message names the zip",
           ".zip" in message_of_refused_upload([up("data.csv", b"")]))
    expect("uploads: a zip with no paper file is refused",
           "no paper file" in message_of_refused_upload(
               [up("p.zip", zip_bytes({"data/r.csv": b"a\n"}))]))
    expect("uploads: a file that is not a zip is refused",
           "cannot be read" in message_of_refused_upload([up("p.zip", b"not a zip")]))
    expect("uploads: no file is refused", "No file" in message_of_refused_upload([]))
    limit = run_library.MAX_UNZIPPED_BYTES
    run_library.MAX_UNZIPPED_BYTES = 100
    try:
        message = message_of_refused_upload([up("p.zip", zip_bytes({"m.tex": PAPER * 50}))])
    finally:
        run_library.MAX_UNZIPPED_BYTES = limit
    expect("uploads: a zip whose files are larger than the limit is refused, with the limit",
           "The limit" in message, message)


def test_multipart(tmp: Path) -> None:
    """The parts of a multipart body, with the bytes of each file kept exactly."""
    body = (b"--XyZ\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"a.tex\"\r\n"
            b"Content-Type: text/x-tex\r\n\r\nline\r\n\r\n"
            b"--XyZ\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"b.md\"\r\n"
            b"\r\n# B\r\n--XyZ--\r\n")
    fields = server.parse_multipart(body, b"XyZ")
    expect("multipart: both files, in order, each with its last CRLF",
           fields == {"upload": [("a.tex", b"line\r\n"), ("b.md", b"# B")]}, repr(fields))


class LocalServer:
    """A `verifier web` server on a free port of 127.0.0.1, for the length of a with block."""

    def __init__(self, folder: Path) -> None:
        """Serve the runs in `folder`."""
        self.library = run_library.RunLibrary(
            folder, run_library.VerifierCommandLine(models.agent_preset.KIMI))
        self.httpd = server.make_server(self.library, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> "LocalServer":
        """Start to serve in a thread."""
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the server."""
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method: str, path: str, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        """Send one request and return the status and the body."""
        request = urllib.request.Request(self.base + path, data=body, method=method,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def change(self, method: str, path: str, value: object = None) -> tuple[int, dict]:
        """Send one change with its header and a JSON body, and return the JSON answer."""
        code, raw = self.call(method, path, None if value is None else json.dumps(value).encode(),
                              {server.HEADER_REQUIRED_FOR_CHANGES: "1",
                               "Content-Type": "application/json"})
        return code, json.loads(raw)


def test_server_guards(tmp: Path) -> None:
    """The page is served, and a request for another host or a change without the header is not."""
    with LocalServer(tmp / "runs") as web:
        code, page = web.call("GET", "/")
        expect("server: GET / gives the page", code == 200 and b"<title>Verifier" in page)
        code, raw = web.call("GET", "/api/runs", headers={"Host": "evil.example:80"})
        expect("server: a request for another host is refused", code == 403, raw.decode())
        code, raw = web.call("POST", "/api/runs", b"x",
                             {"Content-Type": "multipart/form-data; boundary=b"})
        expect("server: a change without the header is refused",
               code == 403 and server.HEADER_REQUIRED_FOR_CHANGES in raw.decode(), raw.decode())
        code, answer = web.change("GET", "/api/runs/20260101-000000-abcdef")
        expect("server: an unknown run is not found", code == 404, repr(answer))
        limit = run_library.MAX_UPLOAD_BYTES
        run_library.MAX_UPLOAD_BYTES = 10
        try:
            code, raw = web.call("POST", "/api/runs", b"x" * 100,
                                 {"Content-Type": "multipart/form-data; boundary=b",
                                  server.HEADER_REQUIRED_FOR_CHANGES: "1"})
        finally:
            run_library.MAX_UPLOAD_BYTES = limit
        expect("server: an upload larger than the limit is refused, and the page gets the "
               "answer", code == 413 and "limit" in raw.decode(), raw.decode())
        connection = http.client.HTTPConnection("127.0.0.1", web.httpd.server_address[1])
        connection.putrequest("POST", "/api/runs/20260101-000000-abcdef/dispute")
        for name, value in ((server.HEADER_REQUIRED_FOR_CHANGES, "1"),
                            ("Content-Length", "-1")):
            connection.putheader(name, value)
        connection.endheaders()
        answer = connection.getresponse()
        expect("server: a negative Content-Length is refused", answer.status == 400,
               str(answer.status))
        connection.close()


def test_setup_failure(tmp: Path) -> None:
    """A run whose `verifier init` fails shows the error of the command."""
    key = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        with LocalServer(tmp / "runs") as web:
            boundary = "b0undary"
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"upload\"; "
                    f"filename=\"main.tex\"\r\n\r\n").encode() + PAPER + \
                   f"\r\n--{boundary}--\r\n".encode()
            code, raw = web.call("POST", "/api/runs", body, {
                server.HEADER_REQUIRED_FOR_CHANGES: "1",
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
            run_id = json.loads(raw)["id"] if code == 202 else ""
            expect("upload: the answer is 202 with the run id", code == 202, raw.decode())
            deadline = time.time() + 60
            view: dict = {}
            while time.time() < deadline:
                view = web.change("GET", f"/api/runs/{run_id}")[1]
                if view.get("state") != run_library.PREPARING:
                    break
                time.sleep(0.5)
            expect("upload: with no key, the setup fails and gives the error of verifier init",
                   view.get("state") == run_library.SETUP_FAILED and view["setup_error"],
                   repr(view))
            project = tmp / "runs" / run_id / "project"
            expect("upload: the project is a git repository with the paper in one commit",
                   runtime_git.output(project, "log", "--format=%s") == "The uploaded files"
                   and runtime_git.output(project, "ls-files") == "main.tex")
            _, listed = web.change("GET", "/api/runs")
            expect("list: the run is listed with its state",
                   [(r["id"], r["state"]) for r in listed["runs"]]
                   == [(run_id, run_library.SETUP_FAILED)], repr(listed))
            code, answer = web.change("DELETE", f"/api/runs/{run_id}")
            expect("delete: the run folder is removed",
                   code == 200 and not (tmp / "runs" / run_id).exists(), repr(answer))
    finally:
        if key is not None:
            os.environ["OPENROUTER_API_KEY"] = key


def git(root: Path, *args: str) -> str:
    """Run git with a fixed identity."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "worker", "GIT_AUTHOR_EMAIL": "w@w",
           "GIT_COMMITTER_NAME": "worker", "GIT_COMMITTER_EMAIL": "w@w"}
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def run_with_first_report(library: run_library.RunLibrary, tmp: Path) -> run_library.WebRun:
    """A ready run whose project is the fixture working directory, with a first report.

    The project has no `verifier init`, so `verifier start-if-pending` starts no daemon.
    """
    run = library.create_run([run_library.UploadedFile("main.tex", PAPER)])
    shutil.rmtree(run.project)
    shutil.copytree(test_core.WORKDIR, run.project)
    root = run.project
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    state = root / ".verifier"
    state.mkdir()
    extensions.ExtensionSet.save_names(state, extensions.DEFAULT_EXTENSIONS)
    shutil.copytree(root / "methods", state / "methods")
    claims = [test_core.claim_entry(c) for c in ("disagrees", "good", "prior", "no-source")]
    register = state / watched_project.WatchedProject.REGISTER_FILE
    register.write_text(json.dumps({"claims": claims}))
    test_core.turn_off_paid_model_checks(state)
    paths = StatePaths(root=root)
    runtime_git.check_out(root, paths.checkout, git(root, "rev-parse", "HEAD"))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        vcheck.main(["--state", str(state), "--root", str(paths.checkout), "run",
                     "--commit", git(root, "rev-parse", "HEAD")])
    run.write_record(setup=run_library.READY)
    return run


def test_report_and_disputes(tmp: Path) -> None:
    """The page reads a report, and disputes and withdraws flags with `verifier dispute`."""
    with LocalServer(tmp / "runs") as web:
        run = run_with_first_report(web.library, tmp)
        code, view = web.change("GET", f"/api/runs/{run.run_id}")
        report = view.get("report") or {}
        expect("run: the page gets the verdict, the issues and the text, not the raw findings",
               code == 200 and view["state"] == "done" and report.get("verdict")
               and report.get("issues") and "findings" not in report
               and "typst" not in report and view["has_pdf"], repr(view)[:600])
        layout = report.get("layout") or {}
        expect("run: the page gets the layout data of the PDF, with its sections",
               layout.get("verdict") == report.get("verdict") and layout.get("sections"),
               repr(layout)[:300])
        flag = next(i["id"] for i in report["issues"] if i["code"] == "number-disagrees")
        code, answer = web.change("POST", f"/api/runs/{run.run_id}/disputes", {"flags": []})
        expect("dispute: no flag is refused", code == 400, repr(answer))
        code, view = web.change("POST", f"/api/runs/{run.run_id}/disputes",
                                {"flags": [flag], "why": WHY})
        issue = next((i for i in (view.get("report") or {}).get("issues", [])
                      if i["id"] == flag), {})
        expect("dispute: verifier dispute records it, and the issue shows the dispute",
               code == 200 and [(d["id"], d["state"]) for d in view["disputes"]]
               == [("D-1", "waiting for a diagnosis")]
               and (issue.get("disputed") or {}).get("dispute_id") == "D-1", repr(view)[:600])
        code, answer = web.change("POST", f"/api/runs/{run.run_id}/disputes",
                                  {"flags": [flag], "why": WHY})
        expect("dispute: the error of verifier dispute is shown, for a flag already disputed",
               code == 409 and "D-1" in answer["error"], repr(answer))
        code, view = web.change("POST", f"/api/runs/{run.run_id}/withdrawals", {"dispute": "D-1"})
        expect("withdraw: verifier dispute --withdraw records it",
               code == 200 and [d["state"] for d in view["disputes"]] == ["withdrawn"],
               repr(view.get("disputes")))
        code, view = web.change("POST", f"/api/runs/{run.run_id}/share", {})
        expect("share: with no written diagnosis, no share file is made",
               code == 200 and view["share_file"] is None, repr(view.get("share_file")))
        code, raw = web.call("GET", f"/api/runs/{run.run_id}/report.pdf")
        expect("pdf: the PDF report is served", code == 200 and raw.startswith(b"%PDF"))


class InitThatWaits(run_library.VerifierCommandLine):
    """`verifier init` that waits until the test lets it go, and starts no daemon."""

    def __init__(self) -> None:
        super().__init__(models.agent_preset.KIMI)
        self.started, self.go_on = threading.Event(), threading.Event()

    def init(self, project: Path) -> None:
        self.started.set()
        self.go_on.wait(30)

    def start_if_pending(self, projects) -> None:
        """Start nothing."""


def test_delete_during_preparation(tmp: Path) -> None:
    """A run that is being prepared cannot be deleted; after its preparation, it can."""
    commands = InitThatWaits()
    library = run_library.RunLibrary(tmp / "runs", commands)
    run = library.create_run([run_library.UploadedFile("main.tex", PAPER)])
    library.start_preparation(run)
    try:
        library.delete_run(run)
        refused = False
    except run_library.RunIsBeingPrepared:
        refused = True
    expect("delete: a run that is being prepared is refused, and stays", refused
           and run.folder.exists())
    commands.go_on.set()
    deadline = time.time() + 30
    while run.run_id in library.runs_being_prepared and time.time() < deadline:
        time.sleep(0.1)
    library.delete_run(run)
    expect("delete: after the preparation, the run is deleted, and nothing writes it again",
           not run.folder.exists())


class FlakyLibrary:
    """A run library whose first start of the waiting work fails with an unexpected error."""

    def __init__(self) -> None:
        self.calls = 0

    def start_pending_work(self) -> None:
        self.calls += 1
        if self.calls <= 2:
            raise FileNotFoundError("run.json is gone")


def test_restart_loop_survives_an_error(tmp: Path) -> None:
    """The loop that starts the waiting work goes on after an error, and logs it."""
    library, stop = FlakyLibrary(), threading.Event()
    wait, server.SECONDS_BETWEEN_PENDING_WORK_STARTS = (
        server.SECONDS_BETWEEN_PENDING_WORK_STARTS, 0.05)
    try:
        loop = threading.Thread(target=server.start_pending_work_every_interval,
                                args=(library, stop))
        loop.start()
        deadline = time.time() + 10
        while library.calls < 4 and time.time() < deadline:
            time.sleep(0.05)
        stop.set()
        loop.join(10)
    finally:
        server.SECONDS_BETWEEN_PENDING_WORK_STARTS = wait
    logged = [r for r in alerts.read(tmp / "alerts.jsonl")
              if r["key"] == "web.start_pending_work_failed"]
    expect("restart loop: after an unexpected error, the loop goes on and logs the error once",
           library.calls >= 3 and len(logged) == 1, f"calls={library.calls} logged={logged}")


def main() -> int:
    """Run every case in its own temporary folder."""
    for group in (test_uploads, test_multipart, test_server_guards, test_setup_failure,
                  test_report_and_disputes, test_delete_during_preparation,
                  test_restart_loop_survives_an_error):
        print(group.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            alerts.configure(Path(tmp) / "alerts.jsonl", "test", Path(tmp))
            group(Path(tmp))
    if FAILED:
        print(f"\n{len(FAILED)} web case(s) failed.")
        return 1
    print("\nAll web cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
