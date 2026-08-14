import json
from types import SimpleNamespace
import pytest


# ---- Fake stream plumbing (mimics openai responses.stream context manager) ----
# Re-calibrated canary (2026-05-30): the probe now asserts the backend DELIVERS
# CONTENT through the production output=None guard, not that the stock parser
# survives an output=None aggregate. So the fake stream can carry
# ``output_text.delta`` text and an optional message item, and its
# ``get_final_response`` mirrors the GUARDED path (returns the coerced output
# without raising) unless ``raise_on_final`` simulates a NEW unhandled shape.
class _FakeStream:
    def __init__(self, completed_output, raise_on_final=False, delta_text="",
                 message_item=True):
        self._output = completed_output
        self._raise = raise_on_final
        self._delta = delta_text
        self._message_item = message_item

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        if self._delta:
            yield SimpleNamespace(type="response.output_text.delta", delta=self._delta)
        if self._message_item:
            yield SimpleNamespace(type="response.output_item.done",
                                  item=SimpleNamespace(type="message"))
        yield SimpleNamespace(type="response.completed",
                              response=SimpleNamespace(output=self._output))

    def get_final_response(self):
        if self._raise:
            raise TypeError("'NoneType' object is not iterable")
        return SimpleNamespace(output=self._output, output_text=self._delta)


class _FakeResponses:
    def __init__(self, completed_output, raise_on_final=False, delta_text="",
                 message_item=True):
        self._o = completed_output
        self._r = raise_on_final
        self._d = delta_text
        self._m = message_item

    def stream(self, **kw):
        return _FakeStream(self._o, self._r, self._d, self._m)


class _FakeCodexClient:
    def __init__(self, completed_output, raise_on_final=False, delta_text="",
                 message_item=True):
        self.responses = _FakeResponses(completed_output, raise_on_final,
                                        delta_text, message_item)


def test_codex_healthy_when_content_delivered_via_deltas_despite_output_none():
    # THE re-calibration: backend streams the real text via output_text.delta but
    # the final aggregate is None (the now-permanent, guard-handled Codex shape).
    # Content arrived -> healthy, NOT drift.
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(
        _FakeCodexClient(None, delta_text="ok", message_item=True), "gpt-5.5")
    assert res.healthy is True
    assert "content" in res.detail.lower()


def test_codex_healthy_when_output_is_nonempty_list():
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(
        _FakeCodexClient([SimpleNamespace(type="message")], delta_text="ok"), "gpt-5.5")
    assert res.healthy is True


def test_codex_drift_when_no_content_at_all():
    # Round-tripped but produced nothing: output=None, zero deltas, zero items.
    # This is the genuinely-empty/blocked case the guard CANNOT backfill -> down.
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(
        _FakeCodexClient(None, delta_text="", message_item=False), "gpt-5.5")
    assert res.healthy is False
    assert "no content" in res.detail.lower()


def test_codex_drift_when_parser_raises_despite_guard():
    # A TypeError even with the guard applied = a NEW shape the guard can't
    # absorb -> drift (down), not inconclusive.
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(
        _FakeCodexClient(None, raise_on_final=True), "gpt-5.5")
    assert res.healthy is False


def test_codex_drift_when_output_not_a_list():
    from obs.backend_conformance_canary import check_codex_conformance
    res = check_codex_conformance(
        _FakeCodexClient("weird", delta_text="", message_item=False), "gpt-5.5")
    assert res.healthy is False
    assert "list" in res.detail.lower()


def test_network_error_is_inconclusive_not_drift():
    from obs.backend_conformance_canary import check_codex_conformance

    class _Boom:
        class responses:
            @staticmethod
            def stream(**kw):
                raise ConnectionError("dns fail")

    res = check_codex_conformance(_Boom(), "gpt-5.5")
    assert res.healthy is None  # inconclusive, NOT down


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


def test_recovered_emits_on_down_to_healthy(tmp_path, monkeypatch):
    # After re-calibration the canary should be ABLE to recover: a healthy probe
    # following a down state emits a 'recovered' event and clears emit_meta.
    import json as _json
    import obs.backend_conformance_canary as canary

    emitted = []

    class _Bus:
        def emit(self, *, event_type, source, payload, priority=None, **kw):
            emitted.append((payload.get("backend"), payload.get("state")))

    state_file = tmp_path / "backend_conformance.json"
    monkeypatch.setattr(canary, "_sentinel_path", lambda: state_file)
    state_file.write_text(_json.dumps({
        "ts": "2000-01-01T00:00:00+00:00",
        "backends": {"codex": {"state": "down", "detail": "x"}},
        "emit_meta": {"codex": {"last_drift_emit": "2000-01-01T00:00:00+00:00"}},
    }), encoding="utf-8")

    healthy = canary.ProbeResult(healthy=True, detail="Codex delivers content")
    prev = canary._load_state()
    meta = dict(prev.get("emit_meta", {}))
    canary._maybe_emit(_Bus(), "codex", healthy, prev, meta)
    assert ("codex", "recovered") in emitted
    assert meta.get("codex") == {}


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


# ---- Manifest "Laptop Monitor" harness arm (Diego, 2026-08-12) ----
# The anthropic arm now routes through the self-hosted Manifest harness
# (base_url /v1/responses, model "auto") instead of api.anthropic.com, which
# 429s under the background agent fleet. These tests exercise the new probe
# with a FAKE client (no openai import) so they stay fast and hermetic.


class _FakeHarnessResponses:
    def __init__(self, status="completed", model="gemini-3.1-flash-lite", text="pong"):
        self._status = status
        self._model = model
        self._text = text

    def create(self, **kw):
        msg = SimpleNamespace(type="message",
                              content=[SimpleNamespace(type="output_text", text=self._text)])
        return SimpleNamespace(status=self._status, model=self._model, output=[msg])


class _FakeHarnessClient:
    def __init__(self, status="completed", model="gemini-3.1-flash-lite", text="pong"):
        self.responses = _FakeHarnessResponses(status, model, text)


def test_manifest_harness_healthy_when_completed_with_text():
    from obs.backend_conformance_canary import check_manifest_harness_conformance
    res = check_manifest_harness_conformance(_FakeHarnessClient(), "auto")
    assert res.healthy is True
    assert "gemini-3.1-flash-lite" in res.detail


def test_manifest_harness_drift_when_completed_but_empty_text():
    from obs.backend_conformance_canary import check_manifest_harness_conformance
    res = check_manifest_harness_conformance(_FakeHarnessClient(text=""), "auto")
    assert res.healthy is False


def test_manifest_harness_drift_when_not_completed():
    from obs.backend_conformance_canary import check_manifest_harness_conformance
    res = check_manifest_harness_conformance(_FakeHarnessClient(status="failed"), "auto")
    assert res.healthy is False


def test_manifest_harness_inconclusive_on_network_error():
    from obs.backend_conformance_canary import check_manifest_harness_conformance

    class _Boom:
        class responses:
            @staticmethod
            def create(**kw):
                raise ConnectionError("dns fail")

    res = check_manifest_harness_conformance(_Boom(), "auto")
    assert res.healthy is None  # inconclusive, NOT down


def test_manifest_harness_client_build_requires_key(monkeypatch, tmp_path):
    import obs.backend_conformance_canary as canary
    monkeypatch.setattr(canary, "_MANIFEST_HARNESS_KEY_FILE", tmp_path / "no-such-key")
    assert canary.build_manifest_harness_probe_client() is None
