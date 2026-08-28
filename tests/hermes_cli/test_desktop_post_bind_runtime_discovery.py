import asyncio
import sys
import threading
import types
from argparse import Namespace

from hermes_cli import main as main_mod
from hermes_cli import web_server


def _serve_args(**overrides):
    values = {
        "headless_backend": True,
        "host": "127.0.0.1",
    }
    values.update(overrides)
    return Namespace(**values)


def test_runtime_discovery_defers_only_for_local_desktop_headless_serve():
    desktop_env = {"HERMES_DESKTOP": "1"}

    assert main_mod._desktop_runtime_discovery_can_follow_bind(
        _serve_args(),
        dashboard_public_url="",
        environ=desktop_env,
    )
    assert not main_mod._desktop_runtime_discovery_can_follow_bind(
        _serve_args(headless_backend=False),
        dashboard_public_url="",
        environ=desktop_env,
    )
    assert not main_mod._desktop_runtime_discovery_can_follow_bind(
        _serve_args(host="0.0.0.0"),
        dashboard_public_url="",
        environ=desktop_env,
    )
    assert not main_mod._desktop_runtime_discovery_can_follow_bind(
        _serve_args(),
        dashboard_public_url="https://example.test/hermes",
        environ=desktop_env,
    )
    assert not main_mod._desktop_runtime_discovery_can_follow_bind(
        _serve_args(),
        dashboard_public_url="",
        environ={},
    )


def test_deferred_plugin_request_waits_instead_of_falling_through(monkeypatch):
    waits = []

    class Pending:
        def is_set(self):
            return False

        def wait(self, timeout):
            waits.append(timeout)
            return False

    monkeypatch.setattr(web_server, "_DEFER_DESKTOP_PLUGIN_API_MOUNT", True)
    monkeypatch.setattr(web_server, "_deferred_plugin_api_routes_ready", Pending())
    request = types.SimpleNamespace(
        url=types.SimpleNamespace(path="/api/plugins/example/status")
    )

    async def call_next(_request):
        raise AssertionError("request must not reach routing before mount completes")

    response = asyncio.run(
        web_server._wait_for_deferred_plugin_api_routes(request, call_next)
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert waits == [10.0]


def test_unrelated_request_never_waits_for_plugin_routes(monkeypatch):
    class Pending:
        def is_set(self):
            return False

        def wait(self, _timeout):
            raise AssertionError("unrelated requests must stay on the fast path")

    monkeypatch.setattr(web_server, "_DEFER_DESKTOP_PLUGIN_API_MOUNT", True)
    monkeypatch.setattr(web_server, "_deferred_plugin_api_routes_ready", Pending())
    request = types.SimpleNamespace(url=types.SimpleNamespace(path="/api/status"))
    expected = object()

    async def call_next(_request):
        return expected

    assert (
        asyncio.run(
            web_server._wait_for_deferred_plugin_api_routes(request, call_next)
        )
        is expected
    )


def test_deferred_discovery_mounts_routes_then_starts_mcp(monkeypatch):
    calls = []
    ready = threading.Event()
    monkeypatch.setattr(web_server, "_DEFER_DESKTOP_PLUGIN_API_MOUNT", True)
    monkeypatch.setattr(web_server, "_deferred_plugin_api_routes_ready", ready)
    monkeypatch.setattr(
        web_server,
        "_mount_plugin_api_routes",
        lambda: calls.append("mount"),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: calls.append("plugins")),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.mcp_startup",
        types.SimpleNamespace(
            start_background_mcp_discovery=lambda **_kwargs: calls.append("mcp")
        ),
    )

    web_server._run_deferred_runtime_discovery()

    assert calls == ["plugins", "mount", "mcp"]
    assert ready.is_set()


def test_deferred_route_waiters_release_when_plugin_mount_fails(monkeypatch):
    ready = threading.Event()
    monkeypatch.setattr(web_server, "_DEFER_DESKTOP_PLUGIN_API_MOUNT", True)
    monkeypatch.setattr(web_server, "_deferred_plugin_api_routes_ready", ready)
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.mcp_startup",
        types.SimpleNamespace(start_background_mcp_discovery=lambda **_kwargs: None),
    )
    monkeypatch.setattr(
        web_server,
        "_mount_plugin_api_routes",
        lambda: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )

    web_server._run_deferred_runtime_discovery()

    assert ready.is_set()


def test_deferred_discovery_timer_is_daemonized_and_started(monkeypatch):
    created = []

    class FakeTimer:
        def __init__(self, delay, target):
            self.delay = delay
            self.target = target
            self.daemon = False
            self.name = ""
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(web_server.threading, "Timer", FakeTimer)

    timer = web_server._schedule_deferred_runtime_discovery(0.25)

    assert timer is created[0]
    assert timer.delay == 0.25
    assert timer.target is web_server._run_deferred_runtime_discovery
    assert timer.daemon is True
    assert timer.name == "desktop-runtime-discovery-delay"
    assert timer.started is True


def test_desktop_cron_and_orphan_reap_wait_for_bound_socket(monkeypatch):
    threads = []
    calls = []

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False
            threads.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(web_server.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        web_server,
        "_start_desktop_cron_ticker",
        lambda stop: calls.append(("cron", stop)),
    )
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.gateway",
        types.SimpleNamespace(
            _reap_unsupervised_gateway_orphans=lambda: calls.append(("reap", None))
        ),
    )
    application = types.SimpleNamespace(state=types.SimpleNamespace())

    stop = web_server._start_desktop_services_after_bind(application)

    assert calls == []
    assert application.state.desktop_post_bind.is_set() is False
    assert {thread.name for thread in threads} == {
        "desktop-gateway-orphan-reaper",
        "desktop-cron-ticker",
    }
    assert all(thread.daemon and thread.started for thread in threads)

    application.state.desktop_post_bind.set()
    for thread in threads:
        thread.target()

    assert calls == [("reap", None), ("cron", stop)]
