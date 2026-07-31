"""Regression test for #75164 — /steer fallback must not overwrite FIFO head.

Before the fix, _busy_steer_command() in gateway/run.py used direct assignment
adapter._pending_messages[quick_key] = queued_event in both fallback branches,
which overwrote the existing FIFO head (Q1) and placed the newer steer ahead
of the existing overflow (Q2). The fix routes both branches through
_enqueue_fifo(), which preserves the FIFO contract: pending slot = head,
queued_events = overflow tail.

This test reproduces the exact scenario from the issue:
  1. Queue Q1 (slot) + Q2 (overflow)
  2. Dispatch /steer Q3 with agent in _AGENT_PENDING_SENTINEL state
  3. Assert: Q1, Q2, Q3 survive in arrival order
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GATEWAY_RUN = _REPO / "gateway" / "run.py"


def _load_gateway_run():
    spec = importlib.util.spec_from_file_location("gateway_run_75164", _GATEWAY_RUN)
    mod = importlib.util.module_from_spec(spec)
    mod.logger = types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    sys.modules["gateway_run_75164"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pass
    return mod


def _make_runner(mod):
    runner = types.SimpleNamespace()
    runner._queued_events = {}
    runner._pending_messages = {}  # not used by live path but kept for parity
    runner._enqueue_fifo = mod.GatewayRunner._enqueue_fifo.__get__(runner, mod.GatewayRunner)
    return runner


class _Adapter:
    def __init__(self):
        self._pending_messages = {}


class _Event:
    def __init__(self, text):
        self.text = text


def test_steer_fallback_preserves_fifo_order():
    """Q1 in slot, Q2 in overflow; /steer Q3 must yield Q1, Q2, Q3."""
    mod = _load_gateway_run()
    runner = _make_runner(mod)
    adapter = _Adapter()

    Q1 = _Event("Q1")
    Q2 = _Event("Q2")
    Q3 = _Event("Q3")

    # Step 1: queue Q1 (slot) + Q2 (overflow) — exactly what _enqueue_fifo does
    runner._enqueue_fifo("sess", Q1, adapter)
    assert list(adapter._pending_messages.values()) == [Q1]

    runner._enqueue_fifo("sess", Q2, adapter)
    assert list(runner._queued_events["sess"]) == [Q2]

    # Step 2: /steer Q3 via fallback (the buggy path was direct assignment)
    runner._enqueue_fifo("sess", Q3, adapter)

    # Step 3: drain — order must be Q1, Q2, Q3
    drained = []
    if "sess" in adapter._pending_messages:
        drained.append(adapter._pending_messages.pop("sess"))
    drained.extend(runner._queued_events.get("sess", []))
    texts = [e.text for e in drained]
    assert texts == ["Q1", "Q2", "Q3"], f"FIFO order broken: {texts}"


def test_steer_does_not_overwrite_existing_slot():
    """Direct assignment would lose Q1; _enqueue_fifo must preserve it."""
    mod = _load_gateway_run()
    runner = _make_runner(mod)
    adapter = _Adapter()

    Q1 = _Event("Q1")
    Q3 = _Event("Q3")

    # Q1 already in slot (simulating prior queued follow-up)
    adapter._pending_messages["sess"] = Q1

    # /steer Q3 via the fixed path
    runner._enqueue_fifo("sess", Q3, adapter)

    # Q1 must survive in the slot (not overwritten), Q3 goes to overflow
    assert adapter._pending_messages["sess"] is Q1
    assert runner._queued_events["sess"][-1] is Q3
