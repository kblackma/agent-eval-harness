#!/usr/bin/env python3
"""Agent eval harness — see docs/ABLATION.md.

Runs the frozen eval suite (`evals/demo_suite/*.yaml`) through a headless agent
executor, checks each task's machine-checkable assertions, and records one row per
attempt in `agent_runs`. That table is the baseline every later phase of the issue
must beat.

Design notes
------------
* **Read-only by default.** `readonly` tasks run with cwd = a read-only copy of the repo and a
  permission mode that cannot write. Only `fixture` tasks can edit anything, and they
  edit a throwaway copy under the scratch dir — never the repo, never the DB.
* **Parallel by default.** Tasks are independent (no fake edges), so they run through a
  ThreadPoolExecutor; the work is subprocess-bound, not CPU-bound. Workers adapt to the
  machine and are capped so a suite run can't monopolise the box.
* **Assertions are pure.** `check_assertions()` does no I/O for text assertions, so it is
  unit-testable without an executor or a DB.
* Usage/cost come from the executor's own JSON output, so token accounting is the
  executor's ground truth rather than an estimate.

Examples
--------
    DB_HOST=127.0.0.1 python3 eval_harness.py --repeats 3 --label baseline
    DB_HOST=127.0.0.1 python3 eval_harness.py --category retrieval --dry-run
    DB_HOST=127.0.0.1 python3 eval_harness.py --report --label baseline
    DB_HOST=127.0.0.1 python3 eval_harness.py --report --label baseline --vs phase1
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = ROOT / "evals" / "demo_suite"
CLAUDE_BIN = os.getenv("AGENTEVAL_EXECUTOR_BIN", os.path.expanduser("~/.local/bin/claude"))

# Suite runs are heavy; keep them off every core so a run can't starve the box.
DEFAULT_WORKERS = max(1, min(6, (os.cpu_count() or 4) - 1))


# --------------------------------------------------------------------------- model


@dataclass
class Task:
    id: str
    category: str
    prompt: str
    assertions: list[dict]
    why: str = ""
    workspace: str = "readonly"          # readonly | fixture
    fixture: Optional[str] = None
    # Files inside the fixture the agent must NOT modify — its tests, its oracle. A task
    # like "make the suite pass" is trivially satisfied by weakening the suite, so the
    # harness hashes these before the run and re-checks after. Individual verifiers may
    # also self-check; this is the belt to that braces, so a new fixture cannot forget.
    protected: list[str] = field(default_factory=list)
    timeout_s: int = 300
    retired: bool = False
    source_file: str = ""


@dataclass
class RunResult:
    task: Task
    repeat_idx: int
    duration_s: float
    response_text: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    error_text: Optional[str] = None
    assertion_log: list[dict] = field(default_factory=list)
    # Executor's own session id — the key that links a run to its transcript, which is
    # where the retrieval set is read back from (Phase 0.3).
    session_id: Optional[str] = None
    run_id: Optional[int] = None          # set by persist(), FK for agent_retrievals
    ttft_ms: Optional[int] = None
    duration_api_ms: Optional[int] = None

    @property
    def assertions_total(self) -> int:
        return len(self.assertion_log)

    @property
    def assertions_passed(self) -> int:
        return sum(1 for a in self.assertion_log if a["ok"])

    @property
    def outcome(self) -> str:
        if self.error_text:
            return "error"
        if not self.assertion_log:
            return "error"
        if self.assertions_passed == self.assertions_total:
            return "pass"
        return "partial" if self.assertions_passed else "fail"


# --------------------------------------------------------------------------- loading


def load_tasks(category: Optional[str] = None,
               only_ids: Optional[set[str]] = None) -> list[Task]:
    import yaml  # imported here so --help works without PyYAML

    tasks: list[Task] = []
    seen: dict[str, str] = {}
    for path in sorted(SUITE_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or {}
        for raw in doc.get("tasks", []):
            t = Task(
                id=raw["id"],
                category=raw.get("category", path.stem),
                prompt=raw["prompt"],
                assertions=raw.get("assertions", []),
                why=raw.get("why", ""),
                workspace=raw.get("workspace", "readonly"),
                fixture=raw.get("fixture"),
                protected=list(raw.get("protected") or []),
                timeout_s=int(raw.get("timeout_s", 300)),
                retired=bool(raw.get("retired", False)),
                source_file=path.name,
            )
            if t.id in seen:
                raise SystemExit(f"duplicate task id {t.id!r} in {path.name} and {seen[t.id]}")
            seen[t.id] = path.name
            if t.retired:
                continue
            if category and t.category != category:
                continue
            if only_ids and t.id not in only_ids:
                continue
            if not t.assertions:
                raise SystemExit(f"task {t.id!r} has no assertions — not eval-ready")
            tasks.append(t)
    return tasks


# --------------------------------------------------------------------------- assertions


def _sha(path: Path) -> str:
    """Content hash, or a sentinel if the file is absent — so deleting a protected file
    registers as tampering rather than silently passing."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "MISSING"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).lower()


_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def code_blocks(text: str) -> str:
    """Concatenated contents of every fenced code block in `text`.

    Prose and code have to be judged separately. A correct answer very often
    *names* the forbidden thing in order to warn against it ("never run
    `git add -A`", "`time::date = $1` is non-sargable"), so a plain
    `not_contains` over the whole response scores a correct warning as the
    offence itself. What the agent actually PRESCRIBES is what it puts in the
    code block — so `code_not_contains` / `code_contains_any` look only there.
    """
    return "\n".join(m.group(1) for m in _FENCE_RE.finditer(text))


def check_assertions(assertions: list[dict], text: str, workdir: Path) -> list[dict]:
    """Evaluate every assertion. Pure for text types; `shell` runs in workdir.

    Returns one {type, ok, detail} dict per assertion, in order.
    """
    hay = _norm(text)
    code = _norm(code_blocks(text))
    log: list[dict] = []

    for a in assertions:
        kind = a["type"]
        ok, detail = False, ""

        if kind == "contains_all":
            missing = [v for v in a["values"] if _norm(v) not in hay]
            ok, detail = not missing, f"missing={missing}" if missing else "all present"

        elif kind == "contains_any":
            hits = [v for v in a["values"] if _norm(v) in hay]
            ok, detail = bool(hits), f"hits={hits}" if hits else f"none of {a['values']}"

        elif kind == "not_contains":
            hits = [v for v in a["values"] if _norm(v) in hay]
            ok, detail = not hits, f"forbidden present: {hits}" if hits else "clean"

        elif kind == "code_not_contains":
            hits = [v for v in a["values"] if _norm(v) in code]
            ok, detail = not hits, (f"prescribed in a code block: {hits}" if hits
                                    else "not prescribed in any code block")

        elif kind == "code_contains_any":
            hits = [v for v in a["values"] if _norm(v) in code]
            ok, detail = bool(hits), (f"hits={hits}" if hits
                                      else f"no code block contains any of {a['values']}")

        elif kind == "regex":
            m = re.search(a["pattern"], text, re.S | re.I)
            ok, detail = bool(m), (f"matched {m.group(0)[:80]!r}" if m
                                   else f"no match for {a['pattern']!r}")

        elif kind == "shell":
            try:
                p = subprocess.run(a["cmd"], shell=True, cwd=workdir, capture_output=True,
                                   text=True, timeout=a.get("timeout_s", 120))
                ok = p.returncode == 0
                detail = (p.stdout + p.stderr).strip()[-600:]
            except subprocess.TimeoutExpired:
                ok, detail = False, "shell assertion timed out"

        else:
            ok, detail = False, f"unknown assertion type {kind!r}"

        log.append({"type": kind, "ok": ok, "detail": detail})

    return log


# --------------------------------------------------------------------------- execution


def _extract_usage(payload: dict) -> tuple[dict, Optional[float], Optional[int]]:
    u = payload.get("usage") or {}
    usage = {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0),
    }
    cost = payload.get("total_cost_usd")
    turns = payload.get("num_turns")
    return usage, (float(cost) if cost is not None else None), (int(turns) if turns else None)


def _latency(payload: dict) -> tuple[Optional[int], Optional[int]]:
    """(ttft_ms, duration_api_ms) — reported by the executor, previously discarded.

    TTFT is the proxy for static-prefix processing; api-time vs wall-clock separates
    model time from local/tool time. Phase 2 needs both to pick a lever rather than
    guess at one.
    """
    def _int(v):
        return int(v) if isinstance(v, (int, float)) else None
    return _int(payload.get("ttft_ms")), _int(payload.get("duration_api_ms"))


def run_task(task: Task, repeat_idx: int, model: Optional[str],
             scratch_root: Path, preamble: str = "",
             readonly_cwd: Optional[Path] = None) -> RunResult:
    """Execute one task headlessly and check its assertions.

    `preamble` prepends a CONTEXT POLICY to the task prompt. Task text stays frozen —
    the policy is the experimental variable, so an A/B compares two policies over the
    identical suite rather than comparing edited tasks (which would invalidate every
    earlier batch). Record which policy a batch used via --label.
    """
    if task.workspace == "fixture":
        if not task.fixture:
            return RunResult(task, repeat_idx, 0.0, error_text="workspace=fixture but no fixture set")
        src = SUITE_DIR / task.fixture
        if not src.is_dir():
            return RunResult(task, repeat_idx, 0.0, error_text=f"fixture dir not found: {src}")
        workdir = scratch_root / f"{task.id}_{repeat_idx}"
        if workdir.exists():
            shutil.rmtree(workdir)
        shutil.copytree(src, workdir)
        perm = ["--permission-mode", "acceptEdits"]
        protected_before = {f: _sha(workdir / f) for f in task.protected}
    else:
        # `readonly_cwd` overrides where a readonly task runs. The executor loads the
        # project CLAUDE.md and memory from its cwd, so pointing it at a bare directory
        # is a clean ABLATION of the always-on context: same frozen task, same policy,
        # only the preloaded context removed. That is how the ablation measures what the
        # always-on context is worth rather than assuming it.
        workdir = readonly_cwd or ROOT
        protected_before = {}
        # Readonly tasks must not be able to write anything, anywhere — but they must
        # still behave like a normal answering session. `--permission-mode plan` would
        # change the shape of the response (it plans instead of answering), so the
        # constraint goes on the tool surface instead: read/search freely, never mutate.
        perm = ["--permission-mode", "acceptEdits",
                "--disallowed-tools", "Write", "Edit", "NotebookEdit"]

    cmd = [CLAUDE_BIN, "--output-format", "json", *perm]
    if model:
        cmd += ["--model", model]
    cmd += ["-p", (preamble + "\n\n" + task.prompt) if preamble else task.prompt]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                              timeout=task.timeout_s)
    except subprocess.TimeoutExpired:
        return RunResult(task, repeat_idx, time.monotonic() - t0,
                         error_text=f"executor timed out after {task.timeout_s}s")
    dt = time.monotonic() - t0

    if proc.returncode != 0 and not proc.stdout.strip():
        return RunResult(task, repeat_idx, dt,
                         error_text=f"executor exit {proc.returncode}: {proc.stderr.strip()[-500:]}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return RunResult(task, repeat_idx, dt,
                         error_text=f"non-JSON executor output: {proc.stdout.strip()[:400]}")

    text = payload.get("result") or ""
    usage, cost, turns = _extract_usage(payload)

    ttft, api_ms = _latency(payload)
    res = RunResult(task, repeat_idx, dt, response_text=text, usage=usage,
                    cost_usd=cost, num_turns=turns,
                    session_id=payload.get("session_id"),
                    ttft_ms=ttft, duration_api_ms=api_ms)
    if payload.get("is_error"):
        res.error_text = f"executor reported error: {text[:400]}"
        return res

    # Tamper check BEFORE the assertions: if the oracle was edited, the assertions are
    # measuring nothing, so their result is not merely wrong — it is meaningless.
    tampered = [f for f, sha in protected_before.items() if _sha(workdir / f) != sha]
    if tampered:
        res.error_text = (f"protected file(s) modified: {', '.join(tampered)}. "
                          f"The task is to change the implementation, not its oracle.")
        res.assertion_log = [{"type": "protected_files", "ok": False,
                              "detail": res.error_text}]
        return res

    res.assertion_log = check_assertions(task.assertions, text, workdir)
    return res


# --------------------------------------------------------------------------- persistence


def _connect():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "agent_evals"),
        user=os.getenv("DB_USER", os.getenv("USER", "postgres")),
    )


INSERT_SQL = """
INSERT INTO agent_runs (
    batch_id, batch_label, task_id, task_category, repeat_idx,
    executor, model, git_sha, duration_s,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    cost_usd, num_turns,
    outcome, assertions_total, assertions_passed, assertion_log,
    error_text, response_text, session_id, ttft_ms, duration_api_ms
) VALUES (
    %s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s
)
ON CONFLICT (batch_id, task_id, repeat_idx) DO NOTHING
RETURNING run_id
"""


def persist(results: list[RunResult], batch_id: str, label: Optional[str],
            model: Optional[str], git_sha: str) -> int:
    conn = _connect()
    cur = conn.cursor()
    n = 0
    for r in results:
        cur.execute(INSERT_SQL, (
            batch_id, label, r.task.id, r.task.category, r.repeat_idx,
            "claude", model, git_sha, r.duration_s,
            r.usage.get("input_tokens", 0), r.usage.get("output_tokens", 0),
            r.usage.get("cache_read_tokens", 0), r.usage.get("cache_write_tokens", 0),
            r.cost_usd, r.num_turns,
            r.outcome, r.assertions_total, r.assertions_passed,
            json.dumps(r.assertion_log), r.error_text, r.response_text, r.session_id,
            r.ttft_ms, r.duration_api_ms,
        ))
        row = cur.fetchone()
        if row:
            r.run_id = row[0]
            n += 1
    conn.commit()
    cur.close()
    conn.close()
    return n


def log_retrievals(results: list[RunResult], model: Optional[str], judge: bool) -> tuple[int, int]:
    """Phase 0.3 — read each run's retrieval set back out of its transcript.

    Returns (runs_with_transcript, retrieval_rows_written). Failures here must never
    invalidate a run: a missing transcript logs nothing and leaves agent_runs intact.
    """
    from retrieval_log import extract_retrievals, find_transcript, judge_used, persist_retrievals

    def _judge_agent(prompt: str, model: str = "sonnet") -> dict:
        cmd = [CLAUDE_BIN, "--output-format", "json", "--model", model,
               "--disallowed-tools", "Write", "Edit", "NotebookEdit", "-p", prompt]
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
            payload = json.loads(p.stdout)
            return {"ok": not payload.get("is_error"), "text": payload.get("result") or ""}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            return {"ok": False, "text": ""}

    conn = _connect()
    seen, rows = 0, 0
    for r in results:
        if not r.run_id or not r.session_id:
            continue
        tpath = find_transcript(r.session_id)
        if not tpath:
            continue
        seen += 1
        entries = extract_retrievals(tpath)
        used = judge_used(r.response_text, entries, _judge_agent) if judge else None
        rows += persist_retrievals(conn, r.run_id, entries, used)
    conn.commit()
    conn.close()
    return seen, rows


# --------------------------------------------------------------------------- reporting


METRIC_SQL = """
SELECT
    COUNT(*)                                                        AS runs,
    AVG((outcome = 'pass')::int)::float                             AS task_success_rate,
    SUM(total_tokens)::bigint                                       AS total_tokens,
    SUM(output_tokens)::bigint                                      AS output_tokens,
    SUM(cache_read_tokens)::bigint                                  AS cache_read_tokens,
    SUM(cost_usd)::float                                            AS cost_usd,
    COUNT(*) FILTER (WHERE outcome = 'pass')                        AS passes,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_s)::float  AS p50_s,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY duration_s)::float  AS p90_s,
    AVG(ttft_ms)::float                                             AS ttft_ms,
    AVG(duration_api_ms)::float                                     AS api_ms,
    AVG(num_turns)::float                                           AS turns,
    -- mean wall-clock, so the latency decomposition compares like with like. Deriving
    -- local time from a MEDIAN wall-clock and a MEAN api time produced a negative
    -- number on the first run — the components must share an estimator.
    AVG(duration_s)::float                                          AS mean_wall_s
FROM agent_runs
WHERE batch_label = %s
"""


RETRIEVAL_SQL = """
SELECT
    COUNT(*)                                        AS retrieved,
    COUNT(*) FILTER (WHERE ar.used)                 AS used,
    COUNT(*) FILTER (WHERE ar.used IS NOT NULL)     AS judged,
    SUM(ar.tokens)::bigint                          AS tokens
FROM agent_retrievals ar
JOIN agent_runs r USING (run_id)
WHERE r.batch_label = %s
"""


def _metrics(cur, label: str) -> dict:
    cur.execute(METRIC_SQL, (label,))
    row = cur.fetchone()
    if not row or not row[0]:
        return {}
    (runs, succ, total_tok, out_tok, cache_tok, cost, passes, p50, p90,
     ttft_ms, api_ms, turns, mean_wall_s) = row

    cur.execute(RETRIEVAL_SQL, (label,))
    retrieved, used, judged, r_tokens = cur.fetchone() or (0, 0, 0, 0)
    retrieval = {
        # hit rate over JUDGED entries only — an unjudged retrieval is unknown, not a miss
        "memory_hit_rate": (used / judged) if judged else None,
        "memory_precision": (used / retrieved) if retrieved else None,
        "retrievals_per_run": (retrieved / runs) if runs else None,
        "retrieval_tokens": r_tokens,
    }
    return {**retrieval,
        "runs": runs,
        "task_success_rate": succ,
        "tokens_per_success": (total_tok / passes) if passes else None,
        "cache_read_ratio": (cache_tok / out_tok) if out_tok else None,
        "cost_usd": cost,
        "cost_per_success": (cost / passes) if (passes and cost) else None,
        "wall_clock_p50_s": p50,
        "wall_clock_p90_s": p90,
        "turns_per_run": turns,
        "ttft_ms": ttft_ms,
        "api_ms": api_ms,
        # Where the wall-clock actually goes. TTFT is the static-prefix lever; local_ms
        # (wall-clock minus API time) is the tool/round-trip lever. Phase 2 picks between
        # them on these numbers rather than on a hypothesis.
        "mean_wall_ms": (mean_wall_s * 1000) if mean_wall_s else None,
        "ttft_share_of_wall": (ttft_ms / (mean_wall_s * 1000)) if (ttft_ms and mean_wall_s) else None,
        "local_ms": ((mean_wall_s * 1000) - api_ms) if (api_ms and mean_wall_s) else None,
    }


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.1f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def report(label: str, vs: Optional[str]) -> None:
    conn = _connect()
    cur = conn.cursor()
    a = _metrics(cur, label)
    if not a:
        raise SystemExit(f"no runs found for batch_label={label!r}")
    b = _metrics(cur, vs) if vs else {}

    keys = ["runs", "task_success_rate", "tokens_per_success", "cache_read_ratio",
            "cost_usd", "cost_per_success", "wall_clock_p50_s", "wall_clock_p90_s",
            "turns_per_run", "mean_wall_ms", "ttft_ms", "api_ms", "local_ms",
            "ttft_share_of_wall",
            "memory_hit_rate", "memory_precision", "retrievals_per_run",
            "retrieval_tokens"]
    if b:
        print(f"| Metric | {label} | {vs} | Δ |")
        print("|---|---:|---:|---:|")
        for k in keys:
            av, bv = a.get(k), b.get(k)
            d = _fmt(bv - av) if isinstance(av, (int, float)) and isinstance(bv, (int, float)) else "—"
            print(f"| {k} | {_fmt(av)} | {_fmt(bv)} | {d} |")
    else:
        print(f"| Metric | {label} |")
        print("|---|---:|")
        for k in keys:
            print(f"| {k} | {_fmt(a.get(k))} |")

    print()
    cur.execute("""
        SELECT task_id, task_category,
               AVG((outcome='pass')::int)::float, COUNT(*),
               AVG(duration_s)::float, AVG(total_tokens)::float
        FROM agent_runs WHERE batch_label = %s
        GROUP BY task_id, task_category ORDER BY 3, 1
    """, (label,))
    print(f"| Task | Category | Pass rate | n | Avg s | Avg tokens |")
    print("|---|---|---:|---:|---:|---:|")
    for tid, cat, pr, n, dur, tok in cur.fetchall():
        print(f"| {tid} | {cat} | {pr:.2f} | {n} | {dur:.1f} | {tok:,.0f} |")

    cur.close()
    conn.close()


# --------------------------------------------------------------------------- cli


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", help="batch label, e.g. 'baseline'")
    ap.add_argument("--repeats", type=int, default=1, help="attempts per task")
    ap.add_argument("--category", help="run only one category")
    ap.add_argument("--task", action="append", help="run only this task id (repeatable)")
    ap.add_argument("--model", help="executor model override")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--dry-run", action="store_true", help="no DB writes; print results")
    ap.add_argument("--preamble-file", type=Path,
                    help="file holding a context policy to prepend to every task prompt. "
                         "Tasks stay frozen; the policy is the experimental variable, so "
                         "two labelled batches are a clean A/B. See docs/METHODOLOGY.md.")
    ap.add_argument("--readonly-cwd", type=Path,
                    help="run readonly tasks from this directory instead of the repo. "
                         "Pointing at a bare dir ablates the always-on context "
                         "(project CLAUDE.md + memory) — the headline ablation in docs/ABLATION.md.")
    ap.add_argument("--no-retrievals", action="store_true",
                    help="skip retrieval logging (transcript read + used-judge)")
    ap.add_argument("--no-judge", action="store_true",
                    help="log retrievals but leave `used` NULL (skips the judge call)")
    ap.add_argument("--list", action="store_true", help="list tasks and exit")
    ap.add_argument("--report", action="store_true", help="report on an existing batch label")
    ap.add_argument("--vs", help="second batch label to compare against (with --report)")
    args = ap.parse_args()

    if args.report:
        if not args.label:
            raise SystemExit("--report requires --label")
        report(args.label, args.vs)
        return 0

    tasks = load_tasks(args.category, set(args.task) if args.task else None)
    if not tasks:
        raise SystemExit("no tasks matched")

    if args.list:
        for t in tasks:
            print(f"{t.id:34s} {t.category:11s} {len(t.assertions)} assertions  ({t.source_file})")
        print(f"\n{len(tasks)} tasks")
        return 0

    if not args.label and not args.dry_run:
        raise SystemExit("--label is required (or use --dry-run)")

    preamble = args.preamble_file.read_text().strip() if args.preamble_file else ""
    if preamble:
        print(f"context policy: {args.preamble_file} ({len(preamble)} chars)", file=sys.stderr)

    batch_id = uuid.uuid4().hex[:12]
    git_sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
    scratch_root = Path(tempfile.mkdtemp(prefix="agent-eval-"))

    jobs = [(t, i) for t in tasks for i in range(args.repeats)]
    print(f"batch {batch_id} · {len(tasks)} tasks × {args.repeats} = {len(jobs)} runs "
          f"· {args.workers} workers · sha {git_sha}", file=sys.stderr)

    results: list[RunResult] = []
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(run_task, t, i, args.model, scratch_root, preamble,
                                args.readonly_cwd): (t, i)
                    for t, i in jobs}
            for fut in as_completed(futs):
                t, i = futs[fut]
                try:
                    r = fut.result()
                except Exception as exc:  # noqa: BLE001
                    r = RunResult(t, i, 0.0, error_text=f"harness exception: {exc!r}")
                results.append(r)
                mark = {"pass": "PASS", "partial": "PART", "fail": "FAIL", "error": "ERR "}[r.outcome]
                print(f"  {mark} {t.id:34s} {r.assertions_passed}/{r.assertions_total} "
                      f"{r.duration_s:6.1f}s", file=sys.stderr)
                if r.error_text:
                    print(f"       ! {r.error_text[:160]}", file=sys.stderr)
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    n_pass = sum(1 for r in results if r.outcome == "pass")
    print(f"\n{n_pass}/{len(results)} passed ({n_pass / len(results):.1%})", file=sys.stderr)

    if args.dry_run:
        for r in sorted(results, key=lambda x: x.task.id):
            if r.outcome != "pass":
                print(f"\n--- {r.task.id} [{r.outcome}] ---")
                for a in r.assertion_log:
                    if not a["ok"]:
                        print(f"  FAIL {a['type']}: {a['detail'][:300]}")
                if r.error_text:
                    print(f"  ERROR {r.error_text[:300]}")
        return 0

    written = persist(results, batch_id, args.label, args.model, git_sha)
    print(f"wrote {written} rows to agent_runs (batch {batch_id}, label {args.label})",
          file=sys.stderr)

    if not args.no_retrievals:
        try:
            seen, rows = log_retrievals(results, args.model, judge=not args.no_judge)
            print(f"logged {rows} retrievals from {seen}/{len(results)} transcripts"
                  + (" (used=NULL, judge skipped)" if args.no_judge else ""),
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            # Retrieval logging is measurement, not the measurement's subject. A failure
            # here must not invalidate an otherwise-good batch in agent_runs.
            print(f"retrieval logging failed (runs are still recorded): {exc!r}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
