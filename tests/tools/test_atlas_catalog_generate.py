"""Tests for the tool-layer atlas/aiproxy generation path (dynamic catalog).

Covers the bug fix: a catalog-only model (e.g. seedance-2.0 reference-to-video)
must be passed through to aiproxy verbatim — never silently swapped for
wan-2.6-flash — and unknown / absent / unvalidatable models fail closed with a
structured error. The atlas plugin is NOT involved (transport is imported, the
static family resolver is bypassed).
"""

from __future__ import annotations

import httpx
import pytest

from tools import atlas_catalog_generate
from tools.media_catalog import (
    ERR_CATALOG_UNAVAILABLE,
    MediaCatalogClient,
    MediaCatalogError,
)

API_ROOT = "http://aiproxy.test"
STATIC_BASE = "https://static.test/schema/"
SEEDANCE_ID = "bytedance/seedance-2.0/reference-to-video"

CATALOG_DATA = [
    {
        "id": SEEDANCE_ID,
        "media_type": "video",
        "vendor": "bytedance",
        "tags": ["reference", "identity"],
    },
    {"id": "kwaivgi/kling-v3.0-pro/image-to-video", "media_type": "video"},
]


def _catalog_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/catalog/models":
        type_filter = request.url.params.get("type")
        data = CATALOG_DATA
        if type_filter:
            data = [m for m in data if m.get("media_type") == type_filter]
        return httpx.Response(200, json={"code": 200, "msg": "succeed", "data": data})
    return httpx.Response(404, json={})


@pytest.fixture
def catalog_stub() -> MediaCatalogClient:
    return MediaCatalogClient(
        api_root=API_ROOT,
        api_key="test-key",
        http=httpx.Client(transport=httpx.MockTransport(_catalog_handler)),
        static_schema_base=STATIC_BASE,
    )


@pytest.fixture
def atlas_env(monkeypatch, catalog_stub):
    """Atlas key set + catalog client stubbed + transport captured."""
    monkeypatch.setenv("ATLAS_API_KEY", "test-key")
    monkeypatch.delenv("ATLAS_API_BASE", raising=False)
    monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
    monkeypatch.delenv("ATLAS_INTERNAL_ENV", raising=False)
    monkeypatch.delenv("ATLAS_VIDEO_MODEL", raising=False)
    monkeypatch.setattr(atlas_catalog_generate, "get_catalog_client", lambda: catalog_stub)

    captured = {}

    async def fake_submit(http, payload, *, api_key, api_root):
        captured["payload"] = payload
        captured["api_key"] = api_key
        captured["api_root"] = api_root
        return "pred-1"

    async def fake_poll(http, prediction_id, **kwargs):
        captured["prediction_id"] = prediction_id
        return {"status": "completed", "body": {"outputs": ["https://cdn.example/out.mp4"]}}

    from plugins.video_gen.atlas import client as atlas_client

    monkeypatch.setattr(atlas_client, "submit", fake_submit)
    monkeypatch.setattr(atlas_client, "poll", fake_poll)
    return captured


def _gen(**kwargs):
    kwargs.setdefault("model", None)
    kwargs.setdefault("video_gen_section", {})
    return atlas_catalog_generate.generate("a dog running", **kwargs)


class TestCatalogPassthrough:
    def test_seedance_r2v_is_not_swapped_to_flash(self, atlas_env):
        """The core bug fix: a catalog-only model is passed through verbatim."""
        result = _gen(model=SEEDANCE_ID)
        assert result["success"] is True
        assert result["video"] == "https://cdn.example/out.mp4"
        assert result["model"] == SEEDANCE_ID
        # Critically NOT the cheap default the plugin used to fall back to.
        assert atlas_env["payload"]["model"] == SEEDANCE_ID
        assert atlas_env["payload"]["model"] != "alibaba/wan-2.6/text-to-video"

    def test_passthrough_resolution_is_not_forced(self, atlas_env):
        result = _gen(model=SEEDANCE_ID, resolution="1080p", duration=10)
        assert result["success"] is True
        # Passthrough sends the requested resolution (aiproxy validates),
        # unlike the plugin's empty-family path which forced 720P.
        assert atlas_env["payload"]["resolution"] == "1080P"
        assert atlas_env["payload"]["duration"] == 10

    def test_unknown_model_fails_closed(self, atlas_env):
        result = _gen(model="ghost/does-not-exist")
        assert result["success"] is False
        assert result["error_type"] == "model_not_found"
        assert "payload" not in atlas_env  # never submitted

    def test_catalog_unreachable_fails_closed(self, monkeypatch):
        monkeypatch.setenv("ATLAS_API_KEY", "test-key")

        def _broken_client():
            raise MediaCatalogError(ERR_CATALOG_UNAVAILABLE, "backend down")

        monkeypatch.setattr(atlas_catalog_generate, "get_catalog_client", _broken_client)
        result = _gen(model="bytedance/seedance-2.5/reference-to-video")
        assert result["success"] is False
        assert result["error_type"] == "catalog_unavailable"

    def test_no_model_is_model_required(self, atlas_env):
        result = _gen(model=None, video_gen_section={})
        assert result["success"] is False
        assert result["error_type"] == "model_required"
        assert "payload" not in atlas_env


class TestLegacyFamilyBehaviorPreserved:
    def test_family_id_maps_and_clamps(self, atlas_env):
        result = _gen(model="wan-2.6-flash", duration=99, resolution="1080p")
        assert result["success"] is True
        # Family id → concrete text model, clamped by the family's constraints.
        assert atlas_env["payload"]["model"] == "alibaba/wan-2.6/text-to-video"
        assert atlas_env["payload"]["duration"] == 15  # clamped from 99
        assert atlas_env["payload"]["resolution"] == "720P"  # wan-flash is 720P only

    def test_full_model_id_wrong_modality_rejected(self, atlas_env):
        result = _gen(model="alibaba/wan-2.6/image-to-video-flash")  # no image_url → text
        assert result["success"] is False
        assert result["error_type"] == "modality_unsupported"
        assert "payload" not in atlas_env


class TestAuth:
    def test_missing_key_is_auth_required(self, monkeypatch):
        for var in ("ATLAS_API_KEY", "ATLAS_DEV_API_KEY", "LLM_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        result = _gen(model=SEEDANCE_ID)
        assert result["success"] is False
        assert result["error_type"] == "auth_required"


class TestToolWiring:
    """End-to-end: the video_generate entry point routes atlas to the catalog
    path and produces the requested model (not the silent flash fallback)."""

    def test_video_generate_routes_atlas_to_catalog_path(self, monkeypatch, catalog_stub):
        import json

        from plugins.video_gen.atlas import AtlasVideoGenProvider
        from plugins.video_gen.atlas import client as atlas_client
        from tools import video_generation_tool

        monkeypatch.setenv("ATLAS_API_KEY", "test-key")
        monkeypatch.delenv("ATLAS_API_BASE", raising=False)
        monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
        monkeypatch.delenv("ATLAS_INTERNAL_ENV", raising=False)
        monkeypatch.setattr(atlas_catalog_generate, "get_catalog_client", lambda: catalog_stub)
        monkeypatch.setattr(
            video_generation_tool, "_resolve_active_provider", lambda: AtlasVideoGenProvider()
        )
        monkeypatch.setattr(video_generation_tool, "_read_video_gen_section", lambda: {})

        captured = {}

        async def fake_submit(http, payload, *, api_key, api_root):
            captured["payload"] = payload
            return "pred-9"

        async def fake_poll(http, prediction_id, **kwargs):
            return {"status": "completed", "body": {"outputs": ["https://cdn.example/tool.mp4"]}}

        monkeypatch.setattr(atlas_client, "submit", fake_submit)
        monkeypatch.setattr(atlas_client, "poll", fake_poll)

        result = json.loads(
            video_generation_tool._handle_video_generate(
                {"prompt": "a dog running", "model": SEEDANCE_ID}
            )
        )
        assert result["success"] is True
        assert result["video"] == "https://cdn.example/tool.mp4"
        assert result["model"] == SEEDANCE_ID
        assert captured["payload"]["model"] == SEEDANCE_ID
        assert captured["payload"]["model"] != "alibaba/wan-2.6/text-to-video"

    def test_video_generate_atlas_unknown_model_errors_not_flash(self, monkeypatch, catalog_stub):
        import json

        from plugins.video_gen.atlas import AtlasVideoGenProvider
        from plugins.video_gen.atlas import client as atlas_client
        from tools import video_generation_tool

        monkeypatch.setenv("ATLAS_API_KEY", "test-key")
        monkeypatch.delenv("ATLAS_API_BASE", raising=False)
        monkeypatch.delenv("ATLAS_BASE_URL", raising=False)
        monkeypatch.delenv("ATLAS_INTERNAL_ENV", raising=False)
        monkeypatch.setattr(atlas_catalog_generate, "get_catalog_client", lambda: catalog_stub)
        monkeypatch.setattr(
            video_generation_tool, "_resolve_active_provider", lambda: AtlasVideoGenProvider()
        )
        monkeypatch.setattr(video_generation_tool, "_read_video_gen_section", lambda: {})

        submitted = {}

        async def fake_submit(http, payload, *, api_key, api_root):
            submitted["payload"] = payload
            return "pred-x"

        async def fake_poll(http, prediction_id, **kwargs):
            return {"status": "completed", "body": {"outputs": ["https://cdn.example/x.mp4"]}}

        monkeypatch.setattr(atlas_client, "submit", fake_submit)
        monkeypatch.setattr(atlas_client, "poll", fake_poll)

        result = json.loads(
            video_generation_tool._handle_video_generate(
                {"prompt": "a dog running", "model": "ghost/does-not-exist"}
            )
        )
        assert result["success"] is False
        assert result["error_type"] == "model_not_found"
        assert "payload" not in submitted  # never fell back to flash + submitted

