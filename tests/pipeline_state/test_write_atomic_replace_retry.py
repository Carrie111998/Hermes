"""Regression test for the Windows WinError 5 (ACCESS_DENIED) race on the
atomic pipeline.json write, fixed in ef3f40070 (2026-07-17).

On Windows, ``os.replace()`` (MoveFileEx REPLACE_EXISTING) fails with
``PermissionError`` [WinError 5] when ANY process has the destination file
open for read at the instant of the rename, because CPython opens files
without FILE_SHARE_DELETE. PipelineManager's readers are lock-free by design
and cross-process readers (Control Center :9120, JobFlow dashboard :3001)
poll pipeline.json, so the sub-millisecond replace window genuinely collides
in production and dead-lettered STATE_TRANSITION_INTENT files.

The fix wraps ``os.replace`` in a bounded retry (``_REPLACE_MAX_ATTEMPTS``
attempts, linear backoff). These tests pin that behavior deterministically by
mocking ``os.replace`` — they do NOT spin real threads to race the file (that
would be inherently flaky).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline_state import PipelineManager
from pipeline_state.manager import _REPLACE_MAX_ATTEMPTS


@pytest.fixture
def manager(tmp_path: Path) -> PipelineManager:
    """A PipelineManager pointed at a fresh temp pipeline.json."""
    return PipelineManager(path=tmp_path / "pipeline.json")


class TestWriteAtomicReplaceRetry:
    """`_write_atomic` retries os.replace across the transient WinError 5 race."""

    @pytest.mark.parametrize("k", [1, 2, _REPLACE_MAX_ATTEMPTS - 1])
    def test_retries_then_succeeds(self, manager: PipelineManager, k: int):
        """os.replace raises PermissionError on the first K calls, succeeds on
        the K+1th. The write must succeed, os.replace must be called exactly
        K+1 times, and the data must actually land on disk."""
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= k:
                raise PermissionError(22, "Access is denied", src, 5, dst)
            return real_replace(src, dst)

        data = {"_schema": "jobflow-pipeline-v1", "jobs": [{"job_id": "x"}]}

        # Patch sleep so the backoff doesn't slow the test; assert it's used.
        with patch("pipeline_state.manager.os.replace", side_effect=flaky_replace) as m_replace, \
                patch("pipeline_state.manager.time.sleep") as m_sleep:
            manager._write_atomic(data)

        assert calls["n"] == k + 1
        assert m_replace.call_count == k + 1
        assert m_sleep.call_count == k  # one backoff sleep per failed attempt

        # The data actually persisted and the temp file was consumed.
        assert json.loads(manager.path.read_text(encoding="utf-8")) == data
        assert not manager.path.with_suffix(manager.path.suffix + ".tmp").exists()

    def test_gives_up_after_max_attempts(self, manager: PipelineManager):
        """If os.replace fails on every attempt, `_write_atomic` re-raises the
        PermissionError after exactly _REPLACE_MAX_ATTEMPTS calls (does not
        loop forever, does not swallow the error)."""
        with patch("pipeline_state.manager.os.replace",
                   side_effect=PermissionError(22, "Access is denied", "tmp", 5, "dst")) as m_replace, \
                patch("pipeline_state.manager.time.sleep") as m_sleep:
            with pytest.raises(PermissionError):
                manager._write_atomic({"jobs": []})

        assert m_replace.call_count == _REPLACE_MAX_ATTEMPTS
        # A backoff sleep between each attempt, but none after the final failure.
        assert m_sleep.call_count == _REPLACE_MAX_ATTEMPTS - 1

    def test_no_retry_on_first_try_success(self, manager: PipelineManager):
        """The happy path (POSIX, or an uncontended Windows write) calls
        os.replace exactly once and never sleeps."""
        with patch("pipeline_state.manager.time.sleep") as m_sleep:
            manager._write_atomic({"jobs": [{"job_id": "y"}]})

        assert m_sleep.call_count == 0
        assert manager.path.exists()
