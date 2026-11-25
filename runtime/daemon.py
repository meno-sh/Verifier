"""The verifier daemon: one for each working directory, which sends events to the agent.

Each tick keeps the OpenCode server alive and sends pending events when the agent is idle.
Before a round, each new commit on the tracked branch becomes an event,
`checkout/` moves to the branch tip, and the claim files are brought in step with its notes.
After each round, the daemon runs `vcheck reorder-claims`, which sorts the claims, and
`vcheck link-claims`, which writes the links between claims, then `vcheck run`, stamps the
claim files if their checks pass, and commits the state directory.
"""
from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from core.methods import computational_methods
from core.project import agent_session, watched_project
from core.registry import extensions, run_options
from core.report_output import report_store
from runtime.agent import agent_launchers, events, inbox, jobs, opencode, rounds
from runtime.project import git, project_status
from runtime.project.paths import StatePaths
from schema.verdict import Verdict
from utils import alerts, claim_files, file_storage, models, spending, structure, version

TEMPLATES = Path(__file__).resolve().parent / "templates"
WORKER_GUIDE_FILE = "WORKER.md"           # the guide for the worker's coding agent, in the state
TICK_SECONDS = 2.0
ROUND_TIMEOUT_SECONDS = 120 * 60
START_GRACE_SECONDS = 30        # a round never seen busy counts as ended after this
RETRY_FIRST_SECONDS = 60        # a failed round's events are sent again after this,
RETRY_MAX_SECONDS = 60 * 60     # doubling each time up to this
# A turn cut by the model's output limit gets this follow-up, at most this many times a round.
CONTINUE_PROMPT = ("Your last turn hit the output limit before you wrote anything. Continue "
                   "the same work in smaller batches.")
MAX_CONTINUES = 2
# The first start of OpenCode is tried this many times before the daemon stops on the error.
# A failure there is often short (a slow start, a port that another process took), and a
# stop on it is a fatal record, which keeps `start-if-pending` from starting the project
# again until `verifier init` runs.
FIRST_START_ATTEMPTS = 3
# and every key variable of utils/model_keys.json (models.key_variables)
PASSED_ENV = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TZ", "TMPDIR")
SYNC_SECONDS = 1800                  # a first build may download a lot
ON_DEMAND = "on-demand"               # in runtime/ while a daemon that stops when idle runs
# The limits of the briefing of a new session, in characters.
BRIEFING_COMMIT = 1500
BRIEFING_REPORT = 400
# The claim steps that code does before the agent's round, in order. check-quotes must pass
# before the classes: code makes classes only of the quotes that the QUOTES stage gave.
CLAIM_STEPS = (["drop-non-claims"], ["label-candidates"], ["check-quotes"],
               ["propose-classes"], ["accept-classes"], ["fill-register"])
# The claim steps after the agent's round, in order. reorder-claims sorts the register
# first, because link-claims drops each link that goes against that order.
LINK_STEPS = (["reorder-claims"], ["link-claims"])
# Said first in a round whose agent guide changed since the last one.
GUIDE_CHANGED_NOTE = ("AGENTS.md changed since your last round. Read AGENTS.md again "
                      "before your first change; its rules come before what earlier rounds "
                      "did.\n\n")
STATE_GITIGNORE = "runtime/\ninbox/\njobs/\ntimers/\ncheckout/\ntmp/\ncache/\n__pycache__/\n"


class Daemon:
    """Supervises the OpenCode server and delivers rounds for one working directory."""

    def __init__(self, root: Path, model: str = models.agent_preset.KIMI,
                 round_timeout: float = ROUND_TIMEOUT_SECONDS,
                 exit_when_idle: bool = False) -> None:
        """Make a daemon for the working directory `root`. run() starts it.

        With `exit_when_idle`, the daemon stops when no round runs and no event waits, unless
        `verifier start` removed its `runtime/on-demand` file since.
        """
        self.paths = StatePaths(root=root.resolve())
        self.agent_inbox = inbox.AgentInbox(self.paths.inbox)
        self.round_history = rounds.RoundHistory(self.paths)
        alerts.configure(self.paths.alerts_log, "daemon", self.paths.root)
        self.model = model
        self.round_timeout = round_timeout
        self.exit_when_idle = exit_when_idle
        self.server: subprocess.Popen | None = None
        self.client: opencode.Client | None = None
        self.server_info = opencode.ServerInfo(port=0, password="", session_id="")
        self.round: rounds.Round | None = None
        self.stopping = False
        self.retry_at = 0.0
        self.retry_wait = RETRY_FIRST_SECONDS
        self.clock = time.time          # how long the claim steps took (a test moves it)

    # Setup.

    def prepare(self) -> None:
        """Create the state directory, its git repo and the agent's config, if missing.

        Raises:
            alerts.Fatal: the model backend or a run option is not valid.
        """
        self.paths.ensure()
        state = self.paths.state
        try:
            models.backend()
        except ValueError as e:
            raise alerts.fail("daemon.options_invalid",
                              f"the model backend is not valid, so the daemon stops: {e}"
                              ) from None
        self.write_agent_guide()
        write_worker_guide(state)
        # the current permissions always, keeping any other settings already there
        config_file = state / "opencode.json"
        template = json.loads((TEMPLATES / "opencode.json").read_text())
        config = json.loads(config_file.read_text()) if config_file.exists() else template
        config["permission"] = {**template["permission"], "bash": opencode.bash_rules()}
        file_storage.JsonFile(config_file, indent=2).write(config)
        (state / ".gitignore").write_text(STATE_GITIGNORE)
        if not (state / ".git").exists():
            git.run_in_state(state, "init", "-q")
            git.run_in_state(state, "add", "-A")
            git.run_in_state(state, "commit", "-q", "-m", "verifier state: start")
        # the methods run on the checked commit, so its uv.lock builds their environment;
        # before the first checkout there is none, and moving the checkout builds it
        if self.paths.checkout.exists():
            sync_environment(self.paths.checkout, state)

    def write_agent_guide(self) -> bool:
        """Write AGENTS.md, the template with each extension's section after it, if it changed.

        The extensions of the project can change while the daemon runs, so each round
        checks again before the agent is woken. True when the file was written.
        """
        state = self.paths.state
        text = "\n".join([(TEMPLATES / "AGENTS.md").read_text(),
                          *extensions.ExtensionSet.of_project(state).agent_instructions()])
        guide = state / "AGENTS.md"
        if guide.exists() and guide.read_text() == text:
            return False
        guide.write_text(text)
        return True

    def env(self) -> dict[str, str]:
        """The environment the agent's shell sees: only what it needs, nothing else.

        It never holds the Claude token (models.CLI_TOKEN): the agent's model could read
        it. A model call through the claude CLI there fails and says why
        (models.cli_token_missing); the steps the daemon runs get the token (step_env).
        """
        env = {k: os.environ[k] for k in (*PASSED_ENV, *models.key_variables())
               if k in os.environ and k != models.CLI_TOKEN}
        launchers = agent_launchers.write_launchers(self.paths.runtime / "bin")
        env["PATH"] = f"{launchers}:{env.get('PATH', '/usr/bin:/bin')}"
        env.update(VERIFIER_STATE=str(self.paths.state), VERIFIER_ROOT=str(self.paths.checkout),
                   VERIFIER_BRANCH=self.paths.branch(),
                   VERIFIER_HOME=str(version.VERIFIER_HOME), VERIFIER_MODEL=self.model,
                   OPENCODE_SERVER_PASSWORD=self.server_info.password)
        env[models.AGENT_SHELL_ENV] = "1"
        return env

    def step_env(self) -> dict[str, str]:
        """The environment of a vcheck step the daemon runs itself: the agent's, with the token.

        The claim steps before a round and `vcheck run` after it call models, and the
        agent does not see them run, so they get models.CLI_TOKEN when it is set.
        """
        env = self.env()
        del env[models.AGENT_SHELL_ENV]
        if os.environ.get(models.CLI_TOKEN):
            env[models.CLI_TOKEN] = os.environ[models.CLI_TOKEN]
        return env

    def ensure_server(self) -> None:
        """Start the OpenCode server if it is not running, and make sure the session exists."""
        if self.server is not None and self.server.poll() is None and self.client \
                and self.client.healthy():
            return
        if self.server is not None:      # a server this daemon started has died or hangs
            alerts.notify("error", "daemon.opencode_restarted",
                          "the OpenCode server stopped answering, so the daemon restarts it",
                          exit_code=self.server.poll())
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
        try:
            saved = opencode.ServerInfo.read(self.paths.server_file)
        except opencode.ServerFileError as e:
            alerts.notify("log", "daemon.server_file_replaced",
                          f"{e}; the agent gets a new session", error=e)
            saved = None
        self.server_info = (dataclasses.replace(saved, port=opencode.free_port()) if saved else
                            opencode.ServerInfo(port=opencode.free_port(),
                                                password=secrets.token_urlsafe(24),
                                                session_id=""))
        self.server = opencode.start_server(self.paths.state, self.paths.runtime,
                                            self.server_info.port, self.env(),
                                            self.paths.runtime / "opencode.log")
        self.client = self.server_info.client()
        sid = self.server_info.session_id
        if not sid or not self.client.session_exists(sid):
            self.create_session()
        self.server_info.save(self.paths.server_file)
        alerts.notify("log", "daemon.opencode_started",
                      f"opencode serving on port {self.server_info.port}",
                      port=self.server_info.port)

    def create_session(self) -> None:
        """Make a new OpenCode session for the agent, with no round sent in it yet."""
        self.server_info = dataclasses.replace(
            self.server_info,
            session_id=self.client.create_session(f"verifier: {self.paths.root.name}"),
            session_first_round=None)
        alerts.notify("log", "daemon.session_created",
                      f"created session {self.server_info.session_id}",
                      session_id=self.server_info.session_id)

    def start_fresh_session_if_due(self, n: int) -> None:
        """Make a new session for round `n` if the agent session policy asks for one.

        The policy is the run option agent_session_policy (core/project/agent_session.py). It is
        read from options.json alone, not with the checks of the project, so a broken
        method file or plug-in check does not stop the daemon: the report of the round
        names it, for the agent to fix. When options.json cannot be read, the round uses
        the default policy, and the alert log says why; the report names the problem too.
        """
        options_file = self.paths.state / watched_project.WatchedProject.OPTIONS_FILE
        try:
            saved = (file_storage.JsonFile(options_file).read() if options_file.exists()
                     else {})
            if not isinstance(saved, dict):
                raise TypeError(f"{options_file} is not a JSON object")
            policy = agent_session.AgentSessionPolicy.from_options(saved)
        except (json.JSONDecodeError, agent_session.AgentSessionError, TypeError) as e:
            # not JSON; a value the policy refuses; a value of the wrong kind
            alerts.notify("error", "daemon.session_policy_unreadable",
                          f"the agent session policy cannot be read, so round {n} uses the "
                          f"default policy: {e}", round=n)
            policy = agent_session.AgentSessionPolicy()
        first = self.server_info.session_first_round
        tokens = opencode.spending(self.paths.runtime,
                                   session_id=self.server_info.session_id).tokens_read
        if policy.needs_fresh_session(0 if first is None else n - first, tokens):
            self.create_session()
            self.server_info.save(self.paths.server_file)

    def fresh_session_briefing(self, events: list[dict]) -> str:
        """What a new session reads before the events of its first round.

        It gives what a new session otherwise looks up before its first change: the commits
        of the round with their files, the pinned files they changed, the methods, the
        start of the last report, and the open requests of the extensions. A replay of the
        TruthSeekingGym test project showed these lookups. OpenCode gives AGENTS.md to each
        new session, so the briefing does not repeat the guide.
        """
        state, checkout = self.paths.state, self.paths.checkout
        lines = ["This is a new session. The earlier rounds ran in other sessions. Their "
                 "results are in the claim files and the reports, not in this conversation. "
                 "This briefing is current: do not run `git show --stat` again before your "
                 "first change.", ""]
        try:
            with claim_files.lock(state).held():
                files, _ = claim_files.loaded(state, checkout)
        except claim_files.ClaimFilesBroken:
            lines += ["The claim files cannot be read now. The events below say why.", ""]
        else:
            lines += self._briefing_of_commits(events, files)
        lines += self._briefing_of_methods_and_report()
        requests = extensions.ExtensionSet.of_project(state).status_lines(checkout, state)
        if requests:
            lines += ["The open requests of the extensions:", *requests, ""]
        return "\n".join(lines)

    def _briefing_of_commits(self, events: list[dict], files: claim_files.ClaimFiles
                             ) -> list[str]:
        """Each commit of the round with its message and files, and the pinned files it changed."""
        checkout = self.paths.checkout
        pinned = sorted({str(e["source"]["path"]) for e in files.register
                         if isinstance(e, dict) and isinstance(e.get("source"), dict)
                         and e["source"].get("kind") == "artifact" and e["source"].get("path")})
        lines = []
        for e in events:
            if e.get("kind") != "commit":
                continue
            shown = git.output(checkout, "show", "--stat", "--format=%B", e["sha"])
            if len(shown) > BRIEFING_COMMIT:
                shown = (shown[:BRIEFING_COMMIT]
                         + "\n... (cut; run `git -C checkout show --stat` for all)")
            changed = structure.changed_files(str(checkout), e["sha"])
            hit = sorted({f for f in changed for p in pinned if fnmatch.fnmatch(f, p)})
            lines += [f"Commit {e['sha'][:12]} (`git show --stat`):", shown.strip(),
                      ("It changed these pinned files: " + ", ".join(hit)) if hit
                      else "It changed no pinned file.", ""]
        return lines

    def _briefing_of_methods_and_report(self) -> list[str]:
        """The method names of each method file, the plug-in checks, and the last report."""
        state = self.paths.state
        lines = []
        method_files = sorted((state / computational_methods.METHODS_DIR).glob("*.py"))
        if method_files:
            lines.append("The method files and their methods:")
            for f in method_files:
                try:
                    names = ", ".join(computational_methods.MethodFolderLoader
                                      .method_names_in_file(f))
                except (computational_methods.MethodFileError, SyntaxError) as e:
                    names = f"(cannot be read: {e})"
                lines.append(f"- methods/{f.name}: {names}")
            lines.append("")
        checks = sorted(p.name for p in (state / "checks").glob("*.py"))
        if checks:
            lines += [f"The plug-in checks: {', '.join(checks)}", ""]
        latest = project_status.latest_report(self.paths)
        if latest is not None:
            commit = latest.saved.commit[:12] if latest.saved.commit else "no commit"
            start = (latest.text or "").strip()[:BRIEFING_REPORT]
            lines += [f"The last report is for {commit}. Its verdict is "
                      f"{latest.saved.verdict.value}. It is in reports/latest.md. It starts:",
                      start, ""]
        return lines

    # The loop.

    def run(self) -> None:
        """Hold the lock and tick until stopped. Raises if another daemon holds the lock."""
        self.paths.ensure()
        if not file_storage.ProcessLock(self.paths.lock).take_for_this_process():
            raise SystemExit(f"a verifier daemon is already running for {self.paths.root}")
        on_demand = self.paths.runtime / ON_DEMAND
        if self.exit_when_idle:
            on_demand.touch()
        self.prepare()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        try:
            self.start_server_with_retries()
            if self.stopping:
                return
            self.resume_round()
            if not self.round:
                write_startup_event(self.paths)
            while not self.stopping:
                try:
                    self.tick()
                    if on_demand.exists() and self.idle():
                        alerts.notify("log", "daemon.idle_exit", "no work waits; the daemon stops")
                        break
                except opencode.OpenCodeError as e:
                    alerts.notify("error", "daemon.opencode_restarted",
                                  f"the OpenCode server failed, so the daemon restarts it: {e}",
                                  error=e, status=e.status)
                    if self.server is not None:
                        self.server.terminate()
                    self.server = None
                time.sleep(TICK_SECONDS)
        finally:
            on_demand.unlink(missing_ok=True)
            # a fatal error also stops the server, so it does not outlive the daemon
            if self.server is not None:
                self.server.terminate()

    def start_server_with_retries(self) -> None:
        """Start OpenCode the first time, with FIRST_START_ATTEMPTS tries.

        A stop request (SIGTERM) between two tries ends the tries; `run` then stops.

        Raises:
            opencode.OpenCodeError: the last try failed too.
        """
        for attempt in range(1, FIRST_START_ATTEMPTS + 1):
            try:
                self.ensure_server()
                return
            except opencode.OpenCodeError as e:
                if attempt == FIRST_START_ATTEMPTS:
                    raise
                alerts.notify("warn", "daemon.opencode_start_failed",
                              f"OpenCode did not start (try {attempt} of "
                              f"{FIRST_START_ATTEMPTS}), so the daemon tries again: {e}",
                              error=e, status=e.status)
                if self.server is not None:
                    self.server.terminate()
                    self.server.wait(timeout=30)
                self.server = None
            if self.stopping:
                return
            time.sleep(TICK_SECONDS)

    def tick(self, now: float | None = None) -> None:
        """One pass: server, jobs, the round in flight, then delivery."""
        now = time.time() if now is None else now
        self.ensure_server()
        jobs.collect(self.paths, now)
        sid = self.server_info.session_id
        if self.round is not None:
            busy = self.client.busy(sid)
            if busy:
                self.round.seen_busy = True
                if now - self.round.sent_at > self.round_timeout and not self.round.aborted:
                    alerts.notify("log", "daemon.round_aborted",
                                  f"round {self.round.n} ran past {self.round_timeout} s; "
                                  "aborting it", round=self.round.n,
                                  timeout_s=self.round_timeout)
                    self.client.abort(sid)
                    self.round.aborted = True
                    self.save_round()
            else:
                r = self.round
                waited = r.seen_busy or now - r.last_prompted_at > START_GRACE_SECONDS
                # within the grace time, an idle session has ended its turn only with an error
                outcome = None if waited else self.client.turn_outcome(sid, r.sent_at,
                                                                       r.last_prompted_at)
                if (waited or outcome.error) and not self.continue_cut_turn(now, outcome):
                    self.finish_round(now)
        if self.round is None:
            self.deliver(now)

    def continue_cut_turn(self, now: float, outcome: opencode.TurnOutcome | None = None) -> bool:
        """Send a follow-up if the agent's last step was cut by the output limit. True if sent.

        A model that spends its whole output on one step (reasoning, or a long script)
        ends the turn with nothing done. The follow-up asks it to go on in smaller
        batches, at most MAX_CONTINUES times a round; after that, or after a failed
        model call, the round ends as usual. `outcome` is the turn's, when the caller
        has fetched it already.
        """
        r = self.round
        sid = self.server_info.session_id
        if r.continued >= MAX_CONTINUES:
            return False
        if outcome is None:
            outcome = self.client.turn_outcome(sid, r.sent_at, r.last_prompted_at)
        if outcome.error or outcome.finish != "length":
            return False
        self.client.prompt(sid, CONTINUE_PROMPT)
        r.continued += 1
        r.prompted_at = now
        r.seen_busy = False
        self.save_round()
        alerts.notify("log", "daemon.round_continued",
                      f"round {r.n}: the agent's turn hit the output limit; follow-up "
                      f"{r.continued} of {MAX_CONTINUES} sent", round=r.n, follow_up=r.continued)
        return True

    def idle(self) -> bool:
        """True when no round runs and no event waits in the inbox."""
        return self.round is None and not self.agent_inbox.pending_files()

    def deliver(self, now: float) -> None:
        """Send every pending event as one round, if there are any.

        Before the agent is woken, the claim files are brought in step with the notes of
        the checkout (utils/claim_files.py). A claim file the agent broke (not valid JSON,
        say) is named in the prompt, for the agent to fix. Missing or old-format claim
        files stop the daemon: a person must delete them, and the next round starts as a
        first round.
        """
        if not self.agent_inbox.pending_files() or now < self.retry_at:
            return
        self.take_in_branch()
        files = self.agent_inbox.pending_files()
        if not files:
            alerts.notify("log", "daemon.nothing_new",
                          "the tracked branch has no commit that was not checked; nothing sent")
            return
        sha = self.paths.checked()
        broken = None
        try:
            with claim_files.lock(self.paths.state).held():
                changed = claim_files.begin_round(self.paths.checkout, self.paths.state, sha)
        except claim_files.ClaimFilesBroken as e:
            broken, changed = str(e), f"not brought in step: {e}"
        except claim_files.ClaimFilesError as e:
            raise alerts.fail("daemon.claim_files_unreadable",
                              f"the claim files cannot be used, so the daemon stops: {e}",
                              commit=sha) from None
        alerts.notify("log", "daemon.claim_files", f"claim files at {sha[:12]}: {changed}",
                      commit=sha)
        n = self.round_history.next_round_number()
        # the run options can change while the daemon runs; the guide follows the keep rule
        guide_note = GUIDE_CHANGED_NOTE if self.write_agent_guide() else ""
        began = self.clock()
        steps_done = self.do_claim_steps(n) if broken is None else []
        if self.stopping:
            alerts.notify("log", "daemon.round_held", f"round {n}: the daemon is stopping, so "
                          "the agent is not woken; the events wait in the inbox", round=n)
            return
        events = [inbox.AgentInbox.read_event(f) for f in files]
        failed = next((s for s in steps_done if s["exit"] != 0), None)
        if failed is not None:
            self.check_papers_only(rounds.Round(
                n=n, events=events, sent_at=now, claim_steps=steps_done, begun_at=now,
                papers_only=True), failed, now, began)
            return
        self.start_fresh_session_if_due(n)
        # the first round of a new session; before round 1 there is nothing to tell. The
        # briefing comes after the claim steps, so it gives the claim files as they are now.
        briefing = self.fresh_session_briefing(events) + "\n\n" \
            if self.server_info.session_first_round is None and n > 1 else ""
        if self.server_info.session_first_round is None:
            self.server_info = dataclasses.replace(self.server_info, session_first_round=n)
            self.server_info.save(self.paths.server_file)
        # the start grace and the round timeout count from the prompt, not from before the
        # claim steps, which can take many minutes
        sent_at = now + max(0.0, self.clock() - began)
        self.round = rounds.Round(n=n, events=events, sent_at=sent_at, model=self.model,
                                  session_id=self.server_info.session_id,
                                  claim_steps=steps_done, begun_at=now)
        self.save_round()
        self.client.prompt(self.server_info.session_id, guide_note + briefing + build_prompt(
            self.round, self.paths.checkout, self.last_aborted(),
            extensions.ExtensionSet.of_project(self.paths.state), broken,
            round_timeout=self.round_timeout))
        self.agent_inbox.mark_delivered(files)
        alerts.notify("log", "daemon.round_sent", f"round {n} sent: {len(events)} event(s)",
                      round=n, events=len(events))

    def check_papers_only(self, r: rounds.Round, failed: dict[str, object],
                          now: float, began: float) -> None:
        """End round `r` without the agent, because the claim step `failed` failed.

        `now` is the time of the tick, and `began` the clock when the claim steps began; the
        round ends at `now` plus the time since `began`, as `deliver` counts `sent_at`.

        The agent cannot fix a failed claim step, so it is not woken. `vcheck run` checks
        the papers only, and its report flags that no claim was checked. The round is saved
        as failed, and the state is committed. The events stay in the inbox, and are sent
        again after the wait of a failed round; the next round runs every claim step again.
        """
        reason = f"vcheck {failed['step']} failed (exit {failed['exit']}): {failed['said']}"
        sha = self.paths.checked()
        sync_environment(self.paths.checkout, self.paths.state)
        r.verdict = self.check_commit(r.n, sha, "--claim-checks-not-run", reason)
        r.ended_at = now + max(0.0, self.clock() - began)
        r.error = f"a claim step failed, so the agent was not woken: {reason}"
        r.spending = project_status.Spending.of_project(self.paths, r.spending_since)
        self.round_history.save(r)
        git.run_in_state(self.paths.state, "add", "-A")
        git.run_in_state(self.paths.state, "commit", "-q", "--allow-empty", "-m",
                         f"round {r.n}: a claim step failed; papers only, verdict "
                         f"{r.verdict.value}")
        # the claim steps and the paper checks can take longer than the wait
        self.retry_at = r.ended_at + self.retry_wait
        alerts.notify("error", "daemon.round_without_claims",
                      f"round {r.n}: {reason}. The agent was not woken. The papers were checked "
                      f"without the claims, with verdict {r.verdict.value}. The "
                      f"{len(r.events)} event(s) will be sent again in {int(self.retry_wait)} s",
                      round=r.n, step=failed["step"], verdict=r.verdict.value,
                      events=len(r.events), retry_in_s=int(self.retry_wait))
        self.retry_wait = min(self.retry_wait * 2, RETRY_MAX_SECONDS)

    def do_claim_steps(self, n: int) -> list[dict[str, object]]:
        """Run the claim steps of code before the agent is woken.

        The steps are CLAIM_STEPS: `vcheck drop-non-claims` drops clear
        non-claims, `vcheck label-candidates` answers the other candidates, `vcheck
        check-quotes` checks the QUOTES stage, `vcheck propose-classes` and `vcheck
        accept-classes` make the classes, and `vcheck fill-register` writes a claim for each
        new class. A step that fails, or runs past claim_steps_timeout, is an `error` record,
        the highest level that does not stop the daemon. The steps after it do not run, and
        the next round runs all steps again. A stop request (SIGTERM) runs no further step.
        The options are read from options.json alone, as in `start_fresh_session_if_due`.

        Returns:
            Each step run: its command, exit code and first line of output.

        Raises:
            alerts.Fatal: options.json cannot be read, or claim_steps_timeout is not valid.
        """
        timeout = self.claim_step_timeout(n)
        done: list[dict[str, object]] = []
        for args in CLAIM_STEPS:
            step = " ".join(args)
            if self.stopping:
                alerts.notify("log", "daemon.claim_step", f"round {n}: the daemon is "
                              f"stopping, so vcheck {step} and later steps do not run",
                              round=n, step=step)
                break
            out = agent_launchers.run_vcheck(args, self.step_env(), timeout=timeout)
            said = (out.stdout.strip().splitlines() or out.stderr.strip().splitlines()
                    or [""])[0]
            done.append({"step": step, "exit": out.returncode, "said": said})
            if out.returncode != 0:
                alerts.notify("error", "daemon.claim_step_failed",
                              f"round {n}: vcheck {step} failed (exit {out.returncode}), so "
                              f"the later claim steps run in the next round: {said}",
                              round=n, step=step, exit_code=out.returncode,
                              stdout=out.stdout, stderr=out.stderr)
                break
            alerts.notify("log", "daemon.claim_step", f"round {n}: vcheck {step}: {said}",
                          round=n, step=step)
        return done

    def do_link_steps(self, n: int) -> list[dict[str, object]]:
        """Run `vcheck reorder-claims`, then `vcheck link-claims`, after the agent's round.

        A failed step is an `error` record. When reorder-claims fails, link-claims does not
        run, because the linker drops each link that goes against the order. The claims that
        are not linked stay without links until a later round links them.

        Returns:
            Each step run: its command, exit code and first line of output.

        Raises:
            alerts.Fatal: as claim_step_timeout raises it.
        """
        timeout = self.claim_step_timeout(n)
        done: list[dict[str, object]] = []
        for args in LINK_STEPS:
            step = " ".join(args)
            out = agent_launchers.run_vcheck(args, self.step_env(), timeout=timeout)
            said = (out.stdout.strip().splitlines() or out.stderr.strip().splitlines()
                    or [""])[0]
            done.append({"step": step, "exit": out.returncode, "said": said})
            if out.returncode != 0:
                alerts.notify("error", "daemon.link_step_failed",
                              f"round {n}: vcheck {step} failed (exit {out.returncode}): "
                              f"{said}", round=n, step=step, exit_code=out.returncode,
                              stdout=out.stdout, stderr=out.stderr)
                if args == ["reorder-claims"]:
                    break                        # no links against an old order
                continue
            alerts.notify("log", "daemon.link_step", f"round {n}: vcheck {step}: {said}",
                          round=n, step=step)
        return done

    def claim_step_timeout(self, n: int) -> int:
        """The timeout of one claim step in options.json, in seconds, read for round `n`.

        Raises:
            alerts.Fatal: options.json cannot be read, or claim_steps_timeout is not valid.
        """
        options_file = self.paths.state / watched_project.WatchedProject.OPTIONS_FILE
        try:
            saved = (file_storage.JsonFile(options_file).read() if options_file.exists()
                     else {})
            if not isinstance(saved, dict):
                raise TypeError(f"{options_file} is not a JSON object")
            return agent_session.claim_steps_timeout(saved)
        except (json.JSONDecodeError, run_options.OptionsError, TypeError) as e:
            raise alerts.fail("daemon.options_invalid",
                              f"a run option is not valid, so the daemon stops: {e}",
                              round=n) from None

    def take_in_branch(self) -> None:
        """Put what arrived on the tracked branch in the inbox, then move the checkout to it.

        `branch_moved` events only say that the branch moved, so they are moved out of
        the inbox. In their place come one `commit` event per commit the branch's tip
        has and the checked commit has not, every parent before its child, after the
        events already waiting. If the branch no longer contains the checked commit
        (it was reset or rewritten), a `branch_reset` event comes before them. A first
        checkout gets one `commit` event for the tip, unless the startup event is waiting,
        which reads the whole history anyway. The new events are in the inbox before the
        checkout moves, so a round that fails to send loses none of them. An event that
        is already waiting is not added again.

        The checked commit is the one the daemon recorded (StatePaths.checked), not the
        checkout's HEAD, which the agent can move. A checkout whose HEAD moved is moved
        back.

        Raises:
            alerts.Fatal: the tracked branch is gone (renamed or deleted), or git cannot
                move the checkout.
        """
        root = self.paths.root
        waiting = {f: inbox.AgentInbox.read_event(f)
                   for f in self.agent_inbox.pending_files()}
        branch = self.paths.branch()
        if not git.has_branch(root, branch):
            raise alerts.fail(
                "daemon.branch_missing",
                f"the tracked branch {branch!r} is gone, so the daemon stops. A renamed "
                f"branch cannot be followed. Give it back its name, or run `verifier stop`, "
                f"delete {self.paths.state} and start again with --branch",
                branch=branch)
        tip = git.tip(root, branch)
        base = self.paths.checked()
        news: list[dict] = []
        if not base:
            if not any(e.get("kind") == "startup" for e in waiting.values()):
                news.append({"kind": "commit", "sha": tip})
        elif base != tip:
            if not git.is_ancestor(root, base, tip):
                news.append({"kind": "branch_reset", "sha": tip, "previous": base})
            news += [{"kind": "commit", "sha": sha} for sha in git.incoming(root, base, tip)]
        # a checkout that failed to move leaves the same events waiting; do not add them twice
        for e in news:
            if e not in waiting.values():
                self.agent_inbox.write_event(**e)
        self.agent_inbox.mark_delivered([f for f, e in waiting.items()
                                         if e.get("kind") == "branch_moved"])
        if base != tip:
            self.move_checkout(tip)
        else:
            self.restore_checkout()

    def move_checkout(self, sha: str) -> None:
        """Move the checkout to `sha`, link the outputs and gitignored files, and record it.

        Raises:
            alerts.Fatal: git cannot move the checkout, for example because a file in it
                was changed. The events stay in the inbox for the next start.
        """
        checkout = self.paths.checkout
        try:
            git.check_out(self.paths.root, checkout, sha)
        except subprocess.CalledProcessError as e:
            raise alerts.fail("daemon.checkout_failed",
                              f"git cannot move {checkout} to {sha[:12]}, so the daemon stops. "
                              f"The waiting events stay in the inbox. Git said: "
                              f"{(e.stderr or '').strip()}",
                              error=e, commit=sha) from None
        self.paths.link_outputs(checkout)
        sync_environment(checkout, self.paths.state)
        file_storage.AtomicallyWrittenFile(self.paths.checked_file).write_text(sha + "\n")

    def restore_checkout(self) -> None:
        """Move the checkout back to the checked commit if its HEAD moved since."""
        sha = self.paths.checked()
        if not self.paths.checkout.exists():
            alerts.notify("error", "daemon.checkout_missing",
                          f"{self.paths.checkout} is gone; the daemon makes it again at the "
                          f"checked commit {sha[:12]}", commit=sha)
            self.move_checkout(sha)
            return
        head = git.run(self.paths.checkout, "rev-parse", "HEAD")
        if head != sha:
            alerts.notify("error", "daemon.checkout_moved",
                          f"the checkout was at {head[:12]}, not at the checked commit "
                          f"{sha[:12]}; the daemon moves it back", commit=sha, head=head)
            self.move_checkout(sha)

    def finish_round(self, now: float) -> None:
        """Record the round's end, write the canonical report, and commit the state."""
        r = self.round
        r.ended_at = now
        r.error = self.client.turn_outcome(self.server_info.session_id, r.sent_at).error
        if r.error:
            self.requeue(r, now)
            return
        self.retry_wait = RETRY_FIRST_SECONDS
        # the agent works in the checkout; the report is for the checked commit
        self.restore_checkout()
        sha = self.paths.checked()
        sync_environment(self.paths.checkout, self.paths.state)
        r.claim_steps = [*r.claim_steps, *self.do_link_steps(r.n)]
        r.verdict = self.check_commit(r.n, sha)
        r.spending = project_status.Spending.of_project(self.paths, r.spending_since)
        self.save_round()
        self.stamp_claim_files(r, sha)
        loaded = extensions.ExtensionSet.of_project(self.paths.state)
        for warning in loaded.after_round_warnings(self.paths.checkout, self.paths.state):
            alerts.notify("log", "daemon.extension_notice", warning, round=r.n)
        git.run_in_state(self.paths.state, "add", "-A")
        git.run_in_state(self.paths.state, "commit", "-q", "--allow-empty", "-m",
             f"round {r.n}: {len(r.events)} event(s), verdict {r.verdict.value}")
        alerts.notify("log", "daemon.round_ended",
                      f"round {r.n} ended: verdict {r.verdict.value}",
                      round=r.n, verdict=r.verdict.value)
        self.round = None

    def check_commit(self, n: int, sha: str, *options: str) -> Verdict:
        """Run `vcheck run` on the checked commit `sha` for round `n`, and give its verdict.

        `options` go to `vcheck run` after the commit.

        Raises:
            alerts.Fatal: vcheck run gave no verdict.
        """
        out = agent_launchers.run_vcheck(["run", "--commit", sha, *options], self.step_env())
        first = (out.stdout.splitlines() or [""])[0]
        if out.returncode not in (0, 1, 2) or not first.startswith("Verdict: "):
            # vcheck writes a report for every problem in the project's inputs, so
            # no verdict means a bug in the verifier, or a vcheck that did not run
            raise alerts.fail("daemon.vcheck_failed",
                              f"vcheck run gave no verdict for round {n} (exit "
                              f"{out.returncode}); the daemon stops",
                              round=n, commit=sha, exit_code=out.returncode,
                              stdout=out.stdout, stderr=out.stderr)
        # the saved report, not the Markdown, whose first line can hold more than the verdict
        saved = report_store.ReportStore(self.paths.state).load_saved_report(sha)
        assert saved is not None                    # vcheck printed a verdict, so it saved one
        return saved.verdict

    def stamp_claim_files(self, r: rounds.Round, sha: str) -> None:
        """Write the checked commit in the claim files' new stamps, if their checks pass.

        If a check fails, the new entries keep their empty stamps, and the next round
        continues them.
        """
        reg = watched_project.WatchedProject.load(self.paths.checkout,
                                                self.paths.state).check_registry()
        try:
            with claim_files.lock(self.paths.state).held():
                done = claim_files.stamp(self.paths.state, sha, reg.computational_methods,
                                         reg.claim_field_types(), self.paths.checkout)
        except claim_files.ClaimFilesBroken:
            done = False
        except claim_files.ClaimFilesError as e:
            raise alerts.fail("daemon.claim_files_unreadable",
                              f"the claim files cannot be used, so the daemon stops: {e}",
                              round=r.n, commit=sha) from None
        alerts.notify("log", "daemon.claim_files_stamped" if done else "daemon.claim_files_open",
                      f"round {r.n}: " + (f"claim files stamped with {sha[:12]}" if done else
                                          "a claim check fails, so the new entries stay open"),
                      round=r.n, commit=sha)

    def requeue(self, r: rounds.Round, now: float) -> None:
        """Put a failed round's events back in the inbox, and wait before sending them again.

        A failed read of the cost of the round is an alert, and the requeue goes on: the cost
        is bookkeeping. If the read raised after the events were back in the inbox, the next
        tick would write them a second time.
        """
        try:
            r.spending = project_status.Spending.of_project(self.paths, r.spending_since)
        except (opencode.OpenCodeError, spending.SpendingError) as error:
            alerts.notify("error", "daemon.round_cost_unread",
                          f"the cost of round {r.n} cannot be read: {error}", error=error,
                          round=r.n)
            r.spending = None
        for e in r.events:
            self.agent_inbox.write_event(e["kind"], **{k: v for k, v in e.items() if k != "kind"})
        self.retry_at = now + self.retry_wait
        alerts.notify("error", "daemon.round_failed",
                      f"round {r.n} failed: {r.error}; its {len(r.events)} event(s) will be "
                      f"sent again in {int(self.retry_wait)} s",
                      round=r.n, round_error=r.error, events=len(r.events),
                      retry_in_s=int(self.retry_wait))
        self.retry_wait = min(self.retry_wait * 2, RETRY_MAX_SECONDS)
        self.save_round()
        self.round = None

    # Round records, so a restarted daemon picks up where it left off.

    def save_round(self) -> None:
        """Write the round in flight to rounds/<n>.json."""
        self.round_history.save(self.round)

    def resume_round(self) -> None:
        """Pick up a round the previous daemon sent but did not see end."""
        resumed = self.round_history.open_round()
        if resumed is not None:
            self.round = resumed
            self.round.seen_busy = True       # it was sent long enough ago to have started
            alerts.notify("log", "daemon.round_resumed", f"resuming round {self.round.n}",
                          round=self.round.n)

    def last_aborted(self) -> bool:
        """True if the agent's last round before the round in flight ran too long.

        Such a round was aborted. A papers-only round between them is skipped, because the
        agent did not run in it.
        """
        previous = self.round_history.last_round_with_agent_before(self.round.n)
        return previous is not None and previous.aborted


def write_worker_guide(state: Path) -> None:
    """Write WORKER.md, the guide for the worker's coding agent, with each extension's section.

    It is written again at each start, so it always matches the installed Verifier.
    """
    (state / WORKER_GUIDE_FILE).write_text("\n".join([
        (TEMPLATES / WORKER_GUIDE_FILE).read_text(),
        *extensions.ExtensionSet.of_project(state).worker_instructions()]))


def write_startup_event(paths: StatePaths) -> None:
    """Put the startup event in the inbox if nothing was checked yet.

    The first round reads every note and commit message, so it needs no commit event.
    The file name is fixed and sorts first, so a second write replaces the first.
    """
    register = paths.state / watched_project.WatchedProject.REGISTER_FILE
    if not register.exists() and not any(paths.rounds.iterdir()):
        file_storage.JsonFileFolder(paths.inbox).write("0-startup.json", {"kind": "startup"})


def sync_environment(root: Path, state: Path) -> None:
    """Make the project's locked environment, which methods run in, match its uv.lock.

    Run at start and before every report, so a commit that changes uv.lock is checked in
    the new environment, and anything installed into it since is removed. Only the
    project's dependencies go in, never its own code. Without a uv.lock there is no
    environment, and the report says so. With one that does not build, the run cannot
    start, and the report says why (core/commands/run.py).
    """
    venv = state / watched_project.WatchedProject.METHOD_ENV
    if (root / "uv.lock").exists():
        try:
            subprocess.run(["uv", "sync", "--frozen", "--no-install-local", "--project", str(root)],
                           env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)},
                           capture_output=True, text=True, check=True, timeout=SYNC_SECONDS)
            return
        except (OSError, subprocess.SubprocessError) as e:
            # a problem in the project: the report says so (core/commands/run.py).
            # This keeps uv's output.
            # After a timeout, stderr is bytes even with text=True, and JSON cannot hold bytes.
            stderr = getattr(e, "stderr", None)
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            alerts.notify("error", "daemon.uv_lock_failed", f"uv.lock did not build: {e}",
                          error=e, stderr=stderr)
    shutil.rmtree(venv, ignore_errors=True)


def build_prompt(r: rounds.Round, root: Path, previous_aborted: bool,
                 extension_set: extensions.ExtensionSet,
                 broken_claim_file: str | None = None, *, round_timeout: float) -> str:
    """The message that starts one round: its time limit, then its events, oldest first.

    `extension_set` describes the events of the extensions.

    `broken_claim_file` says why the claim files could not be brought in step with the
    notes, when a file the agent writes cannot be read.

    `round_timeout` is the time limit of the round in seconds, after which the daemon
    stops it.
    """
    lines = [f"Round {r.n}. Handle these events as AGENTS.md says, then end the round. The "
             f"time limit of this round is {round_timeout / 60:g} minutes.", ""]
    if previous_aborted:
        lines += ["The previous round was stopped for running too long. Check what it left "
                  "unfinished before starting.", ""]
    if broken_claim_file:
        lines += [f"The core could not read the claim files: {broken_claim_file.rstrip('.')}. "
                  "Fix the file first. Then run this command: vcheck check-register", ""]
    texts = {**events.BUILT_IN, **extension_set.event_describers()}
    for i, e in enumerate(r.events, 1):
        lines.append(f"{i}. {events.describe(e, root, texts)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the daemon in the foreground (`verifier start` puts it in the background)."""
    ap = argparse.ArgumentParser(prog="verifier-daemon")
    ap.add_argument("root", type=Path)
    ap.add_argument("--model", default=models.agent_preset.KIMI)
    ap.add_argument("--round-timeout", type=float, default=ROUND_TIMEOUT_SECONDS)
    ap.add_argument("--exit-when-idle", action="store_true")
    args = ap.parse_args(argv)
    Daemon(args.root, args.model, args.round_timeout, args.exit_when_idle).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
