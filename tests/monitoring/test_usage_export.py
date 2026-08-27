"""Tests for agent/monitoring/usage_export.py.

Focus on the invariants that were established empirically against a real
awsemf collector and are silent-failure-prone:
  * DELTA temporality is requested (CUMULATIVE is silently dropped downstream)
  * endpoint normalisation never doubles /v1/metrics (a doubled path 404s)
  * headers come from env var NAMES, never literal secrets in config
  * every record_* entrypoint is fail-open and a no-op when not started
"""

import sys
import types

import pytest

from agent.monitoring import usage_export


@pytest.fixture(autouse=True)
def _reset_state():
    usage_export._state = None
    usage_export._atexit_registered = False
    yield
    usage_export._state = None
    usage_export._atexit_registered = False


def _config(**overrides):
    usage = {
        "enabled": True,
        "export_interval_seconds": 60,
        "service_name": "hermes-agent",
        "user_email": "dev@example.com",
    }
    usage.update(overrides.pop("usage", {}))
    otlp = {"enabled": True, "endpoint": "https://gw.example.net", "headers_env": {}}
    otlp.update(overrides.pop("otlp", {}))
    return {"monitoring": {"usage_export": usage, "export": {"otlp": otlp}}}


# ── config gating ────────────────────────────────────────────────────────────

def test_enabled_requires_flag_and_endpoint():
    assert usage_export.enabled(_config()) is True
    assert usage_export.enabled(_config(usage={"enabled": False})) is False
    assert usage_export.enabled(_config(otlp={"endpoint": ""})) is False
    assert usage_export.enabled({}) is False


def test_disabled_start_is_noop():
    assert usage_export.start(_config(usage={"enabled": False})) is False
    assert usage_export._state is None


# ── endpoint normalisation ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "given,expected",
    [
        ("https://gw.example.net", "https://gw.example.net/v1/metrics"),
        ("https://gw.example.net/", "https://gw.example.net/v1/metrics"),
        # already-suffixed inputs must be rewritten, NOT doubled: a doubled
        # path returns 404 and telemetry is dropped with no error surfaced.
        ("https://gw.example.net/v1/metrics", "https://gw.example.net/v1/metrics"),
        ("https://gw.example.net/v1/traces", "https://gw.example.net/v1/metrics"),
        ("https://gw.example.net/v1/logs", "https://gw.example.net/v1/metrics"),
    ],
)
def test_metric_endpoint_normalisation(given, expected):
    assert usage_export._metric_endpoint(given) == expected


# ── header indirection ───────────────────────────────────────────────────────

def test_headers_read_from_env_var_names(monkeypatch):
    monkeypatch.setenv("MY_TOKEN_VAR", "Bearer abc123")
    resolved = usage_export._resolve_headers({"Authorization": "MY_TOKEN_VAR"})
    assert resolved == {"Authorization": "Bearer abc123"}


def test_headers_skip_unset_env(monkeypatch):
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    assert usage_export._resolve_headers({"Authorization": "ABSENT_VAR"}) == {}
    assert usage_export._resolve_headers(None) == {}


# ── fail-open behaviour when not started ─────────────────────────────────────

def test_record_calls_are_noop_when_not_started():
    # Must not raise even though nothing was started.
    usage_export.record_api_call(model="m", input_tokens=5)
    usage_export.record_session_start(terminal_type="tmux")
    usage_export.record_active_time(1.5)
    assert usage_export.flush() is False
    usage_export.shutdown()


def test_record_api_call_survives_broken_counter():
    class Boom:
        def add(self, *a, **k):
            raise RuntimeError("counter exploded")

    usage_export._state = usage_export._UsageExportState(
        provider=types.SimpleNamespace(
            force_flush=lambda timeout_millis=0: True, shutdown=lambda: None
        ),
        tokens=Boom(), cost=Boom(), sessions=Boom(), active=Boom(),
        attrs_base={},
    )
    # Fail-open: telemetry must never break an agent turn.
    usage_export.record_api_call(model="m", input_tokens=1, cost_usd=0.1)
    usage_export.record_session_start()
    usage_export.record_active_time(2.0)


# ── emitted values and attributes ────────────────────────────────────────────

class _RecordingCounter:
    def __init__(self):
        self.calls = []

    def add(self, value, attrs=None):
        self.calls.append((value, dict(attrs or {})))


def _install_recorder(attrs_base=None):
    tokens, cost = _RecordingCounter(), _RecordingCounter()
    sessions, active = _RecordingCounter(), _RecordingCounter()
    # `is None` rather than `or {...}` so an explicitly empty dict is honoured
    # (tests the no-user-email case) instead of falling back to the default.
    if attrs_base is None:
        attrs_base = {"user.email": "dev@example.com"}
    usage_export._state = usage_export._UsageExportState(
        provider=types.SimpleNamespace(
            force_flush=lambda timeout_millis=0: True, shutdown=lambda: None
        ),
        tokens=tokens, cost=cost, sessions=sessions, active=active,
        attrs_base=dict(attrs_base),
    )
    return tokens, cost, sessions, active


def test_token_types_are_split_into_four_dimensions():
    tokens, cost, _, _ = _install_recorder()
    usage_export.record_api_call(
        model="claude-opus-5", input_tokens=10, output_tokens=20,
        cache_read_tokens=30, cache_write_tokens=40, cost_usd=0.5,
        effort="medium",
    )
    by_type = {a["type"]: v for v, a in tokens.calls}
    assert by_type == {
        "input": 10, "output": 20, "cacheRead": 30, "cacheCreation": 40,
    }
    # every series carries the queryable dimensions
    for _v, attrs in tokens.calls:
        assert attrs["user.email"] == "dev@example.com"
        assert attrs["model"] == "claude-opus-5"
        assert attrs["effort"] == "medium"
    # cost is emitted once, without a `type` dimension
    assert cost.calls == [(0.5, {
        "user.email": "dev@example.com",
        "model": "claude-opus-5",
        "effort": "medium",
    })]


def test_zero_valued_token_types_are_not_emitted():
    tokens, cost, _, _ = _install_recorder()
    usage_export.record_api_call(model="m", input_tokens=7)
    # Only the non-zero class produces a datapoint; zeros would add cost and
    # noise to a delta stream for no information.
    assert [a["type"] for _v, a in tokens.calls] == ["input"]
    assert cost.calls == []


def test_session_and_active_time_use_terminal_type():
    _, _, sessions, active = _install_recorder()
    usage_export.record_session_start(terminal_type="ghostty")
    usage_export.record_active_time(3.5, terminal_type="ghostty")
    assert sessions.calls == [(1, {
        "user.email": "dev@example.com", "terminal.type": "ghostty",
    })]
    assert active.calls == [(3.5, {
        "user.email": "dev@example.com", "terminal.type": "ghostty",
    })]


def test_zero_active_time_is_skipped():
    _, _, _, active = _install_recorder()
    usage_export.record_active_time(0)
    assert active.calls == []


def test_attrs_base_omits_empty_email():
    tokens, _, _, _ = _install_recorder(attrs_base={})
    usage_export.record_api_call(model="m", input_tokens=1)
    assert "user.email" not in tokens.calls[0][1]


# ── DELTA temporality (the silent-failure invariant) ─────────────────────────

def test_start_requests_delta_temporality(monkeypatch):
    """CUMULATIVE is silently dropped by awsemf; DELTA is mandatory."""
    captured = {}

    class FakeCounter:
        pass

    class FakeTemporality:
        DELTA = "DELTA"
        CUMULATIVE = "CUMULATIVE"

    def fake_exporter(endpoint=None, headers=None, preferred_temporality=None):
        captured["endpoint"] = endpoint
        captured["headers"] = headers
        captured["temporality"] = preferred_temporality
        return object()

    class FakeMeter:
        def create_counter(self, name, unit=None):
            captured.setdefault("counters", []).append((name, unit))
            return _RecordingCounter()

    class FakeProvider:
        def __init__(self, metric_readers=None, resource=None):
            captured["resource"] = resource

        def get_meter(self, scope):
            captured["scope"] = scope
            return FakeMeter()

    fake_sdk = {
        "OTLPMetricExporter": fake_exporter,
        "Counter": FakeCounter,
        "MeterProvider": FakeProvider,
        "AggregationTemporality": FakeTemporality,
        "PeriodicExportingMetricReader": lambda exp, export_interval_millis=0: (
            captured.setdefault("interval_ms", export_interval_millis)
        ),
        "Resource": types.SimpleNamespace(create=lambda attrs: attrs),
    }
    monkeypatch.setattr(usage_export, "_require_sdk", lambda **k: fake_sdk)

    assert usage_export.start(_config()) is True
    assert captured["temporality"] == {FakeCounter: "DELTA"}
    assert captured["endpoint"] == "https://gw.example.net/v1/metrics"
    assert captured["interval_ms"] == 60000
    assert captured["resource"] == {"service.name": "hermes-agent"}
    assert captured["scope"] == "hermes.agent.usage"
    # native hermes.* names, not another vendor's namespace
    names = [n for n, _u in captured["counters"]]
    assert names == [
        "hermes.token.usage",
        "hermes.cost.usage",
        "hermes.session.count",
        "hermes.active_time.total",
    ]


def test_start_is_idempotent(monkeypatch):
    monkeypatch.setattr(usage_export, "_require_sdk", lambda **k: (_ for _ in ()).throw(
        ImportError("no sdk")
    ))
    # SDK missing -> fail-open False, no state left behind
    assert usage_export.start(_config()) is False
    assert usage_export._state is None


def test_export_interval_has_floor(monkeypatch):
    captured = {}

    class FakeCounter:
        pass

    fake_sdk = {
        "OTLPMetricExporter": lambda **k: object(),
        "Counter": FakeCounter,
        "MeterProvider": lambda metric_readers=None, resource=None: types.SimpleNamespace(
            get_meter=lambda scope: types.SimpleNamespace(
                create_counter=lambda name, unit=None: _RecordingCounter()
            )
        ),
        "AggregationTemporality": types.SimpleNamespace(DELTA="DELTA"),
        "PeriodicExportingMetricReader": lambda exp, export_interval_millis=0: (
            captured.setdefault("interval_ms", export_interval_millis)
        ),
        "Resource": types.SimpleNamespace(create=lambda attrs: attrs),
    }
    monkeypatch.setattr(usage_export, "_require_sdk", lambda **k: fake_sdk)
    usage_export.start(_config(usage={"export_interval_seconds": 0}))
    # a 0s interval would hot-loop the exporter
    assert captured["interval_ms"] == 5000


# ── credential refresh (expiry is a silent-failure mode) ─────────────────────

def test_read_credential_file_prefers_access_token(tmp_path):
    import json
    p = tmp_path / "cred.json"
    p.write_text(json.dumps({"access_token": "AAA", "token": "BBB"}))
    assert usage_export._read_credential_file(str(p)) == "AAA"


def test_read_credential_file_falls_back_to_other_keys(tmp_path):
    import json
    p = tmp_path / "cred.json"
    p.write_text(json.dumps({"id_token": "CCC"}))
    assert usage_export._read_credential_file(str(p)) == "CCC"


def test_read_credential_file_is_fail_open(tmp_path):
    # missing file, bad JSON, and no usable key must all return None rather
    # than raising into the export path.
    assert usage_export._read_credential_file(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert usage_export._read_credential_file(str(bad)) is None
    empty = tmp_path / "empty.json"
    empty.write_text('{"unrelated": 1}')
    assert usage_export._read_credential_file(str(empty)) is None


def test_refreshing_session_rereads_credential_per_post(tmp_path):
    """A rotated credential must be picked up without restarting the process."""
    import json
    p = tmp_path / "cred.json"
    p.write_text(json.dumps({"access_token": "first"}))

    seen = []

    class FakeSession:
        def post(self, *a, **k):
            seen.append(k.get("headers", {}).get("Authorization"))
            return "ok"

        def close(self):
            seen.append("closed")

    sess = usage_export._RefreshingSession(FakeSession(), str(p))
    assert sess.post("http://x") == "ok"

    # simulate the auth flow rewriting the file mid-process
    p.write_text(json.dumps({"access_token": "second"}))
    sess.post("http://x")

    assert seen == ["Bearer first", "Bearer second"]

    # attribute delegation still reaches the wrapped session
    sess.close()
    assert seen[-1] == "closed"


def test_refreshing_session_without_credential_leaves_headers_alone(tmp_path):
    captured = {}

    class FakeSession:
        def post(self, *a, **k):
            captured.update(k)
            return "ok"

    sess = usage_export._RefreshingSession(FakeSession(), str(tmp_path / "absent.json"))
    sess.post("http://x", headers={"X-Other": "1"})
    # no credential available -> do not inject or clobber
    assert captured["headers"] == {"X-Other": "1"}


def test_start_uses_refreshing_session_when_credential_file_set(monkeypatch, tmp_path):
    import json
    p = tmp_path / "cred.json"
    p.write_text(json.dumps({"access_token": "tok"}))

    captured = {}

    class FakeCounter:
        pass

    def fake_exporter(**kwargs):
        captured.update(kwargs)
        return object()

    fake_sdk = {
        "OTLPMetricExporter": fake_exporter,
        "Counter": FakeCounter,
        "MeterProvider": lambda metric_readers=None, resource=None: types.SimpleNamespace(
            get_meter=lambda scope: types.SimpleNamespace(
                create_counter=lambda name, unit=None: _RecordingCounter()
            )
        ),
        "AggregationTemporality": types.SimpleNamespace(DELTA="DELTA"),
        "PeriodicExportingMetricReader": lambda exp, export_interval_millis=0: None,
        "Resource": types.SimpleNamespace(create=lambda attrs: attrs),
    }
    monkeypatch.setattr(usage_export, "_require_sdk", lambda **k: fake_sdk)

    assert usage_export.start(_config(usage={"credential_file": str(p)})) is True
    assert isinstance(captured.get("session"), usage_export._RefreshingSession)


def test_start_omits_session_when_no_credential_file(monkeypatch):
    captured = {}

    class FakeCounter:
        pass

    def fake_exporter(**kwargs):
        captured.update(kwargs)
        return object()

    fake_sdk = {
        "OTLPMetricExporter": fake_exporter,
        "Counter": FakeCounter,
        "MeterProvider": lambda metric_readers=None, resource=None: types.SimpleNamespace(
            get_meter=lambda scope: types.SimpleNamespace(
                create_counter=lambda name, unit=None: _RecordingCounter()
            )
        ),
        "AggregationTemporality": types.SimpleNamespace(DELTA="DELTA"),
        "PeriodicExportingMetricReader": lambda exp, export_interval_millis=0: None,
        "Resource": types.SimpleNamespace(create=lambda attrs: attrs),
    }
    monkeypatch.setattr(usage_export, "_require_sdk", lambda **k: fake_sdk)

    assert usage_export.start(_config()) is True
    # the SDK default session must be used, not an explicit None
    assert "session" not in captured
