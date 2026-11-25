#!/usr/bin/env python3
"""Run the whole pipeline on a fixture and compare every output with a saved reference.

`fixtures/workdir/` is a small working directory with one claim per
provenance and recompute outcome: a match, a disagreement, a missing artifact, a
changed checksum, a setup that disagrees, a traced claim, a traced claim with no
accepted reason, a traced claim whose anchor is gone, a claim with no source, and
an unknown computational method. Two of the claims also make the claim checks fire.
Four claims are inferred from other claims: a chain of two, and a cycle of two.
Its computational methods are its own method files, in `fixtures/workdir/methods/`; a
fourth run leaves them out, so every artifact claim names an unknown method.

The reference (`fixtures/pipeline_expected.json`) is the full Findings tree
`FindingsAnalyzer.analyse` returns, with the verdict and the triage board, so any change in
behaviour shows up as a difference. The paper's path and the tool's commit are
replaced with fixed values, since they change with the machine and every commit.
Run with --update to rewrite it after an intended change, and review
the diff before committing it.

It also runs the `verifier static-check` command, which has no method, on a LaTeX and a
Markdown file together, on a file of no known format, which it must refuse, and given a
folder instead of a paper, where it must list the folder's main files.

Returns 1 on any difference, so it can gate a commit.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.analysis import analysis_request, findings_analyzer, findings_json, verdict_policy  # noqa: E402
from core.methods import computational_methods  # noqa: E402
from core.registry import extensions  # noqa: E402
from core.report_model import triage_board, verdict_explainer  # noqa: E402
from schema import findings, inputs  # noqa: E402

VERDICT_POLICY = verdict_policy.VerdictPolicy()

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, "fixtures", "workdir")
PAPER = os.path.join(HERE, "fixtures", "broken_paper.tex")
EXPECTED = os.path.join(HERE, "fixtures", "pipeline_expected.json")

# What each claim must end as. Checked by name as well as through the reference, so a
# failure says which outcome broke rather than only that the output changed.
STATUS = {
    "good": "PASS",
    "prior": "PASS",
    "disagrees": "FAIL",
    "missing": "BLOCK",
    "tampered": "BLOCK",
    "setup-mismatch": "BLOCK",
    "traced": "TRACED",
    "bad-reason": "UNREAD",
    "anchor-gone": "BLOCK",
    "no-source": "SKIP",
    "unknown-method": "UNREAD",
    "general": "DERIVED",
    "headline": "DERIVED",
    "loop-a": "DERIVED",
    "loop-b": "DERIVED",
}


def runs() -> dict:
    """Run the pipeline in each mode the reference covers and return the trees by name.

    The claim register's paths are relative to its own folder, so the run does not
    depend on the folder it starts from.
    """
    register = os.path.join(WORKDIR, "claims.json")
    reg = computational_methods.MethodFolderLoader().registry_with_methods(
        extensions.builtin_check_registry(), Path(WORKDIR) / "methods")
    # the document is named by the file's name, so the reference does not hold a machine's path
    paper = {os.path.basename(PAPER): (PAPER,)}
    # claim_uncertainty calls models, which this reference does not cover
    off = {"want_claim_uncertainty": False}
    with_methods = findings_analyzer.FindingsAnalyzer(reg)
    built_in = findings_analyzer.FindingsAnalyzer(extensions.builtin_check_registry())
    return {
        "paper_only": built_in.analyse(analysis_request.AnalysisRequest(documents=paper)),
        "with_claims": with_methods.analyse(analysis_request.AnalysisRequest(
            documents=paper, claim_register_path=register, run_options=off)),
        "seeds_only": with_methods.analyse(analysis_request.AnalysisRequest(
            documents=paper, claim_register_path=register,
            switches_on=frozenset({"checks", "science", "recompute", "seeds"}))),
        "no_methods": built_in.analyse(analysis_request.AnalysisRequest(
            documents=paper, claim_register_path=register, run_options=off)),
    }


def as_json(tree: findings.Findings) -> dict:
    """The tree, its verdict and its board as plain JSON, with machine-specific values fixed."""
    outcome = VERDICT_POLICY.verdict_for(tree)
    board = triage_board.TriageBoardBuilder().build_board(tree)
    out = {"verdict": outcome.value,
           "why": verdict_explainer.VerdictExplainer().explain_verdict(outcome, board, tree),
           "board": board.to_json(),
           "findings": findings_json.FindingsJsonCodec().encode(tree)}
    out["findings"]["inputs"]["paper_paths"] = [os.path.basename(PAPER)]
    out["findings"]["inputs"]["tool_commit"] = "<commit>"
    # paths are resolved against the register's folder, which depends on the machine
    return json.loads(json.dumps(out).replace(WORKDIR, "<workdir>"))


def check_statuses(tree: findings.Findings) -> list[str]:
    """Return one line per claim whose gate status is not the expected one."""
    got = {c.claim.id: c.gate_status_word() for c in tree.claims}
    got |= {entry.id: "UNREAD" for entry in tree.unreadable_claims}
    return [f"{cid}: expected {want}, got {got.get(cid)}"
            for cid, want in STATUS.items() if got.get(cid) != want]


def check_cli() -> list[str]:
    """`verifier static-check` recomputes nothing, reads several paper files, and lists the
    main files when --paper names a folder. Run the way its entry point runs it."""
    verify = [sys.executable, "-P", "-m", "runtime.cli", "static-check"]
    env = {**os.environ, "PYTHONPATH": str(Path(HERE).parent)}
    register = os.path.join(WORKDIR, "claims.json")
    out = []
    done = subprocess.run([*verify, "--paper", PAPER, "--claims", register, "--json",
                           "--no-claim-uncertainty"],
                          capture_output=True, text=True, cwd="/", env=env)
    if done.returncode != 1:
        out.append(f"static-check: exit {done.returncode}, not 1 (MISSING INPUTS, an "
                   f"unreadable claim register entry): {done.stderr[-300:]}")
    else:
        found = json.loads(done.stdout)["findings"]
        good = [c for c in found["claims"] if c["claim"]["id"] == "good"]
        unreadable = [u["id"] for u in found["unreadable_claims"]]
        if good or "good" not in unreadable:
            out.append(f"static-check: claim good is not unreadable with no method: {unreadable}")
    notes = os.path.join(HERE, "fixtures", "broken_notes.md")
    done = subprocess.run([*verify, "--paper", PAPER, notes, "--json"],
                          capture_output=True, text=True, cwd="/", env=env)
    paths = json.loads(done.stdout)["findings"]["inputs"]["paper_paths"] if done.stdout else None
    if paths != [PAPER, notes]:
        out.append(f"static-check --paper TEX MD: read {paths}, not both files: "
                   f"{done.stderr[-300:]}")
    with tempfile.NamedTemporaryFile(suffix=".txt") as unknown:
        done = subprocess.run([*verify, "--paper", unknown.name], capture_output=True,
                              text=True, cwd="/", env=env)
    if done.returncode != 1 or "not a paper file" not in done.stderr:
        out.append(f"static-check --paper FILE.txt: exit {done.returncode}, not 1 with the format "
                   f"refused: {done.stderr[-300:]}")
    done = subprocess.run([*verify, "--paper", os.path.dirname(PAPER)],
                          capture_output=True, text=True, cwd="/", env=env)
    if done.returncode != 1 or f"--paper {PAPER}" not in done.stderr:
        out.append(f"static-check --paper FOLDER: exit {done.returncode}, not 1 with the main "
                   f"files listed: {done.stderr[-300:]}")
    return out


def main() -> int:
    """Compare the pipeline's output with the reference, or rewrite it with --update."""
    trees = runs()
    current = {name: as_json(tree) for name, tree in trees.items()}
    if "--update" in sys.argv:
        with open(EXPECTED, "w") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"wrote {os.path.relpath(EXPECTED)}")
        return 0

    failures = check_statuses(trees["with_claims"])
    failures += check_cli()
    recomputed = [c.claim.id for c in trees["no_methods"].claims
                  if isinstance(c.claim.source, inputs.ArtifactSource)]
    if recomputed:
        failures.append(f"no_methods: claims {recomputed} found a method with no methods given")
    with open(EXPECTED) as fh:
        expected = json.load(fh)
    for name in sorted(set(expected) | set(current)):
        if expected.get(name) != current.get(name):
            failures.append(f"{name}: output differs from the reference")
    for line in failures:
        print(f"  FAIL  {line}")
    if failures:
        print(f"\nFAILED: {len(failures)} difference(s). "
              f"If the change is intended, rerun with --update and review the diff.")
        return 1
    print(f"All {len(STATUS)} claim statuses and {len(current)} runs match the reference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
