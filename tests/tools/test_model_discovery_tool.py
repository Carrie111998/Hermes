"""Tests for the models_explore discovery tool (native action-based shape)."""

from __future__ import annotations

import json

import httpx
import pytest

from tools import model_discovery_tool
from tools.media_catalog import MediaCatalogClient

API_ROOT = "http://aiproxy.test"
STATIC_BASE = "https://static.test/schema/"
SEEDANCE_ID = "bytedance/seedance-2.0/reference-to-video"

CATALOG_DATA = [
    {
        "id": SEEDANCE_ID,
        "media_type": "video",
        "vendor": "bytedance",
        "name": "Seedance 2.0 R2V",
        "description": "reference-to-video, strong identity",
        "tags": ["reference", "identity"],
        "input_modalities": ["image", "text"],
        "output_modalities": ["video"],
    },
    {"id": "google/nano-banana-2/text-to-image", "media_type": "image", "vendor": "google"},
]

SEEDANCE_STATIC_SCHEMA = {
    "components": {
        "schemas": {
            "Input": {
                "required": ["prompt"],
                "properties": {
                    "model": {"type": "string"},
                    "prompt": {"type": "string"},
                    "duration": {"type": "integer", "enum": [4, 5, 10, 15], "default": 5},
                },
            }
        }
    }
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/catalog/models":
        type_filter = request.url.params.get("type")
        data = CATALOG_DATA
        if type_filter:
            data = [m for m in data if m.get("media_type") == type_filter]
        return httpx.Response(200, json={"code": 200, "msg": "succeed", "data": data})
    if path.startswith("/v1/models/"):
        # aiproxy envelope (no params) → forces static schema fallback
        return httpx.Response(200, json={"code": 200, "msg": "succeed", "data": {}})
    if request.url.host == "static.test" and path.endswith(".json"):
        slug = path.rsplit("/", 1)[-1][: -len(".json")]
        if slug == "bytedance-seedance-2.0-reference-to-video":
            return httpx.Response(200, json=SEEDANCE_STATIC_SCHEMA)
        return httpx.Response(404, json={})
    return httpx.Response(404, json={})


@pytest.fixture(autouse=True)
def _stub_catalog_client(monkeypatch):
    client = MediaCatalogClient(
        api_root=API_ROOT,
        api_key="test-key",
        http=httpx.Client(transport=httpx.MockTransport(_handler)),
        static_schema_base=STATIC_BASE,
    )
    monkeypatch.setattr(model_discovery_tool, "get_catalog_client", lambda: client)
    yield


def _run(args):
    return json.loads(model_discovery_tool._handle_models_explore(args))


class TestModelsExplore:
    def test_list_filtered_by_type(self):
        result = _run({"action": "list", "type": "video"})
        assert result["success"] is True
        assert any(m["id"] == SEEDANCE_ID for m in result["models"])
        assert all(m["type"] == "video" for m in result["models"])

    def test_search_by_keyword(self):
        result = _run({"action": "search", "query": "seedance"})
        assert result["success"] is True
        assert result["models"][0]["id"] == SEEDANCE_ID

    def test_search_requires_query(self):
        result = _run({"action": "search"})
        assert result["success"] is False
        assert result["error_type"] == "invalid_param"

    def test_get_returns_params_without_model_field(self):
        result = _run({"action": "get", "model": SEEDANCE_ID})
        assert result["success"] is True
        assert "model" not in result["params"]
        assert result["params"]["prompt"]["required"] is True
        assert result["params"]["duration"]["values"] == ["4", "5", "10", "15"]

    def test_get_unknown_model_structured_error(self):
        result = _run({"action": "get", "model": "ghost/model"})
        assert result["success"] is False
        assert result["error_type"] == "model_not_found"

    def test_recommend_ranks_seedance_for_reference_task(self):
        result = _run({"action": "recommend", "task": "reference identity video", "type": "video"})
        assert result["success"] is True
        assert result["recommendations"][0]["model"]["id"] == SEEDANCE_ID

    def test_invalid_action(self):
        result = _run({"action": "destroy"})
        assert result["success"] is False
        assert result["error_type"] == "invalid_param"
