# SPEC: the state of the Verifier code

This file gives the structure of the code: the layers, the flow of one round, the files of a
watched project, the commands, the claim steps that code runs, the details of use that README.md
leaves out, and the major classes of each layer.

The text is written by hand. The class blocks come from the code. `docs/spec_signatures.py`
writes them, and `tests/test_spec.py` fails when a block does not match the code. After a
change to a major class, run `uv run python docs/spec_signatures.py` and commit the result.

A class is major when 3 or more other classes use it or inherit from it. A short list in
`docs/spec_signatures.py` adds the classes with an important role and fewer users. A block
shows the fields of a class with their types, and its public methods with their signatures.
The comment above a method is the first line of its docstring.

## 1. The layers

From the bottom up:

1. `schema/`: the records that each step gives to the next step. A record never changes.
2. `utils/`: general functions: files, git, models, text, numbers, alerts.
3. `core/`: the checks machinery, the gate, the analysis, the report, and the `vcheck`
   commands of the auditing agent.
4. `checks/`: one registered check in each folder.
5. `runtime/`, `review/` and `diagnosis/`: the daemon, the git hook, the agent, and the
   two extensions.
6. `web/`: the local page of `verifier web`. It runs the `verifier` commands in child
   processes.

The rules, which `tests/test_layers.py` checks:

- A lower folder never imports a higher folder.
- A check reaches the core only through `checks/interface.py`.
- A check never imports another check.
- Only `checks/extension.py` names a registered check.

## 2. The flow of one round

1. A commit moves the tracked branch. The git hook writes an event to the inbox, and returns.
2. The daemon (`runtime/daemon.py`) finds each new commit, moves its own checkout to the
   branch tip, and brings the claim files in step with the notes.
3. The daemon runs the claim steps in code: it drops clear non-claims, labels the rest,
   groups the quotes into classes, and writes the register (section 5). Then it asks the agent session
   policy if the agent gets a new session, and sends the events to the auditing agent as
   one round.
4. The auditing agent gives the claims their sources. It finds them with
   `vcheck shortlist-sources`, checks them with `vcheck check-register`, and gets methods
   with `vcheck write-methods`.
5. When the round ends, the daemon runs `vcheck reorder-claims`: a model sorts the claims,
   the most valuable to a reader first. Then it runs `vcheck link-claims`: a model reads the
   full paper and links each claim with no source to the claims it rests on (stage 5). It
   drops a link to a claim before the claim in the new order. Then it runs
   `vcheck run`. The gate checks the source of each
   claim (provenance) and recomputes its number. Then the claim checks and the paper
   checks run. A claim check gives its stored result when nothing it reads changed.
6. `vcheck run` builds the report, saves it as Markdown, Typst, PDF and JSON, and prints
   the verdict.
7. The daemon stamps the claim files when their checks pass, and commits the state folder.

## 3. The files of a watched project

Everything is in `.verifier/`, which has its own git history:

| Path | What it holds |
|---|---|
| `claim_candidates.json`, `claim_quotes.json`, `dropped.json`, `claim_equiv_classes.json`, `claim_register.json` | the five claim files |
| `claim_scope.json` | the notes that a person selected for claims |
| `options.json` | the run options |
| `extensions.json` | the names of the loaded extensions |
| `methods/`, `checks/` | the computational methods and the plug-in checks |
| `method_needs.json`, `source_shortlists.json` | the input of `write-methods`, and the output of `shortlist-sources` |
| `reports/` | one report for each checked commit, and the latest |
| `rounds/` | one record for each round of the agent |
| `reviews/` | the review requests and their answers |
| `AGENTS.md`, `WORKER.md`, `opencode.json` | the guide of the auditing agent, the guide of the worker's agent, and the OpenCode settings |
| `checkout/`, `inbox/`, `jobs/`, `timers/`, `runtime/`, `cache/` | not committed: the checkout, the events, the long jobs, the timers, the daemon files, and the caches |

## 4. The commands

`verifier`, for a person:

| Command | What it does |
|---|---|
| `start` | prepare a working directory, start its daemon, and open the terminal view |
| `init` | prepare a working directory as `start` does, but start no daemon |
| `static-check` | check a paper and its claim register once, and print the result |
| `start-if-pending` | start the daemon of each working directory where work waits |
| `attach` | open the terminal view of OpenCode on the session of the verifier |
| `stop` | stop the daemon, which stops the OpenCode server |
| `uninstall` | stop the verifier and remove its hook; keep `.verifier/` |
| `status` | the state of the daemon, the round in flight, the jobs and the latest verdict |
| `report` | the latest report, as text or as JSON |
| `diff` | what changed from the report of one commit to the latest report |
| `alerts` | the alert log, newest first, for developers |
| `web` | serve the local page, where a person uploads a paper and reads its report |
| `review`, `decide` | the review extension: read and answer the requests |
| `dispute`, `diagnosis` | the diagnosis extension: dispute a flag, and read the diagnosis |

`vcheck`, for the auditing agent:

| Command | What it does |
|---|---|
| `validate` | load each plug-in check and method, and parse the claim register |
| `run` | check every claim, save the report, and print it (`--no-cache` runs every check again; `--claim-checks-not-run REASON` checks the papers only) |
| `test-check`, `test-method` | run one check or one method on one claim |
| `fields` | the optional claim keys that the checks read |
| `drop-non-claims`, `label-candidates`, `check-quotes`, `propose-classes`, `accept-classes`, `fill-register` | the claim steps that the daemon runs before the agent's round (5.3) |
| `check-register` | the check the agent uses after it gives sources: it checks the register and pins new artifact claims |
| `shortlist-sources` | the likely source files of each claim with no source |
| `write-methods` | write recomputation methods, blind to the claimed values |
| `clear-cache` | remove the stored results of the claim checks and the methods |
| `ask`, `withdraw` | the review extension: ask a person, or withdraw a request |
| `check-diagnosis` | the diagnosis extension: check a diagnosis file |

## 5. Stages 2 to 4 in code

### 5.1 Terms

| Term | Meaning |
|---|---|
| notes | the text files of the watched project, such as a paper's LaTeX or Markdown notes, and its commit messages |
| claim | a statement of the notes that could be checked and found false, such as a result or a conclusion |
| candidate | a piece of the notes that may hold a claim. Stage 1 (`utils/extract.py`) cuts the notes into sentences, whole tables, captions and equations. A table is one candidate, never cut, with all its rows; one above 20,000 characters is a `papers.long_table` warning. |
| quote | words copied exactly from a candidate. A kept candidate becomes one or more quotes. |
| drop | a candidate that is not kept, with a reason |
| class | a group of quotes that state the same claim |
| register | the list of claims, one for each class. A claim has an id, a statement and its quotes. |
| link | a mark that a general claim rests on a more specific claim |
| agent | the auditing agent: a model that works in the project through OpenCode |
| round | one batch of new commits. The daemon prepares it, the agent works on it, and the checks run. |
| claim step | one `vcheck` command that the daemon runs before it wakes the agent |
| brought in step | the claim files updated for a new commit: new candidates added, and the quotes and claims of removed text removed |
| keep score | the labeller's score from 0 to 100 for how much a candidate should be kept. Scores are recorded in the round's `keep_scores`. |
| proposal | the classes that `propose-classes` writes to `.verifier/tmp/class-proposals-<commit>.json` |
| Claude account route | running every model call through Claude Code (`VERIFIER_MODEL_BACKEND=claude-cli`) instead of OpenRouter (`utils/models.py`) |

The messages of the claim steps number the stages from QUOTES, so their "stage 1 (QUOTES)"
is stage 2 here.

### 5.2 What runs

The claims are made in four stages. Code does stages 1 to 4 before the agent's round.
Code does stage 5, the links between claims, after the agent's round.

| Stage | Question | Output file | Claim steps (in order) |
|---|---|---|---|
| 2. Quotes | Keep or drop each candidate? | `claim_quotes.json`, `dropped.json` | `drop-non-claims`, `label-candidates` |
| 3. Classes | Which quotes state the same claim? | `claim_equiv_classes.json` | `check-quotes`, `propose-classes`, `accept-classes` |
| 4. Register | One entry for each claim | `claim_register.json` | `fill-register` |
| 5. Links | Which claims does each claim rest on? | `claim_register.json` (order and `source.claims`), `claim_order.json`, `full_paper_links.jsonl` | `reorder-claims`, `link-claims`, after the agent's round |

`check-quotes` belongs to stage 3 here. Stage 3 runs only if it passes, so code makes
classes only from a finished stage 2.

Why code does these stages. In our tests the code path never left a register empty, cost
less than half as much per paper, and found as many known errors as the agent doing them.

### 5.3 The claim steps

| Command | Module | Calls a model |
|---|---|---|
| `drop-non-claims` | `utils/candidate_rules.py` | no |
| `label-candidates` | `core/claim_steps/labeller.py` | yes, Haiku |
| `check-quotes` | `utils/claim_files.py` | no |
| `propose-classes` | `core/claim_steps/duplicates.py` | yes, Haiku |
| `accept-classes` | `utils/claim_files.py` (`accept_proposal`) | no |
| `fill-register` | `utils/claim_files.py` (`fill_register`) | no |
| `reorder-claims` | `core/claim_steps/claim_reordering.py` | yes, Haiku at effort high |
| `link-claims` | `core/claim_steps/full_paper_linker.py` | yes, Haiku, with the full paper |

`drop-non-claims` drops candidates that are clearly not claims, such as headings, author
lines and thanks. It has 14 rules, R1 to R14. Each drop's reason is `auto:Rn <name>`. The
repeat rules name another candidate. R14 drops a later copy of a sentence, as in
`auto:R14 repeat of <id>`, and the drop stands while the first copy has any quote. R13
drops an equation whose text is inside a sentence, and the drop stands while a quote of
that sentence holds the whole equation.

`label-candidates` asks a model about each candidate that has no answer yet. The model
gives a keep score from 0 to 100, a label (claim, setup, background or junk), and the
values that the candidate reports (`labeller.VALUES_RULE`): not a condition such as n or a model size, an error bar, or a
cited number. A candidate scored at or above the cutoff (`label_cutoff`, 50) is kept. It
gives one quote for each value, each with the whole text of the candidate and its own
`value`: sister quotes. A kept whole table is one quote with `value` null: one claim. A
kept candidate with no value gives one quote with `value` null. A listed
value that the text does not print is left out, with a warning.
A kept candidate labelled setup goes to the setup register (`setup_register.json`), with
the drop reason `auto:setup <score>`. A candidate scored below the cutoff is dropped with
the reason `auto:label <score>`.

`check-quotes` checks that stage 2 is done. Every candidate has a quote or a drop, and
every quote is found exactly in its candidate. A value is printed in its quote, and no
two sister quotes have the same value.

Every reader of a quote or a claim gets it from one function
(`extract.value_marked_text`, through `claim_files.quote_text` and `claim_text`). It
marks the entry's own value as `{{value}}`. For the method writer it is anonymized:
the own value is `{{VALUE}}`, and each sister value is `*redacted*`. The files keep the
entries as they are.

`propose-classes` finds quotes that state the same claim. For each new quote it takes the
most similar quotes and earlier classes, by shared words and rare numbers. Each quote is sent as `quote_text` gives it. A quote of a whole
table skips the model and becomes a class of its own. Two items whose values differ by more than
1%, and by more than 2 steps of the cruder last printed digit (`claim_files.same_value`),
are never joined, and the prompt says that sister quotes are different claims. Then one model
call judges each 2 new quotes against those neighbours. A failed call can miss a duplicate
but never adds one. If more than 10% of the calls fail, the step fails, since the classes
would then have too many quotes left alone.

`accept-classes` takes the proposal as it is. A group of new quotes becomes a new class. A
group that holds an earlier class adds its new quotes to that class. Old classes are never
merged. A new quote in no group becomes a class of its own. The step then runs the stage 3
check, which refuses a class whose quotes have two values.

`fill-register` writes one claim for each class that has none. The statement is one quote
of the class. A sentence wins over a whole table, a caption or an equation. Among
sentences, the one that lacks the fewest of the numbers the other sentences print wins,
and then the shortest. Table numbers and citation years do not count. A class with no
sentence gets its first quote. A class of a whole table gets the table's name, caption
and columns, at most 5,000 characters (`TABLE_STATEMENT_CHARS`). The claim gets the value of its quotes,
so sister claims have the same statement and different values. The step then runs the
stage 4 check. The agent cannot change a statement that this step wrote.

`reorder-claims` sorts the claims that have a statement with a quicksort. Each split shows
the model a JSON list of the claims, each with a short id and `ranks_in_first_half` set to
null. The model marks the claims of the first half. A claim that is high-level, general and
of direct, significant value comes first. No claim of the second half may rest on evidence
from a claim of the first half. The first half must be of more interest to a casual reader.
The reply must give each id once, in order, with only the two fields. It must mark between
one third and two thirds of the claims, rounded to allow more, and each half keeps one
claim. A refused reply is a `warn`, and the model is asked again. 5 refused replies in a row
are `fatal`. The two halves of each split go into one thread pool
(`claim_reordering_pool_size`, 64), and no split waits for its halves. The step writes the
register in the new order, with the claims with no statement last. It skips the model when
the register is in the order of the last sort (`claim_order.json`).

`link-claims` drops each link from a claim to a claim before it in the register. The first
5 drops of a run are each a `warn`.

### 5.4 When a step fails

- A failed step writes an `error` record, `daemon.claim_step_failed`. The steps after it
  do not run that round.
- The agent cannot fix a failed step, so the daemon does not wake the agent. The link
  steps and the stamp of the claim files do not run.
- The daemon runs `vcheck run --claim-checks-not-run REASON`. The reason names the failed
  step and the first line of its output. This run checks the papers only. Its report has
  one flag, `claim-checks-not-run`, with the reason, and the verdict is MISSING INPUTS.
- The daemon saves the round with this verdict, an `error` and `papers_only`, and commits
  the state. It writes the `error` record `daemon.round_without_claims`.
- The note "the previous round was stopped for running too long" skips papers-only rounds.
  So the agent learns of its aborted round also after a papers-only round.
- The events stay in the inbox. They are sent again after the wait of a failed round: 60 s
  first, then two times longer after each failed round, up to 1 hour.
  The next round runs all the steps again, so they answer what this round left.
- A failed `reorder-claims` writes `daemon.link_step_failed`, and `link-claims` does not run
  that round, so no link goes against an old order.

So a failed label call leads to this. `label-candidates` writes no labels and fails. The
drops of `drop-non-claims` stay. The classes and the register stay as they were. The
answers already paid for stay in the cache, and the next round asks only for the rest.

### 5.5 When the register is empty

| Cause | Sign |
|---|---|
| the agent broke a claim file, for example with bad JSON, so no step ran | a `daemon.claim_files` log line that says "not brought in step" |
| a claim file is missing or in an old format | the daemon stopped with `daemon.claim_files_unreadable` |
| a claim step failed (5.4) | a `daemon.claim_step_failed` error, and a report with the flag `claim-checks-not-run` |
| the daemon was stopping | a `daemon.claim_step` log line, "the daemon is stopping" |

To look, read `rounds/<n>.json` (`claim_steps`), run `verifier alerts`, and run
`vcheck check-register`. To run one step again by hand, run
`VERIFIER_STATE=.verifier VERIFIER_ROOT=.verifier/checkout vcheck <step>` in the project.
The daemon runs each step with these two variables.

### 5.6 The options

All options are set with `verifier init` or `verifier start` as `--option key=value`, and
saved in `.verifier/options.json`.

| Option | Default | Set it to | To |
|---|---|---|---|
| `label_examples` | `true` | `false` | leave out the fixed labelled examples of `label-candidates` (5.8) |
| `want_setup_review` | `true` | `false` | turn off the setup review: one Opus call that reads the paper with the setup register, about $1 a paper |
| `claim_steps_timeout` | `1800` | a number of seconds | change how long one step may run |

A value below 1 for `claim_steps_timeout` stops the daemon at the next round with
`daemon.options_invalid`.

### 5.7 What stage 2 keeps

The labeller keeps the claims as quotes: results, comparisons, findings in words and
conclusions. It sends each specific setup fact (model, data, metric, settings,
definitions, notation, equations, method steps) to the setup register, which the
`setup_review` check reads (on by default). It drops the rest, such as headings, pointers,
background, plans and thanks. The rule is `labeller.KEEP_RULE`, with the examples of
`core/claim_steps/label_examples.json`.

A drop's reason `repeat of <id>` must name a quote that exists, and an `auto:` reason must
be what the rules or the labeller gave that candidate.

### 5.8 The labeller and grouping options (`label_*`, `duplicates_*`)

| Option | Default | Meaning |
|---|---|---|
| `label_model` | Haiku 5.5 | the model asked |
| `label_effort` | `off` | its reasoning effort |
| `label_batch` | `1` | candidates in one call. Larger batches lost known errors in our tests. |
| `label_cutoff` | `50` | the lowest keep score that is kept |
| `label_examples` | `true` | put the examples in the prompt, with their answers |
| `label_cache` | `true` | reuse stored answers. `false` asks again and stores nothing. |
| `label_seed` | `0` | the seed of the order the candidates are sent in |
| `label_timeout` | `120` | seconds for one reply |
| `duplicates_model` | Haiku 5.5 | the model `propose-classes` asks |
| `duplicates_effort` | `medium` | its reasoning effort |
| `duplicates_timeout` | `180` | seconds for one reply |

Each labeller call sends the candidate with its kind, its section and the sentence before
it. The reply gives a keep score, a label (claim, setup, background or junk) and the
values.

The labeller stores each answer in `.verifier/cache/label_answers.json`. The key is built
from the candidate and its context, the prompt version, the model (on the Claude account
route, also the Claude model the call maps to), the question, the table
mode, the batch size, the effort, and a hash of the examples file when the
examples are used. A rerun with the cache on gives the same files. A change to any part of
the key asks again.

On the Claude account route (`VERIFIER_MODEL_BACKEND=claude-cli`), the model of each call
comes from its role. `VERIFIER_CLI_MECHANICAL_MODEL` sets it for the labeller and the
grouping (`utils/models.py`).

### 5.9 How the daemon runs a round's steps

Each round, the daemon (`runtime/daemon.py`, `Daemon.deliver`) does this. Items 3 to 5
are in `Daemon.do_claim_steps`.

1. It brings the claim files in step with the new commit. If a file is missing or in an
   old format, the daemon stops (`daemon.claim_files_unreadable`).
2. It writes `.verifier/AGENTS.md`: the template and the section of each extension. Each
   section is one wake-up scenario: an event line, its input and its output. When the
   file changes, the prompt tells the agent to read it again.
3. If the agent broke a claim file, no step runs, and the prompt asks the agent to fix it.
   Otherwise it reads `claim_steps_timeout` from `options.json`. A bad value stops the
   daemon (`daemon.options_invalid`). An edit to `options.json`
   takes effect at the next round.
4. It runs each step as a `vcheck` command, with `claim_steps_timeout`. A step past the
   timeout counts as failed.
5. It records each step in `rounds/<n>.json` under `claim_steps` (command, exit code,
   first line of output), and as a `daemon.claim_step` log line.
6. If a stop is requested, no further step runs. The agent is not woken, and the events
   wait in the inbox (`daemon.round_held`).
7. It wakes the agent with the round. The round line gives the time limit of the round
   (`--round-timeout`, 120 minutes by default). The daemon stops a round that runs longer,
   and the next round starts with the line "The previous round was stopped".

The claim steps and `vcheck run` get the Claude login token when it is set
(`Daemon.step_env`). The agent's shell does not get it. So on the Claude account route a
model command that the agent runs itself is refused.

### 5.10 Where to change things

| To change | Edit |
|---|---|
| the order of the steps | `CLAIM_STEPS` and `LINK_STEPS` in `runtime/daemon.py` |
| a new step | its command in `core/commands/` and `core/cli.py`, and its place in `CLAIM_STEPS` in `runtime/daemon.py` |
| a rule that drops non-claims | `_first_rule` in `utils/candidate_rules.py`. It reads sentences and captions; equations and whole tables get none. The first rule that matches wins, and the repeat rules R13 and R14 run after the others. Give the next number, as in `auto:R15 <name>`. Drop only text that `labeller.KEEP_RULE` drops. Add a test to `tests/test_candidate_rules.py` with one text the rule drops and one it keeps. |
| the labeller's prompt | `KEEP_RULE` in `core/claim_steps/labeller.py` |
| after any prompt change | raise `PROMPT_VERSION` in `core/claim_steps/labeller.py`, so old answers are not reused. Then run `tests/test_labeller.py`, and rerun one test paper to compare what is kept. |
| the labeller's examples | `core/claim_steps/label_examples.json` |
| what the agent is told | `runtime/templates/AGENTS.md` |
| how a claim's statement is picked | `claim_files.statement_of` |

## 6. Use in detail

README.md gives only the commands, their options and the checks. This section gives the
details of each.

### 6.1 Install

| Item | Detail |
|---|---|
| Install mode | `install.sh` makes an editable install. A change to the checkout has an effect without a new install. |
| Projects | One install can watch many projects at the same time. |
| Platforms | Linux (x86-64, ARM) and macOS (Apple silicon, Intel). |
| Commands | `~/.local/bin` |
| Environment of the commands | the uv tool folder, `~/.local/share/uv/tools/verifier`. Thus a new install replaces an earlier install, from this checkout or from a different checkout. |
| OpenCode | `~/.local/share/verifier` |
| Development | `uv run` makes the environment of the checkout (`.venv`) the first time. The tests and ruff use it: `uv run tests/test_core.py`, `uv run ruff check .`. |

### 6.2 Run options

- `--option KEY=VALUE` saves a run option in `.verifier/options.json`.
- Each command of the project reads its options from that file only. Thus the Verifier,
  the auditing agent and your commands always use the same options.
- A later `start` or `init` changes only the options that it gets. It keeps the other
  options.

### 6.3 The claim selection

- The first `verifier start` asks for the files and folders that contain the notes.
- First, it shows the number of candidates in each top-level folder of the tracked
  branch. A candidate is a sentence, a full table, a caption or an equation that possibly
  states a claim.
- After your answer, it shows the number of candidates in your selection. It also shows
  an estimate of the cost and the time of the first round.
- Then you start, select again or stop.
- The estimate uses approximate rates for the default models, in
  `runtime/project/cost_rates.json`. A smaller selection costs less.
- `--claims-from PATH...` gives the selection, and `start` does not ask. An example is
  `--claims-from notes paper/main.tex`. `--claims-from .` selects all notes.
- `start` saves the selection in `.verifier/claim_scope.json`. A later start keeps it.
  A new `--claims-from` replaces it.
- Commit messages always give candidates.
- `verifier init` does not ask a person. Thus the first `init` needs `--claims-from`.

### 6.4 The tracked branch

- The Verifier checks one branch of the project. This branch is `main`, or the branch of
  `--branch`. For `verifier init`, the default is the branch that is checked out.
- You can work on other branches as usual. The Verifier checks their commits when they
  get to the tracked branch, through a merge or a pull.
- The Verifier reads the tracked branch from its own checkout. Thus the branch that you
  have checked out has no effect.
- That checkout links to your gitignored files, for example data and results folders. It
  never links to `.env*` files or to tool folders, for example `.venv`.
- The checkout does not contain other files that are not committed on the tracked branch.
  Thus the Verifier can check a number only if its files are committed or gitignored.
- Git does not tell the Verifier the new name of a branch. Thus the Verifier cannot follow
  a renamed branch.
- If the tracked branch is not there, the Verifier stops. `verifier status DIR` gives the
  reason.
- To continue, do one of these two procedures:
  1. Give the branch its old name again.
  2. Or, run `verifier stop DIR`, delete `.verifier/`, and run `verifier start` again
     with `--branch`.

### 6.5 What `verifier start` does

1. It installs a git hook in the project.
2. It starts the Verifier in the background.
3. It opens the terminal view of the auditing agent.

- When you close the view, all processes continue.
- The instructions of the auditing agent do not let it change your files or your git
  history.

### 6.6 One round

1. The tracked branch moves. For example, you commit on it, merge into it, pull, or move
   it from a different branch with `git fetch . feature:main`.
2. The hook writes a note for the Verifier and stops immediately. Git does not wait.
3. The Verifier finds each new commit on the tracked branch, the oldest first. It also
   finds the commits that a merge adds.
4. The auditing agent reads the message of each commit and its changes to your notes.
   Notes are the files that state results, for example experiment notes, reports, READMEs
   and paper drafts.
5. Code (the claim steps) adds a claim to the claim register for each new number. The
   auditing agent adds a source to each claim with a value: the data file of the number.
   When `vcheck check-register` passes, the Verifier records the checksums of those files.
6. The auditing agent describes how to compute each number. `vcheck write-methods` asks a
   model to write a computational method from that description. A method is a small
   program that calculates the number again from your data.
7. The semi-deterministic core checks each claim in the register, and writes a report for the
   commit. It reads only the claim register and the files of its claims.
8. The auditing agent waits for the next commit. If it started a long job, it also starts
   again when the job stops.

- The notes can be LaTeX, Markdown, PDF or a different format that the auditing agent can
  read.
- When a commit arrives during a round, the next round reads it.
- When a model call fails, the Verifier tries the round again later. Each wait is longer
  than the wait before it.
- In the first round, code makes claims from the selected notes and from all commit
  messages. The auditing agent adds their sources.

### 6.7 The state folder

`.verifier/` has its own git history. Your repository ignores it, so the Verifier changes
none of your tracked files. Section 3 lists all its files. These are the main files:

```
claim_register.json     the claims, with how each number is recomputed or traced
methods/                the computational methods that `vcheck write-methods` wrote
checks/                 the checks that the auditing agent wrote
outputs/                what the auditing agent's long jobs wrote
reports/                one report per checked commit, and the latest as .pdf and .md
reviews/                the questions to a person, and the answers
runtime/alerts.jsonl    the alert log, for the Verifier's developers (not committed)
runtime/spending.jsonl  the cost of each of the Verifier's own model calls (not committed)
cache/                  the number index of the data files, the labeller's answers, and
                        the stored results of the claim checks (not committed)
```

- The Verifier makes its checkout of the tracked branch with `git clone --shared`. It
  makes one more checkout for each long job of the auditing agent in the same way.
- These checkouts use the objects of your repository. They write nothing into its git
  folder.

### 6.8 Stored check results

- `vcheck run` stores the result of each claim check with a checksum. The checksum
  includes all the data that the check reads: its claims, its options, the results of the
  gate, and the files.
- If this data did not change, the next run uses the stored result. It does not run the
  check again.
- `vcheck run --no-cache` runs all checks again.
- `vcheck clear-cache` deletes the stored results.

### 6.9 Round records and cost

Each round record in `rounds/` has these fields:

| Field | Content |
|---|---|
| `spending` | the cost of the calls of the auditing agent in the round (from the OpenCode database), and the cost of the calls of the Verifier, for each purpose (a check, a claim step, or the method writer) |
| `session_id` | the OpenCode session of the auditing agent in the round |

### 6.10 Agent sessions

- Each model call of the auditing agent sends all of its conversation again. Thus a long
  conversation costs more in each round.
- The run option `agent_session_policy` sets when the auditing agent gets a new
  conversation (a session).

| Value | When a new session starts |
|---|---|
| `one-session` | never. One session continues for all of the run. |
| `fresh-each-round` (default) | before each round. On one test project, it cost 11% less than `one-session` over 23 commits. It found the same numbers. |
| `fresh-after-token-limit` | when the current session has read `agent_session_token_limit` tokens (default 2,000,000) |

- A new session loses no result, because the claim files keep the work.
- A new session starts with a short summary. The summary gives the commits of the round
  with the pinned files that each commit changed, the methods and checks, the start of
  the last report, and the open questions.

### 6.11 The alert log

- The alert log records only the processes of the Verifier. The report records the
  problems of your project.
- Each record is one JSON line. It gives the level, the full traceback and the details.

| Level | Meaning |
|---|---|
| `log` | a usual event, for example a round that the Verifier sent |
| `warn` | a failed attempt. The Verifier tries again. |
| `error` | a failure. The background process continues. |
| `fatal` | a bug. The process stops. |

- `verifier alerts DIR` prints the records of level `warn` and more serious levels. It
  prints the newest record first.
- `--level log` prints all records. `--key` and `--since` select records.

### 6.12 The report

| Item | Detail |
|---|---|
| Files | `.verifier/reports/latest.pdf`, `latest.md` (the same report as Markdown) and `latest.json` |
| Flag id | Each flag has an id, for example `F-3a9c1e`. The Verifier makes the id from the type, the subject and the detail of the flag. Thus a flag keeps its id in each report. |
| `latest.json` | gives the flags in `issues`, each with its id and its impact |
| `latest.layout.json` | gives the layout data of the PDF. The local page of `verifier web` draws the same parts from it. |
| `report_id` | changes only when the verdict, the verdict without disputed flags, a flag, a run error or a row under the verdict (for example "Waiting for you") changes. It does not change at each commit. |

Examples of the verdicts:

| Verdict | Example |
|---|---|
| BLOCK UNTIL FIXED | The run of a number did not load its adapter. A caption names a model that is not the model of the run. |
| PASS WITH DISCLOSURE | A result does not beat a trivial baseline. |
| PASS WITH CAVEATS | The notes say "significantly" and give no test. The paper refers to a label that does not exist. The report gives each check that could not run, and the reason. |

### 6.13 Questions to a person

- The usual question is about a changed data file of some claims. The notes say that you
  ran it again on purpose. The auditing agent asks you to confirm this.
- The report shows the question at the top. `verifier status` gives the number of waiting
  questions.
- If you confirm, the new file becomes the reference for its claims.
- If you do not confirm, a claim with a changed number stays rejected. The other claims
  say that a person did not accept the file.
- Each person who can use the shell of the project can answer.

### 6.14 Disputes and diagnoses

- Approximately half of the flags can be false.
- Dispute a flag only when you are sure that it is false.
- Run the commands from the project folder.

```bash
verifier dispute . --flag F-3a9c1e --flag F-0b72d4 --why "The method reads runs/old/; the notes use runs/fixed/."
verifier dispute . --withdraw D-1          # the flags count again
verifier diagnosis .                       # each dispute, and whether its diagnosis is written
verifier diagnosis . D-1                   # one diagnosis
verifier diagnosis . --share               # one file with all written diagnoses, to send
```

The effects of a dispute:

- The report changes immediately. It strikes out each disputed flag, and gives the reason
  below it.
- The verdict without the disputed flags can be different. Then the report shows this
  verdict below the struck-out verdict with all flags.
- `latest.json`, `verifier status` and the exit codes keep the verdict with all flags.
- You cannot dispute a flag that says that the Verifier could not read or check something.
- A dispute applies to a flag while the detail of the flag stays the same. A changed
  detail makes a new flag.

The diagnosis:

- In its next round, the auditing agent finds the cause of each disputed flag.
- It writes a diagnosis for the developers. The diagnosis says if each flag is false, the
  cause, and the change to make in the Verifier.
- The diagnosis stays on your computer.
- `--share` writes a new file at the project root each time, and prints its path. `-o`
  gives a different folder or file.
- The shared file includes all the sections of each diagnosis, and the reason of each
  dispute. These can contain private facts of the project.
- Before you send the file to verifier@meno.sh, do these steps:
  1. Read the file to double-check its anonymity.
  2. Remove all private text.
  3. Do not commit the file.

### 6.15 The worker's guide

- The Verifier is for use with a coding agent, for example Claude Code.
- The coding agent reads each report, examines each flag, and tells you which issues are
  real.
- Each start writes the guide for the coding agent to `.verifier/WORKER.md`.
- The Verifier changes no file that you track. Thus `start` and `init` tell you to do one
  of these:
  - Instruct the coding agent to follow `.verifier/WORKER.md`.
  - Add the `@.verifier/WORKER.md` line to the instruction file of the coding agent.
- The guide tells the coding agent when to read the report, how to examine and dispute
  flags, and how to share the report with you.

### 6.16 `verifier static-check`

`verifier static-check` checks a finished paper, or a project, without the auditing agent.
It runs the semi-deterministic core one time.

```bash
verifier static-check --paper paper.tex                        # the paper on its own
verifier static-check --paper notes.md more-notes.md           # Markdown, several files read as one
verifier static-check --paper paper.tex --claims claims.json   # the paper and its claim register
```

| Item | Detail |
|---|---|
| Files that it reads | the `--paper` files, the files that they `\input` or `\include`, and the `--claims` file. With `--claims`, it also reads the files that the claims and the options name: data files, logs, run records and saved readings. |
| Formats | LaTeX and Markdown. It reads R Markdown (`.Rmd`) and Quarto (`.qmd`) as Markdown. It refuses a file of a different format. |
| Paper only | It compares the paper with itself. It also finds claims with no uncertainty. |
| Numbers against data | It needs a claim register (6.17) and computational methods (6.18). In a watched project, code writes the claims, the auditing agent adds their sources, and `vcheck write-methods` writes the methods. |
| `--checks seeds,comparability` | It runs only these claim checks. |
| `--model-review` | It adds the model review. The model review costs money and needs `OPENROUTER_API_KEY`. In a watched project, `--option want_model_review=true` at the start turns it on. |
| `--paper-only`, `--json`, `--plain` | It leaves out the claim register; prints JSON; prints no colour. |
| Exit code | 0 for a pass, 1 for BLOCK UNTIL FIXED or MISSING INPUTS, 2 for REJECT |

### 6.17 The claim register

- The claim register is a JSON file with one entry for each number to check.
- The example below comes from a study that does not exist. The study asks if retrieval
  helps a language model answer four-choice questions about facts.
- The answers of the study are in `runs/retrieval/answers_seed*.json`. Each record gives
  the answer of the model, the correct answer, and the condition (`retrieval` or
  `no-retrieval`).

```json
{
  "claims": [
    {
      "id": "retrieval-results/accuracy-with-retrieval",
      "statement": "With retrieval, the 7B model answers 68.4% of the questions correctly.",
      "value": 0.684,
      "seeds": 3,
      "baseline": 0.25,
      "lower_is_better": false,
      "setup": {"model": "example-7b"},
      "source": {
        "kind": "artifact",
        "path": "runs/retrieval/answers_seed*.json",
        "method": "accuracy_with_retrieval",
        "sha256": {"answers_seed0.json": "a16cb4b1..."}
      }
    },
    {
      "id": "retrieval-results/accuracy-without-retrieval",
      "statement": "Without retrieval, the 7B model answers 61.2% of the questions correctly.",
      "value": 0.612,
      "source": {
        "kind": "document",
        "path": "logs/eval-no-retrieval.log",
        "must_contain": "accuracy 0.612",
        "why": "pipeline-retired"
      }
    },
    {
      "id": "retrieval-results/retrieval-helps-7b",
      "statement": "Retrieval improves the 7B model's factual accuracy.",
      "source": {
        "kind": "claims",
        "claims": ["retrieval-results/accuracy-with-retrieval",
                   "retrieval-results/accuracy-without-retrieval"]
      }
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `value` | the number as the notes write it. The core checks it to the same number of decimals. Thus write `0.50` if the paper says `0.50`. |
| `tol` | a larger margin. Use it only when necessary, because a large margin lets wrong numbers pass. |
| `source` `artifact` | gives the data files in `path` (a pattern can match many files), and the method that calculates the number again in `method` |
| `source` `document` | connects the number to a line in a log, for a number that no method can calculate again. The log must contain `must_contain`. `why` is `parser-superseded`, `pipeline-retired`, `external-source` or `computed-offline`. |
| `source` `claims` | The claim comes from other claims of the same register. It needs a `statement`, and a `value` is optional. Each claim that it names also needs a `statement`. |
| no `source` | permitted. The report shows the claim as a caveat. |
| `sha256` | the checksum of each file, recorded when `vcheck check-register` passes after the auditing agent adds the source. A later change to the file rejects the claim. |
| `setup` | the setup of the run, as the paper gives it. A field that is different from the `setup` of the method, or that the method does not give, rejects the claim. |
| paths | relative to the folder of the register. An absolute path makes the claim unreadable, because it works on only one computer. |
| `seeds`, `baseline` with `lower_is_better`, `baseline_n`, `model`, `caption_model`, `claimed_significant` | optional keys that turn on claim checks |
| `compare_to`, `config`, `commit`, `varies`, `ignore` | optional keys for comparisons |
| `run` | the optional path to the run record of the run that made the data of the claim |

- When the key of a claim check is missing, the report says that the check did not run.
- A run record is a small JSON file that a training or evaluation run writes about itself.
  Its format is in `checks/run_record.py`.
- A run record gives the config, the applied config, the declared model and adapter, if
  the run loaded them, the seed, the training and evaluation ids, and the outputs.
- The core can find an entry that it cannot use, for example an entry with an incorrect
  key, an incorrect type or an unknown method. It reports the entry as unreadable, with
  the reason. The verdict is then MISSING INPUTS.

### 6.18 Computational methods

- The core never reads your data. Each `artifact` claim names a computational method in
  `.verifier/methods/`.
- `verifier static-check` has no methods. Thus each `artifact` claim is
  unreadable there, and the verdict is MISSING INPUTS.
- The Verifier gets plug-in checks from `.verifier/checks/` in the same way.
- Methods and plug-in checks stay in the project. They are not part of the code of the
  Verifier.

```python
# .verifier/methods/retrieval.py
import glob, json, os
from checks.interface import Computed, method

@method("accuracy_with_retrieval")
def accuracy_with_retrieval(path):
    rows = [r for p in sorted(glob.glob(path)) for r in json.load(open(p))["records"]
            if r["condition"] == "retrieval"]
    correct = [r["answer"] == r["correct_answer"] for r in rows]
    setup = json.load(open(os.path.join(os.path.dirname(path), "setup.json")))
    return Computed(value=sum(correct) / len(correct), n=len(correct), setup=setup)
```

- A method gets the `path` of the claim. It gives a `Computed` with the number and the
  number of observations `n`. It can also give `interval=(lo, hi)` and the `setup` of the
  run.
- Write one method for each quantity. A different subset of the data needs its own
  method.
- A method can fail, or give a value that is not a `Computed`. Then the core rejects only
  the claim of that method.
- `tests/fixtures/workdir/methods/` has a full example.
- Each method runs in its own process.
- A project can have a `uv.lock`. Then the Verifier builds the pinned environment in
  `.verifier/runtime/venv`, with the dependencies only. The methods run there and can use
  the libraries of the project.
- Without a `uv.lock`, methods run with the Python of the Verifier. Write them with the
  standard library only. The libraries of the Verifier are there too, but a later version
  of the Verifier can remove them.
- The report says which environment the methods used, with the checksum of the lockfile.
- Checks always use only the standard library.

### 6.19 The local page

```bash
verifier web                  # serves http://127.0.0.1:8765/ and opens the browser
```

- The server listens only on 127.0.0.1. It refuses a request for a host name other than
  `127.0.0.1`, `localhost` and `[::1]`.
- Each request that changes something must have the header `X-Verifier-Web`. Thus a page
  of another site cannot start a run.
- An upload is one `.zip` file, or one or more LaTeX files (`.tex`, `.ltx`, `.latex`) or
  Markdown files (`.md`, `.markdown`, `.mdown`, `.mkd`, `.mkdn`, `.rmd`, `.qmd`).
- Each upload gets a run folder in `--runs-folder`. The folder holds `run.json` and `project/`.
- `project/` is a new git repository with the uploaded files in one commit on `main`. The
  `.git`, `.verifier` and `__MACOSX` folders of a zip are not copied.
- The server runs `verifier init` with `--claims-from .`, then `verifier start-if-pending`.
  The daemon stops when no work waits.
- Every 30 seconds, the server runs `verifier start-if-pending` for all runs. Thus a
  dispute, an answer, or a job of the agent starts the daemon again.
- The page reads the output of `verifier status`, `report`, `diagnosis` and `review`, each
  with `--json`. It also reads the run's `run.json`, its share file and its PDF report.
- The page draws the report from the layout data under `layout` in `verifier report --json`.
  It has the parts of the PDF: the verdict, the overview, the sections, the cards and the tables.
- The page changes a run only with `verifier dispute`, `decide` and `diagnosis --share`.
  Thus the page and the terminal give the same result.
- The page deletes a run with `verifier stop`, and then deletes the run folder.
- The server writes its alert log to `alerts.jsonl` in the folder of the runs.

### 6.20 The checks

**Consistency**

- It finds a model name in the text that no table contains.
- It finds a number in the text that no table contains.
- It finds references to labels that do not exist.
- It finds tables and figures that have no references.
- It finds rows with an incorrect number of cells.
- It finds captions that name a model that their rows do not show.
- It finds a label that is defined more than once.
- It finds an interval whose lower bound is above its upper bound, and a value outside its
  own interval. A single number in brackets, such as the citation "[21]", is not an
  interval. A list of citations after a decimal value, such as "0.80 [3, 12]", is not an
  interval either.
- It finds a number in the abstract that the body of the paper does not state.
- In Markdown, a reference is a link to an anchor, for example `[see](#results)`.
- In Markdown, a table is a pipe table. Its caption is a one-line paragraph that starts
  with "Table", immediately above or below the table.
- A Markdown table has no label, so no reference can point to it. Thus the report says
  that the check for tables with no references did not run.
- The model names come from a list of models on the Hugging Face Hub and OpenRouter, in
  `utils/model_names.txt`. `utils/refresh_model_names.py` makes the list again.

**Model review**

- You can ask for the model review. If it then gives no answer, the run cannot pass.

**Setup review**

- One model call reads the paper with the setup register (5.7) as a checklist.
- It finds a setup fact that the rest of the paper does not agree with. It also does a
  semantic review of the setup. Its flags start with `setup-review-`.
- It is on by default (`want_setup_review`). With no setup register, it is off.
- The Verifier saves each answer in `readings/setup_review.txt.by-prompt/`, and the last
  answer in `readings/setup_review.txt`.

**Science**

- It reads the paper alone. It finds a comparison with no uncertainty, a claim of
  significance with no test behind it, and many comparisons with no correction.
- Its flags are caveats, because the words alone do not show a statistical claim.

**Claim checks**

- They find a result that does not beat its trivial baseline.
- They find too few seeds, and a caption that names the wrong model.
- They find a scored sample that became smaller, and an interval too near zero.
- They find a comparison of two runs with a difference that the claim does not declare.
- A claim can name its run. Then they also find a config that the run did not apply, and
  a model or adapter that it did not load.
- For such a claim, they also find outputs that are empty, collapsed or from the
  untrained model, and training items in the evaluation.
- They do not run on a claim whose file or number failed.
- They check `--claim-check-workers` claims at the same time (default 64).
- With propagation off, no claim waits for a different claim.
- With propagation on, a claim waits for the claims below it, because it reads their
  uncertainty.
- The number of workers has no effect on the findings.

**Support**

- It finds each claim that rests on itself through other claims. It names the cycle.

**Uncertainty**

- The red team and the quantifier read the full paper. They accept its results as true
  (`WatchedProject.full_paper_text`).
- A red team writes possible follow-up results that go against the claim. A quantifier
  scores them.
- For a flagged claim, the report shows the worst attack as the worst case to examine.
  This case is hypothetical, and possibly has no empirical support.
- By default, the check measures only the step of the claim itself.
- `--propagate-claim-uncertainty` adds the uncertainty of the claims below the claim:
  1 − (1 − this step) × (1 − the largest uncertainty of a claim below).
- With propagation on, the check runs only in a watched project. The reason is that the
  core must calculate each claim below again, and only a watched project has methods.
- Thus, with propagation on, `verifier static-check` does not do this check.
- With propagation off, a claim below with no source does not stop the check. Thus the
  check also runs on a paper with no code.
- Both parts call models. Thus the Verifier saves each reading in the state folder, and
  uses it again.
- A run stops with an error when it has a claim that comes from other claims, and
  `--uncertainty-readings` or the API key of the models is missing.
- In a watched project, `--option want_claim_uncertainty=false` turns the check off.

The flag rule:

1. The quantifier scores the breakage with no attack. This breakage shows how much the
   paper alone goes against the claim.
2. The red team always runs.
3. An attack counts when its believability is more than `--uncertainty-cutoff` (default
   0.59).
4. The check flags the claim when one of two values is more than the same cutoff: the
   breakage with no attack, or the breakage of the worst attack that counts.

The scenario of a flagged claim:

- The report does not show the worst attack as the red team wrote it.
- A model (`--scenario-model`, default Haiku 5.5) writes the gap in the claim that the
  attack shows. It uses no invented numbers, and at most 4 short, plain sentences.
- The Verifier saves the text in `<readings>_scenarios.json`, next to the readings file.
- When the check calls no models and has no saved text, the report shows the text of the
  red team. A label on the text says this.

The parts:

| Part | What it does | Options |
|---|---|---|
| red team `prompt` | asks one model for N proposals in each round. Each round after the first sees the scores of all earlier proposals. | `--red-team-model` (default Haiku 5.5), `red_team_iterations` (default 2 rounds) |
| quantifier `judge` (default) | asks a model for each score in a different request. First it asks for the believability without the claim. Then it asks for the breakage with the claim. | `--judge-model` (default Haiku 5.5) |
| quantifier `coherence` | gets the believability from the judge model. It gets the breakage from one of the two sources below. | `--coherence-model` or `--coherence-command` |

The breakage sources of `coherence`:

- `--coherence-model`: a model answers "Is the claim true?". The breakage is
  P(False) / (P(True) + P(False)), from the log probabilities of the first token.
- `--coherence-command`: a command gets each attack as JSON on stdin. It writes
  `{"breakage": k}` on the last line of stdout. An example is an HTTP client of a
  scoring server that you run yourself.

```bash
export OPENROUTER_API_KEY=...    # the default models are on OpenRouter
# The judge quantifier (the default): one model gives both scores.
verifier start .
# The coherence quantifier: breakage from a model's first-token log probabilities.
verifier start . --option quantifier=coherence \
  --option 'coherence_model=https://openrouter.ai/api/v1/chat/completions#deepseek/deepseek-v4.1-flash-1017'
```

Models and keys:

- Each model is `URL#MODEL`, with a chat-completions URL.
- `utils/model_keys.json` gives the key variable of each host. For example, the key
  variable of `openrouter.ai` is `OPENROUTER_API_KEY`.
- A run with a host that is not in the file stops with an error.
- For a GPU server, for example `http://gpu-host:8000/v1/chat/completions#Qwen/Qwen3-32B`,
  add `"gpu-host": ""`. An empty value means no key.
- The models of the commands of the auditing agent are also run options, for example
  `method_writer_model` and `duplicates_model`.
- `verifier start` stops with an error when the key of a model is missing.
- `verifier static-check --help` gives the other options.

## 7. The layers and their major classes

### 7.1 `schema/`

The records of a run: the inputs (the claim register and its claims), the results of each
check, the gate results, the findings, and the problem kinds. A record has fields of data
that never change. A result class describes itself to the report.

<!-- spec:classes schema -->
```python
# schema/claim/claim_register_parser.py

class ClaimRegisterParser:
    """Turns a claim register file, or its loaded JSON, into a ClaimRegister."""
    REGISTER_KEYS: ClassVar[frozenset[str]]
    BOOKKEEPING_KEYS: ClassVar[frozenset[str]]
    CLAIM_KEYS: ClassVar[frozenset[str]]
    ARTIFACT_KEYS: ClassVar[frozenset[str]]
    DOCUMENT_KEYS: ClassVar[frozenset[str]]
    CLAIMS_KEYS: ClassVar[frozenset[str]]
    # Build a parser for the methods and the claim keys of one check registry.
    def __init__(self,
                 method_names: Collection[MethodName],
                 field_types: Mapping[str, FieldType]) -> None: ...
    # Read and parse a claim register file, keeping every decimal as it was written.
    def parse_file(self, path: str, base_folder: str | None=None) -> ClaimRegister: ...
    # The JSON of a claim register file, each decimal kept as written (0.120 stays 0.120).
    @staticmethod
    def read_raw_file(path: str) -> object: ...
    # Parse a loaded claim register JSON, one entry at a time.
    def parse_json(self, raw: object, base_folder: str) -> ClaimRegister: ...
    # Parse one claim register entry, with every path resolved against `base_folder`.
    def parse_claim_entry(self, raw: object, base_folder: str) -> Claim: ...

# schema/errors.py

class VerifierError(Exception):
    """An input or a state that the Verifier refuses, with the reason in the message."""

# schema/findings.py

class Findings:
    """Everything one run found. The verdict is computed from this, never stored in it."""
    inputs: inputs.Inputs
    papers: Mapping[str, PaperFindings]
    claims: tuple[ClaimFindings, ...]
    unreadable_claims: tuple[inputs.UnreadableClaim, ...]
    superseded: tuple[inputs.Claim, ...]
    must_run: frozenset[str]
    claim_checks_not_run_because: str | None
    # Each document's findings, then each claim's, in order.
    def document_and_claim_findings(self) -> tuple[CheckResults, ...]: ...
    # Every check's result in the tree, documents first, then claims in order.
    def all_check_results(self) -> Iterator[CheckResult]: ...
    # Every Problem inside every Flagged result, documents first, then claims in order.
    def all_problems(self) -> list[Problem]: ...

class CheckResults:
    """The result of each check on one thing, a document or a claim, by check name."""
    checks: Mapping[str, CheckResult]
    # Every Problem in the Flagged results.
    def problems(self) -> list[Problem]: ...

class PaperFindings(CheckResults):
    """Every paper check's result on one document, by check name, and counts of what was read."""
    unreadable_inputs: tuple[str, ...]
    facts: Mapping[str, int]

class ClaimFindings(CheckResults):
    """The gate's results and every claim check's result, for one claim in the claim register."""
    claim: inputs.Claim
    provenance: provenance.ProvenanceResult
    recompute: RecomputeResult
    # True when provenance or recompute is broken, so the claim cannot be verified.
    @property
    def gate_failed(self) -> bool: ...
    # The one-word gate status shown in the terminal table.
    def gate_status_word(self) -> str: ...
    # The sentence shown next to the gate status: the recompute result once it ran.
    def gate_note(self, register_folder: str | None=None) -> str: ...

# schema/inputs.py

class ClaimRegister:
    """A claim register: its parsed claims for the checks, and its entries as written."""
    claims: tuple[Claim, ...]
    unreadable: tuple[UnreadableClaim, ...]
    base: str | None
    entries: tuple[object, ...]
    # A copy of each entry, which a writer can change.
    def entry_copies(self) -> list[object]: ...
    # The register as the file holds it, with each entry as written.
    def to_json(self) -> dict[str, object]: ...

class UnreadableClaim:
    """One claim register entry that could not be parsed, and why."""
    entry: int
    id: ClaimId | None
    reason: str
    # The id of the entry, or `entry N` when it has no usable id.
    def label(self) -> str: ...
    # Where the entry is in the register: `entry N (id)`, or `entry N (no id)`.
    def position_label(self) -> str: ...

class Claim:
    """One entry in the claim register."""
    id: ClaimId
    value: float | None
    statement: str | None
    source: Source | None
    tol: float | None
    setup: Mapping[str, Scalar] | None
    origin: str | None
    superseded_by: ClaimId | None
    fields: Mapping[str, object]
    # `id -> the id of the claim that replaces it`, for a superseded claim.
    def superseded_label(self) -> str: ...
    # The claims this claim is inferred from: its ClaimsSource ids, or none.
    def ids_of_claims_it_is_inferred_from(self) -> tuple[ClaimId, ...]: ...
    # How close a recomputation must be before it counts as matching.
    def recompute_tolerance(self) -> float: ...

class FieldType(Enum):
    """The type of a key a claim check reads from a claim, as it declares it."""

class ArtifactSource:
    """The data a claim's number is recomputed from, and the method that recomputes it."""
    KIND: ClassVar[str]
    path: str
    method: MethodName
    sha256: str | Mapping[str, str] | None
    proposed_sha256: Mapping[str, str] | None

class ClaimsSource:
    """Other claims in the same register that a claim is inferred from."""
    KIND: ClassVar[str]
    claims: tuple[ClaimId, ...]

class ClaimRegisterError(VerifierError, ValueError):
    """A claim register or one of its entries cannot be used as written."""

class PrintedFloat(float):
    """A decimal number that keeps the text it was written as, so "0.50" keeps both decimals."""
    text: str
    SCALES: ClassVar[tuple[float, ...]]
    # `text` as Python reads a number: no thousands separator, and "-" for "−".
    @staticmethod
    def plain(text: str) -> str: ...
    # How many decimals a number was written with: 3 for 0.127, 2 for 0.50, 0 for 1.
    @staticmethod
    def count_printed_decimals(value: float) -> int: ...
    # The values that print as this text: plus or minus half a unit of the last digit.
    def interval(self) -> tuple[float, float]: ...
    # Whether the two printed numbers can be one value, at a scale of 1, 100 or 1/100.
    def could_be_same_value(self, other: PrintedFloat) -> bool: ...
    # Whether the two can be one value, printed with at most one digit more or less.
    def could_be_same_value_at_similar_precision(self, other: PrintedFloat) -> bool: ...

# schema/primitives.py

class CheckResult:
    """The result of one check: Passed, Flagged, NotChecked, Skipped or Off."""
    counts_as_caveat: ClassVar[bool]
    # The problems the check found; none unless the result is Flagged.
    def problems_found(self) -> tuple[Problem, ...]: ...
    # Passed when a check found no problems, otherwise Flagged with all of them.
    @staticmethod
    def from_problems(problems: Iterable[Problem]) -> Passed | Flagged: ...

class GateResult:
    """The result of one step of the gate of a claim: provenance or recompute."""
    gate_status_word: ClassVar[str | None]
    board_code: ClassVar[str | None]
    # One sentence that says what the result means; each subclass gives its own.
    def describe_for_reader(self) -> str: ...
    # A copy whose absolute `path` is relative to `folder`, for display only.
    def with_paths_relative_to(self, folder: str | None) -> Self: ...
    # The description, with paths as the claim register in `folder` wrote them.
    def describe_with_paths_relative_to(self, folder: str | None) -> str: ...

class RecomputeResult(GateResult):
    """The result of recomputing the number of one claim."""
    is_broken: ClassVar[bool]
    is_switched_off: ClassVar[bool]
    # The result that holds the recomputed number (Matches), or None for every other.
    def recomputed_number(self) -> Matches | None: ...
    # Why a claim check that reads the recomputed number cannot run after this result.
    def reason_there_is_no_recomputed_number(self) -> str: ...

class Passed(CheckResult):
    """Ran, and found nothing wrong."""
    measured: float | None

class Flagged(CheckResult):
    """Ran, and found at least one problem."""
    problems: tuple[Problem, ...]
    measured: float | None
    # The problems the check found.
    def problems_found(self) -> tuple[Problem, ...]: ...

class NotChecked(CheckResult):
    """Could not run, because the claim register lacks a field it needs."""
    missing: str
    # Why it did not run: the key the claim does not give.
    def describe_for_reader(self) -> str: ...

class Skipped(CheckResult, RecomputeResult):
    """Did not run, because something before it failed."""
    because: str
    # Why it did not run.
    def describe_for_reader(self) -> str: ...

class Off(CheckResult, RecomputeResult):
    """Switched off with --checks."""
    # That it was switched off.
    def describe_for_reader(self) -> str: ...
    # That recompute was switched off.
    def reason_there_is_no_recomputed_number(self) -> str: ...

class Problem:
    """One specific thing that is wrong."""
    kind: ProblemKind
    detail: str
    where: Location | None
    facts: tuple[tuple[str, str], ...]
    # The problem as the reader sees it, with where it is when that is known.
    def text_for_reader(self) -> str: ...

class Location:
    """Where in the paper a problem is."""
    section: str | None
    excerpt: str

class Severity(Enum):
    """How bad a problem is. Integrity failures have no severity: they come only from the gate."""

class ProblemKind:
    """One kind of problem a check can report, with its severity."""
    code: str
    severity: Severity

class TraceReason(Enum):
    """Why a claim is not recomputed, but only found in a log file instead."""
    # What the reason means, as the report shows it for a traced claim.
    @property
    def meaning_for_reader(self) -> str: ...

# schema/provenance.py

class ProvenanceCategory(Enum):
    """The four groups of provenance results, which the verdict reads."""

class ProvenanceResult(GateResult):
    """The result of the provenance check of one claim. Each subclass sets its category."""
    category: ClassVar[ProvenanceCategory]
    report_word: ClassVar[str]
    allows_recompute: ClassVar[bool]
    gate_note_describes_recompute: ClassVar[bool]
    # True when the claim cannot be verified, so the verdict is REJECT.
    @property
    def is_broken(self) -> bool: ...
    # True when the verdict counts the claim as a caveat: nothing to check, or provisional.
    @property
    def counts_as_caveat(self) -> bool: ...
    # The reason recompute did not run after this result, for the Skipped result.
    def reason_recompute_did_not_run(self) -> str: ...

class Intact(ProvenanceResult):
    """The artifact exists and matches its pinned checksum."""
    path: str
    # That the artifact is there and unchanged.
    def describe_for_reader(self) -> str: ...

# schema/recompute.py

class Matches(RecomputeResult):
    """Recomputed within tolerance of what the paper prints."""
    value: float
    n: int
    interval: tuple[float, float] | None
    tolerance: float
    # The recomputed number and its count.
    def describe_for_reader(self) -> str: ...
    # This result, which holds the recomputed number.
    def recomputed_number(self) -> Matches: ...

# schema/verdict.py

class Verdict(Enum):
    """The one outcome of a run, worst first."""
    # The exit code of static-check and `vcheck run`: 2 REJECT, 1 a block or MISSING INPUTS.
    @property
    def exit_code(self) -> int: ...
    # How this verdict shows in the terminal and in the PDF report.
    @property
    def style(self) -> VerdictStyle: ...
```
<!-- /spec:classes -->

### 7.2 `utils/`

General functions that know nothing of a check. The shared files (`file_storage.py`) give
locks, whole-file writes, JSON files and JSON lines. The model calls (`models.py`) know the
provider and record what each call costs.

<!-- spec:classes utils -->
```python
# utils/claim_files.py

class ClaimFilesError(ValueError):
    """The claim files cannot be used: some are missing, or one is in an old format."""

class ClaimFilesBroken(ClaimFilesError):
    """A claim file is not valid JSON, or not the right shape. The agent can fix it."""

# utils/extract.py

class Paper:
    """What the paper checks read, the same whichever format the paper was written in."""
    formats: frozenset[PaperFormat]
    text: str
    prose: tuple[str, ...]
    sentences: tuple[str, ...]
    statements: tuple[PaperStatement, ...]
    tables: tuple[PaperTable, ...]
    captions: tuple[PaperCaption, ...]
    labels: tuple[str, ...]
    targets: frozenset[str]
    refs: frozenset[str]
    abstract: str
    after_abstract: str
    unreadable: tuple[str, ...]
    equations: tuple[PaperEquation, ...]
    addresses: tuple[PaperAddress, ...]
    sentence_sections: tuple[str, ...]

# utils/file_storage.py

class FileLock:
    """An exclusive lock on a lock file, which every process that uses the file respects."""
    # The lock of the lock file `path`. The file is made at the first use.
    def __init__(self, path: Path) -> None: ...
    # A lock for `guarded` whose lock file is in the system's temporary folder.
    @classmethod
    def in_temporary_folder(cls, purpose: str, guarded: Path) -> FileLock: ...
    # Hold the lock for the block, and wait for it when another process holds it.
    def held(self) -> Iterator[None]: ...
    # Hold the lock for the block if no other process holds it. Gives True if held.
    def held_if_free(self) -> Iterator[bool]: ...

class AtomicallyWrittenFile:
    """A file that a reader always sees whole, never half written."""
    # The file `path`. `mode`, when given, is the mode of every write.
    def __init__(self, path: Path, mode: int | None=None) -> None: ...
    # Replace the file with `text`.
    def write_text(self, text: str) -> None: ...
    # Replace the file with `data`.
    def write_bytes(self, data: bytes) -> None: ...
    # The text of the file.
    def read_text(self) -> str: ...

class JsonFile:
    """One JSON file, written whole, in one fixed format, with a newline at the end."""
    # The JSON file `path`, written with the json.dumps settings given.
    def __init__(self,
                 path: Path,
                 *,
                 indent: int | None=None,
                 sort_keys: bool=False,
                 ensure_ascii: bool=True,
                 lock: FileLock | None=None,
                 mode: int | None=None) -> None: ...
    # Whether the file exists.
    def exists(self) -> bool: ...
    # The JSON value of the file.
    def read(self) -> object: ...
    # Replace the file with `value`.
    def write(self, value: object) -> None: ...
    # Read the file, and write what `update` gives, with the lock held for both.
    def change(self, update: Callable[[object | None], object]) -> None: ...

class JsonFileFolder:
    """A folder with one JSON file for each item, each written whole."""
    # The items in `folder`, written with `indent`.
    def __init__(self, folder: Path, *, indent: int | None=None) -> None: ...
    # The JSON file of one item.
    def file(self, name: str) -> JsonFile: ...
    # The names of the items, sorted; none when the folder does not exist.
    def names(self) -> list[str]: ...
    # Write one item, and give its path.
    def write(self, name: str, value: object) -> Path: ...
    # The JSON value of one item.
    def read(self, name: str) -> object: ...
    # Move one item into `other`, in one step.
    def move_to(self, name: str, other: JsonFileFolder) -> None: ...
    # Remove one item.
    def remove(self, name: str) -> None: ...

# utils/models.py

class ProviderError(Exception):
    """The model gave no usable answer: no key, a refused call, or a cut or empty reply."""

class BadReply(ValueError):
    """The reply breaks a rule of the caller's reader. The message names the rule."""

class agent_preset:
    """The default models of the auditing agent, in OpenCode's form (agent_key_problem)."""
    KIMI: ClassVar[str]

# utils/shared_paper_prefix.py

class PaperTooLongForModelContext(ValueError):
    """The paper has more tokens than MAX_PAPER_TOKENS, by the estimate of CHARACTERS_PER_TOKEN."""
```
<!-- /spec:classes -->

### 7.3 `core/`

The checks machinery (`registry/checks.py`): a check declares its name, its problem kinds and what
it reads, and the registry holds the checks, methods and run options of one run. The
analysis (`analysis/`) runs the gate and the checks, and stores their results by what they
read. The report (`report_model/`, `report_output/`) turns the findings into the report.
The commands of `vcheck` are in `commands/`, one file for each command.

<!-- spec:classes core -->
```python
# core/analysis/analysis_request.py

class AnalysisRequest:
    """What one analysis reads."""
    documents: Mapping[str, tuple[str, ...]]
    claim_register_path: str | None
    run_options: Mapping[str, object]
    paper_only: bool
    switches_on: frozenset[str] | None
    claim_register_base_folder: str | None
    claims_with_claim_checks: frozenset[str] | None
    full_paper_text: str | None
    claim_checks_not_run_because: str | None
    # True when the analysis reads a claim register.
    @property
    def uses_claim_register(self) -> bool: ...
    # The files of every document, in order.
    def all_document_paths(self) -> tuple[str, ...]: ...

# core/analysis/check_result_cache.py

class CheckResultCache:
    """Stores and gives the results of the claim checks of one state folder."""
    # The cache of `state_dir`. The code fingerprint is computed once, here.
    def __init__(self, state_dir: Path, methods_ran_in: str) -> None: ...
    # What the cache knows of one file: its SHA-256, "folder", or "missing".
    def file_state(self, path: str) -> str: ...
    # The state of each Python file in the folder of `path`, which a module can import.
    def python_folder_state(self, path: str) -> dict[str, str]: ...
    # The SHA-256 of all that `check` reads for `claim`, or None.
    def fingerprint(self,
                    check: checks.ClaimCheck,
                    claim: inputs.Claim,
                    context: checks.ClaimContext,
                    provenance_result: ProvenanceResult,
                    recompute_result: RecomputeResult) -> str | None: ...
    # The stored result of this fingerprint, or None when there is none.
    def stored_result(self,
                      fingerprint: str,
                      check: checks.ClaimCheck) -> CheckResult | None: ...
    # Store a result of FindingsJsonCodec.STORED_RESULTS. A crash is never stored.
    def store_result(self, fingerprint: str, result: CheckResult) -> None: ...
    # Record the results this run used, and remove those none of the last 3 runs used.
    def finish_run(self) -> None: ...
    # The SHA-256 of all that one call of a computational method reads, or None.
    def recompute_fingerprint(self,
                              method: computational_methods.ComputationalMethodEntry,
                              source: inputs.ArtifactSource,
                              provenance_result: ProvenanceResult) -> str | None: ...
    # What the method gave for this fingerprint, or None when nothing is stored.
    def stored_computed(self, fingerprint: str) -> computational_methods.Computed | None: ...
    # Store what a method gave. A NaN, which means no data, is not stored.
    def store_computed(self,
                       fingerprint: str,
                       computed: computational_methods.Computed) -> None: ...
    # Remove every stored result of the state folder `state_dir`.
    @staticmethod
    def clear(state_dir: Path) -> None: ...

# core/analysis/claim_checker.py

class ClaimChecker:
    """Runs the gate and the claim checks on each claim, after the claims it rests on."""
    # Check claims with the claim checks of `check_registry` that are not switched off.
    def __init__(self,
                 check_registry: checks.CheckRegistry,
                 run_options: Mapping[str, object],
                 switched_off: frozenset[str],
                 provenance_checker: provenance.ProvenanceChecker,
                 recomputer: recompute.ClaimRecomputer,
                 runner: ClaimCheckRunner,
                 full_paper_text: str | None=None) -> None: ...
    # The findings of every claim that is not superseded, in register order.
    def check_claims_in_dependency_order(self,
                                         claims: Sequence[inputs.Claim],
                                         claims_with_checks: frozenset[str] | None=None) -> tuple[findings.ClaimFindings, ...]: ...
    # Whether a claim check that is on reads the findings of the claims a claim rests on.
    def a_check_reads_findings_of_claims_rested_on(self) -> bool: ...
    # Run the gate on one claim, then every claim check that applies to it.
    def check_one_claim(self,
                        claim: inputs.Claim,
                        context: checks.ClaimContext,
                        switched_off: frozenset[str]) -> findings.ClaimFindings: ...

# core/analysis/document_checker.py

class DocumentChecker:
    """Runs, on one document, each paper check group that is switched on."""
    # Run the groups of `check_registry` that are in `switches_on`, with these options.
    def __init__(self,
                 check_registry: checks.CheckRegistry,
                 run_options: Mapping[str, object],
                 switches_on: frozenset[str]) -> None: ...
    # The results of every group on one document; a group that is off gives Off results.
    def check_document(self,
                       paper: extract.Paper,
                       labels_elsewhere: frozenset[str]) -> findings.PaperFindings: ...

# core/analysis/findings_analyzer.py

class FindingsAnalyzer:
    """Runs all checks that are on, and gives one Findings."""
    # Analyse with the checks and methods of `check_registry`, and `result_cache`.
    def __init__(self,
                 check_registry: checks.CheckRegistry,
                 result_cache: check_result_cache.CheckResultCache | None=None) -> None: ...
    # Run the checks that are on, as the request says, and give what they found.
    def analyse(self, request: AnalysisRequest) -> findings.Findings: ...

# core/analysis/findings_json.py

class FindingsJsonCodec:
    """Turns a Findings, or any part of it, into plain JSON values, and reads a result back."""
    TAGGED_RESULT_BASES: ClassVar[tuple[type, ...]]
    # Encode in the fixed form or in the form of the saved report.
    def __init__(self, fixed_form: bool=False) -> None: ...
    # The value as plain JSON values.
    def encode(self, value: object) -> object: ...
    STORED_RESULTS: ClassVar[tuple[type[CheckResult], ...]]
    # The result of STORED_RESULTS that `encode` gave as `data`.
    def decode_check_result(self,
                            data: object,
                            kinds: Sequence[ProblemKind]) -> CheckResult: ...

# core/analysis/verdict_policy.py

class VerdictPolicy:
    """Gives the one verdict of a Findings: the worst condition wins."""
    # Worst first. Counts never matter: one integrity failure is enough to reject.
    def verdict_for(self, tree: findings.Findings) -> Verdict: ...
    # True when a check that was asked for and must run was Skipped instead.
    @staticmethod
    def a_required_check_was_skipped(tree: findings.Findings) -> bool: ...
    # True when some check did not run, or some claim had nothing to check it against.
    @staticmethod
    def some_check_did_not_run(tree: findings.Findings) -> bool: ...

# core/cli_command.py

class CLICommand(abc.ABC):
    """One command of `vcheck` or `verifier`."""
    HELP: ClassVar[str | None]
    # Add the arguments of the command to its parser.
    def configure_parser(self, parser: argparse.ArgumentParser) -> None: ...
    # Run the command, and give its exit code.
    def execute(self, args: argparse.Namespace) -> int: ...

# core/gate/artifact_pins.py

class ArtifactPins:
    """Compares the files of an artifact source with the checksums the register pins."""
    # Each pinned file that the path of the source matches, by the key of its pin.
    @staticmethod
    def pinned_files(source: inputs.ArtifactSource) -> dict[str, PinnedFile]: ...
    # Each pinned file that `path` matches, by the key of its pin.
    @staticmethod
    def pinned_files_at(path: str,
                        sha256: str | Mapping[str, str] | None) -> dict[str, PinnedFile]: ...
    # The pinned file `file`, if `path` pins it; else None.
    @staticmethod
    def pin_of_file(path: str,
                    sha256: str | Mapping[str, str] | None,
                    file: Path) -> PinnedFile | None: ...
    # Each claim that pins `file`, relative to `root`, with its pinned file.
    @staticmethod
    def claims_pinning_file(claims: Iterable[inputs.Claim],
                            root: Path,
                            file: str) -> list[tuple[inputs.Claim, PinnedFile]]: ...
    # The checksum that `file`, relative to `root`, has now.
    @staticmethod
    def current_checksum(root: Path, file: str) -> str: ...
    # Compare each file of the source with the checksum pinned when the claim was added.
    def compare_files_with_pins(self,
                                source: inputs.ArtifactSource) -> provenance.Changed | provenance.Missing | tuple[str, ...]: ...

# core/gate/provenance.py

class ProvenanceChecker:
    """Checks that the source of a claim exists and has not changed."""
    # Check the files of artifact sources with `pins`.
    def __init__(self, pins: artifact_pins.ArtifactPins) -> None: ...
    # Check the source of one claim.
    def check_claim_source(self, claim: inputs.Claim) -> provenance.ProvenanceResult: ...

# core/gate/recompute.py

class ClaimRecomputer:
    """Recomputes the number of a claim with the computational method the claim names."""
    # Recompute with the methods of the method files; each claim names one.
    def __init__(self,
                 methods: Mapping[MethodName, computational_methods.ComputationalMethodEntry],
                 cache: check_result_cache.CheckResultCache | None=None) -> None: ...
    # Recompute one claim from its data, once provenance found the files intact.
    def recompute_claim(self,
                        claim: inputs.Claim,
                        provenance_result: provenance.ProvenanceResult) -> RecomputeResult: ...

# core/methods/computational_methods.py

class ComputationalMethodEntry:
    """A computational method: how recompute turns a claim's data into one number."""
    name: MethodName
    function: MethodFunction

class Computed:
    """Holds the result of a computational method and refuses values of the wrong type."""
    value: float
    n: int
    interval: tuple[float, float] | None
    setup: Mapping[str, inputs.Scalar] | None

class MethodFileError(VerifierError):
    """A methods folder, or a method file in it, that cannot be loaded."""

class MethodFolderLoader:
    """Adds the computational methods of a methods folder to a copy of a check registry."""
    # Load methods that run with the Python `python`.
    def __init__(self, python: str=sys.executable) -> None: ...
    # A copy of `base` with the methods of the method files in `folder` added.
    def registry_with_methods(self,
                              base: checks.CheckRegistry,
                              folder: Path | None) -> checks.CheckRegistry: ...
    # The names in the @method("name") decorators of a file, read without running it.
    @staticmethod
    def method_names_in_file(path: Path) -> list[str]: ...

# core/methods/method_runner.py

class Computed:
    """Stand-in for core.methods.computational_methods.Computed: it only holds the fields."""
    # Store the fields to send back as JSON.
    def __init__(self,
                 value: object,
                 n: object,
                 interval: object=None,
                 setup: object=None) -> None: ...

# core/methods/method_writer.py

class MethodWriter:
    """Asks a model for recomputation methods, tests them, and keeps the ones that work."""
    # Keep the settings of the calls.
    def __init__(self,
                 data_files: dict[str, Any],
                 root: str,
                 folder: Path,
                 ask: Ask,
                 retries: int=3,
                 needs_per_batch: int=20,
                 prompt_tokens_per_batch: int=20000) -> None: ...
    # The folder of the method modules.
    @property
    def methods_folder(self) -> Path: ...
    # The prompt for one batch: its needs, its masked passages and its data files.
    def prompt_for(self,
                   batch: list[MethodWriterNeed],
                   masked: set[str],
                   taken: frozenset[str]=frozenset()) -> str: ...
    # The needs in batches, with the needs of one data path pattern in one batch.
    def batches(self, needs: list[MethodWriterNeed]) -> list[list[MethodWriterNeed]]: ...
    # The names of the methods in the method folder.
    def taken_method_names(self) -> frozenset[str]: ...
    # The name of a new module: written_methods_<one more than the highest number>.py.
    def next_module_name(self) -> str: ...
    # Write one module to the method folder, run its methods, and give its problems.
    def test_module(self,
                    module: str,
                    mapping: dict[str, str],
                    todo: list[MethodWriterNeed],
                    name: str) -> tuple[list[str], dict[str, str]]: ...
    # Ask for methods for every need, batch by batch, and keep the methods that work.
    def write_methods(self, needs: list[MethodWriterNeed]) -> MethodWriterReport: ...

# core/methods/source_index.py

class SourceIndex:
    """The numbers and the words of the data files of one project."""
    # The index of the project in `root`, kept in its state folder `state_dir`.
    def __init__(self, root: Path, state_dir: Path) -> None: ...
    # Read again each data and log file whose checksum changed; forget removed files.
    def update(self) -> tuple[int, list[str]]: ...
    # The words of each file, keyed by its path: the documents of the text search.
    def documents(self) -> dict[str, str]: ...
    # The cells whose number could be `number`, printed at least as precisely.
    def cells_with(self, number: inputs.PrintedFloat) -> list[_Cell]: ...
    # How many numbers the index holds in each row, given as (path, row).
    def row_sizes(self, rows: list[tuple[str, int]]) -> dict[tuple[str, int], int]: ...
    # The likely source files of one claim, best first: at most 5 for each rule.
    def shortlist(self,
                  numbers: Collection[str],
                  query: str,
                  changed_files: Collection[str]=(),
                  search: text_search.TextSearch | None=None) -> list[SourceCandidate]: ...

# core/project/agent_session.py

class AgentSessionPolicy:
    """Which rounds start in a new session of the auditing agent."""
    kind: str
    token_limit: int
    # The policy from the run options, each at its default when it is not set.
    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> AgentSessionPolicy: ...
    # Whether the next round starts in a new session.
    def needs_fresh_session(self, rounds_in_session: int, tokens_read: int) -> bool: ...

# core/project/check_runs.py

class StaticCheckRun:
    """Checks one paper and its claim register once, with the built-in checks only."""
    # Run the checks of the registry `checks`; the built-in checks by default.
    def __init__(self, checks: checks.CheckRegistry | None=None) -> None: ...
    # Check the paper, read from the files `papers` as one document named by the first.
    def run(self,
            papers: Sequence[str],
            claim_register_path: str | None,
            paper_only: bool,
            chosen_claim_checks: frozenset[str] | None,
            run_options_given: Mapping[str, object]) -> verification_report.VerdictSummary: ...

class WatchedProjectCheckRun:
    """One check of a watched project: analyse, build the report, and write its files."""
    # Run the watched project in `root` with its state folder and these extensions.
    def __init__(self,
                 root: Path,
                 state_dir: Path,
                 extension_set: extensions.ExtensionSet) -> None: ...
    # Check every claim, save the report files, and give the Markdown and the verdict.
    def run(self,
            commit: str | None,
            use_cache: bool=True,
            claim_checks_not_run_because: str | None=None) -> CheckRunOutcome: ...

# core/project/watched_project.py

class WatchedProject:
    """One project folder and its state folder, and what a run of the project needs."""
    REGISTER_FILE: ClassVar[str]
    OPTIONS_FILE: ClassVar[str]
    METHOD_ENV: ClassVar[Path]
    DOCUMENT_START: ClassVar[re.Pattern[str]]
    # Hold the project folder `root`, its state folder, and the extensions to load.
    def __init__(self,
                 root: Path,
                 state_dir: Path,
                 extension_set: extensions.ExtensionSet) -> None: ...
    # One readable claim by id, and every readable claim by id.
    def claim_with_id(self,
                      check_registry: CheckRegistry,
                      claim_id: str) -> tuple[inputs.Claim, dict[str, inputs.Claim]]: ...
    # The watched project in `root`, with the extensions saved in `state_dir`.
    @classmethod
    def load(cls, root: Path, state_dir: Path) -> WatchedProject: ...
    # The built-in checks, the methods and plug-in checks of the state folder, and more.
    def check_registry(self) -> CheckRegistry: ...
    # The values in options.json, by key; none when the file does not exist.
    def saved_run_option_values(self, check_registry: CheckRegistry) -> dict[str, object]: ...
    # Replace options.json with `values`, by key. The caller checks them first.
    def save_run_option_values(self, values: Mapping[str, object]) -> None: ...
    # The run options of the project, which test-check also uses.
    def run_options(self,
                    check_registry: CheckRegistry,
                    saved_values: Mapping[str, object] | None=None) -> dict[str, object]: ...
    # The row of the report that says what the computational methods ran in.
    def method_environment_row(self) -> report_header.ReportHeaderRow: ...
    # The claim register of the project, or an empty one if none is saved yet.
    def claim_register(self, check_registry: CheckRegistry) -> inputs.ClaimRegister: ...
    # The parser of a claim register, for the methods and the claim keys of the registry.
    @staticmethod
    def claim_register_parser(check_registry: CheckRegistry) -> claim_register_parser.ClaimRegisterParser: ...
    # The full text of the project's paper, for the stages that read the whole paper.
    def full_paper_text(self) -> str | None: ...
    # Each document of the project (see documents_in_folder).
    def documents(self) -> dict[str, list[str]]: ...
    # Each document in a folder: its path relative to the folder, and its file.
    @classmethod
    def documents_in_folder(cls, root: Path) -> dict[str, list[str]]: ...
    # Run the paper checks on each document, and check every claim of the claim register.
    def analyse(self,
                check_registry: CheckRegistry | None=None,
                use_cache: bool=True,
                claim_checks_not_run_because: str | None=None) -> findings.Findings: ...

# core/registry/checks.py

class CheckInputScope:
    """What a claim check reads: which claims, which run options, and which files."""
    claims: str
    named_by: str | None
    option_keys: tuple[str, ...] | None
    other_files_read: FilesRead | None
    reads_full_paper_text: bool
    # Whether a result can be stored by what the check reads: not for the whole register.
    @property
    def can_be_cached(self) -> bool: ...
    # The ids of the claims the check reads for `claim`, its own first.
    def claim_ids(self, claim: inputs.Claim) -> tuple[ClaimId, ...]: ...
    # The context with only the claims, options and dependencies of this scope.
    def restricted_context(self,
                           claim: inputs.Claim,
                           context: ClaimContext) -> ClaimContext: ...
    # The files the check reads for `claim`, besides the claim register.
    def files_read(self,
                   claim: inputs.Claim,
                   options: Mapping[str, object]) -> tuple[str, ...]: ...

class ClaimCheck(Check):
    """One check run on each claim, after the gate."""
    input_scope: ClassVar[CheckInputScope]
    fields: Mapping[str, inputs.FieldType]
    needs: tuple[str, ...]
    needs_recompute: bool
    needs_dependencies: bool
    # The result of the check on one claim: Passed or Flagged.
    def check(self,
              claim: inputs.Claim,
              recomputed: recompute.Matches | None,
              context: ClaimContext) -> CheckResult: ...
    # Refuse an untyped key, a key the gate uses, an undeclared need, or both needs_*.
    def refuse_malformed_declarations(self) -> None: ...
    # Whether the check reads ClaimContext.dependencies in a run with these options.
    def reads_findings_of_claims_it_rests_on(self, options: Mapping[str, object]) -> bool: ...
    # Whether this claim check runs on a claim at all.
    def applies_to_claim(self, claim: inputs.Claim) -> bool: ...

class PaperCheckGroup(Check):
    """A set of paper checks run together on one document, one check for each problem kind."""
    must_run_if_requested: bool
    # One result for each check of the group, and the facts it counted.
    def run(self, context: PaperContext) -> PaperOutcome: ...
    # One Off result for each check, for a group that is switched off.
    def results_when_switched_off(self) -> dict[str, CheckResult]: ...

class ClaimContext:
    """What a claim check reads besides its claim and the recomputed number."""
    register: inputs.ClaimsById
    dependencies: Mapping[ClaimId, findings.ClaimFindings]
    options: Mapping[str, object]
    full_paper_text: str | None

class PaperContext:
    """What a paper check group reads."""
    paper: extract.Paper
    options: Mapping[str, object]
    elsewhere: frozenset[str]

class PaperOutcome:
    """What a paper check group returns: one result per check, and facts it counted."""
    checks: Mapping[str, CheckResult]
    facts: Mapping[str, int]

class CheckRegistry:
    """The checks, paper check groups, methods and run options one run uses, in order."""
    # Create an empty registry.
    def __init__(self) -> None: ...
    # A registry holding the same entries and run options, to which more can be added.
    def copy_for_one_run(self) -> CheckRegistry: ...
    # Add a claim check.
    def add_claim_check(self, check: ClaimCheck) -> None: ...
    # Add a paper check group.
    def add_paper_check_group(self, group: PaperCheckGroup) -> None: ...
    # Add a computational method. Refuses a repeated name.
    def add_computational_method(self,
                                 method_entry: computational_methods.ComputationalMethodEntry) -> None: ...
    # Add the RUN_OPTIONS of a module, and its options rules when it is trusted.
    def add_run_options_of_module(self, module: ModuleType, trusted: bool=True) -> None: ...
    # Refuse options of the wrong type, or that the rule of a check that is on refuses.
    def refuse_options_that_do_not_fit(self,
                                       options: Mapping[str, object],
                                       switched_off: frozenset[str]=frozenset()) -> None: ...
    # Every claim key a registered claim check reads, with its type.
    def claim_field_types(self) -> dict[str, inputs.FieldType]: ...
    # The claim checks that read one claim key, in registration order.
    def claim_checks_that_read_field(self, key: str) -> list[str]: ...
    # Everything that can be switched off by name: each paper group and claim check.
    def names_that_can_be_switched_off(self) -> tuple[str, ...]: ...
    # The checks that are on and must not be skipped (see Findings.must_run).
    def checks_that_must_not_be_skipped(self,
                                        switched_off: frozenset[str]) -> frozenset[str]: ...

# core/registry/extensions.py

class Extension:
    """One extension: a subclass overrides the hooks it uses. Each hook does nothing here."""
    # Add checks to the registry of one run.
    def extend(self,
               check_registry: checks.CheckRegistry,
               root: Path,
               state_dir: Path) -> None: ...
    # The rows for the top of the report.
    def notices(self,
                tree: findings.Findings,
                root: Path,
                state_dir: Path) -> list[report_header.ReportHeaderRow]: ...
    # The flags that a person disputed, which the report strikes out.
    def disputed_flags(self, root: Path, state_dir: Path) -> list[DisputedFlag]: ...
    # The commands that the extension adds to `verifier`, by name.
    def verifier_commands(self) -> dict[str, cli_command.CLICommand]: ...
    # The commands that the extension adds to `vcheck`, by name.
    def vcheck_commands(self) -> dict[str, cli_command.CLICommand]: ...
    # The lines for `verifier status`.
    def status_lines(self, root: Path, state_dir: Path) -> list[str]: ...
    # The warnings after a round of the daemon.
    def after_round_warnings(self, root: Path, state_dir: Path) -> list[str]: ...
    # How the extension describes its events to the auditing agent, by event kind.
    def event_describers(self) -> dict[str, EventDescriber]: ...
    # The section of the extension for AGENTS.md, or None.
    def agent_instructions(self) -> str | None: ...
    # The section of the extension for WORKER.md, or None.
    def worker_instructions(self) -> str | None: ...

class ExtensionSet:
    """The loaded extensions, in load order, and a call of each hook on all of them."""
    # Hold the extensions, in the order given.
    def __init__(self, extensions: Iterable[Extension]) -> None: ...
    # Import the named extension classes, and make one of each.
    @classmethod
    def load_named(cls, names: Iterable[str]) -> ExtensionSet: ...
    # One extension, from its name `module:Class`.
    @staticmethod
    def extension_named(name: str) -> Extension: ...
    # The extensions saved in the state folder `state_dir`, loaded.
    @classmethod
    def of_project(cls, state_dir: Path) -> ExtensionSet: ...
    # The extension names saved in `state_dir`, in load order; none with no file.
    @staticmethod
    def saved_names(state_dir: Path) -> tuple[str, ...]: ...
    # The extension names saved in `state_dir`, or DEFAULT_EXTENSIONS when none are saved.
    @classmethod
    def saved_or_default_names(cls, state_dir: Path) -> tuple[str, ...]: ...
    # Whether `state_dir` has a list of extension names.
    @staticmethod
    def names_are_saved(state_dir: Path) -> bool: ...
    # Save the extension names in `state_dir`, in load order.
    @staticmethod
    def save_names(state_dir: Path, names: tuple[str, ...]) -> None: ...
    # A copy of the registry with the checks of every extension added.
    def add_checks_to_registry(self,
                               check_registry: checks.CheckRegistry,
                               root: Path,
                               state_dir: Path) -> checks.CheckRegistry: ...
    # The rows of every extension for the top of the report, in load order.
    def report_notices(self,
                       tree: findings.Findings,
                       root: Path,
                       state_dir: Path) -> list[report_header.ReportHeaderRow]: ...
    # The disputed flags of every extension, in load order.
    def disputed_flags(self, root: Path, state_dir: Path) -> list[DisputedFlag]: ...
    # The commands of every extension for `verifier`.
    def verifier_commands(self) -> dict[str, cli_command.CLICommand]: ...
    # The commands of every extension for `vcheck`.
    def vcheck_commands(self) -> dict[str, cli_command.CLICommand]: ...
    # The lines of every extension for `verifier status`.
    def status_lines(self, root: Path, state_dir: Path) -> list[str]: ...
    # The warnings of every extension after a round of the daemon.
    def after_round_warnings(self, root: Path, state_dir: Path) -> list[str]: ...
    # The sections of every extension for AGENTS.md, the guide of the auditing agent.
    def agent_instructions(self) -> list[str]: ...
    # The sections of every extension for WORKER.md, the guide of the worker's agent.
    def worker_instructions(self) -> list[str]: ...
    # How every extension describes its events to the auditing agent, by event kind.
    def event_describers(self) -> dict[str, EventDescriber]: ...

# core/registry/plugin_checks.py

class PluginCheckFolderLoader:
    """Adds the plug-in checks of a checks folder to a copy of a check registry."""
    # A copy of `base` with the claim checks of the files in `folder` added.
    def registry_with_plugin_checks(self,
                                    base: checks.CheckRegistry,
                                    folder: Path | None) -> checks.CheckRegistry: ...
    # The concrete PluginCheck subclasses that a module defines, in their file order.
    @staticmethod
    def plugin_check_classes_in_module(module: ModuleType) -> list[type[PluginCheck]]: ...
    # Import one plug-in check file as a module named after it.
    @staticmethod
    def import_check_file(path: Path) -> ModuleType: ...

# core/registry/run_options.py

class RegistryError(VerifierError):
    """A check, group, method or run option that cannot be registered as written."""

class OptionsError(VerifierError):
    """A run option has the wrong type, or does not fit with the others."""

# core/report_model/axis.py

class Axis(StrEnum):
    """One axis of the triage board."""
    # The axis of a problem with this severity.
    @classmethod
    def of_severity(cls, severity: Severity) -> Axis: ...
    # The axes in the order the triage board prints them.
    @classmethod
    def in_board_order(cls) -> tuple[Axis, ...]: ...
    # The place of the axis in report order, from 0.
    @property
    def rank(self) -> int: ...
    # The heading of the axis on the triage board.
    @property
    def board_heading(self) -> str: ...
    # How much an issue on this axis matters to the reader.
    def impact(self, disputable: bool) -> str: ...
    # What the axis means for the verdict, shown next to each issue type.
    @property
    def effect(self) -> str: ...
    # The first sentence of each issue type of flags on this axis, or None.
    @property
    def reader_warning(self) -> str | None: ...

# core/report_model/disputed_flags.py

class DisputedFlag(IssueIdentity):
    """One flag that a dispute says is false: the flag and the dispute."""
    dispute: FlagDispute

# core/report_model/issue_collector.py

class IssueCollector:
    """Lists each issue of a Findings, with the place of its flag in the tree."""
    NOT_RUN_SUFFIX: ClassVar[str]
    CLAIM_CHECKS_NOT_RUN: ClassVar[str]
    # Every issue a reader must see, with the place of its flag in the tree.
    def collect_issues(self, tree: findings.Findings) -> list[LocatedIssue]: ...
    # What provenance and then recompute found for a claim, each as (step, text).
    @staticmethod
    def gate_steps(c: findings.ClaimFindings,
                   register_folder: str | None) -> tuple[tuple[str, str], ...]: ...

# core/report_model/issues.py

class IssueIdentity:
    """The code, subject and detail of an issue."""
    code: str
    subject: str
    detail: str
    # The code, subject and detail alone, which name the flag in every report.
    def identity(self) -> IssueIdentity: ...

class FlagDispute:
    """The dispute that says a flag is false: its id and its reason."""
    dispute_id: str
    reason: str
    # The dispute as the saved report writes it.
    def to_json(self) -> dict[str, object]: ...

class Issue(IssueIdentity):
    """One issue of the report: its type, the board axis of that type, what it is about, and what."""
    axis: Axis
    facts: tuple[tuple[str, str], ...]
    measured: float | None
    flag_id: str
    disputable: bool
    dispute: FlagDispute | None
    # True when a dispute names this issue.
    @property
    def is_disputed(self) -> bool: ...

class FlagPlaceInTree:
    """Where the flag of an issue is in the Findings tree, so a copy of the tree can leave it out."""
    paper: str | None
    claim: int | None
    check: str | None
    index: int | None

class ClaimIssueGroup:
    """The issues of one claim, most severe first, under the claim's id and statement."""
    CLAIM_FACTS: ClassVar[frozenset[str]]
    claim_id: str
    statement: str | None
    register_place: int
    register_size: int
    inferred_from_places: tuple[int, ...]
    issues: tuple[Issue, ...]
    # The place of the claim in the register order, as words: "Claim 56 of 112".
    @property
    def place_words(self) -> str: ...
    # The register places of the claims it is inferred from, as words, or None.
    @property
    def inferred_from_words(self) -> str | None: ...
    # The facts of an issue, less the statement and the claims it is inferred from.
    @classmethod
    def facts_without_claim(cls, issue: Issue) -> tuple[tuple[str, str], ...]: ...
    # How many issues the claim has, and how many of them are disputed, as words.
    def count_words(self) -> str: ...

class IssueGroup:
    """The issues of one type, in report order."""
    code: str
    issues: tuple[Issue, ...]
    # The board axis of the type.
    @property
    def axis(self) -> Axis: ...
    # The first sentence of the type: the warning of its axis when its issues are flags.
    @property
    def reader_warning(self) -> str | None: ...
    # How many issues of the group are disputed.
    def disputed_count(self) -> int: ...
    # How many issues the type has, and how many of them are disputed, as words.
    def count_words(self) -> str: ...
    # How many issues the type has, and how many of them are disputed, for a table cell.
    def count_table_cell(self) -> str: ...
    # How both renderers show this group.
    def layout(self) -> IssueGroupLayout: ...

class ReportSectionKind(StrEnum):
    """One section of the report, in report order."""
    # The heading of the section.
    @property
    def heading(self) -> str: ...
    # The sentence under the heading of the section, or None when it has none.
    @property
    def note(self) -> str | None: ...

class ReportSection:
    """One section of the report: its groups of issues, in report order."""
    kind: ReportSectionKind
    groups: tuple[IssueGroup | ClaimIssueGroup, ...]
    # The issues of the section, in report order.
    @property
    def issues(self) -> tuple[Issue, ...]: ...
    # The warning of the caveat flags, once for the section, or None when it has none.
    @property
    def reader_warning(self) -> str | None: ...
    # How many issues the section has, and how many of them are disputed, as words.
    def count_words(self) -> str: ...
    # How many issues the section has, and how many of them are disputed, for a table cell.
    def count_table_cell(self) -> str: ...

# core/report_model/report_builder.py

class VerificationReportBuilder:
    """Builds a VerificationReport once from a Findings."""
    # Build reports with the collectors of this package and the verdict policy.
    def __init__(self) -> None: ...
    # The report of one run.
    def build_report(self,
                     tree: findings.Findings,
                     commit: str | None,
                     rows_under_verdict: Iterable[ReportHeaderRow]=(),
                     disputed_flags: Iterable[DisputedFlag]=()) -> VerificationReport: ...

# core/report_model/report_header.py

class ReportHeaderRow:
    """One row under the verdict: a name, a text, and its indented details as (depth, text)."""
    name: str
    text: str
    details: tuple[tuple[int, str], ...]
    # The row as JSON: [name, text, [[depth, text], ...]].
    def to_json(self) -> list[object]: ...

# core/report_model/triage_board.py

class TriageBoard:
    """The findings of one run on the six axes, each axis in board order."""
    entries_by_axis: Mapping[Axis, tuple[TriageBoardEntry, ...]]
    # The entries on one axis, in board order.
    def entries_on_axis(self, axis: Axis) -> tuple[TriageBoardEntry, ...]: ...
    # The board as a dict of axes, each a list of {code, detail}, for static-check --json.
    def to_json(self) -> dict[str, list[dict[str, str]]]: ...

# core/report_model/verification_report.py

class VerdictSummary:
    """The verdict of the findings, their triage board, and the sentence that explains it."""
    findings: schema_findings.Findings
    verdict: Verdict
    triage_board: TriageBoard
    reason_for_verdict: str
    # The verdict of `tree` by the verdict policy, its board, and the reason.
    @classmethod
    def of_findings(cls, tree: schema_findings.Findings) -> VerdictSummary: ...
    # The verdict, why, the triage board and the findings, as one JSON object.
    def to_json(self) -> dict[str, object]: ...

class VerificationReport(VerdictSummary):
    """Everything one run of `vcheck run` shows, computed once."""
    commit: str | None
    verdict_without_disputed_flags: Verdict | None
    report_sections: tuple[ReportSection, ...]
    rows_under_verdict: tuple[ReportHeaderRow, ...]
    header_rows: tuple[ReportHeaderRow, ...]
    measured_numbers: tuple[MeasuredNumber, ...]
    checks_not_run: ChecksNotRunTable
    DISPUTE_HEADING: ClassVar[str]
    DISPUTE_COMMANDS: ClassVar[tuple[tuple[str, str], ...]]
    DISPUTE_SEND: ClassVar[str]
    OVERVIEW_HEADING: ClassVar[str]
    # The sections with an issue, and the caveats section when a check did not run.
    @property
    def sections_to_show(self) -> list[ReportSection]: ...
    # True when a flag of the report can be disputed, so the report says how.
    @property
    def shows_dispute_help(self) -> bool: ...
    # How many issues each severity (board axis) has, most severe first; none with 0.
    def issue_counts_by_severity(self) -> tuple[tuple[Axis, int], ...]: ...
    # Every issue of the report, in report order: the sections, then their groups.
    @property
    def issues(self) -> tuple[Issue, ...]: ...
    # The verdict without the disputed flags, when it differs from the verdict.
    @property
    def second_verdict_to_show(self) -> Verdict | None: ...
    # How many disputed flags the second verdict leaves out, as words.
    def disputed_flags_removed_words(self) -> str: ...
    # How many issues the report has, as words.
    def issue_count_words(self) -> str: ...
    # The text of the "Methods ran in" row, or None when the run gave none.
    @property
    def methods_ran_in(self) -> str | None: ...

class RunStartFailure:
    """A run of `vcheck run` that could not start: its commit and the error."""
    ACTION: ClassVar[str]
    VERDICT: ClassVar[Verdict]
    commit: str | None
    error: str

# core/report_output/report_comparer.py

class SavedReportComparer:
    """Compares two saved reports for `verifier diff`."""
    SCHEMA_VERSION: ClassVar[int]
    PASSING: ClassVar[frozenset[str]]
    # The change from report `old` to report `new`.
    def compare_reports(self, old: SavedReport, new: SavedReport) -> dict[str, object]: ...

# core/report_output/report_store.py

class ReportStore:
    """Writes and reads the report files of one state folder."""
    REPORTS_DIR: ClassVar[str]
    LAYOUT_SUFFIX: ClassVar[str]
    # Keep the reports of `state_dir`.
    def __init__(self, state_dir: Path) -> None: ...
    # The title of the PDF: the name of the project, the folder that holds the state.
    @property
    def pdf_title(self) -> str: ...
    # Write the report as Markdown, Typst, PDF and JSON, for the commit and as the latest.
    def save_report_files(self,
                          commit: str | None,
                          markdown: str,
                          typst: str,
                          pdf: bytes,
                          saved: SavedReport,
                          layout_data: Mapping[str, object]) -> None: ...
    # The saved report of a commit, or of "latest"; None if there is no such report.
    def load_saved_report(self, name: str) -> SavedReport | None: ...
    # The layout data of a report, or None for a report saved before the layout file.
    def load_layout_data(self, name: str) -> dict[str, object] | None: ...
    # The Markdown (".md") or the Typst markup (".typ") of a report, or None.
    def load_report_text(self, name: str, suffix: str) -> str | None: ...

# core/report_output/saved_report.py

class SavedReportError(VerifierError):
    """A saved report that does not have the keys and types that `vcheck run` writes."""

class SavedFlag(IssueIdentity):
    """One flag as a file saves it: its flag id, code, axis, subject, detail and facts."""
    JSON_KEYS: ClassVar[frozenset[str]]
    flag_id: str
    axis: Axis
    facts: tuple[tuple[str, str], ...]
    # The flag as a file saves it.
    def to_json(self) -> dict[str, object]: ...
    # Read one saved flag.
    @classmethod
    def from_json(cls, item: object) -> SavedFlag: ...
    # The flag in the JSON object `data`, whose keys are checked already.
    @staticmethod
    def flag_in(data: Mapping[str, object]) -> SavedFlag: ...

class SavedReport:
    """The saved JSON of one report, with the keys that `vcheck run` writes (SAVED_REPORT_KEYS)."""
    REPORT_ID_LENGTH: ClassVar[int]
    commit: str | None
    verdict: Verdict
    verdict_without_disputed_flags: Verdict | None
    methods_ran_in: str | None
    run_error: str | None
    findings_json: Mapping[str, object] | None
    issues: tuple[SavedIssue, ...]
    report_id: str
    # The saved form of the report of one run.
    @classmethod
    def from_report(cls, report: VerificationReport) -> SavedReport: ...
    # The saved form of the report of a run that could not start.
    @classmethod
    def from_start_failure(cls, failure: RunStartFailure) -> SavedReport: ...
    # The id of a report, which changes only when what a reader sees changes.
    @classmethod
    def compute_report_id(cls,
                          verdict: Verdict,
                          verdict_without_disputed_flags: Verdict | None,
                          issues: Sequence[SavedIssue],
                          run_error: str | None,
                          rows_under_verdict: Sequence[ReportHeaderRow]) -> str: ...
    # The issue of the report with this flag id, or None.
    def issue_with_flag_id(self, flag_id: str) -> SavedIssue | None: ...
    # The report as `vcheck run` writes it.
    def to_json(self) -> dict[str, object]: ...
    # Read a saved report.
    @classmethod
    def from_json(cls, data: object) -> SavedReport: ...
```
<!-- /spec:classes -->

### 7.4 `checks/`

The built-in checks. A claim check gives one result for one claim. A paper check group
gives one result for each of its problem kinds; the model review and the setup review give
one result for the whole group. `extension.py` adds them to a registry.

<!-- spec:classes checks -->
```python
# checks/extension.py

class BuiltinChecks(interface.Extension):
    """Adds every built-in check and its run options to a registry, in report order."""
    # Add the paper check groups, then the claim checks, then the run options.
    def extend(self,
               check_registry: interface.CheckRegistry,
               root: Path,
               state_dir: Path) -> None: ...

# checks/run_record.py

class RunRecordError(ValueError):
    """A run record, or a file it names, that cannot be used as written."""

# checks/uncertainty/uncertainty.py

class Premise:
    """One claim as the red team and the quantifier read it."""
    id: ClaimId
    statement: str
    value: float | None

class Attack:
    """Hypothetical results the red team proposes together against one claim."""
    hypotheticals: tuple[Hypothetical, ...]

class Assessment:
    """The quantifier's two numbers for one attack, each from 0 to 1."""
    believability: float
    breakage: float

class Local:
    """The uncertainty of one step of inference, from its premises to the claim."""
    value: float
    worst: Attack | None
```
<!-- /spec:classes -->

### 7.5 `runtime/`

The daemon of a working directory, its OpenCode server, the git hook, the rounds, and the
commands of `verifier`.

<!-- spec:classes runtime -->
```python
# runtime/agent/agent_launchers.py

class ReportRefresher:
    """Runs vcheck again for the commit of a checkout, so the report shows a change at once."""
    # Refresh the report of the checkout `root`, with its state folder.
    def __init__(self, root: Path, state_dir: Path) -> None: ...
    # Run vcheck for the commit of the checkout, then print the verdicts it saved.
    def refresh_report_and_print_verdicts(self) -> None: ...

# runtime/agent/inbox.py

class AgentInbox:
    """The inbox of one state folder: the events for the next round of the agent."""
    _last_ns: ClassVar[int]
    # The inbox in `folder`.
    def __init__(self, folder: Path) -> None: ...
    # Add one event to the inbox and return its file.
    def write_event(self, kind: str, **payload: object) -> Path: ...
    # Every event waiting in the inbox, oldest first.
    def pending_files(self) -> list[Path]: ...
    # One event. A file that is not valid JSON is returned as an `unreadable` event.
    @staticmethod
    def read_event(path: Path) -> dict[str, object]: ...
    # Move delivered events out of the inbox, keeping them for the record.
    def mark_delivered(self, files: list[Path]) -> None: ...
    # Every event, pending and delivered.
    def all_events(self) -> list[dict[str, object]]: ...

# runtime/agent/opencode.py

class ServerFileError(ValueError):
    """The server file of the daemon is not in the form that ServerInfo.save writes."""

class Client:
    """HTTP calls to one OpenCode server."""
    # Set up calls to the server on `port`. The calls log in with `password`.
    def __init__(self, port: int, password: str, timeout: float=30.0) -> None: ...
    # True if the server answers its health check.
    def healthy(self) -> bool: ...
    # Create a session and return its id.
    def create_session(self, title: str) -> str: ...
    # True if the server knows the session, False if it answers 404.
    def session_exists(self, session_id: str) -> bool: ...
    # True while the session is running a turn. An idle session is absent from the status.
    def busy(self, session_id: str) -> bool: ...
    # Send one message and return at once; the turn runs in the background.
    def prompt(self, session_id: str, text: str) -> None: ...
    # The turn's first error and last finish reason, from one fetch of the messages.
    def turn_outcome(self,
                     session_id: str,
                     error_since: float,
                     finish_since: float | None=None) -> TurnOutcome: ...
    # Stop the session's current turn.
    def abort(self, session_id: str) -> None: ...

# runtime/agent/rounds.py

class RoundHistory:
    """Reads and writes the round files of one state folder."""
    # The rounds in the state folder of `state_paths`.
    def __init__(self, state_paths: StatePaths) -> None: ...
    # The round files, oldest first; none when the folder does not exist yet.
    def round_files(self) -> list[Path]: ...
    # Every round, oldest first.
    def rounds(self) -> list[Round]: ...
    # The last `count` rounds, oldest first; fewer when fewer exist. Reads only them.
    def last_rounds(self, count: int) -> list[Round]: ...
    # The last round before round `n` in which the agent was woken, or None.
    def last_round_with_agent_before(self, n: int) -> Round | None: ...
    # The last round, or None before the first.
    def last_round(self) -> Round | None: ...
    # The last round if no daemon saw it end, else None.
    def open_round(self) -> Round | None: ...
    # The number of the next round.
    def next_round_number(self) -> int: ...
    # Write a round to rounds/<n>.json.
    def save(self, r: Round) -> None: ...
    # The model of the last round that got an event and ended with no error.
    def model_of_last_clean_round_for_each_event_value(self,
                                                       kind: str,
                                                       key: str) -> dict[str, str | None]: ...

# runtime/daemon.py

class Daemon:
    """Supervises the OpenCode server and delivers rounds for one working directory."""
    # Make a daemon for the working directory `root`. run() starts it.
    def __init__(self,
                 root: Path,
                 model: str=models.agent_preset.KIMI,
                 round_timeout: float=ROUND_TIMEOUT_SECONDS,
                 exit_when_idle: bool=False) -> None: ...
    # Create the state directory, its git repo and the agent's config, if missing.
    def prepare(self) -> None: ...
    # Write AGENTS.md, the template with each extension's section after it, if it changed.
    def write_agent_guide(self) -> bool: ...
    # The environment the agent's shell sees: only what it needs, nothing else.
    def env(self) -> dict[str, str]: ...
    # The environment of a vcheck step the daemon runs itself: the agent's, with the token.
    def step_env(self) -> dict[str, str]: ...
    # Start the OpenCode server if it is not running, and make sure the session exists.
    def ensure_server(self) -> None: ...
    # Make a new OpenCode session for the agent, with no round sent in it yet.
    def create_session(self) -> None: ...
    # Make a new session for round `n` if the agent session policy asks for one.
    def start_fresh_session_if_due(self, n: int) -> None: ...
    # What a new session reads before the events of its first round.
    def fresh_session_briefing(self, events: list[dict]) -> str: ...
    # Hold the lock and tick until stopped. Raises if another daemon holds the lock.
    def run(self) -> None: ...
    # Start OpenCode the first time, with FIRST_START_ATTEMPTS tries.
    def start_server_with_retries(self) -> None: ...
    # One pass: server, jobs, the round in flight, then delivery.
    def tick(self, now: float | None=None) -> None: ...
    # Send a follow-up if the agent's last step was cut by the output limit. True if sent.
    def continue_cut_turn(self,
                          now: float,
                          outcome: opencode.TurnOutcome | None=None) -> bool: ...
    # True when no round runs and no event waits in the inbox.
    def idle(self) -> bool: ...
    # Send every pending event as one round, if there are any.
    def deliver(self, now: float) -> None: ...
    # End round `r` without the agent, because the claim step `failed` failed.
    def check_papers_only(self,
                          r: rounds.Round,
                          failed: dict[str, object],
                          now: float,
                          began: float) -> None: ...
    # Run the claim steps of code before the agent is woken.
    def do_claim_steps(self, n: int) -> list[dict[str, object]]: ...
    # Run `vcheck reorder-claims`, then `vcheck link-claims`, after the agent's round.
    def do_link_steps(self, n: int) -> list[dict[str, object]]: ...
    # The timeout of one claim step in options.json, in seconds, read for round `n`.
    def claim_step_timeout(self, n: int) -> int: ...
    # Put what arrived on the tracked branch in the inbox, then move the checkout to it.
    def take_in_branch(self) -> None: ...
    # Move the checkout to `sha`, link the outputs and gitignored files, and record it.
    def move_checkout(self, sha: str) -> None: ...
    # Move the checkout back to the checked commit if its HEAD moved since.
    def restore_checkout(self) -> None: ...
    # Record the round's end, write the canonical report, and commit the state.
    def finish_round(self, now: float) -> None: ...
    # Run `vcheck run` on the checked commit `sha` for round `n`, and give its verdict.
    def check_commit(self, n: int, sha: str, *options: str) -> Verdict: ...
    # Write the checked commit in the claim files' new stamps, if their checks pass.
    def stamp_claim_files(self, r: rounds.Round, sha: str) -> None: ...
    # Put a failed round's events back in the inbox, and wait before sending them again.
    def requeue(self, r: rounds.Round, now: float) -> None: ...
    # Write the round in flight to rounds/<n>.json.
    def save_round(self) -> None: ...
    # Pick up a round the previous daemon sent but did not see end.
    def resume_round(self) -> None: ...
    # True if the agent's last round before the round in flight ran too long.
    def last_aborted(self) -> bool: ...

# runtime/project/paths.py

class StatePaths:
    """The paths inside one working directory's state directory."""
    root: Path
    # The state directory itself.
    @property
    def state(self) -> Path: ...
    # Holds the name of the tracked branch.
    @property
    def branch_file(self) -> Path: ...
    # The tracked branch at the commit being checked, which the agent and the checks read.
    @property
    def checkout(self) -> Path: ...
    # Holds the commit the daemon last moved the checkout to.
    @property
    def checked_file(self) -> Path: ...
    # The commit the daemon last moved the checkout to, or "" before the first one.
    def checked(self) -> str: ...
    # Events waiting for the next round.
    @property
    def inbox(self) -> Path: ...
    # Events already sent to the agent, kept for the record.
    @property
    def delivered(self) -> Path: ...
    # One record, log and status file per long-running process.
    @property
    def jobs(self) -> Path: ...
    # One file per pending wake-up.
    @property
    def timers(self) -> Path: ...
    # One record per round.
    @property
    def rounds(self) -> Path: ...
    # Daemon and OpenCode files that are never committed.
    @property
    def runtime(self) -> Path: ...
    # The OpenCode server's port, password and session id (mode 600).
    @property
    def server_file(self) -> Path: ...
    # Held by the daemon for its whole life, so only one runs per working directory.
    @property
    def lock(self) -> Path: ...
    # The daemon's raw stdout and stderr; its records are in the alert log too.
    @property
    def daemon_log(self) -> Path: ...
    # The alert log, for the verifier's developers (utils/alerts.py).
    @property
    def alerts_log(self) -> Path: ...
    # What the agent's long jobs wrote, one folder per job.
    @property
    def outputs(self) -> Path: ...
    # Make the outputs folder and the project's gitignored files reachable in a checkout.
    def link_outputs(self, tree: Path) -> None: ...
    # The project's gitignored paths to link into a checkout, relative to the project.
    def ignored(self) -> list[str]: ...
    # The tracked branch, as `verifier start` recorded it. Raises if it never did.
    def branch(self) -> str: ...
    # Create every directory the runtime writes to.
    def ensure(self) -> None: ...

# runtime/project/project_status.py

class Spending:
    """What the model calls of a watched project cost in dollars, in a span of time."""
    agent: opencode.AgentSpending
    model_calls: Mapping[str, spending.Total]
    # The cost of every call, in dollars.
    @property
    def cost(self) -> float: ...
    # How many of the Verifier's own calls gave no cost.
    @property
    def unknown(self) -> int: ...
    # The calls made at or after `since` (epoch seconds).
    @classmethod
    def of_project(cls, paths: StatePaths, since: float=0.0) -> Spending: ...
    # The total, the agent's calls, the Verifier's own calls, and the calls with no cost.
    def to_json(self) -> dict[str, object]: ...
    # Read the JSON that `to_json` gave, as a round file keeps it.
    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Spending: ...
    # One line on what the calls cost, per part.
    def status_line(self) -> str: ...
```
<!-- /spec:classes -->

### 7.6 `review/`

The decisions that only a person makes. The auditing agent asks with `vcheck ask`, a
channel tells a person, and the person answers with `verifier decide`.

<!-- spec:classes review -->
```python
# review/commands.py

class ReviewCommandBase(cli_command.CLICommand):
    """A review command, with the kinds it uses."""
    # Use these kinds; the registered kinds by default.
    def __init__(self, kinds: ReviewKindRegistry=REVIEW_KINDS) -> None: ...

# review/kinds/__init__.py

class ReviewKindRegistry:
    """Holds the review kinds by name."""
    # Hold the kinds, in the order given.
    def __init__(self, kinds: Iterable[ReviewKind]) -> None: ...
    # The kind registered under `name`.
    def kind_named(self, name: str) -> ReviewKind: ...
    # Every kind, in registration order.
    def all_kinds(self) -> list[ReviewKind]: ...
    # The guide of each kind for the auditing agent, in registration order.
    def agent_guide_text(self) -> list[str]: ...

# review/kinds/base.py

class ReviewKind(abc.ABC):
    """One kind of question to a person, and what each answer means for the claims."""
    name: ClassVar[str]
    title: ClassVar[str]
    options: ClassVar[Mapping[str, str]]
    option_effects: ClassVar[Mapping[str, str]]
    problem_kinds: ClassVar[tuple[ProblemKind, ...]]
    raised_by: ClassVar[str]
    agent_guide: ClassVar[str]
    # Whether an option of this kind is a positive answer.
    def option_is_positive(self, option: str) -> bool: ...
    # The facts of a new request and its question, from the key=value pairs given.
    def build_request_facts(self,
                            root: Path,
                            claims: Iterable[inputs.Claim],
                            given: Mapping[str, str]) -> tuple[dict[str, Scalar], str]: ...
    # Whether the facts a request was raised about still hold in the working directory.
    def facts_still_hold(self, facts: Mapping[str, Scalar], root: Path) -> bool: ...
    # The claims a request with these facts bears on.
    def ids_of_claims_it_bears_on(self,
                                  facts: Mapping[str, Scalar],
                                  claims: Iterable[inputs.Claim],
                                  root: Path) -> tuple[ClaimId, ...]: ...
    # The problems a request in this state gives each claim it bears on; none for none.
    def problems_for_claim(self,
                           request: ReviewRequest,
                           state: ReviewState) -> tuple[Problem, ...]: ...
    # What the person should see before answering, one fact per line.
    def evidence_for_person(self, request: ReviewRequest, root: Path) -> str: ...
    # Change the project after a new request is recorded. By default, nothing.
    def after_request_raised(self,
                             request: ReviewRequest,
                             root: Path,
                             state_dir: Path) -> None: ...
    # Change the project after a decision is recorded. By default, nothing.
    def after_decision_recorded(self,
                                request: ReviewRequest,
                                decision: ReviewDecision,
                                root: Path,
                                state_dir: Path) -> None: ...
    # Change the project after a request is withdrawn. By default, nothing.
    def after_request_withdrawn(self,
                                request: ReviewRequest,
                                root: Path,
                                state_dir: Path) -> None: ...

# review/states.py

class ReviewStateResolver:
    """Gives the state of each request from the ledger, the kind, and the project files now."""
    # Resolve states with these kinds, in the working directory `root`.
    def __init__(self, kinds: ReviewKindRegistry, root: Path) -> None: ...
    # The request's state now: withdrawn, superseded, answered, or waiting for an answer.
    def state_of_request(self, request: ReviewRequest, ledger: ReviewLedger) -> ReviewState: ...
    # Every request waiting for an answer, oldest first.
    def requests_waiting_for_answer(self, ledger: ReviewLedger) -> list[ReviewRequest]: ...

# review/store.py

class ReviewError(VerifierError, ValueError):
    """A request or decision the store refuses, with the reason."""

class ReviewLedger:
    """All requests, decisions and withdrawals, read once, oldest first."""
    requests: tuple[ReviewRequest, ...]
    decisions: tuple[ReviewDecision, ...]
    withdrawals: tuple[ReviewWithdrawal, ...]
    # One request by id.
    def request_with_id(self, request_id: str) -> ReviewRequest: ...
    # The first decision on a request, or None.
    def decision_for(self, request_id: str) -> ReviewDecision | None: ...
    # The first withdrawal of a request, or None.
    def withdrawal_for(self, request_id: str) -> ReviewWithdrawal | None: ...
    # The requests of one kind, oldest first.
    def requests_of_kind(self, kind_name: str) -> list[ReviewRequest]: ...

class ReviewStore:
    """Reads and appends the three files of `.verifier/reviews/`."""
    # Open the reviews in `state_dir`.
    def __init__(self, state_dir: Path) -> None: ...
    # Every request, decision and withdrawal.
    def read_ledger(self) -> ReviewLedger: ...
    # Hold the store lock for the block.
    def holding_lock(self) -> Iterator[None]: ...
    # Save a new request.
    def append_request(self, request: ReviewRequest) -> None: ...
    # Save a decision.
    def append_decision(self, decision: ReviewDecision) -> None: ...
    # Save a withdrawal.
    def append_withdrawal(self, withdrawal: ReviewWithdrawal) -> None: ...

# review/types.py

class ReviewRequest:
    """One question for a person, bound to the exact facts it is about."""
    id: str
    kind: str
    binding: str
    facts: Mapping[str, Scalar]
    question: str
    reason: str
    raised_by: str
    raised_in: str
    raised_at: str
    # The checksum of a kind and its facts. One open request per binding.
    @staticmethod
    def binding_for(kind_name: str, facts: Mapping[str, Scalar]) -> str: ...
    # Read one row of requests.jsonl.
    @classmethod
    def from_json(cls, row: dict[str, object]) -> ReviewRequest: ...
    # The row of requests.jsonl.
    def to_json(self) -> dict[str, object]: ...

class ReviewDecision:
    """A person's answer to one request. The saved keys are the field names."""
    request: str
    option: str
    note: str
    decided_at: str
    via: str
    # Read one row of decisions.jsonl.
    @classmethod
    def from_json(cls, row: dict[str, object]) -> ReviewDecision: ...
    # The row of decisions.jsonl.
    def to_json(self) -> dict[str, object]: ...

class ReviewWithdrawal:
    """The withdrawal of one request, with its reason and time."""
    request: str
    reason: str
    at: str
    # Read one row of withdrawals.jsonl.
    @classmethod
    def from_json(cls, row: dict[str, object]) -> ReviewWithdrawal: ...
    # The row of withdrawals.jsonl.
    def to_json(self) -> dict[str, object]: ...

class ReviewState(ComputedState):
    """The state of a request now. It waits when no person has answered it."""
    blocks_a_new_request_with_same_facts: ClassVar[bool]
    # The answer of the person, when the state comes from one.
    @property
    def decision(self) -> ReviewDecision | None: ...
```
<!-- /spec:classes -->

### 7.7 `diagnosis/`

The flags that a person or the worker's agent says are false. A dispute holds the flags it
names, and the next run reports them as disputed.

<!-- spec:classes diagnosis -->
```python
# diagnosis/diagnosis_document.py

class DiagnosisDocument:
    """One diagnosis file, read once: its level-2 sections in order, with their bodies."""
    text: str
    sections: tuple[tuple[str, str], ...]
    # Read the level-2 sections of a diagnosis.
    @classmethod
    def from_text(cls, text: str) -> DiagnosisDocument: ...
    # The headings, in order.
    @property
    def headings(self) -> list[str]: ...
    # The body of the section with this heading (the last one, when it repeats).
    def section(self, heading: str) -> str: ...
    # Each answer line of "Is each flag false", by flag id, in order.
    def answers_by_flag_id(self) -> dict[str, list[str]]: ...
    # The cause, without code marks and a full stop.
    def cause(self) -> str: ...

# diagnosis/dispute_states.py

class DisputeWithState:
    """One dispute and its state now."""
    dispute: Dispute
    state: DisputeState

class DisputeStateResolver:
    """Gives the state of each dispute from the ledger and its diagnosis file now."""
    # The disputes of `state_dir`.
    def __init__(self, state_dir: Path) -> None: ...
    # The state of one dispute. A diagnosis with a problem leaves it waiting.
    def state_of_dispute(self, dispute: Dispute, ledger: DisputeLedger) -> DisputeState: ...
    # Every dispute ever raised, oldest first, with its state.
    def states_of_all_disputes(self) -> list[DisputeWithState]: ...
    # The dispute with this id and its state, or None when there is no such dispute.
    def state_of_dispute_with_id(self, dispute_id: str) -> DisputeWithState | None: ...
    # How many disputes wait for a diagnosis.
    def count_waiting_for_diagnosis(self) -> int: ...

# diagnosis/dispute_store.py

class DisputeStore:
    """Reads and appends the two files of `.verifier/diagnoses/`, and reads each diagnosis."""
    # Open the disputes in `state_dir`.
    def __init__(self, state_dir: Path) -> None: ...
    # Every dispute and withdrawal.
    def read_ledger(self) -> DisputeLedger: ...
    # Where the diagnosis of a dispute is, from the state folder: `diagnoses/D-N.md`.
    @staticmethod
    def diagnosis_name_in_state_folder(dispute_id: str) -> str: ...
    # The diagnosis of a dispute, or None when it is not written.
    def read_diagnosis(self, dispute_id: str) -> DiagnosisDocument | None: ...
    # Hold the store lock for the block. A method that locks must not run inside it.
    def holding_lock(self) -> Iterator[None]: ...
    # Record a new dispute of `flags` (issues of a saved report) and return it.
    def raise_dispute(self, flags: list[SavedFlag], reason: str, commit: str) -> Dispute: ...
    # Record a new dispute of `flags` and return it. The caller holds the store lock.
    def raise_dispute_while_locked(self,
                                   flags: list[SavedFlag],
                                   reason: str,
                                   commit: str) -> Dispute: ...
    # Withdraw an active dispute. Its flags count again in the next report.
    def withdraw_dispute(self, dispute_id: str) -> None: ...

# diagnosis/records.py

class Dispute:
    """One saved dispute: its id D-N, the commit, the reason, the time, and its flags."""
    dispute_id: str
    commit: str
    reason: str
    raised_at: str
    flags: tuple[SavedFlag, ...]
    # Each flag of the dispute, as the report reads it.
    def disputed_flags_for_report(self) -> list[DisputedFlag]: ...
    # Read one row of disputes.jsonl.
    @classmethod
    def from_json(cls, row: dict[str, object]) -> Dispute: ...
    # The row of disputes.jsonl.
    def to_json(self) -> dict[str, object]: ...

class DisputeState(ComputedState):
    """The state of a dispute now. It waits while its diagnosis is not complete."""

class DisputeDiagnosisWritten(DisputeState):
    """The auditing agent wrote a diagnosis with no problem."""
    diagnosis: DiagnosisDocument
```
<!-- /spec:classes -->
