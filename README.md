# agent-eval-harness

**Measuring what an agent's always-on context is actually worth.**

Everyone building with coding agents has an opinion about context: how much to load,
what to trim, whether the big static prefix re-read on every turn is waste. Opinions are
cheap because the question is hard to measure — you can tell when a pipeline *feels*
better, and that is not evidence.

This is the harness I built to settle it on my own system, and the result it produced.

## The short version

I ran the same nine tasks with and without the always-on context — the project rules and
memory loaded into every session — in a git worktree that still had the whole codebase, so
the only thing missing was the rules. Success dropped from 96% to 67%, it took 3.2× the
turns, and 2.7× the tokens per completed task. Take the codebase away as well and it gets
far worse: 22% success and 11.2× the tokens. So I cancelled the plan to trim it.

Then I looked at what the context actually costs. Caching matches the longest identical run
of bytes at the *start* of the prompt, and I had a task ID interpolated near the top, above
3.5 KB of text that never changes. Moving everything stable to the front and everything
variable to the back took the cached portion from 32% to 93%.

I tried one more reorder and stopped: it would only have paid off below the provider's
1,024-token cache minimum, so it bought nothing and risked the persona no longer leading
the prompt.

The real cost isn't the opening prompt anyway — it's the same context replayed on every
turn of a long task. So the agent is instructed to checkpoint at about 80% of context and
continue from that summary instead of its full history.

Two of those three findings are optimisations I killed with my own measurements. The
details are in [docs/ABLATION.md](docs/ABLATION.md) and
[docs/CACHE_SHAPE.md](docs/CACHE_SHAPE.md).

## The result

Nine frozen tasks with machine-checkable assertions, same tasks and same model
throughout. Three conditions, because removing the context by running from an empty
directory takes away the *codebase* too — and the two causes imply opposite actions.

**Rules removed, codebase still present** (git worktree, convention file deleted;
27 runs per arm):

| Metric | baseline | rules removed | Δ |
|---|---:|---:|---:|
| task success rate | 0.963 | **0.667** | −0.30 |
| turns per run | 1.00 | **3.19** | 3.2× |
| tokens per success | 51,535 | **136,939** | **2.7×** |
| mean wall-clock | 11.0 s | 23.8 s | 2.2× |
| time to first token | 6,481 ms | **3,234 ms** | **0.5×** |

**Rules *and* codebase removed** (bare directory), for comparison:

| Metric | baseline | both removed | Δ |
|---|---:|---:|---:|
| task success rate | 1.000 | **0.222** | −0.78 |
| turns per run | 1.00 | **5.44** | 5.4× |
| tokens per success | 46,343 | **516,867** | **11.2×** |

So roughly **40% of the damage is not knowing the rules, and 60% is not having the code to
hand.** Both are large. Even with the entire repository available to search, stripping the
project context costs thirty points of success and 2.7× the tokens.

I went in expecting the opposite. `cache_read_ratio` sat at 178:1 and I had it filed as
drag — big contexts re-read every turn. It is better understood as **the price of not
needing more turns**, and on this evidence it is underpriced. Look at the last row: **TTFT
halves** without the context, and total time still doubles. Turns dominate latency, and
context is what suppresses turns.

The intended optimisation — trim the always-on context — was cancelled by its own
measurement. That is the finding.

Full write-up, including the honest limitation of this ablation design:
**[docs/ABLATION.md](docs/ABLATION.md)**.

## What's here

| | |
|---|---|
| `agent_eval/eval_harness.py` | Loads YAML tasks, runs each through a headless agent CLI, checks assertions, writes one row per attempt to Postgres. Parallel, read-only by default. |
| `agent_eval/eval_score.py` | A single scalar `f` over a batch, computed from the ledger rather than self-reported by the candidate. See [docs/SCORING.md](docs/SCORING.md). |
| `agent_eval/context_profile.py` | Attributes tool-output volume by tool and by call, from each run's own transcript — so you fix the cause, not the symptom. |
| `agent_eval/retrieval_log.py` | Reconstructs what each run actually retrieved (memory / KB / graph / always-on) and judges whether it was used. |
| `schema/001_agent_runs.sql` | The `agent_runs` and `agent_retrievals` ledger. Token accounting, latency decomposition, per-assertion log, response text. |
| `evals/demo_suite/` | Eight runnable demo tasks against a small fixture repo. |
| `docs/METHODOLOGY.md` | How to write a task that measures your system instead of the model. **The most transferable part of this repo.** |
| `docs/CACHE_SHAPE.md` | The companion result: keep the context, then shape it so it caches. Shared prefix 32% -> 93%, and one follow-up optimisation measured and refused. |

## Quick start

```bash
pip install -r requirements.txt
createdb agent_evals && psql agent_evals -f schema/001_agent_runs.sql

# Full demo suite, 3 repeats, labelled batch
python3 agent_eval/eval_harness.py --repeats 3 --label baseline \
    --readonly-cwd evals/demo_suite/demo_repo

# One category, no DB writes, prints per-assertion results
python3 agent_eval/eval_harness.py --category retrieval --dry-run \
    --readonly-cwd evals/demo_suite/demo_repo

# Compare two batches
python3 agent_eval/eval_harness.py --report --label baseline --vs my-change
```

### Reproducing the ablation

`--readonly-cwd` is the ablation primitive: it runs the frozen tasks from a directory you
choose, changing nothing else. Point it at the fixture repo to get "with context", and at
a bare directory to get "without".

```bash
python3 agent_eval/eval_harness.py --repeats 3 --label with-context \
    --readonly-cwd evals/demo_suite/demo_repo

python3 agent_eval/eval_harness.py --repeats 3 --label no-context \
    --readonly-cwd "$(mktemp -d)"

python3 agent_eval/eval_harness.py --report --label with-context --vs no-context
```

## Executor

The harness shells out to a headless agent CLI and parses its JSON output, so token and
cost accounting come from the executor's own ground truth rather than an estimate. It
defaults to `~/.local/bin/claude`; override with `AGENTEVAL_EXECUTOR_BIN`. The executor is
a subprocess boundary, so swapping in another CLI that can emit JSON usage is a small
change, not a rewrite.

## Requirements

Python 3.11+, PostgreSQL 14+, `psycopg2`, `PyYAML`, and a headless agent CLI on PATH.

## Provenance

Extracted from the agent harness of a private production system and generalised. The
measurements above are real and were taken on that system; the demo suite here is a
synthetic stand-in, because the original tasks encode facts about a codebase that is not
public. The harness, the schema and the methodology are unchanged.

MIT licensed.
