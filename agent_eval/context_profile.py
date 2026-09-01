#!/usr/bin/env python3
"""Context profiler — see docs/ABLATION.md.

Phase 0.3 measured `cache_read_ratio` at 327.8 on code-nav vs 76.1 on retrieval, which
says the context drag is concentrated in tasks that search the repo. It does not say
WHAT is big. This answers that before anything is changed: for every run in a batch, it
reads the run's own transcript and attributes tool-output volume by tool and by call.

The point is to avoid treating a symptom. If the volume turns out to be a handful of
unbounded `rg`/`Read` results, compression at the tool boundary is the fix. If it is
spread evenly across many small calls, it is not, and a compression policy would cost
latency for nothing — which is exactly how the KB-first rule failed its A/B here.

    python3 context_profile.py --label hard-baseline
    python3 context_profile.py --label baseline --top 15
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrieval_log import _iter_records, find_transcript  # noqa: E402

CHARS_PER_TOKEN = 4


def _connect():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=os.getenv("DB_NAME", "agent_evals"),
        user=os.getenv("DB_USER", os.getenv("USER", "postgres")),
    )


def profile_transcript(path: Path) -> dict:
    """Tool-output volume for one session, by tool and per call."""
    call_tool: dict[str, str] = {}
    by_tool: dict[str, dict] = defaultdict(lambda: {"calls": 0, "tokens": 0, "max": 0})
    calls: list[tuple[str, int, str]] = []      # (tool, tokens, fingerprint)
    fingerprints: dict[str, str] = {}

    for rec in _iter_records(path):
        if rec.get("type") not in ("user", "assistant"):
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            if block.get("type") == "tool_use":
                tid, name = block.get("id", ""), block.get("name", "?")
                call_tool[tid] = name
                inp = block.get("input") or {}
                fp = str(inp.get("command") or inp.get("file_path")
                         or inp.get("pattern") or inp.get("query") or "")[:110]
                fingerprints[tid] = " ".join(fp.split())
                by_tool[name]["calls"] += 1

            elif block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                name = call_tool.get(tid)
                if not name:
                    continue
                raw = block.get("content")
                if isinstance(raw, list):
                    raw = " ".join(str(c.get("text", "")) for c in raw
                                   if isinstance(c, dict))
                tok = len(str(raw or "")) // CHARS_PER_TOKEN
                by_tool[name]["tokens"] += tok
                by_tool[name]["max"] = max(by_tool[name]["max"], tok)
                calls.append((name, tok, fingerprints.get(tid, "")))

    return {"by_tool": dict(by_tool), "calls": calls}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True)
    ap.add_argument("--top", type=int, default=12, help="largest individual calls to show")
    args = ap.parse_args()

    conn = _connect()
    cur = conn.cursor()
    cur.execute("""SELECT task_id, session_id, cache_read_tokens, total_tokens
                   FROM agent_runs WHERE batch_label = %s AND session_id IS NOT NULL""",
                (args.label,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        raise SystemExit(f"no runs with a session_id for label={args.label!r} "
                         "(batches before migration 016 did not persist it)")

    agg: dict[str, dict] = defaultdict(lambda: {"calls": 0, "tokens": 0, "max": 0})
    all_calls: list[tuple[str, int, str, str]] = []
    missing, cache_total, tool_total = 0, 0, 0

    for task_id, sid, cache_read, _total in rows:
        t = find_transcript(sid)
        if not t:
            missing += 1
            continue
        prof = profile_transcript(t)
        cache_total += cache_read or 0
        for tool, s in prof["by_tool"].items():
            agg[tool]["calls"] += s["calls"]
            agg[tool]["tokens"] += s["tokens"]
            agg[tool]["max"] = max(agg[tool]["max"], s["max"])
            tool_total += s["tokens"]
        for tool, tok, fp in prof["calls"]:
            all_calls.append((tool, tok, fp, task_id))

    print(f"batch {args.label}: {len(rows)} runs, {missing} transcripts missing")
    print(f"tool-output tokens: {tool_total:,}   cache_read tokens: {cache_total:,}   "
          f"tool share of cache-read: "
          f"{(tool_total / cache_total):.1%}" if cache_total else "")
    print()
    print(f"| Tool | Calls | Output tokens | Share | Mean/call | Largest call |")
    print("|---|---:|---:|---:|---:|---:|")
    for tool, s in sorted(agg.items(), key=lambda kv: -kv[1]["tokens"]):
        share = (s["tokens"] / tool_total) if tool_total else 0
        mean = (s["tokens"] / s["calls"]) if s["calls"] else 0
        print(f"| {tool} | {s['calls']} | {s['tokens']:,} | {share:.1%} | "
              f"{mean:,.0f} | {s['max']:,} |")

    # Concentration: is the volume in a few fat calls, or spread thin? This is the
    # question that decides whether compression at the boundary is worth anything.
    all_calls.sort(key=lambda c: -c[1])
    top_sum = sum(c[1] for c in all_calls[:args.top])
    print(f"\ntop {args.top} of {len(all_calls)} calls = {top_sum:,} tokens "
          f"({(top_sum / tool_total):.1%} of tool output)" if tool_total else "")
    print(f"\n| Tokens | Tool | Task | Call |")
    print("|---:|---|---|---|")
    for tool, tok, fp, task in all_calls[:args.top]:
        print(f"| {tok:,} | {tool} | {task} | `{fp[:70]}` |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
