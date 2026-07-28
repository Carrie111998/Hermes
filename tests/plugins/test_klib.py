"""Tests for the klib slash-command plugin."""

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
    plugin_dir = repo_root / "plugins" / "klib"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.klib",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.klib"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.klib"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def _mock_config(monkeypatch, plugin_init, config_block):
    fake_config_mod = types.SimpleNamespace(
        load_config=lambda: {"klib": config_block}
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


class TestKlibCommands:
    def test_successful_query_formats_only_top_five_results(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"path": f"docs/{index}.md", "snippet": f"snippet {index}"}
                        for index in range(1, 8)
                    ]
                },
            )

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("cache invalidation"))

        assert len(requests) == 1
        assert requests[0].url.path == "/query"
        assert requests[0].url.params["q"] == "cache invalidation"
        assert requests[0].url.params["mode"] == "lexical"
        assert requests[0].url.params["limit"] == "5"
        assert "docs/1.md" in result
        assert "docs/5.md" in result
        assert "docs/6.md" not in result
        assert "top 5 of 7" in result
        assert len(result) <= 2800

    def test_successful_query_with_zero_results_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"results": []})

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("nothing here"))

        assert len(calls) == 1
        assert "no results" in result.lower()
        assert "error" not in result.lower()

    def test_missing_config_does_not_make_http_call(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_missing_config(monkeypatch)
        calls = []
        _install_transport(monkeypatch, mod, lambda request: calls.append(request))

        result = _run(mod._handle_klib("anything"))

        assert "not configured or disabled" in result
        assert calls == []

    def test_disabled_config_does_not_make_http_call(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": False, "base_url": "http://klib"})
        calls = []
        _install_transport(monkeypatch, mod, lambda request: calls.append(request))

        result = _run(mod._handle_klib("anything"))

        assert "not configured or disabled" in result
        assert calls == []

    def test_timeout_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})

        def handler(request):
            raise httpx.TimeoutException("simulated timeout", request=request)

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("slow query"))

        assert "timed out" in result

    def test_connection_failure_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})

        def handler(request):
            raise httpx.ConnectError("simulated connection failure", request=request)

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("unreachable query"))

        assert "could not reach" in result

    def test_non_2xx_response_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(500, text="server failure"),
        )

        result = _run(mod._handle_klib("server issue"))

        assert "500" in result

    @pytest.mark.parametrize("raw_args", ["", "   ", "\n\t"])
    def test_empty_query_returns_usage_without_http_call(self, monkeypatch, raw_args):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        calls = []
        _install_transport(monkeypatch, mod, lambda request: calls.append(request))

        result = _run(mod._handle_klib(raw_args))

        assert "Usage: /klib <query>" in result
        assert calls == []

    def test_key_file_read_failure_is_friendly(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        _mock_config(
            monkeypatch,
            mod,
            {
                "enabled": True,
                "base_url": "http://klib",
                "key_file": str(tmp_path / "missing-key"),
            },
        )
        calls = []
        _install_transport(monkeypatch, mod, lambda request: calls.append(request))

        result = _run(mod._handle_klib("anything"))

        assert "could not read" in result
        assert calls == []

    def test_key_file_is_sent_as_bearer_token(self, monkeypatch, tmp_path):
        mod = _load_plugin_init()
        key_file = tmp_path / "key"
        key_file.write_text("test-key\n", encoding="utf-8")
        _mock_config(
            monkeypatch,
            mod,
            {
                "enabled": True,
                "base_url": "http://klib",
                "key_file": str(key_file),
            },
        )
        headers = []

        def handler(request):
            headers.append(request.headers["Authorization"])
            return httpx.Response(200, json={"results": []})

        _install_transport(monkeypatch, mod, handler)
        _run(mod._handle_klib("authenticated query"))

        assert headers == ["Bearer test-key"]
