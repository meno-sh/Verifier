"""verifier: check a research paper or project before you post or submit it.

Each command is in runtime/commands/, in a file named after it. Extensions add more.
The verifier checks only one branch, `main` unless `--branch` says otherwise.
A later `start` with another branch is refused.
Only `verifier stop` stops the verifier. Closing the terminal view does not.

A tool that runs the verifier for many working directories uses `init` once for each,
and then `start-if-pending` often. That daemon runs only while work waits.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import cli_command
from core.registry import extensions
from runtime.commands import alerts as alerts_command
from runtime.commands import (
    attach,
    diff,
    init,
    report,
    start,
    start_if_pending,
    static_check,
    status,
    stop,
    uninstall,
    web,
)
from runtime.project.paths import StatePaths
from utils import alerts

# The built-in commands of verifier, in the order of its help.
BUILT_IN_COMMANDS: dict[str, cli_command.CLICommand] = {
    "start": start.VerifierStartCommand(),
    "init": init.VerifierInitCommand(),
    "static-check": static_check.VerifierStaticCheckCommand(),
    "start-if-pending": start_if_pending.VerifierStartIfPendingCommand(),
    "attach": attach.VerifierAttachCommand(),
    "stop": stop.VerifierStopCommand(),
    "uninstall": uninstall.VerifierUninstallCommand(),
    "status": status.VerifierStatusCommand(),
    "report": report.VerifierReportCommand(),
    "diff": diff.VerifierDiffCommand(),
    "alerts": alerts_command.VerifierAlertsCommand(),
    "web": web.VerifierWebCommand(),
}
# The commands that act on no single working directory, so they have no `root`.
COMMANDS_WITHOUT_PROJECT = frozenset({"static-check", "start-if-pending", "web"})


def _root_of_extension_command(argv: list[str] | None) -> Path:
    """The working directory an extension command names, to read its saved extensions.

    An extension command takes the working directory as its first argument, such as
    `verifier dispute . --flag F-3a9c1e`. Before the parser exists, that argument is the
    first one after the command that is not an option. The current folder is the answer
    when there is none.
    """
    words = sys.argv[1:] if argv is None else argv
    rest = [w for w in words[1:] if not w.startswith("-")]
    return Path(rest[0] if rest else ".").resolve()


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one command."""
    ap = argparse.ArgumentParser(prog="verifier", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name, command in BUILT_IN_COMMANDS.items():
        command.configure_parser(sub.add_parser(name, help=command.HELP))
    # a folder that the Verifier did not prepare yet has the default extensions, so that
    # their commands are in the help everywhere
    extra = extensions.ExtensionSet.load_named(extensions.ExtensionSet.saved_or_default_names(
        StatePaths(root=_root_of_extension_command(argv)).state)).verifier_commands()
    for name, extension_command in extra.items():
        p = sub.add_parser(name, help=extension_command.HELP)
        p.add_argument("root", type=Path, help="the project folder, a git repository")
        extension_command.configure_parser(p)
    args = ap.parse_args(argv)
    if args.command in COMMANDS_WITHOUT_PROJECT:
        return BUILT_IN_COMMANDS[args.command].execute(args)
    paths = StatePaths(root=args.root.resolve())
    alerts.configure(paths.alerts_log, "verifier", paths.root)
    if args.command not in extra:
        args.root = paths.root
        return BUILT_IN_COMMANDS[args.command].execute(args)
    # the parser read the extensions of a guessed folder; the command runs in `root`
    if args.command not in extensions.ExtensionSet.load_named(
            extensions.ExtensionSet.saved_or_default_names(paths.state)).verifier_commands():
        print(f"error: {paths.root} does not load the extension of `{args.command}` (see "
              f"{paths.state / extensions.EXTENSIONS_FILE})", file=sys.stderr)
        return 1
    if not paths.checkout.exists():
        print(f"error: nothing is checked yet, so `{args.command}` has no checkout to "
              "read; wait for the first round", file=sys.stderr)
        return 1
    # an extension command reads the checkout and the state folder, as a vcheck command does
    args.root, args.state = paths.checkout, paths.state
    return extra[args.command].execute(args)


if __name__ == "__main__":
    sys.exit(main())
