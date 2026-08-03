import json

import pytest

from agent import firecrawl_run_state as state
from cron import scheduler
from events.schema import EventType
from plugins.browser.firecrawl import provider as browser_provider_module
from plugins.web.firecrawl import provider as web_provider_module
from tools import browser_tool, web_tools


class RecordingBus:
    def __init__(self):
        self.calls = []

    def emit(self, **kwargs):
        self.calls.append(kwargs)


class SearchFallback:
    name = "search-fallback"

    def __init__(self):
        self.calls = []

    def search(self, query, limit):
        self.calls.append((query, limit))
        return {
            "success": True,
            "data": {"web": [{"url": "https://fallback.example/result"}]},
        }


class ExtractFallback:
    name = "extract-fallback"

    def __init__(self):
        self.calls = []

    async def extract(self, urls, **kwargs):
        self.calls.append((list(urls), kwargs))
        return [
            {"url": url, "title": "fallback", "content": "fallback content"}
            for url in urls
        ]


class PaymentRequiredError(Exception):
    status_code = 402


class FirecrawlClient:
    def __init__(self):
        self.search_calls = []
        self.scrape_calls = []
        self.search_error = PaymentRequiredError("secret response body")

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        raise self.search_error

    def scrape(self, *, url, formats):
        self.scrape_calls.append((url, formats))
        return {
            "markdown": "primary content",
            "metadata": {"title": "primary", "sourceURL": url},
        }


class Response:
    def __init__(self, *, status_code=200, data=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = ""
        self._data = data or {}

    def json(self):
        return self._data


@pytest.mark.asyncio
async def test_scout_activation_stops_later_firecrawl_and_emits_one_credits_action(
    monkeypatch,
):
    import agent.web_search_registry as registry

    client = FirecrawlClient()
    firecrawl_web = web_provider_module.FirecrawlWebSearchProvider()
    firecrawl_browser = browser_provider_module.FirecrawlBrowserProvider()
    search_fallback = SearchFallback()
    extract_fallback = ExtractFallback()
    post_calls = []
    delete_calls = []
    local_calls = []

    monkeypatch.setattr(web_provider_module, "_get_firecrawl_client", lambda: client)
    monkeypatch.setattr(web_provider_module, "check_website_access", lambda url: None)
    monkeypatch.setattr(web_provider_module, "is_safe_url", lambda url: True)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "firecrawl")
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "firecrawl")
    monkeypatch.setattr(web_tools, "async_is_safe_url", lambda url: _true())
    monkeypatch.setattr(registry, "get_provider", lambda name: firecrawl_web)
    monkeypatch.setattr(
        registry,
        "get_fallback_provider",
        lambda capability, *, excluded: (
            search_fallback if capability == "search" else extract_fallback
        ),
    )

    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-only-key")

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return Response(data={"id": "cloud-before", "cdpUrl": "ws://cloud.test"})

    def fake_delete(url, **kwargs):
        delete_calls.append((url, kwargs))
        return Response(status_code=204)

    monkeypatch.setattr(browser_provider_module.requests, "post", fake_post)
    monkeypatch.setattr(browser_provider_module.requests, "delete", fake_delete)
    monkeypatch.setattr(browser_tool, "_active_sessions", {})
    monkeypatch.setattr(browser_tool, "_cached_cloud_provider", None)
    monkeypatch.setattr(browser_tool, "_cloud_provider_resolved", False)
    monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
    monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: firecrawl_browser)
    monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: None)

    def fake_local(task_id):
        local_calls.append(task_id)
        return {
            "session_name": "local-fallback",
            "bb_session_id": None,
            "cdp_url": None,
            "features": {"local": True},
        }

    monkeypatch.setattr(browser_tool, "_create_local_session", fake_local)

    run, token = state.install_firecrawl_run()
    try:
        cloud_before = firecrawl_browser.create_session("before-open")
        extracted_before = await firecrawl_web.extract(
            ["https://example.com/before-open"]
        )
        assert cloud_before["bb_session_id"] == "cloud-before"
        assert extracted_before[0]["content"] == "primary content"
        assert len(post_calls) == 1
        assert len(client.scrape_calls) == 1

        search_result = json.loads(web_tools.web_search_tool("first-402", 3))
        assert search_result["success"] is True
        assert client.search_calls == [{"query": "first-402", "limit": 3}]
        assert search_fallback.calls == [("first-402", 3)]
        assert run.circuit_open is True

        extract_result = json.loads(
            await web_tools.web_extract_tool(["https://example.com/after-open"])
        )
        assert extract_result["results"][0]["content"] == "fallback content"
        assert extract_fallback.calls == [
            (["https://example.com/after-open"], {"format": None})
        ]
        assert len(client.scrape_calls) == 1

        local_session = browser_tool._get_session_info("after-open")
        assert local_session["features"]["local"] is True
        assert local_session["fallback_reason"] == "provider_circuit_open"
        assert local_calls == ["after-open"]
        assert len(post_calls) == 1

        assert firecrawl_browser.close_session("cloud-before") is True
        firecrawl_browser.emergency_cleanup("cloud-before")
        assert len(delete_calls) == 2

        emitter = type("Emitter", (), {"bus": RecordingBus()})()
        scheduler._finalize_agent_iteration_event(
            emitter,
            {"id": "scout-acceptance", "name": "jobflow-scout"},
            "plain response",
            success=True,
            firecrawl_state=run,
        )
        credits = [
            call for call in emitter.bus.calls
            if call["event_type"] == EventType.AGENT_ITERATION
            and call["payload"].get("action_kind") == "credits"
        ]
        assert len(credits) == 1
        assert credits[0]["payload"]["action_required"] is True
    finally:
        state.reset_firecrawl_run(token)

    transient_run, transient_token = state.install_firecrawl_run()
    try:
        client.search_error = TimeoutError("temporary timeout")
        transient_result = firecrawl_web.search("transient")
        assert client.search_calls[-1] == {"query": "transient", "limit": 5}
        assert len(client.search_calls) == 2
        assert transient_result["success"] is False
        assert "error_info" not in transient_result
        assert transient_run.circuit_open is False

        transient_emitter = type("Emitter", (), {"bus": RecordingBus()})()
        scheduler._finalize_agent_iteration_event(
            transient_emitter,
            {"id": "scout-transient", "name": "jobflow-scout"},
            "plain response",
            success=True,
            firecrawl_state=transient_run,
        )
        assert not any(
            call["payload"].get("action_kind") == "credits"
            for call in transient_emitter.bus.calls
        )
    finally:
        state.reset_firecrawl_run(transient_token)

    next_run, next_token = state.install_firecrawl_run()
    try:
        assert next_run.circuit_open is False
    finally:
        state.reset_firecrawl_run(next_token)


async def _true():
    return True
