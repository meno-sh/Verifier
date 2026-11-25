"""The HTTP server of `verifier web`: the page, and a JSON API over the runs.

The server listens only on this machine. Each request that changes something must carry the
header X-Verifier-Web, which a page of another site cannot add without the server's consent,
so another site cannot start a paid run through the browser.
"""
from __future__ import annotations

import json
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from utils import alerts, text
from web import run_library

PAGE = (Path(__file__).parent / "page.html").read_bytes()
# The header that each request that changes something carries.
HEADER_REQUIRED_FOR_CHANGES = "X-Verifier-Web"
# How often the server starts the daemon of each run where work waits, in seconds.
SECONDS_BETWEEN_PENDING_WORK_STARTS = 30
# The host names that the page is served at. A request for another name, such as a name that
# a hostile site points at this machine, is refused.
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


class RunRequestHandler(BaseHTTPRequestHandler):
    """Serves the page at /, and the runs under /api/runs."""
    protocol_version = "HTTP/1.1"
    library: run_library.RunLibrary        # set on the subclass that make_server makes

    def send_complete_response(self, code: int, body: bytes, content_type: str,
                               extra_headers: dict[str, str] | None = None) -> None:
        """Write one complete response with an explicit length, as HTTP/1.1 needs."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, value: object) -> None:
        """Write one JSON response."""
        self.send_complete_response(code, json.dumps(value).encode(), "application/json")

    def send_error_json(self, code: int, message: str) -> None:
        """Write one JSON error, which the page shows as it is."""
        self.send_json(code, {"error": message})

    def log_message(self, *args: object) -> None:
        """Write no line for each request; the file names of an upload can be private."""

    def do_GET(self) -> None:
        """Serve the page, the list of runs, one run, or the PDF report of one run."""
        self.answer_with_route(self.answer_get_request)

    def do_POST(self) -> None:
        """Upload a new run, or dispute, withdraw, decide or share in one run."""
        self.answer_with_route(self.answer_post_request)

    def do_DELETE(self) -> None:
        """Delete one run."""
        self.answer_with_route(self.answer_delete_request)

    def answer_with_route(self, route: object) -> None:
        """Refuse a request for another host, then answer with `route`.

        An error that no route expects is logged and shown on the page; the server goes on.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            return self.send_error_json(403, f"this server answers only at 127.0.0.1, not {host}")
        if self.command != "GET" and not self.headers.get(HEADER_REQUIRED_FOR_CHANGES):
            return self.send_error_json(403,
                                        f"a change needs the header {HEADER_REQUIRED_FOR_CHANGES}")
        size = self.request_body_size()
        if size is None:
            self.close_connection = True
            return self.send_error_json(400, "the request has no valid Content-Length")
        if size > run_library.MAX_UPLOAD_BYTES:
            # read and drop the body, so that the browser gets the answer, not a reset
            for _ in range(0, size, 1024 ** 2):
                if not self.rfile.read(min(1024 ** 2, size)):
                    break
            self.close_connection = True
            return self.send_error_json(
                413, f"the upload has {size:,} bytes; the limit is "
                     f"{run_library.MAX_UPLOAD_BYTES:,} bytes")
        assert callable(route)
        try:
            route(urllib.parse.urlparse(self.path).path)
        except (BrokenPipeError, ConnectionResetError):
            # the browser or the tunnel closed the request before the answer; no one reads it
            return
        except run_library.CommandFailed as e:
            self.send_error_json(409, str(e))
        except Exception as e:  # noqa: BLE001 - logged, and shown on the page; the server goes on
            alerts.notify("error", "web.request_failed",
                          f"{self.command} {self.path} failed: {type(e).__name__}: {e}", error=e)
            self.send_error_json(500, text.last_part(traceback.format_exc(), 1500))

    def answer_get_request(self, path: str) -> None:
        """Answer a GET request."""
        if path == "/":
            return self.send_complete_response(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/runs":
            return self.send_json(200, {"runs": [self.library.list_entry_of_run(r)
                                                 for r in self.library.runs()]})
        found = re.fullmatch(r"/api/runs/([^/]+)(/report\.pdf)?", path)
        run = found and self.library.run_with_id(found.group(1))
        if not found or run is None:
            return self.send_error_json(404, "there is no such run")
        if not found.group(2):
            return self.send_json(200, self.library.page_view_of_run(run))
        pdf = run.report_pdf()
        if not pdf.exists():
            return self.send_error_json(404, "the run has no report yet")
        disposition = f'inline; filename="verifier-{run.run_id}.pdf"'
        self.send_complete_response(200, pdf.read_bytes(), "application/pdf",
                                    {"Content-Disposition": disposition})

    def answer_post_request(self, path: str) -> None:
        """Answer a POST request."""
        if path == "/api/runs":
            return self.create_run_from_upload()
        found = re.fullmatch(r"/api/runs/([^/]+)/(disputes|withdrawals|decisions|share)", path)
        run = found and self.library.run_with_id(found.group(1))
        if not found or run is None:
            return self.send_error_json(404, "there is no such run")
        action = found.group(2)
        body = self.read_json_request_body()
        project = str(run.project)
        verifier_commands = self.library.verifier_commands
        if action == "disputes":
            flags = body.get("flags")
            if not isinstance(flags, list) or not flags:
                return self.send_error_json(400, "select at least one flag to dispute")
            args = [f"--flag={flag}" for flag in flags]
            verifier_commands.run("dispute", project, *args, f"--why={body.get('why') or ''}")
        elif action == "withdrawals":
            verifier_commands.run("dispute", project, f"--withdraw={body.get('dispute')}")
        elif action == "decisions":
            verifier_commands.run("decide", project, str(body.get("request")),
                                  str(body.get("option")), f"--note={body.get('note') or ''}")
        else:
            verifier_commands.run("diagnosis", project, "--share", "-o", str(run.share_file))
            return self.send_json(200, self.library.page_view_of_run(run))
        # a dispute or an answer is an event for the auditing agent
        self.library.start_pending_work([run])
        self.send_json(200, self.library.page_view_of_run(run))

    def answer_delete_request(self, path: str) -> None:
        """Answer a DELETE request."""
        found = re.fullmatch(r"/api/runs/([^/]+)", path)
        run = found and self.library.run_with_id(found.group(1))
        if not found or run is None:
            return self.send_error_json(404, "there is no such run")
        try:
            self.library.delete_run(run)
        except run_library.RunIsBeingPrepared as e:
            return self.send_error_json(409, str(e))
        self.send_json(200, {"deleted": run.run_id})

    def request_body_size(self) -> int | None:
        """The size of the request body from Content-Length: 0 when absent, None when invalid."""
        text = self.headers.get("Content-Length") or "0"
        return int(text) if text.isdigit() else None

    def read_json_request_body(self) -> dict[str, object]:
        """The JSON object of the request body. An empty body is an empty object.

        answer_with_route has refused a body that is larger than the upload limit.
        """
        n = self.request_body_size() or 0
        raw = self.rfile.read(n) if n else b"{}"
        found = json.loads(raw or b"{}")
        if not isinstance(found, dict):
            raise ValueError(f"the body is not a JSON object: {raw[:200]!r}")
        return found

    def create_run_from_upload(self) -> None:
        """Make a run from the uploaded files, and prepare it in a thread of its own."""
        content_type = self.headers.get("Content-Type", "")
        boundary = re.search(r"boundary=([^;]+)", content_type)
        if "multipart/form-data" not in content_type or not boundary:
            return self.send_error_json(400, "send the files as multipart/form-data")
        # answer_with_route has refused a body that is larger than the upload limit
        body = self.rfile.read(self.request_body_size() or 0)
        fields = parse_multipart(body, boundary.group(1).strip('"').encode())
        uploads = [run_library.UploadedFile(name=name, data=data)
                   for name, data in fields.get("upload", []) if name]
        try:
            run = self.library.create_run(uploads)
        except run_library.UploadError as e:
            return self.send_error_json(400, str(e))
        self.library.start_preparation(run)
        self.send_json(202, {"id": run.run_id})


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, list[tuple[str | None, bytes]]]:
    """Return {field_name: [(filename, bytes), ...]}, every value in the order sent.

    `cgi` is not in Python 3.13, so the parts are split here. A field can repeat: several
    files can be uploaded under "upload".
    """
    out: dict[str, list[tuple[str | None, bytes]]] = {}
    for part in body.split(b"--" + boundary):
        if not part.strip() or part[:2] == b"--":
            continue
        head, found, data = part.partition(b"\r\n\r\n")
        if not found:
            continue
        head_text = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', head_text)
        filename = re.search(r'filename="([^"]*)"', head_text)
        if not name:
            continue
        # each part ends with the CRLF before the next boundary, and the data keeps the rest
        out.setdefault(name.group(1), []).append((filename.group(1) if filename else None,
                                                  data.removesuffix(b"\r\n")))
    return out


def make_server(library: run_library.RunLibrary, host: str, port: int) -> ThreadingHTTPServer:
    """A server for the runs of `library`, on `host` and `port`. It does not serve yet."""
    handler = type("LibraryRequestHandler", (RunRequestHandler,), {"library": library})
    return ThreadingHTTPServer((host, port), handler)


def start_pending_work_every_interval(library: run_library.RunLibrary,
                                      stop: threading.Event) -> None:
    """Start the daemon of each run where work waits, at each interval, until `stop`.

    The interval is SECONDS_BETWEEN_PENDING_WORK_STARTS.

    The work is a new dispute or answer, or a job end, check-in or timer of the agent. An
    error that no code expects goes to the alert log once, and the loop goes on: if it
    ended, no run of the page would start again until a person restarts `verifier web`.
    """
    last_error = None
    while not stop.wait(SECONDS_BETWEEN_PENDING_WORK_STARTS):
        try:
            library.start_pending_work()
            last_error = None
        except alerts.Fatal:
            raise                               # a fatal stop is already in the log
        except Exception as e:  # noqa: BLE001 - logged with its traceback; the loop goes on
            # the log gets each new error once, not every time the same error comes back
            if str(e) != last_error:
                alerts.notify("error", "web.start_pending_work_failed",
                              f"starting the waiting work of the runs failed: {e}", error=e)
            last_error = str(e)
