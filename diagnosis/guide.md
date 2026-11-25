# How to diagnose a disputed flag

Read this file when you write a diagnosis (`./diagnoses/D-N.md`). It tells you how the
Verifier finds a flag, which log records each step, and how to read each log. The last
section is an example diagnosis.

All paths that start with `./` are relative to `.verifier`, your working directory.
All other paths are relative to `$VERIFIER_HOME`.

## 1. How the Verifier finds a flag

1. The Verifier reads the claims in `./claim_register.json`.
2. The gate checks the source of each claim. Provenance checks that the files of the
   source exist and did not change. Recomputation runs the method of the claim, and
   compares the result with the claimed value. A gate flag comes from `provenance` or
   `recompute`, not from a check.
3. The claim checks run on each claim. A claim check applies only to some claims. For
   example, `claim_uncertainty` applies only to a claim that is inferred from other claims.
4. The paper checks run on each paper of the project. `setup_review` and `model_review`
   call a model. The other paper checks are rules in code.
5. Each problem that a check finds becomes one flag in the report, with a flag id such
   as `F-3a9c1e`. Some flags come from no check result: `claim-unreadable` (a register
   entry that the run cannot read), `not-checked`, `CHECK-not-run` (a paper check that
   did not run), `unresolved-input` (an `\input` file that the run cannot read),
   `claim-checks-not-run` (no claim was checked), and the provenance caveats of a claim.

These documents give the details:

- `docs/SPEC.md`, section 2: the flow of one round.
- `docs/SPEC.md`, section 6.20: what each built-in check finds.
- `checks/extension.py`: the list of the built-in checks.
- `docs/SCHEMA.md`, sections 2 to 4: the result of a run, the gate, and the kinds and
  severities of problems.
- `checks/NAME/`: the code of each built-in check.

## 2. The logs, and how to read them

### `./reports/SHA.json` and `./reports/latest.json`

The full result of one run, as JSON. Start here.

- `issues`: one item for each flag. `id` is the flag id. `code` is the kind of problem.
  `detail` is the text of the flag. `disputed` gives the dispute, or `null`.
- `subject` of a flag is one of these:
  - the claim id, as `acc-7b`;
  - the claim id and its value, as `acc-7b (value 68.4)`;
  - the claim id and the claims that it is inferred from, as
    `helps (inferred from acc-7b, gain-7b)`;
  - the path of a paper.
  For a flag of a claim, the claim id is the first word of `subject`. Two flags are
  different: `claim-unreadable` gives the entry, as `entry 3 (acc-7b)`, and
  `claim-checks-not-run` gives `claim checks`.
- `axis` of a flag is one of `integrity`, `missing_inputs`, `soundness`, `presentation`,
  `strength` and `caveat`.
- `findings.claims`: a list, with one item for each claim. Find a claim by `claim.id`.
  `claim` is the claim as the run read it. `provenance` and `recompute` are the results
  of the gate. `checks` gives the result of each claim check, by name.
- `findings.papers`: an object keyed by the path of each paper. Each value has `checks`,
  the result of each paper check, by name.
- A check result has a `type`. The types are:
  - `Passed`: the check found no problem. `measured` can give a number, such as an
    uncertainty.
  - `Flagged`: the check found problems. `problems` lists them, each with its `kind`,
    `detail`, `facts` and, for a paper check, `where` (the section and the excerpt).
  - `NotChecked`: the check did not run. `missing` names the claim key that it needed.
  - `Skipped`: the check did not run, because something before it failed. `because`
    gives the reason.
  - `CheckCrashed`: a plug-in check raised an error. `because` names the error.
  - `Off`: the check was switched off.

To find the result behind a flag:

1. Find the flag id in `issues`. Read its `code` and `subject`.
2. If `subject` is a claim id, find that claim in `findings.claims`. Else, find the paper
   in `findings.papers`.
3. Read the result of the check whose problem has the `kind` that is the `code` of the
   flag. A gate flag has its result in `provenance` or `recompute`.

### `./readings/`

The answers of the checks that call a model. A run saves each answer, and the next run
uses the saved answer again. The paths below are the defaults. The run options
`setup_review_out`, `model_review_out` and `claim_uncertainty_readings` can change them.

- `./readings/setup_review.txt`: the last answer of the setup review, as JSON.
  `findings` lists each problem, with its `code`, `where` (the section), `quote` (the
  words of the paper) and `why`.
- `./readings/setup_review.txt.by-prompt/`: each saved answer of the setup review, in a
  file named by the hash of its prompt. A run reads these files again.
- `./readings/model_review.txt`: the last answer of the model review, when the model
  review is on. It has the same fields.
- `./readings/claim_uncertainty.json`: the readings of `claim_uncertainty`, under
  `readings`. Each key is a hash of the claim, its premises, the red team and the
  quantifier. The premise is the full paper. Each reading has:
  - `without_attack`: how much the paper alone goes against the claim, from 0 to 1;
  - `assessed`: each attack of the red team, with its `breakage` and `believability`;
  - `local`: the uncertainty from the worst attack;
  - `worst`: the attack that gave `local`;
  - `attacked`, `calls`, `red_team` and `quantifier`.
  The report compares the larger of `local` and `without_attack` with the cutoff
  (`uncertainty_cutoff`, default 0.59).
- `./readings/claim_uncertainty_reasons.jsonl`: one line for each scored attack, with
  the `claim` (its statement), the `attack`, the `breakage` and the `reasoning` of the
  model. Find the lines of a claim by its statement. The code marks this file as a test
  aid, so a later version can remove it.
- `./readings/claim_uncertainty_scenarios.json`: `{"scenarios": {hash: text}}`. Each
  text is the worst-case scenario of a flagged claim, as the report shows it.

### `./rounds/N.json`

The record of round N. `claim_steps` lists each step that ran before and after your
round, with its command (`step`), its exit code (`exit`) and the first line that it
printed (`said`).
Use it to find how a claim got its statement, its links and its quotes.

### `./runtime/alerts.jsonl`

The alert log of the Verifier's own processes. Each line is one JSON record, with `ts`,
`time`, `level`, `key`, `process`, `pid`, `project`, `verifier_commit`, `message`,
`error_type`, `traceback` and `details`. The levels are:

- `log`: a usual event;
- `warn`: a failed attempt, which the Verifier tried again;
- `error`: a failure, after which the process continued;
- `fatal`: a bug, which stopped the process.

A `warn` or an `error` near the time of the run can explain an unusual result. For
example, it can show a model call that failed and was tried again.

### Do not read

`./cache/` holds stored check results and indexes, to make runs faster. It explains no
flag.

## 3. Run one check again

`vcheck test-check NAME --claim ID` runs one claim check on one claim, and prints its
result. Use it to see if a flag comes again. A check that calls a model costs money, so
run it only when the logs do not explain the flag.

## 4. An example diagnosis

The project has one table, `tab:acc`. No sentence names `tab:acc` with `\ref`. One
sentence has `\ref{tab:missing}`, and no label `tab:missing` exists. The person disputed
the `orphan-float` flag, with the reason "Table tab:acc is the only table, and the text
discusses its numbers directly."

    ## Flags

    - `F-044c1f`: type `orphan-float`, axis caveat, subject `paper.tex`, detail
      "tab:acc is never referenced in the text".

    ## Why the Verifier flagged them

    The `orphan_float` paper check collects each `\label` of a table or a figure, and
    each `\ref` of the paper. It flags each label that no `\ref` names. In `paper.tex`,
    the only table has the label `tab:acc`. The only `\ref` names `tab:missing`, which
    is not a label. Thus the check flagged `tab:acc`. The `dangling_ref` check flagged
    `tab:missing` separately.

    ## Is each flag false

    - `F-044c1f`: partly. The detail is true, because no `\ref{tab:acc}` exists. But the
      flag gives no new information to the reader. The paper has one table, and the
      sentence before the table states its numbers. The real problem is the wrong label
      in `\ref{tab:missing}`, and the `dangling-ref` flag already reports it.

    ## Cause

    the-checks

    ## What to change in the Verifier

    Do not flag an orphan float when the paper has only one float of that kind, and the
    text states the numbers of that float. When a dangling reference and an orphan
    float have similar names, report one flag for a wrong reference, not two flags.
