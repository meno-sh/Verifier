# Tests

The tests are plain scripts. Run each with `uv run`, from the Verifier folder, which
uses Python 3.13 and installs the commands the runtime tests call. Each exits 1 if
any case fails.

```bash
uv run tests/test_consistency.py
```

`fixtures/broken_paper.tex` is a small LaTeX paper that breaks one rule per
construct on purpose: a label defined twice, an interval written backwards, a
percentage above 100, a reference to a label that does not exist, and so on. The
test runs the consistency check on it and fails if any expected finding does not
appear. A check that never fires cannot be told apart from a check that is
missing, which is why the fixture exists. `fixtures/broken_notes.md` makes the same
mistakes in Markdown, and the test also checks how the Markdown reader reads tables,
percent signs, code blocks and headings.

```bash
uv run tests/test_comparability.py
```

`test_comparability.py` runs the comparability check on pairs of run configs taken
from real unlike comparisons in our audit (a judge-read read-out next to stated-P
rows, a different instructor model, a stale baseline commit, a different question
language), plus cases a fixed list of compared fields would get wrong.

```bash
uv run tests/test_claim_checks.py
```

`test_claim_checks.py` runs baseline, seeds, caption, measurement and interval
through the claim check registry, with one case where each flags, one where it
passes, and one where it is not checked. It also covers what the registry decides
for every check: a claim that was not recomputed, a gate that failed, and a check
that is switched off. Interval is tested this way, because the fixture's
methods return no interval.

```bash
uv run tests/test_run_checks.py
```

`test_run_checks.py` writes a clean run into a fresh folder for each case, breaks one
thing, such as an adapter declared but not loaded, outputs equal to the untrained
model's, or an item in both training and evaluation, and checks the matching problem
appears from run_integrity, run_outputs or run_data.

```bash
uv run tests/test_pipeline.py            # compare with the reference
uv run tests/test_pipeline.py --update   # rewrite the reference after an intended change
```

`fixtures/workdir/` is a small working directory, with its own computational methods in
`methods/`, and a claim register holding one claim
per provenance and recompute outcome: a match, a disagreement, a missing artifact,
a changed checksum, a setup that disagrees, a traced claim, a traced claim with no
accepted reason, a traced claim whose anchor is gone, a claim with no source, and
an unknown computational method. Four more claims are inferred from other claims: a
chain of two, and a cycle of two. The test runs the whole pipeline on it, with and
without the methods, and through the `verifier static-check` command, and compares the
Findings tree, the verdict and the triage board with `fixtures/pipeline_expected.json`. Any change in behaviour, including
a reworded message, shows up as a difference. After an intended change, rerun with
`--update` and review the diff before committing it.

```bash
uv run tests/test_uncertainty.py
```

`test_uncertainty.py` runs the two checks for claims inferred from other claims.
`acyclic_support` must flag each claim on a cycle of one, two or three claims and
name the cycle, and pass the others. `claim_uncertainty` runs with a fake red team and
a fake quantifier whose numbers are known, on claims resting on the fixture's verified
claims. With `propagate_claim_uncertainty` on, the uncertainty must compound over a chain
(0.2 and 0.3 give 0.44) and over shared dependencies, and a claim resting on a claim with
no source, a traced claim, a failed claim or a cycle must be Skipped with the reason.
With the option off, each claim has the uncertainty of its own step, and such a premise
does not stop the check. Saved readings must be reused with no model call. A changed
paper text or claim statement makes a reading stale; a changed premise does not, because
the check reads the full paper. The limit of score calls, the
threshold, provider errors (Skipped, with an error in the alert log), and parts that return the wrong
thing are covered too.

```bash
uv run tests/test_alerts.py
```

`test_alerts.py` covers the alert log in `utils/alerts.py` and the shared model call in
`utils/models.py`. A record must keep every field whole, refuse a bad level, key or
detail without writing anything, and four processes writing at once must leave only
whole lines. A process that ends with an exception must leave one fatal record with its
traceback. Against a fake provider, HTTP 429 and 5xx, an error body marked 502 and a
timeout must be retried with the waits asked for and raised after the fourth call. HTTP
4xx, a cut reply, a refusal and a reply with no output tokens must be raised at once.
A reply the reader refuses must be asked for again once, with the error added.

```bash
uv run tests/test_red_team.py
```

`test_red_team.py` covers the red team in `checks/uncertainty/red_team/`: its run options, its
version and its options rule, the prompt texts, the strict reading of a reply (one case
for each rule it refuses), the request it gives `models.ask_and_read`, its rounds (the
second round is shown the first round's proposals with their scores, and only those are
scored), the options rule on rounds and the limit of score calls, a reading saved through
`claim_uncertainty` with a fake model call and a fake quantifier, and the request body that
`utils/models.py` builds (the reasoning effort, no token budget, and a cache breakpoint
after the static instructions), with no network.

```bash
uv run tests/test_without_attack.py
```

`test_without_attack.py` covers the breakage with no attack, which `claim_uncertainty`
scores before the red team: the gate in `uncertainty.read`, the check's flag with no red
team, saved readings with and without attacks when the threshold changes, the judge's
breakage prompt with the premises alone, and the coherence chat and command request with
no hypothetical result. The parts and the model calls are fakes.

```bash
uv run tests/test_loud_settings.py
```

`test_loud_settings.py` covers the settings that must stop a run before it starts:
`utils/model_keys.json` and a missing key, the model review's run options,
`verifier start-if-pending` with no key, and a `uv.lock` that did not build.

```bash
uv run tests/test_duplicates.py
```

`test_duplicates.py` covers the merge pass in `core/claim_steps/duplicates.py`: its settings and the
run options that change them, the prompt texts (claims are numbered, and no
claim id is sent), the strict reading of the equivalence classes each call gives (one
case for each rule the readers refuse), the three proposing calls at once and the
deciding call, each in its own random order, the support links, the equivalence classes
kept separate (listed again after the words of any of their claims change), and
`vcheck duplicates` and `vcheck keep-separate` on a small state directory, with a fake
model call and no network.

```bash
uv run tests/test_schema.py
```

`test_schema.py` covers the typed schema in `schema/`. The claim register parser
must accept the fixture's well-formed claims and reject malformed ones with an
error naming the claim, and the README's example register must parse. Every trace
reason must have a description in the report. Claim checks must declare their keys
with real types. Every problem kind must have a fixed severity. And `verdict()`
is run on one hand-built tree per outcome.

```bash
uv run tests/test_web.py
```

`test_web.py` checks the server of `verifier web`. It checks that uploads become a
project, that zips stay in their folder, and that the server refuses another host and a
change without its header. It also checks that a failed `verifier init` shows its error,
and that a dispute and a withdrawal go through `verifier dispute` on a run with a report.
It starts no daemon and calls no model.

Calling a real model is not covered, because it costs money. Reading a saved reply,
and the model review's calls to a fake provider, are covered in `test_schema.py`.

```bash
uv run tests/test_core.py
uv run tests/test_runtime.py
uv run tests/test_checkout_watch.py
uv run tests/test_opencode_contract.py   # needs OpenCode 1.19.0; skipped otherwise
```

`test_core.py` covers the real-time core in `core/`: the fixture gives the same gate
results as the static pipeline, plug-in checks and methods in `.verifier/checks/` and
`.verifier/methods/` register and run (and are refused, Skipped or MethodFailed when
they misbehave), a superseded claim is not checked, `vcheck pin` catches a later change to a pinned
file, a claim traced to its own note is a caveat, and a file matching only a proposed
checksum is recomputed and counted as a caveat. It also covers the saved report: each
issue with its flag id, the same flag ids and report id at a later commit, and longer ids
when two flags would get the same one. `test_runtime.py` covers the git hook,
jobs and timers, the daemon against a stub of OpenCode, with what it writes to the alert
log, and tracking one branch: merges, moves made from another branch, resets, and checks
run in the daemon's own checkout, and the guide for the worker's agent (`WORKER.md`) that
each start writes. `test_checkout_watch.py` covers one hook for two
checkouts on two branches, `start-if-pending`, the JSON commands, the change between two
reports, and the rules that keep the agent's code inside `.verifier/`.
`test_opencode_contract.py`
runs the daemon against the real OpenCode binary with a fake model, including that the
agent's shell runs git commands that read and refuses the ones that write.

```bash
uv run tests/test_disputed.py
```

`test_disputed.py` covers disputed flags (`core/report_model/disputed_flags.py`). A small test extension,
`fakes/fixed_disputes.py`, gives fixed disputes. A disputed problem leaves its check
result, and a disputed gate flag leaves its claim out, so the verdict without the disputed
flags is PASS WITH CAVEATS at best. A dispute with another detail does not apply, and an
unreadable claim register entry cannot be disputed. The report strikes out each disputed
flag with its reason, in each layout, and gives two verdict lines only when the verdicts
differ. `vcheck run` saves both verdicts, and its exit code follows the verdict with all
flags.

```bash
uv run tests/test_diagnosis.py
```

`test_diagnosis.py` covers `verifier dispute` end to end on a committed copy of the
fixture: a dispute of two flags is one line, and the report strikes both out at once;
unknown, undisputable and already disputed flags and bad reasons are refused; and a
withdrawal makes the flags count again. It also covers the diagnosis: the dispute event
and the agent's rules, `vcheck check-diagnosis` on a complete diagnosis and on broken
ones, `verifier diagnosis` and its share file with all the sections, and the
warnings after a round.

```bash
uv run tests/test_review.py
```

`test_review.py` covers human review end to end for the `file-regenerated` kind, run
as an extension: two claims on one data file, one whose number moves when the file is
edited and one whose number does not. It checks what each claim gets with no review,
while a request waits, after a decline and after a confirm, that a further change
supersedes the request, withdrawing, the command-line answers and the decision event
for the agent, and that the schema, the checks and the core never import the review
package.

`test_layers.py` checks the separation rules: which folder may import which. From the
bottom, `schema/`, `utils/`, `core/` and `checks/` never import a higher folder. A check
reaches the core only through `checks/interface.py`, and never imports another check.
Only `checks/extension.py` names a registered check. CI runs it with the other test scripts. `test_model_names.py` covers
the one rule for model names in `utils/model_names.py`, and `utils/run_configs.py`.
