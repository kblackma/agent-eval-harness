# Writing tasks that measure your system, not the model

The harness is the easy part. Nearly all of the value is in the task suite, and nearly all
of the failure modes are here too.

## The one rule that matters

**A task a fresh model passes with no repo context is a bad task.** It measures nothing
about your system.

Every task should target something the agent is supposed to get right *because of* your
memory, context or retrieval layer. In practice that means the answer is a
repository-specific fact that lives somewhere in your stack — a convention file, a memory
store, a knowledge base.

When adding tasks, prefer facts that are:

- **written down somewhere** in your stack,
- **non-obvious** — a competent model cannot guess them,
- **something you have actually got wrong before.**

That last one is the best filter. A task drawn from a real past bug is automatically
relevant, automatically non-obvious, and automatically worth preventing.

## Format

One YAML file per category, each holding a `tasks:` list.

```yaml
tasks:
  - id: unique_snake_case_id          # also the agent_runs.task_id
    category: retrieval               # retrieval | code-nav | discipline | data-db | code-edit
    why: one line — what capability this measures
    prompt: |
      The prompt handed to the agent verbatim.
    workspace: readonly               # readonly (default) | fixture (task may edit files)
    fixture: demo_repo                # required when workspace: fixture
    protected:                        # files the agent must not touch — its own oracle
      - CLAUDE.md
    timeout_s: 300
    assertions:
      - type: contains_all            # every value must appear (case-insensitive)
        values: ["events"]
      - type: contains_any            # at least one value must appear
        values: ["per-stream", "not globally unique"]
      - type: not_contains            # none may appear — catches the known wrong answer
        values: ["seq is globally unique"]
      - type: code_not_contains       # as not_contains, but only inside code blocks
        values: ["git add -A"]
      - type: code_contains_any
        values: ["git worktree add"]
      - type: regex                   # python re.search, DOTALL | IGNORECASE
        pattern: "ts\\s*>=\\s*\\$"
      - type: shell                   # run in the workspace; must exit 0 (fixture tasks only)
        cmd: "python3 -c 'import ast; ast.parse(open(\"src/store.py\").read())'"
```

`outcome` in `agent_runs` is `pass` when every assertion passes, `partial` when some do,
`fail` when none do, and `error` when the executor itself failed.

### `code_not_contains` exists for a reason

A *correct* answer often names the forbidden thing in order to warn against it. A plain
`not_contains` scores that as a violation. `code_not_contains` looks only at what the
answer puts inside a code block — what it actually prescribes. This distinction cost me a
whole batch before I found it.

## Discipline

**Tasks are frozen.** Changing a task invalidates every comparison against earlier
batches. Add new tasks with new ids; retire a task with `retired: true`, never by editing
it.

**No task may mutate shared state** — no DB writes, no service restarts, no git operations
outside its own workspace. `readonly` tasks run against a read-only copy; `fixture` tasks
get a throwaway copy that is deleted afterwards, with `protected` files hash-checked before
and after.

**Assertions must be checkable without a human.** If you cannot write the assertion, the
task is not ready. This is a hard gate, not a preference — the moment a human is in the
scoring loop, batches stop being comparable and the suite stops being run.

**Verify every assertion against the repo *before* it is allowed to score anything.** This
went wrong three separate times here, each time the same way: an assertion encoded what I
*expected* to be true, the agent gave a correct — sometimes better — answer, and the
harness scored it a failure.

- A `not_contains` counted a warning as the offence.
- A retrieval policy was assumed to be right and quietly lost its own A/B.
- A write was assumed to be permission-denied when only one tool path was actually blocked.

Run the grep. Read the settings file. Check the doc. *Then* write the assertion.

**A failing run is a hypothesis, not a verdict.** Read the response before believing the
score. If the answer is right and the assertion is wrong, fix the assertion — and if that
task has already scored a published batch, retire it rather than editing it.

## Batch hygiene

- `batch_id` groups every task of one invocation, so a before/after comparison is a
  two-batch query rather than a timestamp guess.
- Use `--repeats` (3 is a reasonable floor). Single-run differences on agent tasks are
  mostly noise.
- Label batches for what changed, not when: `baseline`, `memory-typed`, `no-context`.
- Keep `response_text`. Post-hoc analysis of *why* a task failed is where most of the
  learning is, and you cannot recover it later.
