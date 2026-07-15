"""Unit tests for tools.tool_timeout_context — generic runtime context for
the effective outer tool-execution timeout.

Covers:
  1. Default value is None (no outer timeout).
  2. set_current_tool_timeout / reset_current_tool_timeout round-trip.
  3. Context isolation: different contexts don't bleed.
  4. Thread propagation via contextvars.copy_context().
  5. HERMES_CONCURRENT_TOOL_TIMEOUT_S default/override/<=0 behavior
     (via _resolve_concurrent_tool_timeout regression checks).
"""

from __future__ import annotations

import contextvars
import os
import threading

import pytest

from tools.tool_timeout_context import (
    get_current_tool_timeout,
    set_current_tool_timeout,
    reset_current_tool_timeout,
)


# --------------------------------------------------------------------------- #
# 1. Default value
# --------------------------------------------------------------------------- #

class TestDefaultValue:
    def test_default_is_none(self):
        assert get_current_tool_timeout() is None

    def test_default_is_none_in_fresh_context(self):
        """In a fresh contextvar context, the default must still be None."""
        ctx = contextvars.copy_context()
        result = ctx.run(get_current_tool_timeout)
        assert result is None


# --------------------------------------------------------------------------- #
# 2. Set / reset round-trip
# --------------------------------------------------------------------------- #

class TestSetReset:
    def test_set_returns_float(self):
        token = set_current_tool_timeout(420.0)
        try:
            assert get_current_tool_timeout() == 420.0
        finally:
            reset_current_tool_timeout(token)

    def test_set_none(self):
        token = set_current_tool_timeout(None)
        try:
            assert get_current_tool_timeout() is None
        finally:
            reset_current_tool_timeout(token)

    def test_reset_restores_previous(self):
        token1 = set_current_tool_timeout(100.0)
        token2 = set_current_tool_timeout(200.0)
        try:
            assert get_current_tool_timeout() == 200.0
        finally:
            reset_current_tool_timeout(token2)
        try:
            assert get_current_tool_timeout() == 100.0
        finally:
            reset_current_tool_timeout(token1)

    def test_reset_restores_none(self):
        token = set_current_tool_timeout(300.0)
        reset_current_tool_timeout(token)
        assert get_current_tool_timeout() is None


# --------------------------------------------------------------------------- #
# 3. Context isolation
# --------------------------------------------------------------------------- #

class TestContextIsolation:
    def test_set_in_one_context_does_not_leak(self):
        token = set_current_tool_timeout(999.0)
        try:
            ctx = contextvars.copy_context()
            result_in_ctx = ctx.run(get_current_tool_timeout)
            assert result_in_ctx == 999.0

            # Modify inside the copied context
            def _modify():
                inner_token = set_current_tool_timeout(111.0)
                val = get_current_tool_timeout()
                reset_current_tool_timeout(inner_token)
                return val

            modified_val = ctx.run(_modify)
            assert modified_val == 111.0

            # Outer context unaffected
            assert get_current_tool_timeout() == 999.0
        finally:
            reset_current_tool_timeout(token)


# --------------------------------------------------------------------------- #
# 4. Thread propagation
# --------------------------------------------------------------------------- #

class TestThreadPropagation:
    def test_value_visible_in_worker_thread_via_copy_context(self):
        """When set before spawning a thread that runs copy_context(),
        the worker should see the value."""
        token = set_current_tool_timeout(450.0)
        results = []
        try:
            def worker():
                # Simulate what propagate_context_to_thread does:
                # it calls contextvars.copy_context() on the parent,
                # then ctx.run(target).
                results.append(get_current_tool_timeout())

            ctx = contextvars.copy_context()
            t = threading.Thread(target=lambda: ctx.run(worker))
            t.start()
            t.join()
            assert results == [450.0]
        finally:
            reset_current_tool_timeout(token)

    def test_value_not_visible_without_copy_context(self):
        """A bare thread (no context copy) should see the default None."""
        token = set_current_tool_timeout(500.0)
        results = []
        try:
            def worker():
                results.append(get_current_tool_timeout())

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            # Bare thread — no context propagation — sees default None.
            assert results == [None]
        finally:
            reset_current_tool_timeout(token)


# --------------------------------------------------------------------------- #
# 5. _resolve_concurrent_tool_timeout regression checks
# --------------------------------------------------------------------------- #

class TestResolveConcurrentToolTimeout:
    """Verify the env-var-driven timeout resolution still works correctly.
    These are NOT testing tool_timeout_context itself, but verify the
    integration point doesn't regress."""

    def test_default_returns_420(self, monkeypatch):
        monkeypatch.delenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
        from agent.tool_executor import _resolve_concurrent_tool_timeout
        assert _resolve_concurrent_tool_timeout() == 420.0

    def test_override_returns_value(self, monkeypatch):
        monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "600")
        from agent.tool_executor import _resolve_concurrent_tool_timeout
        assert _resolve_concurrent_tool_timeout() == 600.0

    def test_zero_or_negative_returns_none(self, monkeypatch):
        monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "0")
        from agent.tool_executor import _resolve_concurrent_tool_timeout
        assert _resolve_concurrent_tool_timeout() is None

        monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "-1")
        assert _resolve_concurrent_tool_timeout() is None

    def test_invalid_value_falls_back_to_420(self, monkeypatch):
        monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "not_a_number")
        from agent.tool_executor import _resolve_concurrent_tool_timeout
        assert _resolve_concurrent_tool_timeout() == 420.0

    def test_empty_string_returns_420(self, monkeypatch):
        monkeypatch.setenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", "")
        from agent.tool_executor import _resolve_concurrent_tool_timeout
        assert _resolve_concurrent_tool_timeout() == 420.0
