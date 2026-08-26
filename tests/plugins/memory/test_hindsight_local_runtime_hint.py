"""NousResearch/hermes-agent#7718 — actionable message when local_embedded
runtime (`hindsight-all`) is missing.

`local_embedded` imports `from hindsight import HindsightEmbedded`, provided
only by `hindsight-all`. When it's absent the provider disables itself; the
disable warning should point the user at the fix rather than just echoing
`No module named 'hindsight'`.
"""

import sys

import plugins.memory.hindsight as hs
from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    _check_local_runtime,
    _local_runtime_hint,
)


def test_fastmcp_mcp_conflict_detected():
    """_check_local_runtime detects the fastmcp/mcp 1.x vs mcp 2.0 conflict.

    #95855: When fastmcp cannot import 'request_ctx' from mcp 2.0.0, the probe
    surfaces actionable guidance about the incompatibility instead of a generic
    'No module named' hint.
    """
    # Mock the exact ImportError pattern fastmcp emits when mcp 2.0.0 is pinned.
    exc = ImportError(
        "cannot import name 'request_ctx' from 'mcp.server.lowlevel.server'"
    )
    hs.importlib.import_module = lambda name, package=None: (_ for _ in ()).throw(exc)
    try:
        available, reason = _check_local_runtime()
        assert not available
        assert reason is not None
        assert "fastmcp" in reason.lower()
        assert "mcp 1.x" in reason
        assert "mcp 2.0.0" in reason
        assert "local_embedded mode cannot run in the same venv" in reason
    finally:
        # Restore original importlib.
        import importlib

        hs.importlib = importlib


def test_hint_for_missing_hindsight_all():
    hint = _local_runtime_hint("No module named 'hindsight'")
    assert "hindsight-all" in hint
    assert "hermes memory setup" in hint
    assert sys.executable in hint


def test_hint_for_missing_hindsight_embed():
    hint = _local_runtime_hint("No module named 'hindsight_embed.daemon_embed_manager'")
    assert "hindsight-all" in hint


def test_no_hint_for_unrelated_runtime_error():
    # e.g. the NumPy-on-old-CPU failure _check_local_runtime also guards against
    assert _local_runtime_hint("Illegal instruction (NumPy SIMD)") == ""
    assert _local_runtime_hint(None) == ""


# unavailable_reason() — surfaces the hint through the reachable path (#7718):
# is_available() gates initialize() out, so the hint must come from here.


def test_unavailable_reason_surfaces_hint_for_local_embedded(monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {"mode": "local_embedded"})
    monkeypatch.setattr(
        hs, "_check_local_runtime", lambda: (False, "No module named 'hindsight'")
    )
    reason = HindsightMemoryProvider().unavailable_reason()
    assert "hindsight-all" in reason
    assert reason == reason.strip()  # no leading/trailing whitespace


def test_unavailable_reason_empty_for_cloud(monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {"mode": "cloud"})
    # Should not even probe the runtime for a cloud provider.
    monkeypatch.setattr(
        hs,
        "_check_local_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("probed")),
    )
    assert HindsightMemoryProvider().unavailable_reason() == ""


def test_unavailable_reason_empty_when_runtime_present(monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {"mode": "local_embedded"})
    monkeypatch.setattr(hs, "_check_local_runtime", lambda: (True, None))
    assert HindsightMemoryProvider().unavailable_reason() == ""
