# Scoring: a number the candidate cannot author

`eval_score.py` reduces a batch to one scalar. Everything about its design comes from
getting it wrong first.

## Why not let the agent report its own metrics

The first scored task wrote its own `metrics.json` — and therefore chose its own score.
That is not a measurement, it is a self-assessment with extra steps. The fix is to prefer a
score the agent cannot reach: this script reads `agent_runs`, rows written by the harness
rather than the candidate, and prints one number. A verification gate runs it as an
external command.

## Why not success rate

Success on the retrieval category was already at **ceiling** — the control batch scored
10/10. An objective that can only be tied is not an objective. Ranking attempts on a metric
pinned at 1.0 produces a lineage of ties that says nothing.

So correctness and cost are separated:

- **Correctness gate** — success rate must be ≥ `--min-success` (default 1.0, the measured
  control). Short of that, exit non-zero and print nothing rankable. A faster policy that
  gets answers wrong has not won.
- **Score** — mean wall-clock seconds, minimised.

## The two defects the first real scored run exposed

Both are now enforced in code, and both are the kind of thing that silently produces a
confident wrong answer.

### 1. The task set drifted underneath the comparison

The control was measured over 10 tasks. By the time a candidate ran, one task had been
removed from the suite, so the candidate was scored over 9. That task happened to be the
**slowest in the batch** (14.57s), so dropping it moved the incumbent from 9.09s to 8.48s
on its own — roughly **57% of the apparent improvement was the missing task, not the
policy.**

`--require-tasks` now pins the set. A batch that does not cover exactly the expected task
ids is **refused, not scored**.

### 2. The margin was inside the noise

Repeat-to-repeat spread on the same task averaged ~1.0s, with per-task deltas ranging −3.9s
to +3.5s. The measured 0.83s mean gain gave t ≈ −1.0, p ≈ 0.34 over 9 tasks.

Wall-clock is a measurement, but a measurement **with variance**, and ranking on one sample
of it is the multiple-comparisons hazard wearing a safe costume. The scorer now requires
repeats, reports the standard error, and demands a `min_delta` wider than the noise.

**A tie is the correct verdict for a difference that size.** Reporting it as a win would
have been the easy thing to do and would have been wrong.

## Measured baseline (retrieval category)

| batch | success | mean wall-clock | verdict |
|---|---:|---:|---|
| `retrieval-probe2` (control) | 1.00 | 9.09s (10 tasks) / 8.48s (common 9) | incumbent |
| `latency-policy-v2` | 1.00 | 7.65s (9 tasks, 2 repeats) | **TIE** — inside the noise |
| `kb-first-retrieval` | 0.90 | 24.6s | **lost** — failed the correctness gate |

That last row is worth dwelling on. A knowledge-base-first retrieval policy looked
obviously correct on paper. It lost its own A/B, badly, on both axes. It was reverted.

## Usage

```bash
python3 agent_eval/eval_score.py --label <batch> [--category retrieval] [--min-success 1.0]
```
