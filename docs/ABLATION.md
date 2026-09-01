# Is the always-on context worth what it costs?

A measurement, its result, and what the result overturned.

## The question

An agent working in a real repository carries a static prefix into every turn: project
conventions, memory, tool definitions. On my system that was about **10.7k tokens**,
loaded on every session, re-read on every turn. The observed `cache_read_ratio` was
**178:1**.

That number reads like waste, and I treated it as waste. The plan was to trim the
always-on context. Before trimming it, I measured it.

## Phase 0.3 — where does the time actually go?

First, decompose the wall-clock. Migration `018` added `ttft_ms` and `duration_api_ms`,
both already reported by the executor on every run and previously discarded.

| | code-nav | retrieval |
|---|---:|---:|
| turns | 2.13 | 1.00 |
| mean wall-clock | 8,492 ms | 8,684 ms |
| **time to first token** | 2,726 ms (32%) | 3,620 ms (42%) |
| local / tool time | 691 ms | 584 ms |

Local time is negligible. Essentially all of it is API time, and a third to nearly half of
*that* is time-to-first-token — which is the static prefix being processed.

> **A methodology note worth keeping.** The first version of this table produced a
> *negative* local time, because I derived it from a median wall-clock against a mean API
> time. Components of a decomposition must share an estimator. Fixed to mean-vs-mean.

This made "shrink the always-on context" the obvious lever. So it was tested rather than
assumed.

## Phase 2 — the ablation

`--readonly-cwd` runs the frozen tasks from a bare directory. The agent loads its project
context from its working directory, so pointing it elsewhere removes the preloaded context
while changing **nothing else** about the task: same nine frozen retrieval tasks, same
model, same prompts, same assertions.

| Metric | with context | without | Δ |
|---|---:|---:|---:|
| `task_success_rate` | **1.000** | **0.222** | **−0.78** |
| `turns_per_run` | 1.00 | **5.44** | 5.4× |
| mean wall-clock | 8,684 ms | **27,334 ms** | 3.1× |
| `tokens_per_success` | 46,343 | **516,867** | **11.2×** |
| `ttft_ms` | 3,620 | 4,971 | 1.4× |

## What it means

**The ~10.7k of always-on context is the single best-value thing in the system.** Remove
it and the agent does not answer more cheaply. It launches a five-turn hunt, takes three
times as long, burns eleven times the tokens per success, and still gets it right 22% of
the time instead of 100%.

The context is precisely what collapses a multi-turn search into a single-turn answer —
and every avoided turn would have re-read a static prefix of its own.

This inverts the framing the work started from. The 178:1 cache-read ratio is not drag; it
is **the price of not needing more turns**, and it is heavily underpriced. Look at the
last row: TTFT barely moved (3.6s → 5.0s) while total time tripled. **Turns dominate
latency, and context is what suppresses turns.**

**So no trimming shipped, and that is the finding.** The remaining honest levers are:

1. Keep the always-on context *accurate*. A wrong always-loaded line is now provably
   expensive, which raises the value of the memory linter and the staleness check.
2. Reduce turns — which the context is already doing about as well as it can, at 1.0–2.1
   turns per task.

## Limitation, stated plainly

`--readonly-cwd` removes the preloaded context **and** moves the working directory out of
the repo. "Lost the rules" and "lost the repo as cwd" are therefore **not separable in
this design**. The direction is unambiguous at this effect size, but the exact split is not
measured.

A cleaner ablation would keep the working directory at the repo and strip only the project
convention file. That means temporarily editing a tracked file that every concurrent
session shares, which I chose not to do. Recorded as a caveat rather than smoothed over.

If you reproduce this, that is the first thing to improve.

## Reproducing

See the README. The demo suite ships the same primitive:

```bash
python3 agent_eval/eval_harness.py --repeats 3 --label with-context \
    --readonly-cwd evals/demo_suite/demo_repo
python3 agent_eval/eval_harness.py --repeats 3 --label no-context \
    --readonly-cwd "$(mktemp -d)"
python3 agent_eval/eval_harness.py --report --label with-context --vs no-context
```

Effect sizes on the eight-task demo suite will not match the numbers above — different
tasks, different repo, much smaller context. The *direction* should hold, and if it does
not on your system, that is worth knowing and is the reason to run it.
