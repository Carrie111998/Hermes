import json
import threading

import pytest

from plugins.memory.timem import (
    TimemMemoryProvider,
    _clean_text_for_capture,
    _extract_memories,
    _format_prefetch_context,
    _load_timem_config,
    _save_timem_config,
)


class FakeClient:
    def __init__(self, api_key: str, base_url: str, timeout: float,
                 user_id: str, character_id: str, domain: str):
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.user_id = user_id
        self.character_id = character_id
        self.domain = domain
        self.search_results = []
        self.search_calls = []
        self.ingest_calls = []
        self.add_calls = []
        self.profile_response = {}
        self.closed = False

    def search(self, query, *, limit, score_threshold):
        self.search_calls.append({"query": query, "limit": limit,
                                  "score_threshold": score_threshold})
        return self.search_results

    def ingest_turn(self, session_id, messages, metadata=None):
        self.ingest_calls.append({"session_id": session_id,
                                  "messages": messages, "metadata": metadata})
        return {"task_id": "task_1"}

    def add_fact(self, content, *, tags=None, session_id=None):
        self.add_calls.append({"content": content, "tags": tags,
                               "session_id": session_id})
        return {"id": "mem_1"}

    def get_profile(self):
        return self.profile_response

    def close(self):
        self.closed = True


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMEM_API_KEY", "test-key")
    monkeypatch.setattr("plugins.memory.timem._TimemClient", FakeClient)
    p = TimemMemoryProvider()
    p.initialize("session-1", hermes_home=str(tmp_path), platform="cli")
    return p


def _join_worker_threads(p):
    for t in (p._prefetch_thread, p._sync_thread):
        if t and t.is_alive():
            t.join(timeout=5.0)
    # mirror threads are fire-and-forget; give them a beat
    for t in threading.enumerate():
        if t.name == "timem-mirror":
            t.join(timeout=5.0)


# ─── Availability ────────────────────────────────────────────────────────────

def test_is_available_false_without_api_key(monkeypatch):
    monkeypatch.delenv("TIMEM_API_KEY", raising=False)
    assert TimemMemoryProvider().is_available() is False


def test_is_available_true_when_import_missing_but_key_set(monkeypatch):
    # is_available() must NOT gate on the timem SDK being importable — the
    # SDK is lazy-installed at client construction. Mirrors supermemory/mem0.
    monkeypatch.setenv("TIMEM_API_KEY", "test-key")

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "timem" or name.startswith("timem."):
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert TimemMemoryProvider().is_available() is True


# ─── Config ──────────────────────────────────────────────────────────────────

def test_load_and_save_config_round_trip(tmp_path):
    _save_timem_config({"user_id": "u-42", "auto_capture": False,
                        "score_threshold": 0.7}, str(tmp_path))
    cfg = _load_timem_config(str(tmp_path))
    assert cfg["user_id"] == "u-42"
    assert cfg["auto_capture"] is False
    assert cfg["auto_recall"] is True
    assert cfg["score_threshold"] == 0.7


def test_config_clamps_bad_values(tmp_path):
    _save_timem_config({"max_recall_results": 999, "score_threshold": 5,
                        "api_timeout": -3}, str(tmp_path))
    cfg = _load_timem_config(str(tmp_path))
    assert cfg["max_recall_results"] == 20
    assert cfg["score_threshold"] == 1.0
    assert cfg["api_timeout"] == 1.0


def test_save_config_filters_unknown_keys(provider, tmp_path):
    provider.save_config({"user_id": "u-1", "api_key": "should-not-persist",
                          "evil": "x"}, str(tmp_path))
    cfg = _load_timem_config(str(tmp_path))
    assert cfg["user_id"] == "u-1"
    raw = json.loads((tmp_path / "timem.json").read_text(encoding="utf-8"))
    assert "api_key" not in raw
    assert "evil" not in raw


# ─── Result normalization ────────────────────────────────────────────────────

def test_extract_memories_handles_shape_variants():
    assert _extract_memories(None) == []
    assert _extract_memories({}) == []
    # {"memories": [...]} with dict content and layer/score aliases
    out = _extract_memories({"memories": [
        {"id": "1", "content": {"text": "likes tea"}, "layer": "L3", "retrieval_score": 0.9},
        {"memory": "works at ACME", "layer_type": "L1", "similarity": 0.6},
        {"content": ""},  # dropped: no text
    ]})
    assert [m["text"] for m in out] == ["likes tea", "works at ACME"]
    assert out[0]["layer"] == "L3" and out[0]["score"] == 0.9
    assert out[1]["layer"] == "L1" and out[1]["score"] == 0.6
    # {"results": [...]} and nested {"data": {"memories": [...]}}
    assert _extract_memories({"results": ["plain string"]})[0]["text"] == "plain string"
    assert _extract_memories({"data": {"memories": [{"text": "nested"}]}})[0]["text"] == "nested"


def test_format_prefetch_context_dedupes_and_caps():
    memories = [{"text": "fact A", "layer": "L2", "score": 0.8},
                {"text": "fact A"},
                {"text": "fact B"}]
    block = _format_prefetch_context(memories, max_results=8)
    assert block.startswith("<timem-context>")
    assert block.count("fact A") == 1
    assert "fact B" in block
    assert "[L2]" in block and "[80%]" in block
    assert _format_prefetch_context([], 8) == ""


def test_clean_text_strips_injected_context():
    text = "hello\n<timem-context>ignore me</timem-context>\nworld"
    assert _clean_text_for_capture(text) == "hello\nworld"


# ─── Recall / capture ────────────────────────────────────────────────────────

def test_prefetch_returns_formatted_context(provider):
    provider._client.search_results = [{"text": "user prefers dark mode",
                                        "layer": "L4", "score": 0.75}]
    provider.queue_prefetch("what theme should I use for the app?")
    result = provider.prefetch("what theme should I use for the app?")
    assert "user prefers dark mode" in result
    assert provider._client.search_calls  # search actually ran


def test_prefetch_strips_previous_injection_from_query(provider):
    provider.queue_prefetch("<timem-context>old</timem-context>real question here")
    provider.prefetch("real question here")
    assert provider._client.search_calls
    assert "old" not in provider._client.search_calls[0]["query"]


def test_sync_turn_submits_cleaned_exchange(provider):
    provider.sync_turn("I moved to Berlin last month", "Noted — congrats!",
                       session_id="session-1")
    _join_worker_threads(provider)
    assert len(provider._client.ingest_calls) == 1
    call = provider._client.ingest_calls[0]
    assert call["session_id"] == "session-1"
    assert call["messages"][0]["role"] == "user"
    assert call["messages"][1]["role"] == "assistant"


def test_sync_turn_skips_trivial_messages(provider):
    provider.sync_turn("ok", "Anything else?")
    provider.sync_turn("short", "too short to capture")
    _join_worker_threads(provider)
    assert provider._client.ingest_calls == []


def test_sync_turn_skipped_for_non_primary_context(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMEM_API_KEY", "test-key")
    monkeypatch.setattr("plugins.memory.timem._TimemClient", FakeClient)
    p = TimemMemoryProvider()
    p.initialize("cron-1", hermes_home=str(tmp_path), platform="cron",
                 agent_context="cron")
    p.sync_turn("scheduled system prompt content here", "done")
    _join_worker_threads(p)
    assert p._client.ingest_calls == []


def test_on_memory_write_mirrors_add(provider):
    provider.on_memory_write("add", "user", "User's birthday is March 3rd")
    _join_worker_threads(provider)
    assert len(provider._client.add_calls) == 1
    call = provider._client.add_calls[0]
    assert call["content"]["text"] == "User's birthday is March 3rd"
    assert "builtin-memory" in call["tags"]


def test_on_memory_write_ignores_remove(provider):
    provider.on_memory_write("remove", "user", "some removed entry content")
    _join_worker_threads(provider)
    assert provider._client.add_calls == []


# ─── Tools ───────────────────────────────────────────────────────────────────

def test_tool_schemas_exposed(provider):
    names = [s["name"] for s in provider.get_tool_schemas()]
    assert names == ["timem_search", "timem_add", "timem_profile"]


def test_timem_search_tool(provider):
    provider._client.search_results = [{"text": "fact", "score": 0.9}]
    result = json.loads(provider.handle_tool_call("timem_search", {"query": "fact"}))
    assert result["count"] == 1
    assert result["results"][0]["text"] == "fact"


def test_timem_search_tool_requires_query(provider):
    result = provider.handle_tool_call("timem_search", {})
    assert "query" in result


def test_timem_add_tool(provider):
    result = json.loads(provider.handle_tool_call(
        "timem_add", {"content": "likes espresso", "tags": ["prefs"]}))
    assert result["result"] == "Fact stored."
    call = provider._client.add_calls[0]
    assert call["content"]["text"] == "likes espresso"
    assert "prefs" in call["tags"] and "hermes" in call["tags"]


def test_timem_profile_tool(provider):
    provider._client.profile_response = {"persona": "engineer"}
    result = json.loads(provider.handle_tool_call("timem_profile", {}))
    assert result["profile"] == {"persona": "engineer"}


def test_unknown_tool_returns_error(provider):
    result = provider.handle_tool_call("timem_bogus", {})
    assert "Unknown tool" in result


# ─── Circuit breaker ─────────────────────────────────────────────────────────

def test_circuit_breaker_opens_after_failures(provider):
    def boom(*args, **kwargs):
        raise RuntimeError("api down")

    provider._client.search = boom
    for _ in range(5):
        provider.handle_tool_call("timem_search", {"query": "x"})
    result = provider.handle_tool_call("timem_search", {"query": "x"})
    assert "temporarily unavailable" in result


def test_circuit_breaker_resets_after_cooldown(provider, monkeypatch):
    provider._consecutive_failures = 5
    provider._breaker_opened_at = 0.0  # long ago in monotonic terms
    monkeypatch.setattr("plugins.memory.timem.time.monotonic", lambda: 1e9)
    assert provider._is_breaker_open() is False


# ─── Setup schema ────────────────────────────────────────────────────────────

def test_config_schema_has_secret_api_key(provider):
    schema = provider.get_config_schema()
    by_key = {f["key"]: f for f in schema}
    assert by_key["api_key"]["secret"] is True
    assert by_key["api_key"]["env_var"] == "TIMEM_API_KEY"
    assert by_key["base_url"]["default"] == "https://api.timem.cloud"


def test_shutdown_closes_client(provider):
    client = provider._client
    provider.shutdown()
    assert client.closed is True
    assert provider._client is None
