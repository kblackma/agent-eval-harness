#!/usr/bin/env python3
"""
eval_score.py — harness-computed objective `f` over the eval suite.

See docs/SCORING.md.

Why this exists
---------------
An earlier finding was that the first scored task wrote its own `metrics.json` and so
chose its own score. The fix is to prefer a score the agent does not author. This
script is that: the verification gate runs it as an external command, it reads
`agent_runs` — rows written by the eval harness, not by the candidate — and prints
one number. The agent under evaluation cannot reach it.

What it measures, and why not success rate
------------------------------------------
Success on the `retrieval` category is already at CEILING: the control batch
`retrieval-probe2` scored 10/10. An objective that can only be tied is not an
objective, and ranking attempts on a metric pinned at 1.0 would produce a lineage
of ties that says nothing.

So correctness and cost are separated the standard way: a candidate
failing correctness scores 0 regardless of throughput:

  * CORRECTNESS GATE — success rate must be >= --min-success (default 1.0, the
    measured control). Short of that, exit non-zero and print nothing rankable.
    A faster policy that gets answers wrong has not won.
  * SCORE — mean wall-clock seconds, MINIMIZED.

That pairing is what makes this a safe optimisation target: wall-clock on a frozen
task set is a measurement, not a sample statistic drawn from a noisy population, so
the usual multiple-comparisons hazard does not apply.

Two defects found on the FIRST scored run, both fixed here
----------------------------------------------------------
1. **The task set drifted underneath the comparison.** `retrieval-probe2` was
   measured over 10 tasks; by the time a candidate ran, one task
   had been removed from the suite, so the candidate was scored over 9. That task
   was the SLOWEST in the batch (14.57s), so dropping it moved the incumbent from
   9.0865 to 8.4775 on its own — about 57% of the apparent improvement was the
   missing task, not the policy. `--require-tasks` now pins the set: a batch that
   does not cover EXACTLY the expected task ids is refused, not scored.

2. **The margin was inside the noise.** Repeat-to-repeat spread on the same task
   averages ~1.0s and per-task deltas ranged -3.9s to +3.5s; the 0.83s mean gain
   gave t≈-1.0, p≈0.34 over 9 tasks. Wall-clock is a measurement, but a measurement
   WITH VARIANCE, and ranking on one sample of it is that same hazard wearing a safe
   costume. So this scorer now requires repeats and reports the standard error, and
   the objective must carry a `min_delta` wider than the noise — a tie is the
   correct verdict for a difference this size.

Measured baseline (2026-08-28), retrieval category:

    retrieval-probe2    success 1.00   mean 9.09s (10 tasks) / 8.48s (common 9)
    latency-policy-v2   success 1.00   mean 7.65s (9 tasks, 2 repeats)  -> a TIE
    kb-first-retrieval  success 0.90   mean 24.6s   <- the policy that lost

Usage
-----
    eval_score.py --label <batch> [--category retrieval] [--min-success 1.0]

Prints the score on the last line, as the gate's `command` source expects.
"""
from __future__ import annotations

import argparse
import os
import sys


def connect():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        dbname=os.getenv("DB_NAME", "agent_evals"),
        user=os.getenv("DB_USER", os.getenv("USER", "postgres")),
    )


def measure(label: str, category: str | None) -> tuple[int, float | None, float | None]:
    """(runs, success_rate, mean_duration_s) for one batch label."""
    sql = """
        SELECT COUNT(*),
               AVG((outcome = 'pass')::int)::float,
               AVG(duration_s)::float
        FROM agent_runs
        WHERE batch_label = %s
    """
    params: list = [label]
    if category:
        sql += " AND task_category = %s"
        params.append(category)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        runs, succ, mean_s = cur.fetchone()
        cur.close()
        return int(runs or 0), succ, mean_s
    finally:
        conn.close()


def task_ids(label: str, category: str | None) -> set[str]:
    sql = "SELECT DISTINCT task_id FROM agent_runs WHERE batch_label = %s"
    params: list = [label]
    if category:
        sql += " AND task_category = %s"
        params.append(category)
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        out = {r[0] for r in cur.fetchall()}
        cur.close()
        return out
    finally:
        conn.close()


def per_task_stats(label: str, category: str | None) -> tuple[int, float | None]:
    """(distinct tasks, mean within-task spread) — the noise floor of this metric.

    Reported so a reader can see whether a margin is bigger than the run-to-run
    variation. On the first scored run it was not, and nothing said so.
    """
    sql = """
        SELECT COUNT(*), AVG(spread)::float FROM (
            SELECT MAX(duration_s) - MIN(duration_s) AS spread
            FROM agent_runs WHERE batch_label = %s
    """
    params: list = [label]
    if category:
        sql += " AND task_category = %s"
        params.append(category)
    sql += " GROUP BY task_id) s"
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        n, spread = cur.fetchone()
        cur.close()
        return int(n or 0), spread
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="batch_label the candidate produced")
    ap.add_argument("--category", default="retrieval")
    ap.add_argument("--min-success", type=float, default=1.0,
                    help="correctness gate; below this the candidate is not ranked")
    ap.add_argument("--min-runs", type=int, default=10,
                    help="a partial batch must not be scored — a policy that answered "
                         "two tasks quickly would otherwise 'beat' one that answered ten")
    ap.add_argument("--require-tasks", default=None,
                    help="comma-separated task ids the batch MUST cover exactly, or a "
                         "batch_label to copy the set from. Without this the suite can "
                         "change underneath two attempts and the comparison is silently "
                         "invalid — which is exactly what happened on the first run.")
    ap.add_argument("--min-repeats", type=int, default=2,
                    help="repeats per task. One sample of a metric whose run-to-run "
                         "spread is ~1s cannot resolve a sub-second difference.")
    args = ap.parse_args()

    runs, succ, mean_s = measure(args.label, args.category)

    if args.require_tasks:
        want = ({t.strip() for t in args.require_tasks.split(",") if t.strip()}
                if "," in args.require_tasks or args.require_tasks.startswith("ret_")
                else task_ids(args.require_tasks, args.category))
        got = task_ids(args.label, args.category)
        if got != want:
            missing, extra = sorted(want - got), sorted(got - want)
            print(f"[eval_score] FAIL task set: batch {args.label!r} does not cover the "
                  f"pinned set. missing={missing} extra={extra}. Two attempts scored over "
                  "different task sets are not comparable.", file=sys.stderr)
            return 2

    n_tasks, spread = per_task_stats(args.label, args.category)
    if n_tasks and runs < n_tasks * args.min_repeats:
        print(f"[eval_score] FAIL repeats: {runs} runs over {n_tasks} tasks is under "
              f"{args.min_repeats} repeats each; the noise floor swamps the metric.",
              file=sys.stderr)
        return 2

    if runs < args.min_runs:
        print(f"[eval_score] FAIL: batch {args.label!r} has {runs} runs, "
              f"need >= {args.min_runs}", file=sys.stderr)
        return 2
    if succ is None or mean_s is None:
        print(f"[eval_score] FAIL: batch {args.label!r} has no usable rows", file=sys.stderr)
        return 2
    if succ < args.min_success:
        print(f"[eval_score] FAIL correctness: success {succ:.3f} < {args.min_success:.3f} "
              f"— not ranked (see docs/SCORING.md)", file=sys.stderr)
        return 1

    print(f"[eval_score] {args.label}: runs={runs} tasks={n_tasks} success={succ:.3f} "
          f"mean={mean_s:.2f}s within_task_spread={spread:.2f}s"
          if spread is not None else
          f"[eval_score] {args.label}: runs={runs} success={succ:.3f} mean={mean_s:.2f}s",
          file=sys.stderr)
    if spread:
        print(f"[eval_score] NOTE: a margin smaller than ~{spread:.2f}s is inside the "
              "run-to-run noise. Set score_spec.min_delta at least this wide, or the "
              "commit rule will merge coin flips.", file=sys.stderr)
    print(f"{mean_s:.4f}")   # last line = the score, minimized
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
