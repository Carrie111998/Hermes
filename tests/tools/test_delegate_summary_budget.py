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
    """Mirrors the real ContextCompressor's sentinel semantics: 0 = "no
    real usage yet", -1 = "awaiting real usage after compression"."""

    def __init__(
        self,
        context_length,
        max_tokens,
        last_real_prompt_tokens=0,
        last_prompt_tokens=0,
    ):
        self.context_length = context_length
        self.max_tokens = max_tokens
        self.last_real_prompt_tokens = last_real_prompt_tokens
        self.last_prompt_tokens = last_prompt_tokens


class _FakeParent:
    def __init__(
        self,
        context_length,
        used_tokens,
        max_tokens,
        last_real_prompt_tokens=0,
        last_prompt_tokens=0,
    ):
        self.context_compressor = _FakeCompressor(
            context_length,
            max_tokens,
            last_real_prompt_tokens=last_real_prompt_tokens,
            last_prompt_tokens=last_prompt_tokens,
        )
        self.session_prompt_tokens = used_tokens


def test_summary_budget_uses_live_occupancy_not_cumulative_counter():
    """#84020: session_prompt_tokens is a monotonic cumulative counter that
    exceeds context_length on long cache-heavy sessions. The budget must
    size against the compressor's real window occupancy, not the cumulative
    counter, or every summary is crushed to the 2000-char floor."""
    # Cumulative counter already exceeds the window (11M cumulative vs 1M
    # window), but live occupancy is a comfortable 143K of 1M.
    parent = _FakeParent(
        context_length=1_000_000,
        used_tokens=11_000_000,
        max_tokens=8_000,
        last_real_prompt_tokens=143_000,
        last_prompt_tokens=143_000,
    )
    budget = dt._parent_summary_char_budget(parent, n_summaries=2)
    assert budget is not None
    assert budget > 2_000  # NOT the floor
    assert budget < 4_000_000  # sane upper bound (fraction of headroom)


def test_summary_budget_zero_sentinel_falls_back_to_cumulative():
    """last_real_prompt_tokens=0 (documented "no real usage yet") must not
    be treated as empty-window occupancy - fall back to the cumulative
    counter, which is small pre-first-response."""
    parent = _FakeParent(
        context_length=200_000,
        used_tokens=10_000,
        max_tokens=8_000,
        last_real_prompt_tokens=0,
        last_prompt_tokens=0,
    )
    budget = dt._parent_summary_char_budget(parent, n_summaries=1)
    assert budget is not None
    assert budget > 2_000


def test_summary_budget_negative_sentinel_uses_last_real_occupancy():
    """After compression, last_prompt_tokens=-1 ("awaiting real usage")
    while last_real_prompt_tokens keeps the pre-compaction reading. The
    budget must use that real reading - falling back to the large
    cumulative counter would crush the summary to the floor."""
    parent = _FakeParent(
        context_length=1_000_000,
        used_tokens=11_000_000,
        max_tokens=8_000,
        last_real_prompt_tokens=143_000,
        last_prompt_tokens=-1,
    )
    budget = dt._parent_summary_char_budget(parent, n_summaries=1)
    assert budget is not None
    assert budget > 2_000  # NOT the floor


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
