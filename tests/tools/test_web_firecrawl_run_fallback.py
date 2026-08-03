import json

import pytest

from agent import firecrawl_run_state as state
from tools import web_tools


CREDITS = {
    "code": "provider_credits_exhausted",
    "provider": "firecrawl",
    "scope": "account",
    "retryable": False,
}
CIRCUIT = {
    "code": "provider_circuit_open",
    "provider": "firecrawl",
    "scope": "account",
    "retryable": False,
}


class SearchProvider:
    name = "firecrawl"
    display_name = "Firecrawl"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def supports_search(self):
        return True

    def search(self, query, limit):
        self.calls.append((query, limit))
        return self.responses.pop(0)


class FallbackSearch:
    name = "fallback-a"
    display_name = "Fallback A"

    def __init__(self):
        self.calls = []

    def supports_search(self):
        return True

    def search(self, query, limit):
        self.calls.append((query, limit))
        return {"success": True, "data": {"web": [{"url": f"https://{query}.test"}]}}


def _patch_search(monkeypatch, primary, fallback_resolver):
    import agent.web_search_registry as registry

    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_search_backend", lambda: "firecrawl")
    monkeypatch.setattr(registry, "get_provider", lambda name: primary)
    monkeypatch.setattr(registry, "get_fallback_provider", fallback_resolver)


def test_search_402_uses_one_memoized_non_firecrawl_provider(monkeypatch):
    primary = SearchProvider([
        {"success": False, "error": "credits", "error_info": CREDITS},
        {"success": False, "error": "open", "error_info": CIRCUIT},
    ])
    fallback_a = FallbackSearch()
    fallback_b = FallbackSearch()
    selections = []

    def resolve(capability, *, excluded):
        selections.append((capability, excluded))
        return fallback_a if len(selections) == 1 else fallback_b

    _patch_search(monkeypatch, primary, resolve)
    _, token = state.install_firecrawl_run()
    try:
        first = json.loads(web_tools.web_search_tool("first", 3))
        second = json.loads(web_tools.web_search_tool("second", 4))
    finally:
        state.reset_firecrawl_run(token)

    assert first["success"] is True
    assert second["success"] is True
    assert selections == [("search", frozenset({"firecrawl"}))]
    assert fallback_a.calls == [("first", 3), ("second", 4)]
    assert fallback_b.calls == []


def test_search_open_circuit_without_fallback_keeps_sanitized_evidence(monkeypatch):
    primary = SearchProvider([
        {"success": False, "error": "open", "error_info": CIRCUIT},
    ])
    calls = []
    _patch_search(
        monkeypatch,
        primary,
        lambda capability, *, excluded: calls.append(capability) or None,
    )
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(web_tools.web_search_tool("query"))
    finally:
        state.reset_firecrawl_run(token)

    assert result["error_info"] == CIRCUIT
    assert calls == ["search"]


def test_transient_search_failure_does_not_enter_credits_fallback(monkeypatch):
    primary = SearchProvider([{"success": False, "error": "timeout"}])
    calls = []
    _patch_search(
        monkeypatch,
        primary,
        lambda capability, *, excluded: calls.append(capability),
    )
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(web_tools.web_search_tool("query"))
    finally:
        state.reset_firecrawl_run(token)

    assert result == {"success": False, "error": "timeout"}
    assert calls == []


class ExtractProvider:
    name = "firecrawl"
    display_name = "Firecrawl"

    def __init__(self, results):
        self.results = results
        self.calls = []

    def supports_extract(self):
        return True

    async def extract(self, urls, **kwargs):
        self.calls.append((list(urls), kwargs))
        return list(self.results)


class AsyncFallbackExtract:
    name = "fallback-extract"
    display_name = "Fallback Extract"

    def __init__(self, results=None, error=None):
        self.results = results
        self.error = error
        self.calls = []

    async def extract(self, urls, **kwargs):
        self.calls.append((list(urls), kwargs))
        if self.error:
            raise self.error
        return list(self.results or [])


class SyncFallbackExtract:
    name = "sync-fallback-extract"
    display_name = "Sync Fallback Extract"

    def __init__(self, results):
        self.results = results
        self.calls = []

    def extract(self, urls, **kwargs):
        self.calls.append((list(urls), kwargs))
        return list(self.results)


def _patch_extract(monkeypatch, primary, fallback, unsafe=()):
    import agent.web_search_registry as registry

    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "firecrawl")
    monkeypatch.setattr(registry, "get_provider", lambda name: primary)
    monkeypatch.setattr(registry, "get_fallback_provider", lambda *a, **k: fallback)

    async def safe(url):
        return url not in unsafe

    monkeypatch.setattr(web_tools, "async_is_safe_url", safe)


@pytest.mark.asyncio
async def test_extract_falls_back_only_credit_entries_and_preserves_order(monkeypatch):
    urls = [
        "https://example.com/success",
        "https://example.com/policy",
        "https://example.com/credit",
        "https://example.com/credit",
        None,
        "http://127.0.0.1/private",
    ]
    primary = ExtractProvider([
        {"url": urls[0], "title": "ok", "content": "primary"},
        {"url": urls[1], "title": "", "content": "", "error": "blocked", "blocked_by_policy": {"host": "example.com", "rule": "r", "source": "s"}},
        {"url": urls[2], "title": "", "content": "", "error": "credits", "error_info": CREDITS},
        {"url": urls[3], "title": "", "content": "", "error": "open", "error_info": CIRCUIT},
    ])
    fallback = AsyncFallbackExtract([
        {"url": urls[2], "title": "f1", "content": "fallback-1"},
        {"url": urls[3], "title": "f2", "content": "fallback-2"},
    ])
    _patch_extract(monkeypatch, primary, fallback, unsafe={urls[5]})
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(await web_tools.web_extract_tool(urls))
    finally:
        state.reset_firecrawl_run(token)

    assert primary.calls[0][0] == urls[:4]
    assert fallback.calls == [([urls[2], urls[3]], {"format": None})]
    assert [item["url"] for item in result["results"]] == [
        urls[0], urls[1], urls[2], urls[3], "", urls[5]
    ]
    assert [item["content"] for item in result["results"][:4]] == [
        "primary", "", "fallback-1", "fallback-2"
    ]
    assert result["results"][1]["blocked_by_policy"]["rule"] == "r"
    assert "Invalid URL item" in result["results"][4]["error"]
    assert "private or internal" in result["results"][5]["error"]


@pytest.mark.asyncio
async def test_extract_fallback_exception_is_normalized_per_unresolved_url(monkeypatch):
    urls = ["https://example.com/a", "https://example.com/b"]
    primary = ExtractProvider([
        {"url": urls[0], "error": "credits", "error_info": CREDITS},
        {"url": urls[1], "error": "open", "error_info": CIRCUIT},
    ])
    fallback = AsyncFallbackExtract(error=RuntimeError("fallback unavailable"))
    _patch_extract(monkeypatch, primary, fallback)
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(await web_tools.web_extract_tool(urls))
    finally:
        state.reset_firecrawl_run(token)

    assert [item["url"] for item in result["results"]] == urls
    assert all("Fallback extract failed" in item["error"] for item in result["results"])


@pytest.mark.asyncio
async def test_extract_short_fallback_result_marks_only_missing_position(monkeypatch):
    urls = ["https://example.com/a", "https://example.com/b"]
    primary = ExtractProvider([
        {"url": urls[0], "error": "credits", "error_info": CREDITS},
        {"url": urls[1], "error": "open", "error_info": CIRCUIT},
    ])
    fallback = AsyncFallbackExtract([
        {"url": "wrong-provider-url", "title": "first", "content": "ok"},
    ])
    _patch_extract(monkeypatch, primary, fallback)
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(await web_tools.web_extract_tool(urls))
    finally:
        state.reset_firecrawl_run(token)

    assert [item["url"] for item in result["results"]] == urls
    assert result["results"][0]["content"] == "ok"
    assert result["results"][1]["content"] == ""
    assert result["results"][1]["error"] == (
        "Extract backend returned no result for this URL"
    )


@pytest.mark.asyncio
async def test_extract_supports_sync_fallback_provider(monkeypatch):
    url = "https://example.com/a"
    primary = ExtractProvider([
        {"url": url, "error": "credits", "error_info": CREDITS},
    ])
    fallback = SyncFallbackExtract([
        {"url": url, "title": "sync", "content": "sync result"},
    ])
    _patch_extract(monkeypatch, primary, fallback)
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(await web_tools.web_extract_tool([url]))
    finally:
        state.reset_firecrawl_run(token)

    assert fallback.calls == [([url], {"format": None})]
    assert result["results"][0]["content"] == "sync result"


@pytest.mark.asyncio
async def test_extract_transient_primary_failure_does_not_fallback(monkeypatch):
    url = "https://example.com/a"
    primary = ExtractProvider([{"url": url, "error": "timeout"}])
    fallback = AsyncFallbackExtract([{"url": url, "content": "unexpected"}])
    _patch_extract(monkeypatch, primary, fallback)
    _, token = state.install_firecrawl_run()
    try:
        result = json.loads(await web_tools.web_extract_tool([url]))
    finally:
        state.reset_firecrawl_run(token)

    assert result["results"][0]["error"] == "timeout"
    assert fallback.calls == []
