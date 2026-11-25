"""Prepare a working directory for the verifier: its extensions, run options, branch and hook.

`verifier start` and `verifier init` prepare the directory. `verifier start-if-pending`
checks again, with saved_project_problem, that the daemon can run before it starts one.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

from core.claim_steps import duplicates, labeller
from core.methods import method_writer
from core.project import agent_session, watched_project
from core.registry import extensions, run_options
from runtime import daemon
from runtime.agent import opencode
from runtime.project import git, hook, scope
from runtime.project.paths import StatePaths
from utils import file_storage, models

DEFAULT_BRANCH = "main"
# The line that imports the guide in the instruction file of the coding agent. The
# Verifier changes no file that the worker tracks, so the person adds it.
WORKER_GUIDE_IMPORT_LINE = "@.verifier/" + daemon.WORKER_GUIDE_FILE


def worker_guide_message(paths: StatePaths) -> str:
    """What `start` and `init` print so that the person gives the guide to their coding agent.

    The person does one of two things: tell the agent to follow the guide, or add the
    import line to the agent's instruction file, so that every later session reads it too.
    """
    guide = paths.state / daemon.WORKER_GUIDE_FILE
    return (f"Instruct your coding agent to follow {guide}, or add this line to its "
            f"instruction file (CLAUDE.md or AGENTS.md): {WORKER_GUIDE_IMPORT_LINE}")


def prepare(paths: StatePaths, branch: str, model: str, claims_from: list[str] | None,
            ask: Callable[[str], str] | None = None,
            options: list[str] | None = None,
            extension_names: tuple[str, ...] | None = None) -> str | None:
    """Check what the daemon needs, select the notes, record the branch and install the hook.

    `claims_from` and `ask` are as in `scope.select`, and `options` as in
    `run_options_given_at_start`. `extension_names`, when given, replace the saved
    extensions of the project; with none given and none saved, DEFAULT_EXTENSIONS are used.
    The extensions add checks and options, so the options are checked with them loaded.
    The extension names and the options are saved only when every check passes.
    Returns what is missing, or None when the directory is ready.
    """
    if not (paths.root / ".git").exists():
        return f"{paths.root} is not a git repository"
    try:
        names = (extension_names if extension_names is not None
                 else extensions.ExtensionSet.saved_or_default_names(paths.state))
        project = watched_project.WatchedProject(paths.root, paths.state,
                                                 extensions.ExtensionSet.load_named(names))
    except extensions.ExtensionsFileError as e:
        return str(e)
    values, problem = run_options_given_at_start(project, options or [])
    problem = problem or run_problem(project, model, values)
    if problem:
        return problem
    try:
        found = opencode.version()
    except opencode.OpenCodeError as e:
        return str(e)
    if found != opencode.PINNED_VERSION:
        return f"OpenCode {found} is installed; the verifier is tested on {opencode.PINNED_VERSION}"
    problem = branch_problem(paths, branch) or hook.hooks_problem(paths.root)
    if problem:
        return problem
    extensions.ExtensionSet.save_names(paths.state, names)
    if values is not None:
        project.save_run_option_values(values)
    paths.ensure()
    daemon.write_worker_guide(paths.state)
    try:
        scope.select(paths, branch, claims_from, ask)
    except (scope.Cancelled, ValueError) as e:
        return str(e)
    file_storage.AtomicallyWrittenFile(paths.branch_file).write_text(branch + "\n")
    hook.install(paths.root, branch)
    return None


def run_options_given_at_start(project: watched_project.WatchedProject, given: list[str]
                               ) -> tuple[dict[str, object] | None, str | None]:
    """The run options to save in .verifier/options.json, and what is wrong with them.

    The options are each `--option KEY=VALUE`. A value given now replaces the saved one,
    and the other saved values stay. Each command of the project reads only that file
    (core/commands/run.py), never its own environment. Gives (None, None) when no option is given,
    and (None, the problem) when an option is wrong. Nothing is saved here.
    """
    texts: dict[str, tuple[str, str]] = {}
    for item in given:
        key, sep, text = item.partition("=")
        if not sep:
            return None, f"--option {item}: write KEY=VALUE"
        texts[key] = (f"--option {key}", text)
    if not texts:
        return None, None
    try:
        reg = project.check_registry()
        values = project.saved_run_option_values(reg)
    except watched_project.PROJECT_INPUT_ERRORS as e:
        return None, f"the run options do not fit: {e}"
    for key, (where, text) in texts.items():
        if key not in reg.run_option_catalog.options:
            known = sorted(reg.run_option_catalog.options)
            return None, f"{where} names no run option; known are {known}"
        try:
            values[key] = reg.run_option_catalog.options[key].parse_text_value(text)
        except ValueError as e:
            return None, f"{where}: {e}"
    try:
        reg.refuse_options_that_do_not_fit(project.run_options(reg, values))
    except (TypeError, ValueError, run_options.OptionsError) as e:
        return None, f"the run options do not fit, so none is saved: {e}"
    return values, None


def run_problem(project: watched_project.WatchedProject, model: str,
                saved_values: Mapping[str, object] | None = None) -> str | None:
    """Why the daemon cannot run for this project with the agent's `model`, or None.

    It looks at every model a round calls: the agent's own model, and those of the run
    options (options_problem). Each key is read with models.api_key, as the model call
    reads it. `saved_values` is as in WatchedProject.run_options.
    """
    problem = models.agent_key_problem(model)
    if problem:
        return problem
    return options_problem(project, saved_values)


def saved_project_problem(paths: StatePaths, model: str) -> str | None:
    """Why the daemon cannot run for the project as `verifier init` saved it, or None."""
    try:
        project = watched_project.WatchedProject.load(paths.root, paths.state)
    except extensions.ExtensionsFileError as e:
        return str(e)
    return run_problem(project, model)


def options_problem(project: watched_project.WatchedProject,
                    saved_values: Mapping[str, object] | None = None) -> str | None:
    """What is wrong with the run options of the project, or None.

    Every check is taken as on, as when the auditing agent later adds a claim it applies
    to, so a missing threshold or API key shows now and not at the first commit. The
    agent session policy, the claim steps, the labeller settings and the model backend
    ($VERIFIER_MODEL_BACKEND) are checked too.
    `saved_values` is as in WatchedProject.run_options.
    """
    try:
        models.backend()
        reg = project.check_registry()
        options = project.run_options(reg, saved_values)
        reg.refuse_options_that_do_not_fit(options)
        agent_session.AgentSessionPolicy.from_options(options)
        agent_session.claim_steps_timeout(options)
        # the model calls of the claim steps and the method writer, which no check's rule
        # covers
        called = [duplicates.Settings.from_options(options).model,
                  method_writer.MODEL.value_in_options(options),
                  labeller.Settings.from_options(options).model]
        for model in called:
            models.check_key(model)
    except (*watched_project.PROJECT_INPUT_ERRORS, TypeError, ValueError) as e:
        return f"the run options do not fit: {e}"
    return None


def branch_problem(paths: StatePaths, branch: str) -> str | None:
    """Why `branch` cannot be the tracked branch, or None if it can."""
    if paths.branch_file.exists() and paths.branch() != branch:
        return (f"{paths.root} is checked on branch {paths.branch()}, not {branch}; "
                f"its claim register describes {paths.branch()}")
    if not git.has_branch(paths.root, branch):
        return f"{paths.root} has no branch {branch!r} with a commit on it"
    return None
