# Is the always-on context worth what it costs?

A measurement, its result, and what the result overturned.

## The question

An agent working in a real repository carries a static prefix into every turn: project
conventions, memory, tool definitions. On my system that was about **10.7k tokens**, loaded into
every session and re-read on every turn, with an observed `cache_read_ratio` of **178:1**.

That number reads like waste, and I treated it as waste. The plan was to trim the always-on
context. Before trimming it, I measured it.

## Why one ablation is not enough

The obvious ablation is to run the agent from an empty directory. The agent loads its project
context from its working directory, so pointing it elsewhere removes the context.

That removes **two** things at once:

- **A — the rules.** The project conventions and memory that would have been preloaded.
- **B — the codebase.** The agent is no longer standing *in* the repo, so it cannot grep or read
  files relative to its working directory. It has to go and find them first.

One change, two causes. Measure it and you get a number you cannot attribute.

This matters because the two imply **opposite actions**. If the damage is mostly A, invest in the
context and keep it accurate. If it is mostly B, the lesson is just "run agents inside the repo"
— and the context could be trimmed after all.

So the study needs a third condition that isolates A: a **git worktree** — a full, independent
checkout — with only the project convention file deleted inside it. The agent then has the entire
codebase under its feet and none of the rules. Nothing in the shared checkout is touched, so
concurrent work is unaffected.

That distinction is the crux. The eval tasks are all repo-specific facts, and most are
*discoverable* by searching the codebase. Whether the agent still finds them when nobody tells it
is exactly the question.

## Three conditions

Nine frozen tasks with machine-checkable assertions, identical across every arm (verified by
task-id set, not assumed).

| Condition | rules | repo under cwd | date | runs |
|---|:--:|:--:|---|--:|
| **Baseline** | ✅ | ✅ | 2026-08-06 / 2026-09-02 | 9 / 27 |
| **Rules removed** (worktree, convention file deleted) | ❌ | ✅ | 2026-09-02 | 27 |
| **Both removed** (bare directory) | ❌ | ❌ | 2026-08-06 | 9 |

Each treatment arm is compared against the baseline measured **on the same day**, because the two
pairs are four weeks apart on different model builds.

### Pair 1 — rules removed, codebase still present (2026-09-02, 3 repeats/task)

| Metric | baseline | rules removed | Δ |
|---|---:|---:|---:|
| task success rate | 0.963 | **0.667** | −0.30 |
| turns per run | 1.00 | **3.19** | 3.2× |
| tokens per success | 51,535 | **136,939** | **2.7×** |
| mean wall-clock | 11.0 s | 23.8 s | 2.2× |
| time to first token | 6,481 ms | **3,234 ms** | **0.5×** |

### Pair 2 — rules *and* codebase removed (2026-08-06)

| Metric | baseline | both removed | Δ |
|---|---:|---:|---:|
| task success rate | 1.000 | **0.222** | −0.78 |
| turns per run | 1.00 | **5.44** | 5.4× |
| tokens per success | 46,343 | **516,867** | **11.2×** |
| mean wall-clock | 8.7 s | 27.3 s | 3.1× |
| time to first token | 3,620 ms | 4,971 ms | 1.4× |

## The decomposition

Reading the two pairs together separates the causes:

| | success drop | extra turns | tokens per success |
|---|---:|---:|---:|
| **Losing the rules alone** | −0.30 | +2.19 | **2.7×** |
| **Losing the rules *and* the codebase** | −0.78 | +4.44 | **11.2×** |
| *therefore, the marginal cost of having to find the repo* | *≈ −0.48* | *≈ +2.25* | *≈ 4.1× on top* |

**Roughly 40% of the damage is not knowing the rules; roughly 60% is not having the code to hand.**
Both matter, and neither is small. The bottom row is an approximation across two runs four weeks
apart, not a measured arm — treat it as an order of magnitude, not a coefficient.

## What it means

**The always-on context earns its keep, and the case does not rest on the confound.** Even with
the entire codebase available to search, removing the project context drops success by thirty
points, triples the turns, and costs **2.7× the tokens per completed task**.

Look at the last row of pair 1, because it is the mechanism. **Time to first token halves** when
the context is removed — a smaller prefix genuinely does start faster, and TTFT falls from 59% of
wall-clock to 14%. The agent still takes twice as long overall, because it now needs 3.2 turns
instead of 1. **Turns dominate latency, and context is what suppresses turns.**

That inverts the framing this work started from. The 178:1 cache-read ratio is not drag; it is
**the price of not needing more turns**, and it is underpriced.

**So no trimming shipped, and that is the finding.** The remaining honest levers are to keep the
context *accurate* — a wrong always-loaded line is now provably expensive — and to reduce turns,
which the context is already doing about as well as it can at 1.0 turns per task.

For what to do once you have decided to keep the context, see [CACHE_SHAPE.md](CACHE_SHAPE.md).

## The per-task result is the interesting one

The aggregate hides two distinct failure modes. From the rules-removed arm:

| Task | with rules | rules removed | turns without |
|---|---:|---:|---:|
| `ret_db_host` | 1.00 | **0.00** | 2.67 |
| `ret_trifecta_scope` | 1.00 | **0.00** | 3.67 |
| `ret_option_expiry_valuation` | 1.00 | **0.33** | 1.00 |
| `ret_930_contamination` | 0.67 | 0.67 | 2.67 |
| `ret_market_calendar` | 1.00 | 1.00 | **6.00** |
| `ret_net_gex_semantics` | 1.00 | 1.00 | 1.67 |
| `ret_outbound_http` | 1.00 | 1.00 | 3.33 |
| `ret_entry_ask_exit_bid` | 1.00 | 1.00 | 4.67 |
| `ret_batch_job_pattern` | 1.00 | 1.00 | 3.00 |

Three tasks collapse outright. Those encode conventions that exist **only** in the convention file
— a chosen rule, not a fact derivable from the code. No amount of searching recovers them, and a
confident wrong answer is the result.

The rest still pass, but pay in turns. `ret_market_calendar` goes from one turn and 8 seconds to
six turns and 41 seconds. `ret_net_gex_semantics` passes in under two turns but spends 72 seconds
searching.

**That is the whole thesis in one table.** The always-on context does two different jobs: it turns
*findable-slowly* into *known-instantly*, and it turns *unfindable* into *known*. Only the second
shows up in a success-rate column, which is why success rate alone understates it.

## Limitations, stated plainly

- **The worktree drops the memory store as well as the convention file.** The memory directory is
  keyed to the repository path, so a worktree at a different path does not see it. Pair 1 measures
  *project conventions + memory* removed, not the convention file alone. Separating those needs a
  fourth arm.
- **The user-global convention file still loads in every arm.** "Context removed" means
  *project-level* context throughout.
- **The two pairs are four weeks apart** on different model builds, and their baselines differ
  (1.000 vs 0.963). Each treatment is therefore compared only against its own contemporaneous
  baseline; the cross-pair decomposition is indicative, not measured.
- **Nine tasks, one category.** The effect sizes are large relative to run-to-run spread, but this
  is not a broad benchmark and does not claim to be.

## Reproducing

`--readonly-cwd` runs the frozen tasks from a directory you choose, changing nothing else. That
one flag gives you all three conditions:

```bash
# arm 1 — baseline
python3 agent_eval/eval_harness.py --repeats 3 --label with-context

# arm 2 — rules removed, codebase present
git worktree add /tmp/ablation-norules HEAD --detach
rm /tmp/ablation-norules/CLAUDE.md        # worktree-local; shared checkout untouched
python3 agent_eval/eval_harness.py --repeats 3 --label no-rules \
    --readonly-cwd /tmp/ablation-norules

# arm 3 — both removed
python3 agent_eval/eval_harness.py --repeats 3 --label no-context \
    --readonly-cwd "$(mktemp -d)"

python3 agent_eval/eval_harness.py --report --label with-context --vs no-rules
```

Run every arm fresh against the same task set, and check the task-id sets actually match.
Comparing a new arm against an older stored batch is how you get a confident wrong answer — see
[SCORING.md](SCORING.md), where exactly that accounted for 57% of an apparent improvement.

Effect sizes on the eight-task demo suite will not match the numbers above — different tasks,
different repo, much smaller context. The *direction* should hold, and if it does not on your
system, that is worth knowing and is the reason to run it.
