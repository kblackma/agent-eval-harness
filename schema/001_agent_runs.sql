-- 015_agent_eval_ledger.sql
-- docs/ABLATION.md Phase 0.2 — the run ledger.
--
-- Two tables. `agent_runs` is one row per (eval task x harness run): what ran,
-- what it cost, and whether it passed its machine-checkable assertions.
-- `agent_retrievals` is one row per memory/KB entry a run pulled in, so
-- memory_hit_rate / memory_precision (see docs/METHODOLOGY.md) become measurable instead of
-- assumed. Both are plain Postgres tables (NOT hypertables) — the volume is
-- eval-suite scale, not tick scale.
--
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id          BIGSERIAL PRIMARY KEY,
    -- batch_id groups every task of one `eval_harness.py` invocation, so a
    -- before/after comparison is a two-batch query, not a timestamp guess.
    batch_id        TEXT        NOT NULL,
    batch_label     TEXT,                      -- e.g. 'baseline', 'phase1-memory-typed'
    task_id         TEXT        NOT NULL,      -- evals/demo_suite/<task_id>.yaml
    task_category   TEXT,                      -- retrieval | code-nav | code-edit | data-db | discipline
    repeat_idx      INT         NOT NULL DEFAULT 0,  -- 0..N-1 for N repeats of the same task

    executor        TEXT        NOT NULL DEFAULT 'claude',
    model           TEXT,
    git_sha         TEXT,

    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_s      DOUBLE PRECISION,

    -- Token accounting, mirrored from the executor's JSON usage block. Kept raw
    -- (rate-independent) alongside the stamped cost, same convention as
    -- claude_usage_daily.
    input_tokens        BIGINT DEFAULT 0,
    output_tokens       BIGINT DEFAULT 0,
    cache_read_tokens   BIGINT DEFAULT 0,
    cache_write_tokens  BIGINT DEFAULT 0,
    total_tokens        BIGINT GENERATED ALWAYS AS (
        COALESCE(input_tokens,0) + COALESCE(output_tokens,0)
        + COALESCE(cache_read_tokens,0) + COALESCE(cache_write_tokens,0)
    ) STORED,
    cost_usd        NUMERIC(12,6),
    num_turns       INT,

    outcome         TEXT        NOT NULL,      -- pass | fail | partial | error
    assertions_total  INT       NOT NULL DEFAULT 0,
    assertions_passed INT       NOT NULL DEFAULT 0,
    assertion_log   JSONB,                     -- per-assertion {type, ok, detail}
    error_text      TEXT,
    response_text   TEXT,                      -- final assistant text (for post-hoc analysis)

    CONSTRAINT agent_runs_outcome_ck
        CHECK (outcome IN ('pass','fail','partial','error')),
    CONSTRAINT agent_runs_unique_attempt
        UNIQUE (batch_id, task_id, repeat_idx)
);

CREATE INDEX IF NOT EXISTS agent_runs_batch_idx   ON agent_runs (batch_id);
CREATE INDEX IF NOT EXISTS agent_runs_task_idx    ON agent_runs (task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_started_idx ON agent_runs (started_at DESC);


CREATE TABLE IF NOT EXISTS agent_retrievals (
    retrieval_id BIGSERIAL PRIMARY KEY,
    run_id       BIGINT      NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,

    source       TEXT        NOT NULL,   -- memory | kb | lightrag | graphify | claude_md
    entry_id     TEXT        NOT NULL,   -- memory slug, chunk id, file path...
    score        DOUBLE PRECISION,       -- retriever's relevance score, if any
    tokens       INT,                    -- cost of injecting this entry

    injected     BOOLEAN     NOT NULL DEFAULT FALSE,  -- did it actually enter the window
    used         BOOLEAN,                             -- did the output rely on it (NULL = not yet judged)
    used_judge   TEXT,                                -- how `used` was determined: llm | citation | ablation
    judged_at    TIMESTAMPTZ,

    CONSTRAINT agent_retrievals_source_ck
        CHECK (source IN ('memory','kb','lightrag','graphify','claude_md'))
);

CREATE INDEX IF NOT EXISTS agent_retrievals_run_idx    ON agent_retrievals (run_id);
CREATE INDEX IF NOT EXISTS agent_retrievals_source_idx ON agent_retrievals (source, entry_id);

COMMENT ON TABLE agent_runs IS
    'docs/ABLATION.md Phase 0.2 — one row per eval task attempt; '
    'source of task_success_rate / tokens_per_success / cache_read_ratio / wall_clock.';
COMMENT ON TABLE agent_retrievals IS
    'docs/ABLATION.md Phase 0.3 — one row per retrieved entry; '
    'source of memory_hit_rate (used/injected) and memory_precision (used/retrieved).';
-- 016_agent_runs_session_id.sql
-- see docs/ABLATION.md.
--
-- Persist the executor's session_id on each run. Phase 0.3 used it transiently to find
-- the run's transcript and then discarded it, which meant earlier batches could not be
-- re-analysed afterwards — exactly what Phase 2 needs (where do the cache-read tokens
-- actually go?). Storing it makes every future batch retro-analysable from its own
-- transcripts without re-running anything.
--
-- Idempotent: safe to re-run.

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS session_id TEXT;

CREATE INDEX IF NOT EXISTS agent_runs_session_idx ON agent_runs (session_id);

COMMENT ON COLUMN agent_runs.session_id IS
    'Executor session id — locates this run''s transcript under ~/.claude/projects/*/. '
    'Enables after-the-fact analysis of a batch (tool-output sizes, retrieval sets) '
    'without re-running it.';
-- 018_agent_runs_latency.sql
-- see docs/ABLATION.md.
--
-- The premise correction of 2026-08-06 made LATENCY (not cost) one of the two real
-- currencies. But `agent_runs` only stored wall-clock duration, which cannot say WHERE
-- the time goes — and Phase 2 already burned one wrong hypothesis by treating a symptom
-- before diagnosing it (tool-output compression, retracted).
--
-- The executor already reports these on every run and we were discarding them:
--   ttft_ms         time to first token — dominated by processing the static prefix,
--                   so this is the metric the "shrink the always-on context" lever moves
--   duration_api_ms time inside the API across all turns
--   (duration_s - duration_api_ms) is then local/tool time, i.e. the round-trip lever
--
-- Idempotent: safe to re-run.

ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS ttft_ms         INTEGER;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS duration_api_ms INTEGER;

COMMENT ON COLUMN agent_runs.ttft_ms IS
    'Time to first token (ms). Proxy for static-prefix processing — the metric that moves '
    'if the always-on context (CLAUDE.md + MEMORY.md + tool defs) shrinks.';
COMMENT ON COLUMN agent_runs.duration_api_ms IS
    'Total time inside the API across all turns (ms). duration_s*1000 - this = local and '
    'tool-execution time.';
