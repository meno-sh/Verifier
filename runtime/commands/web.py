"""`verifier web`: serve the local page, where a person uploads a paper and reads its report."""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path
from typing import ClassVar

from core import cli_command
from utils import alerts, models
from web import run_library, server

# The folder of the runs, next to the pinned OpenCode that install.sh puts in place.
DEFAULT_RUNS_FOLDER = Path.home() / ".local" / "share" / "verifier" / "web-runs"
DEFAULT_PORT = 8765


class VerifierWebCommand(cli_command.CLICommand):
    """Runs `verifier web`: serves the page until the person stops it with Ctrl-C."""
    HELP: ClassVar[str] = "serve the local page: upload a paper, and read and dispute its report"

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `verifier web`."""
        p.add_argument("--port", type=int, default=DEFAULT_PORT,
                       help=f"the port on 127.0.0.1 (default {DEFAULT_PORT})")
        p.add_argument("--runs-folder", type=Path, default=DEFAULT_RUNS_FOLDER, metavar="FOLDER",
                       help=f"the folder that keeps the runs (default {DEFAULT_RUNS_FOLDER})")
        p.add_argument("--model", default=models.agent_preset.KIMI,
                       help="the model of the auditing agent of each run")
        p.add_argument("--no-browser", action="store_true", help="do not open the browser")

    def execute(self, args: argparse.Namespace) -> int:
        """Serve the page, start the work that waits in each run, and open the browser."""
        runs: Path = args.runs_folder.expanduser().resolve()
        runs.mkdir(parents=True, exist_ok=True)
        alerts.configure(runs / "alerts.jsonl", "web", runs)
        library = run_library.RunLibrary(runs, run_library.VerifierCommandLine(args.model))
        try:
            httpd = server.make_server(library, "127.0.0.1", args.port)
        except OSError as e:
            print(f"error: cannot listen on 127.0.0.1:{args.port}: {e}. Give another --port",
                  file=sys.stderr)
            return 1
        stop = threading.Event()
        threading.Thread(target=server.start_pending_work_every_interval, args=(library, stop),
                         daemon=True).start()
        url = f"http://127.0.0.1:{args.port}/"
        print(f"serving {url}, with the runs in {runs}. Stop with Ctrl-C.")
        if not args.no_browser:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("stopped. The daemon of each run stops when its work is done.")
        finally:
            stop.set()
            httpd.server_close()
        return 0
