"""Tests for the kmdaily on-demand trigger slash-command plugin."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _load_plugin_init():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "kmdaily"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.kmdaily",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.kmdaily"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.kmdaily"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def _mock_config(monkeypatch, config_block):
    fake_config_mod = types.SimpleNamespace(
        load_config=lambda: {"kmdaily": config_block}
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config_mod)


def _mock_missing_config(monkeypatch):
    fake_config_mod = types.SimpleNamespace(load_config=lambda: {})
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config_mod)


def _install_transport(monkeypatch, plugin_init, handler):
    """Route the plugin's normal AsyncClient through an explicit MockTransport."""
    transport = httpx.MockTransport(handler)
    original_client = plugin_init.httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(plugin_init.httpx, "AsyncClient", make_client)


class TestKmdailyConfig:
    def test_missing_config_returns_not_configured(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_missing_config(monkeypatch)

        result = _run(mod._handle_kmdaily(""))

        assert "not configured" in result

    def test_disabled_config_returns_not_configured(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": False, "base_url": "http://kmdaily"})

        result = _run(mod._handle_kmdaily(""))

        assert "not configured" in result

    def test_missing_base_url_returns_not_configured(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True})

        result = _run(mod._handle_kmdaily(""))

        assert "not configured" in result


class TestKmdailyUsage:
    def test_unrecognized_argument_returns_usage(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})

        result = _run(mod._handle_kmdaily("run_cycle"))

        assert "Usage: /kmdaily" in result

    def test_run_cycle_is_never_reachable_as_an_action(self, monkeypatch):
        # Guard rail: on-demand triggers must never default to the combined
        # cycle action for convenience, per T0051-spec Guard rail 6.
        mod = _load_plugin_init()
        assert "run_cycle" not in mod._ACTION_MAP.values()


class TestKmdailyTrigger:
    def test_default_argument_triggers_ingest_with_idempotency_key(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(
            monkeypatch,
            {
                "enabled": True,
                "base_url": "http://kmdaily",
                "key_file": None,
            },
        )
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                202,
                json={"run_id": "run_abc", "action": "ingest", "status": "running"},
            )

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_kmdaily(""))

        assert len(requests) == 2  # POST /runs + one follow-up GET
        post = requests[0]
        assert post.method == "POST"
        assert post.url.path == "/api/v1/runs"
        body = post.content.decode()
        assert '"action":"ingest"' in body
        assert '"dry_run":false' in body
        key = post.headers["Idempotency-Key"]
        assert key.startswith("hermes-telegram-kmdaily-ingest-")
        assert "Authorization" not in post.headers
        assert "run_id=run_abc" in result
        assert "action=ingest" in result

    def test_digest_argument_maps_to_send_digest_action(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                202, json={"run_id": "run_x", "status": "running"}
            )

        _install_transport(monkeypatch, mod, handler)
        _run(mod._handle_kmdaily("digest"))

        assert '"action":"send_digest"' in requests[0].content.decode()
        assert "hermes-telegram-kmdaily-send_digest-" in requests[0].headers["Idempotency-Key"]

    def test_notion_argument_maps_to_sync_notion_action(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(202, json={"run_id": "run_y", "status": "running"})

        _install_transport(monkeypatch, mod, handler)
        _run(mod._handle_kmdaily("notion"))

        assert '"action":"sync_notion"' in requests[0].content.decode()

    def test_key_file_is_read_and_sent_as_bearer_token(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        key_file = tmp_path / "kmdaily-api-token"
        key_file.write_text("  secret-token-value  \n")
        _mock_config(
            monkeypatch,
            {"enabled": True, "base_url": "http://kmdaily", "key_file": str(key_file)},
        )
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(202, json={"run_id": "run_z", "status": "running"})

        _install_transport(monkeypatch, mod, handler)
        _run(mod._handle_kmdaily(""))

        assert requests[0].headers["Authorization"] == "Bearer secret-token-value"

    def test_unreadable_key_file_returns_error_without_calling_service(
        self, monkeypatch, tmp_path
    ):
        mod = _load_plugin_init()
        missing = tmp_path / "does-not-exist"
        _mock_config(
            monkeypatch,
            {"enabled": True, "base_url": "http://kmdaily", "key_file": str(missing)},
        )
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(202, json={"run_id": "run_z", "status": "running"})

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_kmdaily(""))

        assert "could not read the configured API key file" in result
        assert len(requests) == 0

    def test_401_returns_unauthorized_message(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})

        def handler(request):
            return httpx.Response(401, json={"detail": {"code": "unauthorized"}})

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_kmdaily(""))

        assert "unauthorized" in result

    def test_409_reports_existing_run_id_without_creating_a_new_run(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})

        def handler(request):
            return httpx.Response(
                409,
                json={"detail": {"code": "run_in_progress", "run_id": "run_existing"}},
            )

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_kmdaily(""))

        assert "already in progress" in result
        assert "run_existing" in result

    def test_timeout_returns_static_message(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})

        def handler(request):
            raise httpx.TimeoutException("timed out")

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_kmdaily(""))

        assert "timed out" in result

    def test_follow_up_poll_updates_reported_status(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, {"enabled": True, "base_url": "http://kmdaily"})
        calls = []

        def handler(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(
                    202, json={"run_id": "run_poll", "status": "running"}
                )
            return httpx.Response(200, json={"run_id": "run_poll", "status": "completed"})

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_kmdaily(""))

        assert calls[1].method == "GET"
        assert calls[1].url.path == "/api/v1/runs/run_poll"
        assert "status=completed" in result
