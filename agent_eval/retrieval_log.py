#!/usr/bin/env python3
"""Retrieval logging — docs/ABLATION.md Phase 0.3.

Populates `agent_retrievals` so `memory_hit_rate` and `memory_precision` stop being
guesses. For each eval run we already know the executor's `session_id`; its transcript
records every retrieval the agent actually performed, so the retrieval set is read back
from there rather than being instrumented into the harness.

WHAT THIS CAN AND CANNOT SEE — stated up front, because the metric is only as honest as
its coverage:

  * OBSERVABLE — agent-initiated retrieval. Reads/greps of memory files, `search_kb.py`
    queries, `graphify query`, LightRAG MCP calls, and reads of CLAUDE.md. These are
    tool calls, so they appear in the transcript with their results.
  * ALWAYS-ON — `MEMORY.md` and the project `CLAUDE.md` are loaded into every session
    regardless. Recorded as injected from first principles, not from the transcript.
  * NOT OBSERVABLE — per-memory *automatic recall*. If the harness injects a recalled
    memory as background context, no transcript record distinguishes it (checked against
    a 1.1 MB real session: no attachment type carries it). So `memory_hit_rate` here
    measures **retrieval the agent chose to do**, not everything that reached its window.
    Closing that gap needs harness-side instrumentation; until then the metric must be
    read with that scope attached.

`used` is decided by a cheap post-hoc judge: given the final answer and the entries that
were retrieved, which did the answer actually rely on? That is a weaker instrument than
ablation (re-run without the entry, compare outcome) and is recorded as such in
`agent_retrievals.used_judge`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator, Optional

PROJECTS_DIR = Path(os.getenv("AGENTEVAL_PROJECTS_DIR", Path.home() / ".claude/projects"))
MEMORY_DIR = Path(os.getenv(
    "AGENTEVAL_MEMORY_DIR",
    Path.home() / ".claude/projects/-home-user-project/memory"))

# Bash invocations that are retrievals against a known store.
_KB_RE = re.compile(r"search_kb\.py\s+(?:['\"])?([^'\"|;&\n]+)", re.I)
_GRAPHIFY_RE = re.compile(r"graphify\s+query\s+(?:['\"])?([^'\"|;&\n]+)", re.I)


def find_transcript(session_id: str) -> Optional[Path]:
    """Locate a session's JSONL. The project dir is derived from cwd, so search all."""
    hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _iter_records(path: Path) -> Iterator[dict]:
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _classify(name: str, inp: dict, memory_dir: Path) -> Optional[tuple[str, str]]:
    """Map one tool call to (source, entry_id), or None if it isn't a retrieval."""
    mem = str(memory_dir)

    if name in ("Read", "Grep", "Glob"):
        target = str(inp.get("file_path") or inp.get("path") or "")
        if target.startswith(mem):
            return ("memory", Path(target).name)
        if target.endswith("CLAUDE.md"):
            return ("claude_md", "CLAUDE.md")
        return None

    if name == "Bash":
        cmd = str(inp.get("command") or "")
        if mem in cmd or "/memory/" in cmd:
            # a grep/rg/cat aimed at the memory store
            files = re.findall(r"([A-Za-z0-9_\-]+\.md)", cmd)
            return ("memory", files[0] if files else "memory-store-scan")
        m = _KB_RE.search(cmd)
        if m:
            return ("kb", f"search_kb:{m.group(1).strip()[:120]}")
        m = _GRAPHIFY_RE.search(cmd)
        if m:
            return ("graphify", f"query:{m.group(1).strip()[:120]}")
        if "CLAUDE.md" in cmd:
            return ("claude_md", "CLAUDE.md")
        return None

    if name.startswith("mcp__lightrag") or "lightrag" in name.lower():
        q = str(inp.get("query") or inp.get("prompt") or "")[:120]
        return ("lightrag", f"query:{q}")

    return None


def extract_retrievals(transcript: Path,
                       memory_dir: Path = MEMORY_DIR) -> list[dict]:
    """Retrieval events for one session, deduped by (source, entry_id).

    `tokens` approximates the cost of the entry entering context from the size of the
    tool result it produced (~4 chars/token). A retrieval whose result was an error or
    empty is still recorded — a miss is exactly what `memory_hit_rate` needs to see.
    """
    calls: dict[str, tuple[str, str]] = {}     # tool_use_id -> (source, entry_id)
    found: dict[tuple[str, str], dict] = {}

    for rec in _iter_records(transcript):
        if rec.get("type") not in ("user", "assistant"):
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            if block.get("type") == "tool_use":
                hit = _classify(block.get("name", ""), block.get("input") or {}, memory_dir)
                if hit:
                    calls[block.get("id", "")] = hit
                    found.setdefault(hit, {
                        "source": hit[0], "entry_id": hit[1],
                        "tokens": 0, "injected": True, "score": None,
                    })

            elif block.get("type") == "tool_result":
                hit = calls.get(block.get("tool_use_id", ""))
                if not hit:
                    continue
                raw = block.get("content")
                if isinstance(raw, list):
                    raw = " ".join(str(c.get("text", "")) for c in raw if isinstance(c, dict))
                found[hit]["tokens"] += len(str(raw or "")) // 4

    # Always-on context: present in every session regardless of what the agent did.
    # Their token cost is charged to EVERY run, so it is estimated from file size
    # (~4 chars/token) rather than left at 0 — "what does the always-loaded context cost
    # us per session, and does anything use it?" is the central Phase 2 question, and a
    # zero here would hide it.
    for src, eid, path in (("memory", "MEMORY.md", memory_dir / "MEMORY.md"),
                           ("claude_md", "CLAUDE.md", Path(os.getenv(
                               "AGENTEVAL_ROOT", str(Path.cwd()))) / "CLAUDE.md")):
        try:
            est = path.stat().st_size // 4
        except OSError:
            est = 0
        found.setdefault((src, eid), {"source": src, "entry_id": eid,
                                      "tokens": est, "injected": True, "score": None})

    return list(found.values())


JUDGE_PROMPT = """Below is an agent's final answer to a task, and a list of context entries that
were available to it (memory files, knowledge-base queries, code-graph queries).

Decide which entries the answer ACTUALLY RELIED ON — i.e. the answer contains a specific fact,
path, rule, or number that plausibly came from that entry. Be strict:

- An entry that was retrieved but contributed nothing to the answer is NOT used.
- Do not mark an entry used just because it is topically related.
- General knowledge the model would have anyway does not count as reliance.
- If the answer would be identical without the entry, it is NOT used.

Output ONLY a JSON object, no prose:
{{"used": ["<entry_id>", ...]}}

ENTRIES:
{entries}

ANSWER:
{answer}
"""


def judge_used(answer: str, entries: list[dict], run_agent, model: str = "sonnet") -> set[str]:
    """Ask a cheap model which retrieved entries the answer relied on.

    Weaker than ablation, and recorded as `used_judge='llm'` so the provenance of the
    number travels with it. Returns an empty set on any failure — an unjudged retrieval
    stays NULL in the DB rather than being silently counted either way.
    """
    if not entries or not answer.strip():
        return set()
    listing = "\n".join(f"- {e['entry_id']}  (source={e['source']})" for e in entries)
    r = run_agent(JUDGE_PROMPT.format(entries=listing, answer=answer[:12_000]),
                  model=model)
    if not r.get("ok"):
        return set()
    m = re.search(r"\{.*\}", r.get("text", ""), re.S)
    if not m:
        return set()
    try:
        return set(json.loads(m.group(0)).get("used", []))
    except (json.JSONDecodeError, AttributeError):
        return set()


INSERT_SQL = """
INSERT INTO agent_retrievals
    (run_id, source, entry_id, score, tokens, injected, used, used_judge, judged_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s IS NULL THEN NULL ELSE now() END)
"""


def persist_retrievals(conn, run_id: int, entries: list[dict],
                       used: Optional[set[str]]) -> int:
    cur = conn.cursor()
    n = 0
    for e in entries:
        was_used = None if used is None else (e["entry_id"] in used)
        judge = None if used is None else "llm"
        cur.execute(INSERT_SQL, (run_id, e["source"], e["entry_id"], e.get("score"),
                                 e.get("tokens"), e.get("injected", True),
                                 was_used, judge, judge))
        n += 1
    cur.close()
    return n
