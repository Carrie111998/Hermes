"""Regression tests for issue #17335.

The ``quiet_mode=True`` fast path in :func:`model_tools.get_tool_definitions`
memoizes results to avoid re-walking the registry on every Gateway call. The
cached object must NOT be aliased into callers' return values \u2014 long-lived
Gateway processes mutate the returned list (``run_agent`` appends memory and
LCM context-engine tool schemas to ``self.tools``), and a shared list would
poison subsequent agent inits with duplicate tool names. Providers that
enforce uniqueness (DeepSeek, Xiaomi MiMo, Moonshot/Kimi) then reject the
API call with HTTP 400.

These tests pin:
- the cache-hit path returns a fresh list (existing #17098 behavior)
- the first uncached call also returns a fresh list (the fix)
- every call returns a list that is not the cached one, even after mutation
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier

import pytest

import model_tools


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test starts with an empty quiet_mode cache."""
    model_tools._tool_defs_cache.clear()
    yield
    model_tools._tool_defs_cache.clear()


class TestQuietModeCacheIsolation:

    def test_same_size_same_mtime_config_rewrite_recomputes_definitions(
        self, tmp_path, monkeypatch
    ):
        """A content edit must invalidate the quiet-mode cache even when a
        writer preserves both the config file's size and nanosecond mtime."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("toolsets:\n  - file\n", encoding="utf-8")
        original_stat = config_file.stat()
        monkeypatch.setattr(
            "hermes_cli.config.get_config_path", lambda: config_file
        )

        calls = 0
        original_compute = model_tools._compute_tool_definitions

        def compute(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_compute(*args, **kwargs)

        monkeypatch.setattr(model_tools, "_compute_tool_definitions", compute)

        model_tools.get_tool_definitions(
            enabled_toolsets=["file"], quiet_mode=True
        )
        config_file.write_text("toolsets:\n  - test\n", encoding="utf-8")
        os.utime(
            config_file,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        assert config_file.stat().st_size == original_stat.st_size
        assert config_file.stat().st_mtime_ns == original_stat.st_mtime_ns

        model_tools.get_tool_definitions(
            enabled_toolsets=["file"], quiet_mode=True
        )

        assert calls == 2

    def test_first_uncached_call_returns_fresh_list(self):
        """The first quiet_mode call must not alias the cached object \u2014
        otherwise a caller mutating the returned list mutates the cache."""
        first = model_tools.get_tool_definitions(quiet_mode=True)
        assert isinstance(first, list)
        # Find the cached value to compare identity.
        assert len(model_tools._tool_defs_cache) == 1
        cached = next(iter(model_tools._tool_defs_cache.values()))
        assert first is not cached, (
            "issue #17335: first quiet_mode call returned the cached list "
            "by reference \u2014 mutations will leak into subsequent calls."
        )

    def test_cache_hit_returns_fresh_list(self):
        """The cache-hit path already returned a copy pre-fix; pin it."""
        first = model_tools.get_tool_definitions(quiet_mode=True)
        second = model_tools.get_tool_definitions(quiet_mode=True)
        assert first is not second
        cached = next(iter(model_tools._tool_defs_cache.values()))
        assert second is not cached



    def test_cache_bounded_by_eviction(self):
        """The cache evicts the oldest entry when it reaches the cap,
        keeping the cache bounded instead of growing unbounded over a
        long-lived Gateway's lifetime (#19251)."""
        cap = model_tools._TOOL_DEFS_CACHE_MAX
        # Fill cache to the cap with distinct keys by varying enabled_toolsets.
        for i in range(cap):
            model_tools.get_tool_definitions(
                enabled_toolsets=[f"fake_toolset_{i}"], quiet_mode=True,
            )
        assert len(model_tools._tool_defs_cache) == cap

        # Adding one more must evict the oldest, not clear everything and
        # not grow past the cap.
        model_tools.get_tool_definitions(
            enabled_toolsets=["fake_toolset_overflow"], quiet_mode=True,
        )
        assert len(model_tools._tool_defs_cache) == cap, (
            "Eviction should keep the cache at the cap, not clear it or grow"
        )

    def test_non_quiet_mode_does_not_use_cache(self):
        """Sanity: quiet_mode=False (TUI path) skips the cache entirely \u2014
        explains why the bug only hit Gateway."""
        model_tools.get_tool_definitions(quiet_mode=False)
        assert len(model_tools._tool_defs_cache) == 0

    def test_concurrent_capacity_misses_evict_atomically(self, monkeypatch):
        """Two profile/toolset misses at capacity cannot race on eviction."""
        barrier = Barrier(2)

        def compute(*args, **kwargs):
            barrier.wait(timeout=2)
            return []

        monkeypatch.setattr(model_tools, "_compute_tool_definitions", compute)
        for index in range(model_tools._TOOL_DEFS_CACHE_MAX):
            model_tools._tool_defs_cache[("old", index)] = []

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    model_tools.get_tool_definitions,
                    enabled_toolsets=[f"concurrent_{index}"],
                    quiet_mode=True,
                )
                for index in range(2)
            ]
            assert [future.result(timeout=2) for future in futures] == [[], []]

        assert len(model_tools._tool_defs_cache) == model_tools._TOOL_DEFS_CACHE_MAX
