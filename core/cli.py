"""vcheck: the verifier agent's one way into the deterministic core.

Each command reads the claim register, the methods and the plug-in checks in the state
directory.
The state directory is $VERIFIER_STATE, else ./.verifier. The working directory is
$VERIFIER_ROOT, else the state directory's parent; the daemon sets it to its checkout of
the tracked branch. `run` exits 2 on REJECT, 1 on BLOCK UNTIL FIXED or MISSING INPUTS,
and 0 otherwise. Other commands exit 1 on a problem.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from core import cli_command
from core.claim_steps import claim_reordering, duplicates, labeller
from core.commands import (
    accept_classes,
    check_quotes,
    check_register,
    clear_cache,
    drop_non_claims,
    fields,
    fill_register,
    label_candidates,
    link_claims,
    propose_classes,
    reorder_claims,
    run,
    shortlist_sources,
    test_check,
    test_method,
    validate,
    write_methods,
)
from core.project import watched_project
from core.registry import extensions
from utils import alerts, claim_files, structure

# The built-in commands of vcheck, in the order of its help. The claim steps
# (CLAIM_STEPS in runtime/daemon.py) come in the order code runs them.
BUILT_IN_COMMANDS: dict[str, cli_command.CLICommand] = {
    "validate": validate.VcheckValidateCommand(),
    "run": run.VcheckRunCommand(),
    "test-check": test_check.VcheckTestCheckCommand(),
    "test-method": test_method.VcheckTestMethodCommand(),
    "fields": fields.VcheckFieldsCommand(),
    "drop-non-claims": drop_non_claims.VcheckDropNonClaimsCommand(),
    "label-candidates": label_candidates.VcheckLabelCandidatesCommand(),
    "check-quotes": check_quotes.VcheckCheckQuotesCommand(),
    "propose-classes": propose_classes.VcheckProposeClassesCommand(),
    "accept-classes": accept_classes.VcheckAcceptClassesCommand(),
    "check-register": check_register.VcheckCheckRegisterCommand(),
    "fill-register": fill_register.VcheckFillRegisterCommand(),
    "reorder-claims": reorder_claims.VcheckReorderClaimsCommand(),
    "link-claims": link_claims.VcheckLinkClaimsCommand(),
    "shortlist-sources": shortlist_sources.VcheckShortlistSourcesCommand(),
    "write-methods": write_methods.VcheckWriteMethodsCommand(),
    "clear-cache": clear_cache.VcheckClearCacheCommand(),
}


def main(argv: list[str] | None = None) -> int:
    """Parse the command line and run one command."""
    ap = argparse.ArgumentParser(prog="vcheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", type=Path,
                    default=Path(os.environ.get("VERIFIER_STATE", structure.STATE_DIR_NAME)))
    # The extensions of the project add commands, so the state folder is read first.
    first = argparse.ArgumentParser(add_help=False)
    first.add_argument("--state", type=Path, default=ap.get_default("state"))
    known, _ = first.parse_known_args(argv)
    ap.add_argument("--root", type=Path,
                    default=Path(os.environ["VERIFIER_ROOT"]) if "VERIFIER_ROOT" in os.environ
                    else None,
                    help="the working directory (default: $VERIFIER_ROOT, else the state's parent)")
    sub = ap.add_subparsers(dest="command", required=True)
    commands = {**BUILT_IN_COMMANDS,
                **extensions.ExtensionSet.of_project(known.state.resolve()).vcheck_commands()}
    for name, command in commands.items():
        command.configure_parser(sub.add_parser(name, help=command.HELP))
    args = ap.parse_args(argv)
    args.state = args.state.resolve()
    args.root = (args.root or args.state.parent).resolve()
    # the project is the folder that holds the state, as the daemon records it; `root`
    # can be a checkout of it
    alerts.configure(alerts.project_log(args.state), "vcheck", args.state.parent)
    try:
        return commands[args.command].execute(args)
    except (*watched_project.PROJECT_INPUT_ERRORS, duplicates.DuplicatesError,
            labeller.LabelError, claim_files.ClaimFilesError,
            claim_reordering.ClaimReorderingError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
