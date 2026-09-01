# Cache shape beats context size

A companion result to [ABLATION.md](ABLATION.md), and the natural follow-on from it.

The ablation says **keep the always-on context** — removing it costs 5.4× the turns and
11.2× the tokens per success. That raises an obvious next question: if the context is
staying, what does it actually cost, and can that cost be engineered down without removing
anything?

The answer turned out to be **shape, not size.**

## The mechanism

Anthropic prompt caching matches the **longest byte-identical leading prefix** of a request
— system prompt, tool schemas, and leading messages. One differing byte at position *n*
invalidates everything from *n* onward.

A rule follows directly, and it is stronger than a style preference:

> **Anything that varies run-to-run is structurally banned from the prefix.**

Not "discouraged". A single interpolated UUID sitting above otherwise stable text costs you
the entire prefix below it.

## The audit

The harness assembles each agent's prompt from several segments. Classifying every segment
as stable or volatile, and checking where each actually sat, found four cache-busters:

| # | Segment | Verdict |
|---|---|---|
| 1 | Persona scaffold | **stable** — correctly first |
| 2 | Harness header ("you are running inside a task worktree") | stable |
| 3 | Task ID / worktree path bullets | **volatile, sitting above stable content** ❌ |
| 4 | Mandatory pre-work instructions | **volatile** — the *instruction* is stable, only its argument was not ❌ |
| 5 | Procedural-skills list | stable, but stranded below #3/#4 |
| 6 | Guardrails | **volatile** — interpolated the worktree path mid-block ❌ |
| 7 | Completion contract | **volatile** — same cause ❌ |
| 8 | Critical-reasoning note | stable, but stranded last |
| 9 | Experiment treatment directive | volatile-tail — correct, and must stay volatile |
| 10 | Task prompt | volatile-tail — correct |

Roughly **3.5 KB of static guardrails, skills and completion contract was being re-billed
uncached on every single task**, purely because two interpolated identifiers sat above it.

## The change, and the result

Behaviour-preserving: nothing removed, no contract altered, the agent receives exactly the
same information. The context block was split into a **byte-stable half with zero
interpolation** and a small **run-context half** carrying the identifiers. Guardrail prose
that inlined the worktree path now says "your worktree (Run Context below)" — the literal
path is still supplied, once, in the volatile section.

Assembly order became:

```
STABLE    persona scaffold → static harness block → task header + reasoning note
VOLATILE  run context → treatment directive → task prompt
```

**Shared leading prefix across two runs: 32% → 93%** (1,699 → 5,328 characters of ~5,700).

Three things were deliberately **left** volatile, because moving them would be wrong rather
than merely unhelpful:

- the experiment's treatment directive — its presence *is* the independent variable;
- anything derived from cross-episode lineage (gists, causal lessons) — per-run by
  construction, and the most volatile content in the system;
- the task prompt itself.

## The follow-up optimisation that was measured and refused

The obvious next move: five personas means five distinct cache prefixes. Hoist the shared
static block *above* the persona and they collapse to one. The measured cross-scaffold
common prefix was **17 characters**, so the premise was correct — there really were five
independent prefixes.

It was still a **NO-GO**, for three reasons found by measuring rather than assuming:

1. **The gain falls under the cache floor.** The shared block is ~820 tokens; Anthropic's
   minimum cacheable prefix is 1,024. The *only* thing the reorder buys is a prefix shared
   **across** personas — and at 820 tokens that prefix is below the floor, so it is never
   cached. Five entries would collapse to **zero**, not one.

   To be precise about what is *not* affected: caching **within** a single persona already
   works and would continue to, because there the match runs persona + shared block ≈ 1,420
   tokens, which clears the floor. That is the 93% measured above. The reorder does not
   shrink it — it simply fails to add the cross-persona sharing it was proposed for.
2. **The saving is rounding error.** Even granting a cacheable 820 tokens, the delta is
   write-price versus read-price on 820 tokens per task. A task here routinely burns
   100k–500k tokens. That is under 0.01% of task cost.
3. **Cache warmth is unlikely anyway.** Prompts go to CLIs as a single opening turn.
   Cross-task reuse needs a second task on the same provider *and* model inside the TTL, and
   the harness rotates providers deliberately.

Against that, the cost was real: the persona scaffold is the agent's role framing, and
demoting it below 3.3 KB of guardrails is an unmeasured behavioural risk.

**A saving under 0.01% of task cost does not buy an unmeasured behavioural risk.** The
lever was closed, not shipped.

## Where the real tax is

The opening prompt was never the problem. The measured cost is **multi-turn conversation
replay** — the same prefix re-read on every turn of a long task, growing quadratically.

That is addressed as a convention rather than a control loop, because the executors are
third-party CLIs whose inner loop the harness does not own: at roughly 80% of usable
context the agent overwrites a checkpoint file with `Findings` / `Decisions` / `Open
questions` / `Next step`, then continues from that checkpoint plus a short verbatim tail
instead of re-reading history. Quadratic replay becomes linear.

The directive is ~90 tokens and byte-stable, so it rides the cached prefix established
above. It also explicitly defers to any CLI that already auto-compacts, so the two
mechanisms compose instead of fighting.

## What transfers

1. **Measure the shared prefix, not the prompt length.** Two prompts of identical size can
   differ 3× in what they cost, depending entirely on where the first varying byte sits.
2. **Ban interpolation from the prefix as a rule, not a preference.** One UUID is enough.
3. **Check the cache floor before celebrating.** An optimisation that lands under it buys
   nothing.
4. **A high cache-read ratio is evidence the caching is working**, not evidence of waste.
   Reading it as waste is what motivated the trimming plan the ablation went on to kill.
