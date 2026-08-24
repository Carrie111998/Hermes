"""hindsight_update_memory: the missing write path for occurred_* fields (#93568).

Retained memories land with occurred_start/occurred_end = null and no Hermes
tool could ever set them — the plugin's only write tool was hindsight_retain,
and the SDK's generated MemoryApi (0.6.1) has no per-memory PATCH method.
The new tool drives ApiClient.param_serialize/call_api directly with the
constant resource path the generated methods use, so host/auth/timeout stay
in the SDK Configuration's domain.

These tests use a fake ApiClient (no hindsight-client import needed) to pin:
schema registration, provided-fields-only patch bodies with empty-string
passthrough, the missing-argument guards, and rejection-body surfacing.
"""

import json

import pytest

from plugins.memory.hindsight import HindsightMemoryProvider, UPDATE_SCHEMA


class _FakeResponse:
    def __init__(self, status: int, data: bytes):
        self.status = status
        self.data = data
        self.reason = f"reason-{status}"

    async def read(self):
        pass


class _FakeApiClient:
    """Stands in for hindsight_client_api.ApiClient.

    param_serialize echoes back a RequestSerialized-shaped tuple whose URL
    prefix represents the Configuration-owned host; call_api returns a
    canned response. Everything the plugin is responsible for — method,
    resource path, path params, header params, body — is captured for
    assertions. Auth is Configuration-owned and deliberately not faked
    here.
    """

    def __init__(self, status: int = 200, body: bytes = b'{"ok": true}'):
        self.serialize_calls = []
        self.call_api_calls = []
        self.status = status
        self.body = body

    def param_serialize(self, **kwargs):
        self.serialize_calls.append(kwargs)
        # Mirror the real ApiClient: path params are interpolated (and
        # URL-quoted) into the resource path at this layer.
        resource_path = kwargs["resource_path"]
        for key, value in (kwargs.get("path_params") or {}).items():
            resource_path = resource_path.replace("{" + key + "}", str(value))
        url = "https://configured-server.test" + resource_path
        return (kwargs["method"], url, dict(kwargs.get("header_params") or {}),
                kwargs.get("body"), kwargs.get("post_params") or [])

    async def call_api(self, *args, **request_kwargs):
        self.call_api_calls.append((args, request_kwargs))
        return _FakeResponse(self.status, self.body)


class _FakeClient:
    def __init__(self, api_client):
        self._api_client = api_client


def _make_provider(tmp_path, monkeypatch, fake_api_client) -> HindsightMemoryProvider:
    config = {
        "mode": "cloud",
        "api_url": "https://configured-server.test",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
    }
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(
        "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
    )
    provider = HindsightMemoryProvider()
    provider.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
    provider._client = _FakeClient(fake_api_client)
    return provider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL", "HINDSIGHT_BANK_ID",
        "HINDSIGHT_MODE", "HINDSIGHT_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_update_tool_registered_in_schemas(tmp_path, monkeypatch):
    provider = _make_provider(tmp_path, monkeypatch, _FakeApiClient())
    schemas = {s["name"]: s for s in provider.get_tool_schemas()}
    assert "hindsight_update_memory" in schemas
    schema = schemas["hindsight_update_memory"]
    assert schema["parameters"]["required"] == ["memory_id"]
    for prop in ("occurred_start", "occurred_end", "text", "context"):
        assert prop in schema["parameters"]["properties"]
    # The tool description must carry the server's curatability caveat so
    # the model doesn't burn calls on derived observation facts.
    assert "world/experience" in schema["description"]
    assert UPDATE_SCHEMA["name"] == "hindsight_update_memory"


def test_update_patches_only_provided_fields(tmp_path, monkeypatch):
    api = _FakeApiClient()
    provider = _make_provider(tmp_path, monkeypatch, api)
    result = json.loads(provider.handle_tool_call("hindsight_update_memory", {
        "memory_id": "mem-42",
        "occurred_start": "2026-08-24T03:48:54Z",
        "occurred_end": "",  # explicit clear must pass through, not drop
    }))
    assert result["result"] == "Memory updated successfully."
    assert result["memory_id"] == "mem-42"
    assert result["updated_fields"] == ["occurred_end", "occurred_start"]

    assert len(api.serialize_calls) == 1
    call = api.serialize_calls[0]
    assert call["method"] == "PATCH"
    assert call["resource_path"] == "/v1/default/banks/{bank_id}/memories/{memory_id}"
    assert call["path_params"] == {"bank_id": "test-bank", "memory_id": "mem-42"}
    # Provided keys only — text/context were omitted and must not appear.
    assert call["body"] == {
        "occurred_start": "2026-08-24T03:48:54Z",
        "occurred_end": "",
    }
    assert call["header_params"]["Content-Type"] == "application/json"

    args, request_kwargs = api.call_api_calls[0]
    assert args[0] == "PATCH"
    assert args[1] == "https://configured-server.test/v1/default/banks/test-bank/memories/mem-42"
    assert request_kwargs["_request_timeout"] > 0


def test_update_requires_memory_id(tmp_path, monkeypatch):
    provider = _make_provider(tmp_path, monkeypatch, _FakeApiClient())
    out = provider.handle_tool_call("hindsight_update_memory", {"occurred_start": "2026-01-01T00:00:00Z"})
    assert "memory_id" in out


def test_update_requires_at_least_one_field(tmp_path, monkeypatch):
    provider = _make_provider(tmp_path, monkeypatch, _FakeApiClient())
    out = provider.handle_tool_call("hindsight_update_memory", {"memory_id": "mem-42"})
    assert "at least one field" in out


def test_update_surfaces_server_rejection_detail(tmp_path, monkeypatch):
    api = _FakeApiClient(
        status=400,
        body=b'{"detail":"only world/experience facts can be curated. '
             b'Observations are derived and regenerate from their sources."}',
    )
    provider = _make_provider(tmp_path, monkeypatch, api)
    out = provider.handle_tool_call("hindsight_update_memory", {
        "memory_id": "mem-42",
        "occurred_start": "2026-08-24T03:48:54Z",
    })
    assert "Failed to update memory" in out
    assert "HTTP 400" in out
    assert "world/experience" in out


def test_update_accepts_empty_204_body(tmp_path, monkeypatch):
    api = _FakeApiClient(status=204, body=b"")
    provider = _make_provider(tmp_path, monkeypatch, api)
    result = json.loads(provider.handle_tool_call("hindsight_update_memory", {
        "memory_id": "mem-42",
        "context": "project decision",
    }))
    assert result["result"] == "Memory updated successfully."
