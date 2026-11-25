Verdict: MISSING INPUTS  (commit cccccccccccc)

**Supply what the run was missing, then run it again.**

- **Documents checked:** 0 (no tracked .tex file has \documentclass, so the paper checks are off)
- **Claims checked:** 13
- **Sources:** recomputed from data 4, inferred from other claims 4, missing file 1, changed file 1, traced to a log 1, anchor gone 1, no source 1

## How to Dispute False Issues

```bash
verifier dispute . --flag F-3a9c1e --why "your reason"  # mark one flag as false, with your reason
verifier diagnosis . --share  # write the diagnosis of your disputes to one file
```

Please read the file to double-check its anonymity, then send it to verifier@meno.sh.

## Overview of Issues

| Section | Issues |
|---|--:|
| [Setup Issues Needing Attention](#setup-issues-needing-attention) | 2 |
| [Claims Needing Attention](#claims-needing-attention) | 13 |
| [Caveats](#caveats) | 1 |

| Severity | Issues |
|---|--:|
| integrity issue (rejecting) | 5 |
| missing input (cannot pass) | 2 |
| soundness issue (blocking) | 2 |
| presentation issue (blocking) | 1 |
| strength issue (disclosure) | 5 |
| caveat (pass with caveats) | 1 |

## Setup Issues Needing Attention

### `claim-unreadable`

2 issues, missing input (cannot pass)

- `F-f47ad1` `entry 8 (bad-reason)`: claim 'bad-reason' source: `why` 'inconvenient' is not one of computed-offline, external-source, parser-superseded, pipeline-retired
- `F-4bb20e` `entry 11 (unknown-method)`: claim 'unknown-method' source: `method` 'ms' is not a known computational method; known are mean_post, trained_brier_post, trained_brier_pre

## Claims Needing Attention

### Claim 1 of 13: `good`

*Training lowers the Brier score to 0.075 on the main run.*

- `F-f8dc80` `undeclared-difference`, strength issue (disclosure): compared against prior but judge.model also differs ('claude-haiku-4.5' vs 'gpt-4o')

### Claim 2 of 13: `prior`

*The untrained model has a Brier score of 0.205.*

- `F-6c8d68` `caption-names-other-model`, presentation issue (blocking): caption says model 'Llama-3-8B', run setup says 'Qwen3-8B'
- `F-da9073` `below-baseline`, strength issue (disclosure): does not beat the trivial baseline: scored 0.205 against a baseline of 0.200, and lower is better here, so this result is no better than always predicting the base rate
- `F-751760` `too-few-seeds`, strength issue (disclosure): only 1 seed(s); the text must not claim reliability
- `F-3cb308` `sample-shrank`, strength issue (disclosure): scored episodes dropped to 4 from 10 -- measurement may be destroyed, not improved
- `F-759ec3` `undeclared-difference`, strength issue (disclosure): compared against good but judge.model also differs ('gpt-4o' vs 'claude-haiku-4.5')

### Claim 3 of 13: `disagrees`

- `F-f3e838` `number-disagrees`, integrity issue (rejecting)
  - **Paper:** 0.2
  - **Data:** 0.075
  - **Difference:** -0.125
  - **Tolerance:** 0.1

### Claim 4 of 13: `missing`

- `F-2ec93d` `artifact-missing`, integrity issue (rejecting): provenance: file missing: runs/gone/*.json; recompute: not recomputed: provenance failed

### Claim 5 of 13: `tampered`

- `F-f508cd` `hash-mismatch`, integrity issue (rejecting): provenance: checksum mismatch for results.json: file changed since it was recorded; recompute: not recomputed: provenance failed

### Claim 6 of 13: `setup-mismatch`

- `F-edc31a` `setup-mismatch`, integrity issue (rejecting): provenance: artifact present and matches its pinned checksum; recompute: the claim says model='Qwen3-32B', the data's own record says 'Qwen3-8B'

### Claim 8 of 13: `anchor-gone`

- `F-0f6aaf` `anchor-missing`, integrity issue (rejecting): provenance: anchor not found in run.md: 'brier 0.999'; recompute: not recomputed: provenance failed

### Claim 12 of 13: `loop-a`

*This claim rests on loop-b.*

Inferred from claim 13.

- `F-25f927` `circular-support`, soundness issue (blocking): loop-a is inferred from itself: loop-a -> loop-b -> loop-a

### Claim 13 of 13: `loop-b`

*This claim rests on loop-a.*

Inferred from claim 12.

- `F-24ca35` `circular-support`, soundness issue (blocking): loop-b is inferred from itself: loop-b -> loop-a -> loop-b

## Caveats

These do not block: a check did not run, or a cautious check flagged a sentence.

### `no-source`

1 issue, caveat (pass with caveats)

- `F-c8c2a6` `no-source (value 0.5)`: no source declared

### Checks that did not run

| Check | Why it did not run | Claims |
|---|---|--:|
| `run_data` | no `run` given | 8 |
| `run_integrity` | no `run` given | 8 |
| `run_outputs` | no `run` given | 8 |
| `caption` | no `model` given | 6 |
| `comparability` | no `compare_to` given | 6 |
| `seeds` | no `seeds` given | 6 |
| `baseline` | no `baseline` given | 2 |
| `interval` | the computational method gives no interval | 2 |
| `measurement` | no `baseline_n` given | 2 |
| `interval` | not recomputed: the claim register gives no source | 1 |
| `interval` | not recomputed: traced to a log on purpose | 1 |
