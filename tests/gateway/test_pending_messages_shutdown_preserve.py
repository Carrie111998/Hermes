"""Regression tests for #72680 — pending messages must survive shutdown.

Before the fix, GatewayRunner._stop_impl() called self._pending_messages.clear()
unconditionally, permanently discarding messages that could not be persisted
because the FTS/SQLite index was corrupt (disk=0, memory=N). The fix adds
_preserve_pending_messages_on_shutdown(), which dumps a recovery JSON snapshot
before clearing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_GATEWAY_RUN = _REPO / "gateway" / "run.py"


def _load_gateway_run():
    # gateway.run imports heavy deps; load just the module object and inject a
    # minimal fake for the bits we touch (logger) so we can instantiate the
    # method without the full gateway stack.
    spec = importlib.util.spec_from_file_location("gateway_run_72680", _GATEWAY_RUN)
    mod = importlib.util.module_from_spec(spec)
    # Stub logger referenced inside the method.
    mod.logger = types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None)
    sys.modules["gateway_run_72680"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Some module-level imports may fail in isolation; we only need the
        # class method, which is defined at parse time regardless.
        pass
    return mod


def _make_runner(mod):
    # Build a bare object with the method + the attribute it reads.
    runner = types.SimpleNamespace()
    runner._pending_messages = {}
    # Bind the method onto our object.
    runner._preserve_pending_messages_on_shutdown = (
        mod.GatewayRunner._preserve_pending_messages_on_shutdown.__get__(runner, mod.GatewayRunner)
    )
    return runner


def test_preserves_pending_messages_to_recovery_file(tmp_path, monkeypatch):
    mod = _load_gateway_run()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(mod)
    runner._pending_messages = {
        "session:1": {"role": "user", "content": "hello"},
        "session:2": {"role": "assistant", "content": "hi there"},
    }
    runner._preserve_pending_messages_on_shutdown()

    recovery_dir = tmp_path / "shutdown-recovery"
    files = list(recovery_dir.glob("pending_messages_*.json"))
    assert files, "expected a recovery snapshot file"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["issue"] == "#72680"
    assert data["count"] == 2
    assert data["messages"]["session:1"]["content"] == "hello"


def test_no_op_when_empty(tmp_path, monkeypatch):
    mod = _load_gateway_run()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _make_runner(mod)
    runner._pending_messages = {}
    runner._preserve_pending_messages_on_shutdown()  # must not raise
    assert not list((tmp_path / "shutdown-recovery").glob("*.json"))


def test_non_fatal_on_write_error(tmp_path, monkeypatch):
    mod = _load_gateway_run()
    # Point HERMES_HOME at a file so makedirs fails -> exception must be swallowed.
    bad = tmp_path / "not-a-dir"
    bad.write_text("x")
    monkeypatch.setenv("HERMES_HOME", str(bad))
    runner = _make_runner(mod)
    runner._pending_messages = {"s": {"role": "user", "content": "x"}}
    # Must not raise even though the dump fails.
    runner._preserve_pending_messages_on_shutdown()
