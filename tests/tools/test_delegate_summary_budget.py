"""Tests for subagent summary budgeting (PR #9126).

delegate_task caps subagent summaries against the parent's remaining context
headroom (split across the batch) before they enter the parent's context, and
spills the full text to disk so nothing is lost. This guards the
compression/429 death spiral that batch fan-out could trigger by returning N
full summaries verbatim into the parent.
"""

import os
import tempfile

import pytest

import tools.delegate_tool as dt


class _FakeCompressor:
    def __init__(self, context_length, max_tokens, last_real_prompt_tokens=0):
        self.context_length = context_length
        self.max_tokens = max_tokens
        # 当前上下文占用（API 实测）。真实 compressor 压缩后保留旧值。
        self.last_real_prompt_tokens = last_real_prompt_tokens


class _FakeParent:
    def __init__(self, context_length, used_tokens, max_tokens, session_prompt_tokens=None):
        self.context_compressor = _FakeCompressor(
            context_length, max_tokens, last_real_prompt_tokens=used_tokens
        )
        # 会话累积消费（含缓存命中）——真实 agent 里它是只增不减的计数器，
        # 与"当前占用"是两回事。默认给一个远超上下文的累积值，让测试模型
        # 贴近真实：若实现误用该计数器，地板截断会立即暴露。
        self.session_prompt_tokens = (
            session_prompt_tokens if session_prompt_tokens is not None
            else context_length * 10
        )


def test_small_summaries_pass_through_untouched():
    parent = _FakeParent(context_length=200_000, used_tokens=10_000, max_tokens=8_000)
    results = [
        {"task_index": 0, "summary": "short result A", "status": "completed"},
        {"task_index": 1, "summary": "short result B", "status": "completed"},
    ]
    dt._apply_summary_budget(results, parent)
    assert results[0]["summary"] == "short result A"
    assert "summary_truncated" not in results[0]
    assert "summary_truncated" not in results[1]


def test_batch_overflow_trimmed_and_spilled_losslessly(monkeypatch):
    # Isolate spill directory to a temp HERMES_HOME.
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", os.path.join(td, ".hermes"))
        # Distinct head + tail markers so we can prove the tail survives.
        big = "HEAD_MARKER\n" + ("X" * 50_000) + "\nTAIL_MARKER"
        # Parent nearly full (120k/131k) → tiny headroom → aggressive trim.
        parent = _FakeParent(context_length=131_000, used_tokens=120_000, max_tokens=8_000)
        results = [
            {"task_index": i, "summary": big, "status": "completed"} for i in range(5)
        ]
        dt._apply_summary_budget(results, parent)
        for r in results:
            assert r["summary_truncated"] is True
            assert len(r["summary"]) < len(big)
            # Head+tail window: both ends survive in-context.
            assert "HEAD_MARKER" in r["summary"]
            assert "TAIL_MARKER" in r["summary"]
            path = r.get("summary_full_path")
            assert path and os.path.exists(path)
            # The spill file holds the FULL original text — nothing is lost.
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == big
            # The footer points the parent at the full version with an offset.
            assert "read_file" in r["summary"]
            assert "offset=" in r["summary"]
            # Spilled into the delegation cache (mounted into remote backends).
            assert os.path.join("cache", "delegation") in path


def test_empty_results_is_noop():
    # No summaries → nothing to do, must not raise.
    dt._apply_summary_budget([], _FakeParent(131_000, 1_000, 8_000))
    dt._apply_summary_budget(
        [{"task_index": 0, "status": "failed", "summary": None}],
        _FakeParent(131_000, 1_000, 8_000),
    )


def test_cumulative_session_tokens_do_not_trigger_floor():
    """Regression: used-tokens must be CURRENT context occupancy, not the
    session counter.

    Real agents accumulate session_prompt_tokens += prompt_tokens (incl.
    cache hits) on every API response — in a long session that easily reaches
    millions while the actual context window is barely a fraction full. The
    old implementation read session_prompt_tokens as "used", which made
    headroom ≤ 0 on any long session and crushed every subagent summary to
    the _MIN_SUMMARY_CHARS floor. See delegate budget code around
    _parent_summary_char_budget.
    """
    # Reproduce the reported session: 1M window, ~143K current occupancy,
    # ~11M cumulative (168K new + 10.9M cache reads), 100K output reserve.
    parent = _FakeParent(
        context_length=1_000_000,
        used_tokens=143_000,
        max_tokens=100_000,
        session_prompt_tokens=11_000_000,
    )
    results = [{"task_index": 0, "summary": "S" * 8_000, "status": "completed"}]
    dt._apply_summary_budget(results, parent)

    # Budget must come from remaining headroom, NOT the cumulative counter.
    # 1M - 143K - 100K = 757K → half → ~378K tokens → ~1.5M chars, so an
    # 8K-char summary passes untouched. Old buggy code: headroom ≤ 0 →
    # summary trimmed to the 2000-char floor.
    assert results[0]["summary"] == "S" * 8_000
    assert "summary_truncated" not in results[0]
