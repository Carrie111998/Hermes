"""Tests for the klib slash-command plugin."""

import asyncio
import hashlib
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


def _keyboard_buttons(keyboard):
    return [button for row in keyboard.inline_keyboard for button in row]


def _callback_for_button(keyboard, text):
    return next(
        button.callback_data
        for button in _keyboard_buttons(keyboard)
        if button.text == text
    )


def _distinct_results(count):
    return [
        {"path": f"docs/{index}.md", "snippet": f"snippet {index}"}
        for index in range(1, count + 1)
    ]


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
        assert isinstance(result, tuple)
        text, keyboard = result
        assert "**docs/1.md**" in text
        assert "**docs/5.md**" in text
        assert "docs/6.md" not in text
        assert "Page 1 of 2." in text
        assert _callback_for_button(keyboard, "Next").startswith("klib:page:2:")
        assert len(text) <= 2800

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

        assert isinstance(result, tuple)
        text, keyboard = result
        assert text.count("docs/shared.md") == 1
        assert "first hit" in text
        assert "duplicate hit" not in text
        assert "**docs/4.md**" in text
        assert "docs/5.md" not in text
        assert "Page 1 of 2." in text
        assert _callback_for_button(keyboard, "Next").startswith("klib:page:2:")

    def test_exactly_page_size_results_remain_bare_string(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200, json={"results": _distinct_results(mod._PAGE_SIZE)}
            ),
        )

        result = _run(mod._handle_klib("five files", chat_id="chat-a"))

        assert isinstance(result, str)
        assert not mod._PAGINATION_SESSIONS

    def test_six_distinct_results_attach_next_keyboard(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        fixed_time = 1234.5
        monkeypatch.setattr(mod.time, "time", lambda: fixed_time)
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200, json={"results": _distinct_results(6)}
            ),
        )

        result = _run(mod._handle_klib("six files", chat_id="chat-a"))

        assert isinstance(result, tuple)
        text, keyboard = result
        assert "Page 1 of 2." in text
        assert isinstance(keyboard, mod.InlineKeyboardMarkup)
        expected_session_id = hashlib.sha256(
            f"chat-a:six files:{fixed_time}".encode()
        ).hexdigest()[:8]
        assert _callback_for_button(keyboard, "Next") == (
            f"klib:page:2:{expected_session_id}"
        )
        assert (
            mod._PAGINATION_SESSIONS[expected_session_id]["expires_at"]
            == fixed_time + 1800
        )

    def test_register_registers_command_and_callback_prefix(self):
        mod = _load_plugin_init()
        registered = {}

        class Context:
            def register_command(self, name, **kwargs):
                registered["command"] = (name, kwargs)

            def register_callback_handler(self, prefix, handler):
                registered["callback"] = (prefix, handler)

        mod.register(Context())

        assert registered["command"][0] == "klib"
        assert registered["callback"] == ("klib:", mod._handle_klib_callback)

    def test_page_navigation_round_trip_is_byte_identical(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200, json={"results": _distinct_results(6)}
            ),
        )

        initial = _run(mod._handle_klib("round trip", chat_id="chat-a"))
        initial_text, initial_keyboard = initial
        next_data = _callback_for_button(initial_keyboard, "Next")

        page_two_text, page_two_keyboard = _run(
            mod._handle_klib_callback(next_data, "chat-a")
        )
        prev_data = _callback_for_button(page_two_keyboard, "Prev")
        round_trip_text, _ = _run(mod._handle_klib_callback(prev_data, "chat-a"))

        assert "Page 2 of 2." in page_two_text
        assert "**docs/6.md**" in page_two_text
        assert round_trip_text == initial_text

    def test_mismatched_chat_id_uses_expired_session_rejection(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200, json={"results": _distinct_results(6)}
            ),
        )

        result = _run(mod._handle_klib("private search", chat_id="chat-a"))
        _, keyboard = result
        callback_data = _callback_for_button(keyboard, "Next")
        unknown = _run(mod._handle_klib_callback("klib:page:2:unknown", "chat-a"))
        mismatched = _run(mod._handle_klib_callback(callback_data, "chat-b"))

        assert mismatched == unknown
        assert mismatched[0] == mod._INVALID_PAGINATION_REPLY
        assert mismatched[1] is None

    @pytest.mark.parametrize("page", ["0", "-1", "abc"])
    def test_invalid_page_number_uses_same_rejection(self, monkeypatch, page):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        _install_transport(
            monkeypatch,
            mod,
            lambda request: httpx.Response(
                200, json={"results": _distinct_results(6)}
            ),
        )

        result = _run(mod._handle_klib("invalid page", chat_id="chat-a"))
        _, keyboard = result
        session_id = _callback_for_button(keyboard, "Next").rsplit(":", 1)[1]
        rejected = _run(
            mod._handle_klib_callback(f"klib:page:{page}:{session_id}", "chat-a")
        )

        assert rejected == (mod._INVALID_PAGINATION_REPLY, None)

    def test_unknown_and_expired_sessions_use_same_rejection(self, monkeypatch):
        mod = _load_plugin_init()
        unknown = _run(mod._handle_klib_callback("klib:page:1:missing", "chat-a"))
        mod._PAGINATION_SESSIONS["expired"] = {
            "chat_id": "chat-a",
            "query": "old",
            "distinct_results": _distinct_results(6),
            "expires_at": 0,
        }

        expired = _run(mod._handle_klib_callback("klib:page:1:expired", "chat-a"))

        assert unknown == (mod._INVALID_PAGINATION_REPLY, None)
        assert expired == unknown

    def test_paging_past_five_page_ceiling_does_not_refetch(self, monkeypatch):
        mod = _load_plugin_init()
        _mock_config(monkeypatch, mod, {"enabled": True, "base_url": "http://klib"})
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(
                200, json={"results": _distinct_results(mod._FETCH_LIMIT)}
            )

        _install_transport(monkeypatch, mod, handler)
        result = _run(mod._handle_klib("twenty five files", chat_id="chat-a"))
        _, keyboard = result
        session_id = _callback_for_button(keyboard, "Next").rsplit(":", 1)[1]

        page_five, _ = _run(
            mod._handle_klib_callback(f"klib:page:5:{session_id}", "chat-a")
        )
        no_more = _run(
            mod._handle_klib_callback(f"klib:page:6:{session_id}", "chat-a")
        )

        assert "Page 5 of 5." in page_five
        assert no_more == (mod._NO_MORE_RESULTS_REPLY, None)
        assert len(requests) == 1

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
        assert "**docs/dominant.md**" in result
        assert "**docs/other-1.md**" in result
        assert "**docs/other-2.md**" in result
        assert "**docs/other-3.md**" in result
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
        assert "docs/manual.md" in result
        assert "# Manual" in result
        assert "Full text from klib." in result
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

        assert "Usage: /klib read <path>" in result
        assert calls == []

    def test_query_returns_raw_file_path_in_bold_label(self, monkeypatch):
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

        assert "1. **wiki/ccs/ccj-issue-108.md** — relevant page" in result
        assert r"wiki/ccs/ccj\-issue\-108\.md" not in result

    def test_query_returns_raw_markdownv2_snippet(self, monkeypatch):
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

        result = _run(mod._handle_klib("cost > $100"))

        assert "klib results for 'cost > $100':" in result
        assert "cost > $100 (approx) [see note].!" in result
        assert r"cost \> $100 \(approx\) \[see note\]\.\!" not in result

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
