"""Tests for the bundled NVIDIA NIM image_gen plugin."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

nvidia_nim_plugin = importlib.import_module("plugins.image_gen.nvidia-nim")


_PNG_HEX = (
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


def _b64_png() -> str:
    import base64

    return base64.b64encode(bytes.fromhex(_PNG_HEX)).decode()


def _fake_response(*, b64=None, url=None, revised_prompt=None):
    item = SimpleNamespace(b64_json=b64, url=url, revised_prompt=revised_prompt)
    return SimpleNamespace(data=[item])


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    return nvidia_nim_plugin.NvidiaNimImageGenProvider()


class TestMetadata:
    def test_name(self, provider):
        assert provider.name == "nvidia-nim"
        assert provider.display_name == "NVIDIA NIM"

    def test_default_model(self, provider):
        assert provider.default_model() == "black-forest-labs/flux.1-schnell"

    def test_list_models(self, provider):
        models = provider.list_models()
        assert len(models) == 7
        ids = [m["id"] for m in models]
        assert "black-forest-labs/flux.1-schnell" in ids
        assert "qwen/qwen-image-edit" in ids

    def test_catalog_entries_have_fields(self, provider):
        for entry in provider.list_models():
            assert entry["display"]
            assert entry["speed"]
            assert entry["strengths"]
            assert entry["price"] == "free credits"


class TestAvailability:
    def test_no_api_key_unavailable(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        assert nvidia_nim_plugin.NvidiaNimImageGenProvider().is_available() is False

    def test_api_key_set_available(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
        assert nvidia_nim_plugin.NvidiaNimImageGenProvider().is_available() is True


class TestModelResolution:
    def test_env_var_override(self, monkeypatch):
        monkeypatch.setenv("NVIDIA_IMAGE_MODEL", "qwen/qwen-image")
        model_id, meta = nvidia_nim_plugin._resolve_model()
        assert model_id == "qwen/qwen-image"
        assert meta["display"] == "Qwen-Image"

    def test_config_model(self, tmp_path):
        import yaml

        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({
                "image_gen": {"nvidia_nim": {"model": "qwen/qwen-image-2512"}}
            })
        )
        model_id, meta = nvidia_nim_plugin._resolve_model()
        assert model_id == "qwen/qwen-image-2512"

    def test_fallback_default(self):
        model_id, meta = nvidia_nim_plugin._resolve_model()
        assert model_id == "black-forest-labs/flux.1-schnell"


class TestGenerate:
    def test_requires_prompt(self, provider):
        res = provider.generate("")
        assert res["success"] is False
        assert res["error_type"] == "invalid_argument"

    def test_requires_key(self, monkeypatch):
        monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
        provider = nvidia_nim_plugin.NvidiaNimImageGenProvider()
        res = provider.generate("a cat")
        assert res["success"] is False
        assert res["error_type"] == "auth_required"

    @patch.object(nvidia_nim_plugin, "save_b64_image")
    def test_generate_success(self, mock_save, provider):
        mock_client = MagicMock()
        mock_client.images.generate.return_value = _fake_response(b64=_b64_png())
        mock_save.return_value = "/tmp/cache/img.png"

        fake_openai = MagicMock()
        fake_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": fake_openai}):
            res = provider.generate("a red apple")
            assert res["success"] is True
            assert res["provider"] == "nvidia-nim"
            assert res["model"] == "black-forest-labs/flux.1-schnell"
            assert res["modality"] == "text"
            assert res["image"] == "/tmp/cache/img.png"

    def test_generate_editing_unsupported(self, provider, monkeypatch):
        monkeypatch.setenv("NVIDIA_IMAGE_MODEL", "black-forest-labs/flux.1-schnell")
        res = provider.generate("edit", image_url="https://example.com/a.png")
        assert res["success"] is False
        assert res["error_type"] == "model_mismatch"


class TestRegistration:
    def test_register(self):
        ctx = MagicMock()
        nvidia_nim_plugin.register(ctx)
        ctx.register_image_gen_provider.assert_called_once()
        instance = ctx.register_image_gen_provider.call_args[0][0]
        assert isinstance(instance, nvidia_nim_plugin.NvidiaNimImageGenProvider)
