"""The CLI/agent-init path must start the usage exporter, not just the gateway.

Regression test for a real miss: start() was wired only into gateway boot, so
every bare-CLI session exported nothing — record_api_call() returned at the
uninitialised-state guard and no error was logged anywhere.
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


def test_agent_init_contains_start_call():
    """agent_init must call usage_export.start(), not only record_session_start()."""
    import inspect
    from agent import agent_init

    src = inspect.getsource(agent_init)
    assert "usage_export.start(" in src, (
        "agent_init must start the exporter; a gateway-only start silently "
        "disables export for every CLI session"
    )
    assert "atexit.register(usage_export.shutdown)" in src, (
        "a CLI process has no gateway shutdown hook, so the final export "
        "interval would be lost without an atexit drain"
    )


def test_gateway_run_still_starts_exporter():
    """The gateway path must keep its own start() — both entrypoints matter."""
    path = "gateway/run.py"
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(usage_export.__file__))))
    with open(os.path.join(root, path)) as fh:
        src = fh.read()
    assert "usage_export.start(load_config())" in src
    assert "usage_export.shutdown()" in src


def test_start_is_idempotent_across_sessions(monkeypatch):
    """Many sessions in one process must not build many providers."""
    builds = []

    class FakeCounter:
        pass

    class FakeRecorder:
        def add(self, *a, **k):
            pass

    def fake_exporter(**kwargs):
        builds.append(kwargs)
        return object()

    fake_sdk = {
        "OTLPMetricExporter": fake_exporter,
        "Counter": FakeCounter,
        "MeterProvider": lambda metric_readers=None, resource=None: types.SimpleNamespace(
            get_meter=lambda scope: types.SimpleNamespace(
                create_counter=lambda name, unit=None: FakeRecorder()
            )
        ),
        "AggregationTemporality": types.SimpleNamespace(DELTA="DELTA"),
        "PeriodicExportingMetricReader": lambda exp, export_interval_millis=0: None,
        "Resource": types.SimpleNamespace(create=lambda attrs: attrs),
    }
    monkeypatch.setattr(usage_export, "_require_sdk", lambda **k: fake_sdk)

    cfg = {
        "monitoring": {
            "usage_export": {"enabled": True, "user_email": "dev@example.com"},
            "export": {"otlp": {"enabled": True, "endpoint": "https://gw.example.net"}},
        }
    }
    assert usage_export.start(cfg) is True
    assert usage_export.start(cfg) is True
    assert usage_export.start(cfg) is True
    # exporter constructed exactly once despite three session starts
    assert len(builds) == 1
