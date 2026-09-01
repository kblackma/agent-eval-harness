# demo_repo — project conventions

A deliberately small fixture. The conventions below are **non-obvious and
project-specific**: a model that has never seen this repo cannot guess them, and a
model that has the file loaded answers in one turn. That is the whole point — see
`docs/METHODOLOGY.md`.

## Storage

- `events` is a partitioned table. **Every query must include a `ts >= ` bound**, or the
  planner scans all partitions. Predicates must be sargable: `ts >= $1`, never
  `date_trunc('day', ts) = $1`.
- `events.seq` is per-`stream_id`, **not** globally unique. Joining on `seq` alone is the
  most common bug in this repo's history.
- `EventStore.append()` is the only supported write path. `_raw_insert()` exists for the
  backfill script and bypasses the dedup check.

## Time

- All timestamps are stored UTC. The API renders in the caller's timezone, never the
  storage layer.
- The ingest window opens at **09:31**, not 09:30 — the first minute is discarded because
  upstream replays it.

## Git

- Never `git add -A` or `git add .`; stage explicit paths. Concurrent work is normal here
  and a blanket add sweeps up someone else's in-flight files.
- Branch by creating a worktree, never `git checkout -b` in this directory.
