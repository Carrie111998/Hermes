import json
from pathlib import Path
from unittest.mock import patch

import pytest

from events.state import load_state, save_state
from events.state import _REPLACE_MAX_ATTEMPTS


def test_load_state_returns_default_when_file_missing(tmp_path):
    assert load_state(tmp_path / "missing.json", default={"x": 1}) == {"x": 1}


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"foo": "bar", "n": 42})
    assert load_state(path, default={}) == {"foo": "bar", "n": 42}


def test_load_state_falls_back_on_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not-json", encoding="utf-8")
    assert load_state(path, default={"fallback": True}) == {"fallback": True}


def test_save_state_creates_parent_directories(tmp_path):
    nested = tmp_path / "a" / "b" / "c" / "state.json"
    save_state(nested, {"ok": True})
    assert nested.exists()


def test_save_state_is_atomic(tmp_path):
    """Writing should use a tmp file + rename to avoid partial writes."""
    path = tmp_path / "state.json"
    save_state(path, {"count": 1})
    save_state(path, {"count": 2})
    assert json.loads(path.read_text()) == {"count": 2}
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


class TestSaveStateReplaceRetry:
    """`save_state` retries os.replace across the transient Windows WinError 5
    race, mirroring pipeline_state.manager._write_atomic.

    On Windows, ``os.replace`` fails with ``PermissionError`` [WinError 5] when
    a lock-free reader has the destination open at the instant of the rename
    (CPython omits FILE_SHARE_DELETE). ``notifier_batch.json`` and other
    subscriber state written via ``save_state`` are polled cross-process, so the
    replace window genuinely collides in production (observed 2026-07-17 21:15
    on notifier_batch.json). These tests pin the bounded retry deterministically
    by mocking ``os.replace`` — they do NOT spin real threads (that would flake).
    """

    @pytest.mark.parametrize("k", [1, 2, _REPLACE_MAX_ATTEMPTS - 1])
    def test_retries_then_succeeds(self, tmp_path: Path, k: int):
        """os.replace raises PermissionError on the first K calls, succeeds on
        the K+1th; the write lands, os.replace is called exactly K+1 times."""
        import os as _os

        real_replace = _os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= k:
                raise PermissionError(22, "Access is denied", src, 5, dst)
            return real_replace(src, dst)

        path = tmp_path / "state.json"
        data = {"batch": [1, 2, 3]}

        with patch("events.state.os.replace", side_effect=flaky_replace) as m_replace, \
                patch("events.state.time.sleep") as m_sleep:
            save_state(path, data)

        assert calls["n"] == k + 1
        assert m_replace.call_count == k + 1
        assert m_sleep.call_count == k  # one backoff sleep per failed attempt
        assert json.loads(path.read_text(encoding="utf-8")) == data
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_gives_up_after_max_attempts(self, tmp_path: Path):
        """If os.replace fails on every attempt, save_state re-raises after
        exactly _REPLACE_MAX_ATTEMPTS calls (no infinite loop, no swallow)."""
        path = tmp_path / "state.json"
        with patch("events.state.os.replace",
                   side_effect=PermissionError(22, "Access is denied", "tmp", 5, "dst")) as m_replace, \
                patch("events.state.time.sleep") as m_sleep:
            with pytest.raises(PermissionError):
                save_state(path, {"batch": []})

        assert m_replace.call_count == _REPLACE_MAX_ATTEMPTS
        assert m_sleep.call_count == _REPLACE_MAX_ATTEMPTS - 1

    def test_no_retry_on_first_try_success(self, tmp_path: Path):
        """The happy path (POSIX, or uncontended Windows) calls os.replace once
        and never sleeps."""
        path = tmp_path / "state.json"
        with patch("events.state.time.sleep") as m_sleep:
            save_state(path, {"batch": [7]})

        assert m_sleep.call_count == 0
        assert path.exists()
