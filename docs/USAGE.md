# Using the Verifier

This page has the details the README leaves out: the main commands and options, and how a
check from the terminal works step by step. Start with the [README](../README.md) if you
have not installed the Verifier yet.

## Check a paper from the terminal

The paper must be in a git repository with at least one commit.

```bash
cd /path/to/paper
verifier start . --claims-from main.tex
```

What happens:

1. `start` reads the files you name in `--claims-from`. These are the paper's main `.tex`
   file and any Markdown notes. `--claims-from .` reads every note. Without the flag,
   `start` asks you.
2. It shows its estimate of the cost and the time and asks you to confirm. The estimate
   is rough and usually higher than the real cost.
3. It adds a git hook to the repository, starts the Verifier in the background, and opens
   a view where you can watch the agent work.
4. Each new commit on the branch starts a new check. Do not rename or delete the branch.

`start` follows the branch `main`. If your branch has another name, such as `master` (the
default of a plain `git init`), add `--branch master`. You cannot change the branch later.

`verifier status .` shows whether a check is running, the cost so far, and the verdict
of the latest report. `verifier stop .` stops the Verifier. `verifier attach .` opens the agent's view
again.

**Working with a coding agent.** `verifier start` writes a guide for coding agents to
`.verifier/WORKER.md`. It tells the agent when a report is ready, how to read it, and how
to dispute a flag. In Claude Code, add this line to the project's `CLAUDE.md`:

```
@.verifier/WORKER.md
```

Other agents that read `AGENTS.md` may not follow `@` imports. Point them at the file in
your own words instead.

## When a flag is wrong

```bash
verifier dispute . --flag F-3a9c1e --why "the table reports a different seed"
verifier diagnosis . --share -o disputes.md
```

`dispute` marks a flag as wrong. `verifier diagnosis .` shows your disputes and, once the
Verifier has worked it out, why it raised each flag. `--share` writes the disputes whose
diagnosis is ready to one Markdown file, in full. Read it to double-check its anonymity
before you send it to verifier@meno.sh. Each dispute
starts a paid round of the agent, so dispute all the flags of one report in one command
(`--flag A --flag B`).
`--withdraw D-N` cancels a dispute.

## Commands

| Command | Option | What it does |
|---|---|---|
| `verifier web` | | Opens the browser page, where you upload a paper and read its report. |
| | `--port PORT` | Sets the port on 127.0.0.1 (default 8765). |
| | `--runs-folder FOLDER` | Where checks are kept (default `~/.local/share/verifier/web-runs/`). |
| | `--no-browser` | Does not open the browser. |
| | `--model openrouter/<vendor>/<model>` | The model the agent runs on (default Kimi K3). |
| `verifier start DIR` | | Checks a paper in a git repository, and again on each new commit. |
| | `--branch NAME` | The branch to follow (default `main`). |
| | `--claims-from PATH...` | The files to read for claims (`.` for all notes). |
| | `--option KEY=VALUE` | Sets an option (see below). |
| | `--model openrouter/<vendor>/<model>` | The model the agent runs on (default Kimi K3). |
| | `--round-timeout SECONDS` | The longest one round of the agent may take (default 120 minutes). A check can take more than one round. |
| | `--no-attach` | Does not open the agent's view. |
| `verifier status DIR` | `--json` | Whether a check is running, the cost so far, and the verdict of the latest report. |
| `verifier report DIR` | `--json` | Prints the latest report. |
| `verifier attach DIR` | | Opens the agent's view again. |
| `verifier stop DIR` | | Stops the Verifier. |
| `verifier uninstall DIR` | | Stops the Verifier and removes its git hook. Keeps `.verifier/`. |
| `verifier static-check` | `--paper FILE...` | Runs the static checks on a paper and prints the result. `--json` prints JSON. |
| | `--claims FILE` | Gives a claim register. Then the command also checks the claims. |
| | `--uncertainty-readings FILE` | Saves the uncertainty readings in `FILE`. A claim register with inferred claims needs it, or `--no-claim-uncertainty`. |
| `verifier dispute DIR` | `--flag ID --why TEXT` | Marks a flag as wrong. `--withdraw D-N` cancels a dispute. |
| `verifier diagnosis DIR [D-N]` | `--share`, `-o PATH`, `--json` | Shows your disputes and why each flag was raised. `--share` writes them to one file, in full. Read it to double-check its anonymity before you send it to verifier@meno.sh. |
| `verifier diff DIR --from COMMIT` | | The changes from the report of `COMMIT` to the latest report, as JSON. |
| `verifier review DIR` | `--json` | Asks you each question that the agent is waiting on, and records your answers. |
| `verifier decide DIR REQUEST OPTION` | `--note TEXT` | Records 1 answer to 1 question of the agent. |
| `verifier alerts DIR` | `--level LEVEL` | The Verifier's own error log, for bug reports. |

`verifier init` and `verifier start-if-pending` exist for tools that run the Verifier for
you. `verifier COMMAND --help` lists every option of a command, also the options that
this page leaves out.

## Options

Pass options to `verifier start` as `--option KEY=VALUE`, for example
`--option want_model_review=true`.

| Option | Default | What it does |
|---|---|---|
| `want_setup_review` | `true` | The LLM read for setup errors. |
| `want_claim_uncertainty` | `true` | Uncertainty quantification of each claim that rests on other claims. |
| `want_model_review` | `false` | An extra model read of whether the claims follow from the measurements. |

The static checks (numbers, tables, references, captions and statistics) always run.

## Where files go

- A check from the terminal writes everything into `.verifier/` inside the project, except
  a git hook, its watch list, and a line in `.git/info/exclude`, which go in the project's
  `.git/` folder.
  `verifier uninstall .` removes the hook. The latest report is
  `.verifier/reports/latest.pdf` and `latest.md`.
- A check on the browser page lives in `~/.local/share/verifier/web-runs/` until you
  delete it on the page.
- The install lives in the `Verifier` folder you cloned (the code), `~/.local/bin` (the
  commands), uv's tool folder (the Python environment) and `~/.local/share/verifier/`
  (OpenCode, the program the agent runs in, and the checks from the browser page).
