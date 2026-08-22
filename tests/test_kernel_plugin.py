"""Sabotage-run tests for plugins/kernel/.

Same discipline as the rest of the harness's own testing
(plugins/contrib-screen/'s claim-tool tests): prove the check actually
fires on a deliberately-diverged input, not just that the comparison
exists. A message-count drop between two consecutive calls in the same
session is the concrete failure mode plugins/kernel/kernel.py claims to
catch (see its module docstring for what's in and out of scope).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "kernel"


def _load_kernel_module():
    """Import plugins/kernel/kernel.py without going through the plugin loader."""
    sys.path.insert(0, str(PLUGIN_DIR.parent.parent))
    try:
        module = importlib.import_module("plugins.kernel.kernel")
        importlib.reload(module)
        return module
    finally:
        sys.path.remove(str(PLUGIN_DIR.parent.parent))


kernel = _load_kernel_module()


def test_content_hash_is_deterministic():
    a = kernel.content_hash({"b": 2, "a": 1})
    b = kernel.content_hash({"a": 1, "b": 2})
    assert a == b


def test_content_hash_detects_real_change():
    a = kernel.content_hash({"messages": [{"role": "user", "content": "hi"}]})
    b = kernel.content_hash({"messages": [{"role": "user", "content": "bye"}]})
    assert a != b


def test_first_call_in_a_session_has_no_baseline(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    coverage_state, violation = kernel.check_continuity("session-1", 4, log_path)
    assert coverage_state == kernel.CoverageState.UNKNOWN.value
    assert violation is None


def test_growing_history_is_not_a_violation(tmp_path):
    log_path = str(tmp_path / "events.jsonl")
    kernel.append_event(
        log_path,
        kernel.KernelEvent(
            kind="api_request",
            session_id="session-1",
            api_request_id="req-1",
            message_count=4,
        ),
    )
    coverage_state, violation = kernel.check_continuity("session-1", 6, log_path)
    assert coverage_state == kernel.CoverageState.ATTRIBUTION_ONLY.value
    assert violation is None


def test_sabotage_run_shrinking_history_is_caught(tmp_path):
    """The actual sabotage run: feed it a message list that's shrunk from
    what the prior call in the same session sent, confirm it's flagged —
    not just that the comparison exists."""
    log_path = str(tmp_path / "events.jsonl")
    kernel.append_event(
        log_path,
        kernel.KernelEvent(
            kind="api_request",
            session_id="session-1",
            api_request_id="req-1",
            message_count=12,
        ),
    )
    coverage_state, violation = kernel.check_continuity("session-1", 5, log_path)
    assert coverage_state == kernel.CoverageState.ATTRIBUTION_ONLY.value
    assert violation is not None
    assert violation["prior_message_count"] == 12
    assert violation["current_message_count"] == 5
    assert violation["prior_api_request_id"] == "req-1"


def test_violation_is_scoped_to_its_own_session(tmp_path):
    """A shrink in a DIFFERENT session must not false-positive here —
    the invariant is per-session continuity, not a global counter."""
    log_path = str(tmp_path / "events.jsonl")
    kernel.append_event(
        log_path,
        kernel.KernelEvent(
            kind="api_request",
            session_id="session-A",
            api_request_id="req-A1",
            message_count=50,
        ),
    )
    coverage_state, violation = kernel.check_continuity("session-B", 3, log_path)
    assert coverage_state == kernel.CoverageState.UNKNOWN.value
    assert violation is None


def test_load_session_events_ignores_malformed_lines(tmp_path):
    log_path = tmp_path / "events.jsonl"
    log_path.write_text("not json\n{\"session_id\": \"session-1\", \"kind\": \"api_request\", \"message_count\": 3}\n")
    events = kernel.load_session_events(str(log_path), "session-1")
    assert len(events) == 1
    assert events[0]["message_count"] == 3


def test_hook_registration_via_doctor_finds_no_drift():
    """The same real drift class caught for contrib-screen's claim tool:
    plugin.yaml's provides_hooks must match what register() actually
    registers. Runs Hermes' real scanner and registration path
    (hermes plugins doctor's own implementation), not a synthetic import
    check."""
    from hermes_cli.plugin_dev import doctor_plugin

    report = doctor_plugin(str(PLUGIN_DIR))
    assert report.ok, [f.message for f in report.findings]
    assert set(report.registered_hooks) == {"pre_api_request", "post_api_request"}
