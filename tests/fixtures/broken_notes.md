---
title: Broken notes, the Markdown twin of broken_paper.tex
---

## Abstract

Our method improves the score from 0.412 to 0.201, a clear advance.

## Results {#results}

The effect is significant and holds across settings.
Accuracy reached 148.5% of the previous state of the art, and this clause survives.
We measure +0.330 [+0.410, +0.220] for the treatment condition.
The control gives +0.900 [+0.100, +0.200], well outside expectations.
Performance of GPT-4o is discussed in [a section](#missing).
The setup is in [Setup](#setup), and the results again in [Results](#results).

Table 1: Results for Llama-3.1-8B under both conditions.
| condition | score | n |
| --- | --- | --- |
| Qwen3-32B | 0.201 | 100 |
| Qwen3-32B | 0.315 | 100 | 7 |

## Setup

<a id="results"></a>
The runs used the defaults.

```python
print(0.999)  # code is not a claim, so this number needs no table
```
