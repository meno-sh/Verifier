"""`vcheck write-methods`, a command of the auditing agent.

Write methods for method_needs.json, blind to the claimed values, and print claim -> method.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core import cli_command
from core.methods import method_writer
from core.project import watched_project
from utils import claim_files, structure

# Beside the register, like claim_notes.json: read by write-methods, by no check.
NEEDS = "method_needs.json"


class VcheckWriteMethodsCommand(cli_command.CLICommand):
    """Runs `vcheck write-methods` for the auditing agent."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck write-methods`."""
        p.add_argument("--retries", type=int, default=3)

    def execute(self, args: argparse.Namespace) -> int:
        """Write methods for method_needs.json, blind to the claimed values; print claim -> method.

        The values to mask are read from the claim register, so the agent cannot leak one by
        accident. The needs go to the model in batches (core/methods/method_writer.py), and
        each reply writes its own module, methods/written_methods_<n>.py. The method of each
        claim is recorded in its source in the register. Every need is checked
        before the paid call, so a wrong need stops the command before it spends money.
        """
        root: Path = args.root
        state_dir: Path = args.state
        path = state_dir / NEEDS
        needs = json.loads(path.read_text()) if path.exists() else []
        if not needs:
            print(f"nothing to do: {NEEDS} is empty or missing")
            return 0
        with claim_files.lock(state_dir).held():
            files, _ = claim_files.loaded(state_dir, root)
        todo = []
        for n in needs:
            entry = files.claim(n.get("claim"))
            if entry is None:
                print(f"error: a need names {n.get('claim')!r}, which is not in the claim register",
                      file=sys.stderr)
                return 1
            try:
                claim_files.artifact_source(files, n["claim"])
            except ValueError as e:
                print(f"error: {e}. Only a claim with an artifact source can get a method. "
                      f"Remove this need from {NEEDS}.", file=sys.stderr)
                return 1
            todo.append(method_writer.MethodWriterNeed(
                need_id=f"{n['claim']}/n1", claim_id=n["claim"],
                quantity=n.get("quantity", ""), path=n["path"],
                value=None if entry.get("value") is None else str(entry["value"]),
                sister_values=tuple(claim_files.claim_sister_values(files, n["claim"])),
                statement=claim_files.claim_text(files, n["claim"], anonymized=True),
                passages=tuple(claim_files.claim_passages(files, n["claim"]))))
        project = watched_project.WatchedProject.load(root, state_dir)
        options = project.run_options(project.check_registry())
        model = method_writer.MODEL.value_in_options(options)
        per_batch = method_writer.NEEDS_PER_BATCH.value_in_options(options)
        tokens = method_writer.PROMPT_TOKENS_PER_BATCH.value_in_options(options)
        assert isinstance(model, str) and isinstance(per_batch, int) \
            and isinstance(tokens, int), (model, per_batch, tokens)     # their kinds
        writer = method_writer.MethodWriter(structure.data_files(str(root)), str(root), state_dir,
                                        lambda prompt: method_writer.ask(prompt, model=model),
                                        args.retries, per_batch, tokens)
        report = writer.write_methods(todo)
        methods = {}
        for n in todo:
            method = report.methods.get(n.need_id)
            print(f"{n.claim_id}: " + (f"method {method}" if method else
                                       "no working method: " + report.unresolved[n.need_id]))
            if method:
                methods[n.claim_id] = method
        if methods:
            # the model calls ran without the lock, so the files are read again before the
            # write, to keep what another process changed meanwhile
            with claim_files.lock(state_dir).held():
                files, r = claim_files.loaded(state_dir, root)
                try:
                    for claim_id, method in methods.items():
                        claim_files.set_method(files, r, claim_id, method)
                except ValueError as e:
                    print(f"error: {e}", file=sys.stderr)
                    return 1
                claim_files.save(files, state_dir)
                claim_files.save_round(r, state_dir)
            print(f"methods recorded in {claim_files.REGISTER} for {len(methods)} claims")
        return 1 if report.unresolved else 0
