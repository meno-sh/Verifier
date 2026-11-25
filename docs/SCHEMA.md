# The Verifier's schema, in plain words

This guide explains what the Verifier reads, what it produces, and how a verdict is
reached, without needing the code. The code is in `schema/` (the types) and
`core/registry/checks.py` (how checks plug in). Where this guide and the code differ, the
code is right, and this guide should be fixed.

## 1. What a run reads

A run reads up to two things.

- **A paper**: one or more `.tex` or `.md` files, read as one paper. The paper checks
  read this, through the reader for each file's format (`utils/extract.py`). The
  consistency checks also read the labels of the other documents. The setup review also
  reads the setup register, and both reviews can read a saved reading that an option
  names. It
  is optional: the real-time verifier runs on a research working directory with no
  paper.
- **A claim register**: a JSON file listing the claims to check. Most claims are one
  number someone wrote down, with where it came from. A claim can also be inferred
  from other claims in the same register, such as a general conclusion that rests on
  several results.

One claim in a register, from a made-up study of whether retrieval helps a language
model answer factual questions (the README uses the same study):

```json
{
  "id": "retrieval-results/accuracy-with-retrieval",
  "statement": "With retrieval, the 7B model answers 68.4% of the questions correctly.",
  "value": 0.684,
  "origin": "notes/retrieval-results.md",
  "seeds": 3,
  "source": {
    "kind": "artifact",
    "path": "runs/retrieval/answers_seed*.json",
    "method": "accuracy_with_retrieval",
    "sha256": {"answers_seed0.json": "a16cb4b1..."}
  }
}
```

- `id` names the claim. `value` is the number as written, kept with its decimals,
  so `0.50` is checked to two places. It is required for an `artifact` or `document`
  source, and optional for a claim with a `claims` source or with no source.
- `statement` is the claim in words. It is required for a claim with a `claims`
  source, and for every claim that another claim names in its `claims` source.
- `origin` is the note the claim was written in. It is optional.
- `superseded_by` names a later claim that replaces this one, for example after the
  note's number was corrected. A superseded claim is listed in the report and not
  checked. If it names no other readable claim, the claim is unreadable.
- `source` says where the number comes from. It is one of three kinds:
  - `artifact`: a data file the number is **recomputed** from, with the
    computational `method` that recomputes it. Methods are written for the
    workspace, in method files (section 6), and the schema says nothing about how
    the data is stored.
  - `document`: a log the number is only **traced** to. The tool checks the log
    still contains the exact text in `must_contain`, and `why` must give one of four
    accepted reasons for not recomputing.
  - `claims`: other claims in the same register that this claim is **inferred**
    from, as `{"kind": "claims", "claims": ["id-1", "id-2"]}`. Such a claim is a
    derived claim. Each id must name another readable claim that is not superseded.
    So the register holds a graph of which claims support which. A cycle in it is
    not refused here; the check `acyclic_support` reports it.
- `sha256` is the **pin**: the checksum each file had when the claim was added. A
  later change to the file is caught by it. The key of each checksum is the path of the
  file from the folder before the first wildcard of `path`. For `runs/*/metrics.json`,
  the keys are `a/metrics.json` and `b/metrics.json`. When only the file name has a
  wildcard, the key is the file name.
- `tol` and `setup` are keys of the schema too. The keys `created`, `last_updated` and
  `quotes` are bookkeeping of the claim steps: the parser accepts them, and the parsed
  `Claim` does not hold them. The entries of the register keep them as written.
- Every other key, such as `seeds` above, `baseline` or `compare_to`, belongs to a
  claim check. Each check declares the keys it reads and their types, and the parser
  accepts exactly those, so the schema knows none of them. A parsed claim keeps them
  in `Claim.fields`. `vcheck fields` lists them for the checks a run has.

**Paths.** Every path in a register is relative to the register's own folder, and is
read from there wherever the program runs. An absolute path such as `/data/jobs/...`
makes that claim unreadable, because it names a file on one machine only. A caller
may name a different base folder: the real-time verifier keeps its register in
`.verifier/` and reads paths from the working directory.

**Strictness.** An entry the tool cannot use as written becomes an *unreadable claim*,
with the reason, and the rest are still read. Examples are an unknown key (a
misspelled `seed`), a wrong type, an unknown method, an absolute path, a repeated
id, a `superseded_by` naming no other readable claim, or a `claims` source naming a
claim that is missing, unreadable, superseded or without a `statement`. A claim that
rests on an unreadable claim is unreadable too. An unreadable claim makes the verdict
MISSING INPUTS.

## 2. What a run produces: the Findings tree

A run returns one `Findings` record.

```
Findings
  inputs             what was read: papers, register, base folder, tool version
  papers             {paper path: PaperFindings}
    unreadable_inputs  \input files that could not be read
    facts              counts the paper groups report, e.g. {"tables": 4}
    checks             {check name: result}       e.g. "dangling_ref", "model_review"
  claims             one ClaimFindings per readable claim
    claim              the claim as parsed
    provenance         is the file there and unchanged?        (the gate, part 1)
    recompute          does the number come back from the data?  (the gate, part 2)
    checks             {check name: result}       e.g. "seeds", "plugin_check:leakage"
  unreadable_claims  register entries that could not be read
  superseded         claims replaced by a later claim: listed, not checked
  must_run           checks that were asked for and must not be skipped
  claim_checks_not_run_because  why no claim was checked, when the claim register
                     could not be built; else none
```

Every result in `checks` is one of six things:

```
Passed       ran and found nothing wrong
Flagged      ran and found problems; holds a list of Problem
             (both can hold `measured`, a number the check measured, such as an
             uncertainty; it is None for most checks)
NotChecked   could not run because the claim lacks a field it needs
Skipped      did not run because something before it failed (with the reason)
CheckCrashed a plug-in check raised an error; a kind of Skipped, with the error
Off          was switched off
```

A check that does not apply to a claim gives no result at all, so it is not a
caveat. A check that reads the recomputed number does not apply to a derived claim, and a
check that reads the claims a claim is inferred from applies only to a derived claim.

A `Problem` has a **kind**, a one-sentence **detail**, and, for paper problems,
where in the paper it is.

**Why `checks` is a mapping and not fixed fields.** The tree does not know which
checks exist. Built-in checks, plug-in checks written for one workspace (named
`plugin_check:...`), and review checks all sit side by side under their names. Adding or removing a check changes no type in `schema/`.

Example, from one claim in the test fixture:

```json
"provenance": {"type": "Intact", "path": "runs/main/results.json"},
"recompute":  {"type": "Matches", "value": 0.205, "n": 4, "interval": null, "tolerance": 0.001},
"checks": {
  "seeds": {"type": "Flagged", "problems": [
    {"kind": "too-few-seeds", "detail": "only 1 seed(s); the text must not claim reliability"}]},
  "caption": {"type": "Passed"}
}
```

## 3. The gate: provenance and recompute

Every claim first passes through two steps that no plug-in check can replace.

**Provenance** gives one result, in one of four groups:

```
Fine         Intact               the file exists and matches its pin
             Traced               traced to a log on purpose, and the text is still there
             Derived              inferred from other claims, so there is no file
Provisional  ProvisionallyIntact  the file changed, but matches a checksum a person has
                                  been asked to confirm (proposed_sha256)
Caveat       NoSource             the register gives no source
             SelfTraced           traced only to the note the claim was written in
Broken       Missing, Changed, AnchorMissing
```

Each result is a subclass of `ProvenanceResult`, and each class sets its group
(`ProvenanceCategory`). The verdict reads the group, never the class.

Provenance only looks at files: that they exist, and their checksums. It never opens
them. A derived claim's gate is always Derived. If a claim it rests on fails its own
gate, that claim already makes the verdict REJECT.

The claims are checked in dependency order: each claim after the claims it is inferred
from, so a claim check can read their findings. Claims on a cycle, and claims that rest
on one, are placed in a greedy order. Some of them come before a claim that they rest on,
and do not see its findings. The tree keeps register order.

**Recompute** runs when provenance is Intact or ProvisionallyIntact. It calls the
claim's method with the claim's path. If the claim states a `setup`, it compares it
with the setup the method reports. Then it compares the number with the claim's
value, to the precision the claim was written with:

```
Matches       the number came back (with n, and an interval if the method gives one)
Broken        Disagrees     the data gives a different number
              NotANumber    the method returned NaN (no usable data)
              MethodFailed  the method crashed, or returned something that is not a Computed
              SetupDisagrees   the method's setup differs from the claim's
              SetupUnrecorded  the claim states a setup the method did not report
Skipped, Off  did not run, or was switched off
```

Each result is a subclass of `RecomputeResult`. A broken result sets `is_broken`.
`Skipped` and `Off` are also check results, so they are subclasses of `CheckResult` too.

## 4. Problem kinds and severities

Every problem kind has a **code** (such as `too-few-seeds`) and a **severity**. There
are four severities:

```
soundness      the number may not measure what     -> BLOCK UNTIL FIXED
               the claim says (e.g. the run behind
               it was not the one described)
presentation   the text misdescribes a result      -> BLOCK UNTIL FIXED
strength       the result is weaker than stated    -> PASS WITH DISCLOSURE
caveat         the wording may say more than the   -> PASS WITH CAVEATS
               evidence (e.g. "significantly" with
               no test; found by words alone)
```

There is no "integrity" severity. A problem a check reports never rejects: only an
unreadable register entry or the gate (provenance and recompute) can.

Each check declares its own kinds when it registers. There is no central list.
The registry refuses:

- a kind without a real severity;
- a code that another check already uses;
- a check that reports a kind it did not declare (a built-in check stops the run;
  a check the agent wrote gets a Skipped result instead).

So every problem reaching the verdict has a severity, and only its own check can
report it.

## 5. The verdict

The verdict is computed from the tree, never stored in it. The worst condition wins:

```
MISSING INPUTS        an \input file of the paper, or a claim register entry, could not
                      be read
REJECT                a Broken provenance or recompute result
BLOCK UNTIL FIXED     a soundness or presentation problem
MISSING INPUTS        a check in must_run was Skipped (a requested model review or setup
                      review that gave no reading), or claim_checks_not_run_because is set
PASS WITH DISCLOSURE  a strength problem
PASS WITH CAVEATS     a caveat problem, a check NotChecked or Skipped, or a claim with a
                      Caveat or Provisional provenance result
PASS                  none of the above
```

Counts never matter. One broken claim is enough to reject, and no number of strong
results can make up for it.

## 6. How checks plug in

All checks and computational methods live in a **registry**. The core runs whatever
the registry holds and names none of them.

```python
TOO_FEW_SEEDS = ProblemKind(code="too-few-seeds", severity=Severity.STRENGTH)

class SeedsCheck(interface.ClaimCheck):   # from checks import interface
    name = "seeds"
    title = "Enough seeds"
    why = "Flags results resting on one or two seeds."
    problem_kinds = (TOO_FEW_SEEDS,)
    fields = read_only({"seeds": FieldType.INTEGER})
    needs = ("seeds",)

    def check(self, claim, recomputed, context):
        ...

@method("accuracy_with_retrieval")       # in a method file of .verifier/methods/
def accuracy_with_retrieval(path):
    ...                                   # reads the data at path, returns a Computed
```

A claim check is a subclass of `ClaimCheck`, a paper check group a subclass of
`PaperCheckGroup`, and a plug-in check a subclass of `PluginCheck`. A check imports these
from `checks/interface.py`, the one module of `checks/` that imports the core. The
extension `checks/extension.py` (`BuiltinChecks`) adds one object of each built-in check
to the registry, in report order. The core loads it by its name, and imports no check.

- `fields` declares the claim keys the check reads, each with a `FieldType`
  (integer, number, boolean, text, text list, object, or path; a path is read
  relative to the register's folder, like every other path). Two checks may read
  the same key only with the same type, and no check may declare a key the gate
  uses (`id`, `value`, `source`, `tol`, `setup`, ...).
- `needs` lists the declared keys the check cannot run without; the claim check runner
  (`core/analysis/claim_checker.py`) gives NotChecked when one is missing.
- `needs_recompute=True` means the check reads the recomputed value; the claim check
  runner gives Skipped when there is none. It does not apply to a derived claim.
- `needs_dependencies=True` means the check runs only on a derived claim. It reads
  the findings of the claims the claim is inferred from in `context.dependencies`.
- A claim check gets the claim, the recomputed value (`Matches` or None) and a
  `ClaimContext`: `register`, `dependencies` (the findings of the claims a derived claim
  is inferred from), `options` (the run's options; each check reads its own keys) and
  `full_paper_text` (None when the run has no paper). The input scope of the check
  limits what it gets. With the scope `WHOLE_REGISTER` (the default), the check gets
  all of it. With another scope, `register` holds only the claims of the scope,
  `dependencies` is filled only for `PREMISES`, and `full_paper_text` is None unless
  the scope sets `reads_full_paper_text`.
- A paper check group registered with `must_run_if_requested=True` is put in
  `must_run` when it is on (see the verdict).
- A method gets the claim's `path`, resolved against the base folder, and returns
  `Computed(value, n, interval=None, setup=None)`. How it reads the data is its own
  business. A claim recomputed from data needs a method file of a watched project, in
  `.verifier/methods/`. Each method runs in its own
  process (core/methods/computational_methods.py). The `verifier static-check` command has no method.

The registry is built from four places:

```
built in            checks/                       claim and paper checks only, added by
                                                   the extension checks/extension.py
plug-in checks      .verifier/checks/             a watched project only
                                                   (core/registry/plugin_checks.py; untrusted,
                                                   checks named "plugin_check:<name>")
methods             .verifier/methods/            a watched project only
                                                   (core/methods/computational_methods.py)
extensions          core/registry/extensions.py             checks an extension adds, such as
                                                   human review (review/)
```

Paper checks run in groups (consistency, registered as `checks`; science; model review;
setup review), registered the same way with their kinds. Each group fills one result per
kind, or one result for the whole group in the case of model review and setup review.
