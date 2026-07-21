"""Tests for the tool-layer media catalog client (aiproxy, atlas CLI pattern)."""

from __future__ import annotations

import json

import httpx
import pytest

from tools.media_catalog import (
    MediaCatalogClient,
    MediaCatalogError,
    ERR_MODEL_NOT_FOUND,
)

API_ROOT = "http://aiproxy.test"
STATIC_BASE = "https://static.test/schema/"

SEEDANCE_ID = "bytedance/seedance-2.0/reference-to-video"

# aiproxy /api/v1/catalog/models payload (views.Response{code,msg,data}).
CATALOG_DATA = [
    {
        "id": SEEDANCE_ID,
        "object": "model",
        "type": "video",
        "media_type": "video",
        "vendor": "bytedance",
        "name": "Seedance 2.0 R2V",
        "description": "reference-to-video, strong identity",
        "tags": ["reference", "identity"],
        "input_modalities": ["image", "text"],
        "output_modalities": ["video"],
        "supported_features": ["reference-to-video"],
        "schema_url": STATIC_BASE + "bytedance-seedance-2.0-reference-to-video.json",
    },
    {
        "id": "kwaivgi/kling-v3.0-pro/image-to-video",
        "media_type": "video",
        "vendor": "kwaivgi",
        "tags": ["multi-shot", "audio"],
    },
    {
        "id": "google/nano-banana-2/text-to-image",
        "media_type": "image",
        "vendor": "google",
    },
    {
        "id": "openai/gpt-4o",
        "media_type": "chat",
        "vendor": "openai",
    },
]

# OpenAPI static schema for seedance-2.0 r2v (the real constraint source).
SEEDANCE_STATIC_SCHEMA = {
    "components": {
        "schemas": {
            "Input": {
                "required": ["prompt"],
                "properties": {
                    "model": {"type": "string"},  # must be skipped
                    "prompt": {"type": "string", "description": "video prompt"},
                    "duration": {"type": "integer", "enum": [4, 5, 10, 15], "default": 5},
                    "resolution": {"type": "string", "enum": ["480p", "720p", "1080p"]},
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
                },
            }
        }
    }
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    host = request.url.host

    if path == "/api/v1/catalog/models":
        type_filter = request.url.params.get("type")
        data = CATALOG_DATA
        if type_filter:
            data = [m for m in data if m.get("media_type") == type_filter]
        return httpx.Response(200, json={"code": 200, "msg": "succeed", "data": data})

    if path.startswith("/v1/models/"):
        model_id = path[len("/v1/models/"):]
        if model_id == "rich/text-to-video":
            # Raw ModelSchema body (atlas CLI shape) — used directly, no static call.
            return httpx.Response(200, json={
                "id": model_id,
                "type": "video",
                "vendor": "rich",
                "params": {
                    "model": {"type": "string"},
                    "prompt": {"type": "string", "required": True},
                    "duration": {"type": "integer", "min": 3, "max": 15},
                },
            })
        if model_id == SEEDANCE_ID:
            # aiproxy answers /v1/models/<id> with an envelope (no params) →
            # client must fall through to the static schema.
            return httpx.Response(200, json={"code": 200, "msg": "succeed", "data": {"id": model_id}})
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    if host == "static.test" and path.endswith(".json"):
        slug = path.rsplit("/", 1)[-1][: -len(".json")]
        if slug == "bytedance-seedance-2.0-reference-to-video":
            return httpx.Response(200, json=SEEDANCE_STATIC_SCHEMA)
        return httpx.Response(404, json={})

    return httpx.Response(404, json={})


@pytest.fixture
def client() -> MediaCatalogClient:
    transport = httpx.MockTransport(_handler)
    return MediaCatalogClient(
        api_root=API_ROOT,
        api_key="test-key",
        http=httpx.Client(transport=transport),
        static_schema_base=STATIC_BASE,
    )


class TestListModels:
    def test_list_video_filters_by_type(self, client):
        models = client.list_models(type="video")
        ids = {m.id for m in models}
        assert SEEDANCE_ID in ids
        assert "kwaivgi/kling-v3.0-pro/image-to-video" in ids
        assert "openai/gpt-4o" not in ids  # chat excluded
        assert "google/nano-banana-2/text-to-image" not in ids  # image excluded

    def test_list_all_returns_every_type(self, client):
        models = client.list_models()
        assert len(models) == len(CATALOG_DATA)

    def test_model_fields_parsed(self, client):
        models = {m.id: m for m in client.list_models(type="video")}
        seedance = models[SEEDANCE_ID]
        assert seedance.type == "video"
        assert seedance.vendor == "bytedance"
        assert "image" in seedance.input_modalities
        assert "reference" in seedance.tags

    def test_model_exists_true_and_false(self, client):
        assert client.model_exists(SEEDANCE_ID, type="video") is True
        assert client.model_exists("nope/does-not-exist", type="video") is False


class TestGetModelSchema:
    def test_static_schema_fallback_for_aiproxy_envelope(self, client):
        # /v1/models/<seedance> returns an envelope (no params) → static schema.
        schema = client.get_model_schema(SEEDANCE_ID)
        assert schema.id == SEEDANCE_ID
        assert "model" not in schema.params  # skipped
        assert "prompt" in schema.params
        assert schema.params["prompt"].required is True
        assert schema.params["duration"].values == ["4", "5", "10", "15"]
        assert schema.params["duration"].default == 5
        assert schema.params["resolution"].values == ["480p", "720p", "1080p"]
        assert schema.params["aspect_ratio"].values == ["16:9", "9:16", "1:1"]

    def test_raw_model_schema_used_directly(self, client):
        schema = client.get_model_schema("rich/text-to-video")
        assert "model" not in schema.params
        assert schema.params["prompt"].required is True
        assert schema.params["duration"].min == 3
        assert schema.params["duration"].max == 15

    def test_unknown_model_raises_not_found(self, client):
        with pytest.raises(MediaCatalogError) as exc_info:
            client.get_model_schema("ghost/model")
        assert exc_info.value.code == ERR_MODEL_NOT_FOUND

    def test_empty_model_id_is_invalid(self, client):
        with pytest.raises(MediaCatalogError):
            client.get_model_schema("  ")


class TestRecommend:
    def test_keyword_and_tag_ranking(self, client):
        results = client.recommend("reference identity video", type="video")
        assert results, "expected at least one recommendation"
        top_model, reason = results[0]
        assert top_model.id == SEEDANCE_ID
        assert "reference" in reason or "identity" in reason

    def test_recommend_respects_type(self, client):
        results = client.recommend("gpt chat", type="video")
        assert all(model.type == "video" for model, _ in results)
