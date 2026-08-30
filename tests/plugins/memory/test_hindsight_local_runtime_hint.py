"""Actionable local_embedded runtime checks for the MCP-2-safe package split."""

import sys

import plugins.memory.hindsight as hs
from plugins.memory.hindsight import HindsightMemoryProvider, _local_runtime_hint


def test_hint_for_missing_hindsight_embed():
    hint = _local_runtime_hint("No module named 'hindsight_embed'")
    assert "hindsight-embed" in hint
    assert "hindsight-all" not in hint
    assert "hermes memory setup" in hint
    assert sys.executable in hint


def test_hint_for_missing_hindsight_client():
    hint = _local_runtime_hint("No module named 'hindsight_client'")
    assert "hindsight-client" in hint
    assert "hindsight-all" not in hint


def test_local_runtime_probe_avoids_full_server_stack(monkeypatch):
    calls = []

    def fake_import(name):
        calls.append(name)
        if name == "hindsight_client":
            return type("ClientModule", (), {"Hindsight": object})
        return object()

    monkeypatch.setattr(hs.importlib, "import_module", fake_import)

    assert hs._check_local_runtime() == (True, None)
    assert calls == ["hindsight_embed.daemon_embed_manager", "hindsight_client"]
    assert "hindsight" not in calls
    assert "sentence_transformers" not in calls


def test_no_hint_for_unrelated_runtime_error():
    # e.g. the NumPy-on-old-CPU failure _check_local_runtime also guards against
    assert _local_runtime_hint("Illegal instruction (NumPy SIMD)") == ""
    assert _local_runtime_hint(None) == ""


# unavailable_reason() — surfaces the hint through the reachable path (#7718):
# is_available() gates initialize() out, so the hint must come from here.


def test_unavailable_reason_surfaces_hint_for_local_embedded(monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {"mode": "local_embedded"})
    monkeypatch.setattr(hs, "_check_local_runtime", lambda: (False, "No module named 'hindsight_embed'"))
    reason = HindsightMemoryProvider().unavailable_reason()
    assert "hindsight-embed" in reason
    assert "hindsight-all" not in reason
    assert reason == reason.strip()  # no leading/trailing whitespace


def test_unavailable_reason_empty_for_cloud(monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {"mode": "cloud"})
    # Should not even probe the runtime for a cloud provider.
    monkeypatch.setattr(hs, "_check_local_runtime", lambda: (_ for _ in ()).throw(AssertionError("probed")))
    assert HindsightMemoryProvider().unavailable_reason() == ""


def test_unavailable_reason_empty_when_runtime_present(monkeypatch):
    monkeypatch.setattr(hs, "_load_config", lambda: {"mode": "local_embedded"})
    monkeypatch.setattr(hs, "_check_local_runtime", lambda: (True, None))
    assert HindsightMemoryProvider().unavailable_reason() == ""
