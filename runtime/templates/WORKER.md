# The Verifier: a guide for the worker's agent

The Verifier checks the claims in the notes of this project at each commit on the
tracked branch. Its auditing agent records the claims, and deterministic code checks
them. Each round ends with a report: one verdict and a list of flags.

Run each command from the project folder. Never edit anything in `.verifier/`.

## 1. What the flags are

- A flag is a lead, not a fact. About half of the flags can be false.
- One wrong computation often gives many later flags.
- You know the project. Judge each flag, and tell the person which issues are real.

## 2. When to read the report

1. After each commit on the tracked branch, continue your work. Do not wait for the round.
2. Run `verifier status . --json`.
3. The report is ready when `checked` is your commit or a later commit, `round.ended` is
   true, and `round.error` is null.

## 3. Where to read the report

- `verifier report . --json` gives the report under the key `report`: the verdict, the
  verdict without the disputed flags (`verdict_without_disputed`), the flags (`issues`),
  and the `report_id`.
- Each flag has an id, such as `F-3a9c1e`. The same flag has the same id in each report.
  In rare cases an id gets longer, because two flags of one report have the same start.
- To find the new flags, compare the full ids with the ids of the report that you read
  last. Read a flag whose id is new as a new flag, also when its id starts with an old id.
  `.verifier/reports/<commit>.json` holds the report of each commit.
- `.verifier/reports/latest.pdf` is the report for the person.

## 4. How to judge each flag

1. Open the code, the data and the note that the flag names.
2. Decide: false with certainty, real, or not certain.
   - "False with certainty" means that the code or the data shows that the flag is wrong.
   - A flag that depends on what the person meant is not certain.
3. Look for one cause of many flags.
4. Fix a real issue in the code or the data. Do not change a note only to remove a flag.

## 5. How to dispute a flag

The `impact` of each flag in `issues` tells you what to do. Put all the disputes of one
report in one command, because each dispute starts one paid round of the auditing agent.

    verifier dispute . --flag F-3a9c1e --flag F-0b72d4 --why "The method reads runs/old/; the notes use runs/fixed/."

- Low impact, false with certainty: dispute it. You do not have to tell the person.
- High impact, false with certainty: dispute it. Tell the person in your next message.
  A short list is enough.
- Not certain: ask the person first. Dispute it only after the person agrees, with the
  reason that the person approves.
- A flag whose `disputable` is false cannot be disputed.
- If you find that a disputed flag is real, withdraw the dispute:
  `verifier dispute . --withdraw D-N`.

## 6. How to share the report with the person

Do these steps in each reply to the person.

1. Read the `report_id` of the latest report.
2. If you already shared this `report_id`, stop here.
3. If you can attach files, attach `.verifier/reports/latest.pdf`.
4. If you cannot attach files, give the path of `latest.pdf` and a summary. The summary
   gives both verdicts, the real issues with their files, and the flags that you
   disputed or that need the person.
