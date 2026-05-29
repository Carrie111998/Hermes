import json
from types import SimpleNamespace
import pytest


# ---- Fake stream plumbing (mimics openai responses.stream context manager) ----
class _FakeStream:
    def __init__(self, completed_output, raise_on_final=False):
        self._output = completed_output
        self._raise = raise_on_final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield SimpleNamespace(type="response.output_item.done",
                              item=SimpleNamespace(type="message"))
        yield SimpleNamespace(type="response.completed",
                              response=SimpleNamespace(output=self._output))

    def get_final_response(self):
        if self._raise:
            raise TypeError("'NoneType' object is not iterable")
        return SimpleNamespace(output=self._output)


class _FakeResponses:
    def __init__(self, completed_output, raise_on_final=False):
        self._o = completed_output
        self._r = raise_on_final

    def stream(self, **kw):
        return _FakeStream(self._o, self._r)


class _FakeCodexClient:
    def __init__(self, completed_output, raise_on_final=False):
        self.responses = _FakeResponses(completed_output, raise_on_final)


def test_codex_healthy_when_output_is_list():
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(_FakeCodexClient([SimpleNamespace(type="message")]), "gpt-5.5")
    assert res.healthy is True


def test_codex_drift_when_output_none_via_final():
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(_FakeCodexClient(None), "gpt-5.5")
    assert res.healthy is False
    assert "output" in res.detail.lower()


def test_codex_drift_when_stock_parser_raises():
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(_FakeCodexClient(None, raise_on_final=True), "gpt-5.5")
    assert res.healthy is False


def test_network_error_is_inconclusive_not_drift():
    from obs.backend_conformance_canary import check_codex_conformance

    class _Boom:
        class responses:
            @staticmethod
            def stream(**kw):
                raise ConnectionError("dns fail")

    res = check_codex_conformance(_Boom(), "gpt-5.5")
    assert res.healthy is None  # inconclusive, NOT down


def test_ensure_stock_parser_removes_guard():
    import openai.lib._parsing._responses as r
    from agent.openai_codex_compat import apply_codex_output_none_guard
    from obs.backend_conformance_canary import _ensure_stock_parser
    apply_codex_output_none_guard(force=True)
    assert getattr(r.parse_response, "_hermes_codex_output_none_guard", False) is True
    _ensure_stock_parser()
    assert getattr(r.parse_response, "_hermes_codex_output_none_guard", False) is False


def test_edge_trigger_emits_once_for_consecutive_drift(tmp_path, monkeypatch):
    import obs.backend_conformance_canary as canary

    emitted = []

    class _Bus:
        def emit(self, *, event_type, source, payload, priority=None, **kw):
            emitted.append((payload.get("backend"), payload.get("state")))

    state_file = tmp_path / "backend_conformance.json"
    monkeypatch.setattr(canary, "_sentinel_path", lambda: state_file)

    drift = canary.ProbeResult(healthy=False, detail="output is None")
    bus = _Bus()

    # First drift -> emit "down".
    meta = dict(canary._load_state().get("emit_meta", {}))
    canary._maybe_emit(bus, "codex", drift, canary._load_state(), meta)
    canary._write_sentinel({"codex": drift}, emit_meta=meta)

    # Second consecutive drift (sentinel now records codex=down) -> NO new emit.
    meta = dict(canary._load_state().get("emit_meta", {}))
    canary._maybe_emit(bus, "codex", drift, canary._load_state(), meta)

    assert [e for e in emitted if e[1] == "down"] == [("codex", "down")]


def test_dual_backend_drift_preserves_each_emit_meta(tmp_path, monkeypatch):
    # Regression: when BOTH backends change emit-state in one cycle, neither may
    # revert the other's last_drift_emit (rate-cap integrity).
    import json as _json
    import obs.backend_conformance_canary as canary

    class _Bus:
        def __init__(self):
            self.calls = []

        def emit(self, *, event_type, source, payload, priority=None, **kw):
            self.calls.append((payload.get("backend"), payload.get("state")))

    state_file = tmp_path / "backend_conformance.json"
    monkeypatch.setattr(canary, "_sentinel_path", lambda: state_file)

    old = "2000-01-01T00:00:00+00:00"  # >1h ago -> codex re-pages this cycle
    state_file.write_text(_json.dumps({
        "ts": old,
        "backends": {"codex": {"state": "down", "detail": "x"},
                     "anthropic": {"state": "healthy", "detail": "ok"}},
        "emit_meta": {"codex": {"last_drift_emit": old}},
    }), encoding="utf-8")

    bus = _Bus()
    drift = canary.ProbeResult(healthy=False, detail="drift")
    prev = canary._load_state()
    emit_meta = dict(prev.get("emit_meta", {}))
    # codex re-pages (was down, old emit); anthropic newly drifts -- SHARED dict.
    canary._maybe_emit(bus, "codex", drift, prev, emit_meta)
    canary._maybe_emit(bus, "anthropic", drift, prev, emit_meta)

    # codex's re-page timestamp must NOT be reverted to `old`; anthropic recorded.
    assert emit_meta["codex"]["last_drift_emit"] != old
    assert emit_meta.get("anthropic", {}).get("last_drift_emit")
    assert ("codex", "down") in bus.calls and ("anthropic", "down") in bus.calls
