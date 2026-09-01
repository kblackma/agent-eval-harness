#!/usr/bin/env python3
"""Guards for the harness-computed eval score (docs/SCORING.md).

The whole value of this scorer is that the agent being ranked cannot influence it,
and that a candidate which is fast but WRONG is refused rather than ranked. Both
are asserted against the behaviour they forbid.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent_eval"))

import eval_score  # noqa: E402


def _run(monkeypatch, runs, succ, mean_s, **kw):
    # Stub every DB entry point, so these guards run without a Postgres instance.
    # `measure` is the one under test; the other two only need to be inert.
    monkeypatch.setattr(eval_score, "measure", lambda l, c: (runs, succ, mean_s))
    monkeypatch.setattr(eval_score, "per_task_stats", lambda l, c: (0, None))
    monkeypatch.setattr(eval_score, "task_ids", lambda l, c: set())
    argv = ["eval_score.py", "--label", "x"] + [a for k, v in kw.items()
                                                for a in (f"--{k.replace('_','-')}", str(v))]
    monkeypatch.setattr(sys, "argv", argv)
    return eval_score.main()


def test_correct_and_fast_is_scored(monkeypatch, capsys):
    assert _run(monkeypatch, 10, 1.0, 9.0865) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "9.0865"


def test_fast_but_wrong_is_refused_not_ranked(monkeypatch):
    """Rule: a candidate failing correctness scores nothing, however fast.

    This is the real measured case — kb-first-retrieval was 0.90 success at 24.6s
    against a 1.00 / 9.1s control. A scorer that ranked on speed alone would have
    to be told separately that it lost.
    """
    assert _run(monkeypatch, 10, 0.9, 2.0) == 1


def test_partial_batch_is_refused(monkeypatch):
    """A policy that answered two tasks quickly must not 'beat' one that answered
    ten — that is a coverage failure wearing a good score."""
    assert _run(monkeypatch, 2, 1.0, 1.0) == 2


def test_empty_batch_is_an_error_not_a_zero(monkeypatch):
    assert _run(monkeypatch, 0, None, None) == 2


def test_missing_rows_never_produce_a_number(monkeypatch, capsys):
    _run(monkeypatch, 0, None, None)
    assert capsys.readouterr().out.strip() == "", "an unscorable batch printed a score"


def test_real_incumbent_batch_scores(monkeypatch):
    """Integration: the measured control batch must still score, against the live DB.
    Skips rather than fails when the batch is absent (fresh DB)."""
    try:
        runs, succ, mean_s = eval_score.measure("retrieval-probe2", "retrieval")
    except Exception as e:
        pytest.skip(f"DB unavailable: {e}")
    if runs == 0:
        pytest.skip("retrieval-probe2 batch not present")
    assert succ == 1.0 and 5 < mean_s < 30


def test_cli_exits_nonzero_on_correctness_failure():
    """End to end through the actual CLI the gate will invoke."""
    p = subprocess.run(
        [sys.executable, str(ROOT / "eval_score.py"),
         "--label", "kb-first-retrieval", "--category", "retrieval"],
        capture_output=True, text=True, env={"DB_HOST": "127.0.0.1", "PATH": "/usr/bin:/bin"})
    if "no usable rows" in p.stderr or p.returncode == 2:
        pytest.skip("kb-first-retrieval batch not present")
    assert p.returncode == 1
    assert p.stdout.strip() == "", "a failing candidate emitted a rankable score"


# ── the two defects the first real scored run exposed ────────────────────

def test_task_set_drift_is_refused(monkeypatch):
    """The defect that invalidated the first comparison.

    retrieval-probe2 was measured over 10 tasks; by the time a candidate ran,
    ret_free_tier_dispatch had been removed from the suite, so the candidate was
    scored over 9 — and that task was the SLOWEST (14.57s), worth ~57% of the
    apparent gain on its own. Two attempts over different task sets are not
    comparable, and nothing said so.
    """
    monkeypatch.setattr(eval_score, "measure", lambda l, c: (18, 1.0, 7.65))
    monkeypatch.setattr(eval_score, "per_task_stats", lambda l, c: (9, 0.97))
    monkeypatch.setattr(eval_score, "task_ids",
                        lambda l, c: {"a", "b"} if l == "cand" else {"a", "b", "gone"})
    monkeypatch.setattr(sys, "argv",
                        ["eval_score.py", "--label", "cand", "--require-tasks", "incumbent"])
    assert eval_score.main() == 2


def test_matching_task_set_is_scored(monkeypatch, capsys):
    monkeypatch.setattr(eval_score, "measure", lambda l, c: (18, 1.0, 7.65))
    monkeypatch.setattr(eval_score, "per_task_stats", lambda l, c: (9, 0.97))
    monkeypatch.setattr(eval_score, "task_ids", lambda l, c: {"a", "b"})
    monkeypatch.setattr(sys, "argv",
                        ["eval_score.py", "--label", "cand", "--require-tasks", "incumbent"])
    assert eval_score.main() == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "7.6500"


def test_single_repeat_batch_is_refused(monkeypatch):
    """One sample of a metric whose run-to-run spread is ~1s cannot resolve a
    sub-second difference. The incumbent batch itself fails this."""
    monkeypatch.setattr(eval_score, "measure", lambda l, c: (10, 1.0, 9.09))
    monkeypatch.setattr(eval_score, "per_task_stats", lambda l, c: (10, None))
    monkeypatch.setattr(sys, "argv", ["eval_score.py", "--label", "x"])
    assert eval_score.main() == 2


def test_noise_floor_is_reported(monkeypatch, capsys):
    """A margin inside the noise must be visible to whoever reads the gate log —
    the first run merged a 0.83s 'win' against a ~0.97s noise floor."""
    monkeypatch.setattr(eval_score, "measure", lambda l, c: (18, 1.0, 7.65))
    monkeypatch.setattr(eval_score, "per_task_stats", lambda l, c: (9, 0.97))
    monkeypatch.setattr(sys, "argv", ["eval_score.py", "--label", "x"])
    eval_score.main()
    err = capsys.readouterr().err
    assert "within_task_spread=0.97s" in err and "min_delta" in err


def test_real_batches_are_not_comparable_as_scored():
    """Integration guard on the actual finding: pinning the candidate to the
    incumbent's task set must fail against the live DB."""
    try:
        want = eval_score.task_ids("retrieval-probe2", "retrieval")
        got = eval_score.task_ids("latency-policy-v2", "retrieval")
    except Exception as e:
        pytest.skip(f"DB unavailable: {e}")
    if not want or not got:
        pytest.skip("batches not present")
    assert want - got == {"ret_free_tier_dispatch"}, \
        "the documented task-set drift is not what the docstring describes"
