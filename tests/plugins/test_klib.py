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
        assert requests[0].url.params["limit"] == str(mod._FETCH_LIMIT)
        assert r"docs/1\.md" in result
        assert r"docs/5\.md" in result
        assert r"docs/6\.md" not in result
        assert "top 5 of 7" in result
        assert len(result) <= 2800

    def test_query_deduplicates_same_file_and_counts_distinct_files(
        self, monkeypatch
    ):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"path": "docs/shared.md", "snippet": "first hit"},
                        {"path": "docs/shared.md", "snippet": "duplicate hit"},
                        {"path": "docs/1.md", "snippet": "snippet 1"},
                        {"path": "docs/2.md", "snippet": "snippet 2"},
                        {"path": "docs/3.md", "snippet": "snippet 3"},
                        {"path": "docs/4.md", "snippet": "snippet 4"},
                        {"path": "docs/5.md", "snippet": "snippet 5"},
                    ]
                },
            )

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("duplicate files"))

        assert result.count(r"docs/shared\.md") == 1
        assert "first hit" in result
        assert "duplicate hit" not in result
        assert r"docs/4\.md" in result
        assert r"docs/5\.md" not in result
        assert r"Showing top 5 of 6 distinct files\." in result

    def test_query_overfetches_past_file_dominated_server_limit(
        self, monkeypatch
    ):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        requests = []
        corpus = [
            {"path": "docs/dominant.md", "snippet": f"dominant hit {index}"}
            for index in range(20)
        ] + [
            {"path": f"docs/other-{file_index}.md", "snippet": "other hit"}
            for file_index in range(1, 4)
            for _ in range(2)
        ]

        def handler(request):
            requests.append(request)
            limit = int(request.url.params["limit"])
            return httpx.Response(200, json={"results": corpus[:limit]})

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("shared topic"))

        assert len(requests) == 1
        assert requests[0].url.params["limit"] == str(mod._FETCH_LIMIT)
        assert r"docs/dominant\.md" in result
        assert r"docs/other\-1\.md" in result
        assert r"docs/other\-2\.md" in result
        assert r"docs/other\-3\.md" in result
        assert "Showing top 4 of 4 distinct files." not in result

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

        assert r"Usage: /klib <query\>" in result
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

    def test_read_success_returns_path_and_full_text(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "path": "docs/manual.md",
                    "raw": "# Manual\n\nFull text from klib.",
                    "status": 200,
                },
            )

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("read docs/manual.md"))

        assert len(requests) == 1
        assert requests[0].url.path == "/read"
        assert requests[0].url.params["path"] == "docs/manual.md"
        assert r"docs/manual\.md" in result
        assert r"\# Manual" in result
        assert "Full text from klib\\." in result
        assert "\n\n" in result
        assert len(result) <= 2800

    def test_read_404_is_a_friendly_not_found_message(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(404, text="not found"),
        )

        result = _run(mod._handle_klib("read docs/missing.md"))

        assert "not found" in result.lower()
        assert "HTTP status 404" not in result

    def test_read_timeout_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})

        def handler(request):
            raise httpx.TimeoutException("simulated timeout", request=request)

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("read docs/slow.md"))

        assert "timed out" in result

    def test_read_connection_failure_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})

        def handler(request):
            raise httpx.ConnectError("simulated connection failure", request=request)

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("read docs/unreachable.md"))

        assert "could not reach" in result

    def test_read_non_2xx_response_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(500, text="server failure"),
        )

        result = _run(mod._handle_klib("read docs/server-error.md"))

        assert "HTTP status 500" in result

    def test_read_invalid_json_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(200, text="not json"),
        )

        result = _run(mod._handle_klib("read docs/broken.md"))

        assert "invalid JSON" in result

    def test_read_invalid_response_format_is_friendly(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(200, json={"path": "docs/broken.md"}),
        )

        result = _run(mod._handle_klib("read docs/broken.md"))

        assert "invalid response format" in result

    @pytest.mark.parametrize("raw_args", ["read", "read "])
    def test_read_without_path_returns_usage_without_http_call(
        self, monkeypatch, raw_args
    ):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        calls = []
        _install_transport(monkeypatch, mod, lambda request: calls.append(request))

        result = _run(mod._handle_klib(raw_args))

        assert r"Usage: /klib read <path\>" in result
        assert calls == []

    def test_query_escapes_markdownv2_file_path(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "path": "wiki/ccs/ccj-issue-108.md",
                            "snippet": "relevant page",
                        }
                    ]
                },
            ),
        )

        result = _run(mod._handle_klib("issue lookup"))

        assert r"wiki/ccs/ccj\-issue\-108\.md" in result

    def test_query_escapes_markdownv2_snippet(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "path": "docs/cost.md",
                            "snippet": "cost > $100 (approx) [see note].!",
                        }
                    ]
                },
            ),
        )

        result = _run(mod._handle_klib("cost"))

        assert r"cost \> $100 \(approx\) \[see note\]\.\!" in result

    def test_semantic_prefix_sends_semantic_mode(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"results": []})

        _install_transport(monkeypatch, mod, handler)
        _run(mod._handle_klib("semantic cache invalidation"))

        assert len(requests) == 1
        assert requests[0].url.params["q"] == "cache invalidation"
        assert requests[0].url.params["mode"] == "semantic"

    def test_plain_query_explicitly_defaults_to_lexical_mode(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"results": []})

        _install_transport(monkeypatch, mod, handler)
        _run(mod._handle_klib("cache invalidation"))

        assert len(requests) == 1
        assert requests[0].url.params["q"] == "cache invalidation"
        assert requests[0].url.params["mode"] == "lexical"
