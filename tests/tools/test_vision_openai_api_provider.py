"""``openai-api`` must reach native vision, same as every other OpenAI id.

Regression for a weekly image-curation job that ran with
``provider: openai-api`` (api-key access to api.openai.com) on a
vision-capable model. Both capability gates missed the id — it was absent
from ``PROVIDER_TO_MODELS_DEV`` and from the media-in-tool-results allowlist
— so every ``vision_analyze`` call was demoted to the auxiliary text model
instead of attaching the pixels to the main model. Crop reads were the
visible casualty: they only exist to be looked at.
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import patch

import pytest

from agent.image_routing import decide_image_input_mode, _lookup_supports_vision
from agent.models_dev import PROVIDER_TO_MODELS_DEV, get_model_capabilities
from tools.vision_tools import (
    _handle_vision_analyze,
    _should_use_native_vision_fast_path,
    _supports_media_in_tool_results,
)


_MODEL = "gpt-5.6-luna"

# Minimal valid 1x1 PNG bytes.
_TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Shape mirrors a real models.dev entry: vision is read off modalities.input.
_FAKE_CATALOG = {
    "openai": {
        "models": {
            _MODEL: {
                "tool_call": True,
                "reasoning": True,
                "modalities": {"input": ["text", "image"]},
                "limit": {"context": 1050000, "output": 128000},
                "family": "gpt-luna",
            }
        }
    }
}


# ─── models.dev provider alias ───────────────────────────────────────────────


class TestModelsDevAlias:
    def test_openai_api_maps_to_openai(self):
        assert PROVIDER_TO_MODELS_DEV.get("openai-api") == "openai"
        assert PROVIDER_TO_MODELS_DEV.get("openai-codex") == "openai"

    def test_capability_lookup_resolves(self):
        """Regression: this returned None, so vision routing saw "unknown"."""
        with patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG):
            caps = get_model_capabilities("openai-api", _MODEL)
        assert caps is not None
        assert caps.supports_vision is True

    def test_capability_lookup_matches_the_openai_id(self):
        with patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG):
            via_alias = get_model_capabilities("openai-api", _MODEL)
            via_canonical = get_model_capabilities("openai", _MODEL)
        assert via_alias == via_canonical

    def test_unmapped_provider_still_returns_none(self):
        """The alias is a targeted addition, not a blanket fallthrough."""
        with patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG):
            assert get_model_capabilities("not-a-provider", _MODEL) is None


# ─── media-in-tool-results allowlist ─────────────────────────────────────────


class TestSupportsMediaInToolResults:
    def test_openai_api_yes(self):
        assert _supports_media_in_tool_results("openai-api", _MODEL) is True

    def test_case_and_whitespace_tolerant(self):
        assert _supports_media_in_tool_results("  OpenAI-API  ", _MODEL) is True

    def test_unknown_provider_still_no(self):
        assert _supports_media_in_tool_results("some-local-relay", _MODEL) is False


# ─── the routing decision ────────────────────────────────────────────────────


class TestImageInputMode:
    def test_openai_api_routes_native(self):
        with patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG):
            assert _lookup_supports_vision("openai-api", _MODEL, {}) is True
            assert decide_image_input_mode("openai-api", _MODEL, {}) == "native"

    def test_explicit_aux_vision_config_does_not_preempt_native(self):
        """The cron's config sets auxiliary.vision; it is a fallback, not a veto."""
        cfg = {"auxiliary": {"vision": {"provider": "openai-api", "model": "gpt-4o-mini"}}}
        with patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG):
            assert decide_image_input_mode("openai-api", _MODEL, cfg) == "native"


# ─── the gate the cron actually hit ──────────────────────────────────────────


class TestNativeFastPathGate:
    def test_fast_path_selected_under_openai_api(self):
        with (
            patch("agent.auxiliary_client._read_main_provider", return_value="openai-api"),
            patch("agent.auxiliary_client._read_main_model", return_value=_MODEL),
            patch("hermes_cli.config.load_config", return_value={}),
            patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG),
        ):
            assert _should_use_native_vision_fast_path() is True


class TestRegionReadReachesNativeInput:
    """The reproduced failure: a region crop under ``openai-api``.

    Acceptance is that the crop arrives as image input for the main model and
    the auxiliary vision model is never called at all.
    """

    @pytest.mark.asyncio
    async def test_region_crop_returns_multimodal_envelope(self, tmp_path):
        pytest.importorskip("PIL", reason="region cropping requires Pillow")
        from PIL import Image

        img = tmp_path / "candidate.png"
        Image.new("RGB", (200, 120), (40, 40, 90)).save(img, format="PNG")

        async def _boom(*args, **kwargs):
            raise AssertionError("auxiliary vision model must not be called")

        with (
            patch("agent.auxiliary_client._read_main_provider", return_value="openai-api"),
            patch("agent.auxiliary_client._read_main_model", return_value=_MODEL),
            patch("hermes_cli.config.load_config", return_value={}),
            patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG),
            patch("tools.vision_tools.async_call_llm", new=_boom),
        ):
            result = await _handle_vision_analyze({
                "image_url": str(img),
                "question": "Full-size read: banding, artifacting, smearing.",
                "region": [20, 20, 140, 100],
            })

        assert isinstance(result, dict), result
        assert result.get("_multimodal") is True
        parts = result["content"]
        assert [p["type"] for p in parts] == ["text", "image_url"]
        assert parts[1]["image_url"]["url"].startswith("data:image/")
        # The crop offset has to survive, or reported coordinates are wrong.
        assert "offset (20, 20)" in parts[0]["text"]

    @pytest.mark.asyncio
    async def test_whole_image_also_reaches_native_input(self, tmp_path):
        img = tmp_path / "candidate.png"
        img.write_bytes(_TINY_PNG)

        async def _boom(*args, **kwargs):
            raise AssertionError("auxiliary vision model must not be called")

        with (
            patch("agent.auxiliary_client._read_main_provider", return_value="openai-api"),
            patch("agent.auxiliary_client._read_main_model", return_value=_MODEL),
            patch("hermes_cli.config.load_config", return_value={}),
            patch("agent.models_dev.fetch_models_dev", return_value=_FAKE_CATALOG),
            patch("tools.vision_tools.async_call_llm", new=_boom),
        ):
            result = await _handle_vision_analyze({
                "image_url": str(img),
                "question": "Thumbnail read: does the composition stay legible?",
            })

        assert isinstance(result, dict), result
        assert result.get("_multimodal") is True
