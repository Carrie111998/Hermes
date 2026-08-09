from __future__ import annotations

import json
import pytest

from agent import image_gen_registry
from agent.image_gen_provider import ImageGenProvider


@pytest.fixture(autouse=True)
def _reset_registry():
    image_gen_registry._reset_for_tests()
    yield
    image_gen_registry._reset_for_tests()


class _FakeCodexProvider(ImageGenProvider):
    @property
    def name(self) -> str:
        return "codex"

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        return {
            "success": True,
            "image": "/tmp/codex-test.png",
            "model": "gpt-5.2-codex",
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "provider": "codex",
        }


class TestPluginDispatch:
    def test_dispatch_routes_to_codex_provider(self, monkeypatch, tmp_path):
        from tools import image_generation_tool
        from agent import image_gen_registry as registry_module
        from hermes_cli import plugins as plugins_module

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text("image_gen:\n  provider: codex\n")
        image_gen_registry.register_provider(_FakeCodexProvider())

        monkeypatch.setattr(image_generation_tool, "_read_configured_image_provider", lambda: "codex")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda: None)
        monkeypatch.setattr(registry_module, "get_provider", lambda name: _FakeCodexProvider() if name == "codex" else None)

        dispatched = image_generation_tool._dispatch_to_plugin_provider("draw cat", "square")
        payload = json.loads(dispatched)

        assert payload["success"] is True
        assert payload["provider"] == "codex"
        assert payload["image"] == "/tmp/codex-test.png"
        assert payload["aspect_ratio"] == "square"


    def test_deepinfra_key_alone_does_not_select_image_backend(self, monkeypatch):
        """DeepInfra chat credentials do not imply consent to image billing."""
        from tools import image_generation_tool

        monkeypatch.setenv("DEEPINFRA_API_KEY", "«redacted:sk-…»")
        monkeypatch.delenv("FAL_KEY", raising=False)
        monkeypatch.setattr(image_generation_tool, "_read_configured_image_provider", lambda: None)
        assert image_generation_tool._dispatch_to_plugin_provider("a cat", "square") is None

    def test_requirements_ignore_unselected_paid_plugin(self, monkeypatch):
        from tools import image_generation_tool

        monkeypatch.setattr(image_generation_tool, "check_fal_api_key", lambda: False)
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_provider", lambda: None
        )
        assert image_generation_tool.check_image_generation_requirements() is False


class _MisconfiguredProvider(ImageGenProvider):
    """An explicitly selected provider whose credentials are missing."""

    @property
    def name(self) -> str:
        return "external-provider"

    def is_available(self) -> bool:
        return False

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        return {
            "success": False,
            "image": None,
            "error": "The selected image provider is not configured.",
            "error_type": "auth_required",
            "provider": self.name,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "setup_action": "Configure EXTERNAL_IMAGE_KEY with `hermes tools`.",
        }


class _ForbiddenFallbackProvider(ImageGenProvider):
    def __init__(self, name):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        raise AssertionError(f"{self.name} fallback must not be called")


class TestExplicitProviderFailsClosed:
    def test_misconfigured_provider_surfaces_its_error_without_fallback(
        self, monkeypatch
    ):
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        image_gen_registry.register_provider(_MisconfiguredProvider())
        image_gen_registry.register_provider(_ForbiddenFallbackProvider("openai"))
        image_gen_registry.register_provider(_ForbiddenFallbackProvider("fal"))

        monkeypatch.setattr(
            image_generation_tool,
            "_read_configured_image_provider",
            lambda: "external-provider",
        )
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_model", lambda: None
        )
        monkeypatch.setattr(
            plugins_module, "_ensure_plugins_discovered", lambda **kw: None
        )
        monkeypatch.setattr(
            image_generation_tool,
            "image_generate_tool",
            lambda **kw: (_ for _ in ()).throw(
                AssertionError("legacy FAL fallback must not be called")
            ),
        )

        payload = json.loads(
            image_generation_tool._handle_image_generate(
                {"prompt": "draw a fox", "aspect_ratio": "square"}
            )
        )

        assert payload["success"] is False
        assert payload["provider"] == "external-provider"
        assert payload["error_type"] == "auth_required"
        assert "EXTERNAL_IMAGE_KEY" in payload["setup_action"]

    def test_misconfigured_provider_remains_exposed(self, monkeypatch):
        """The tool stays available so the provider's own error reaches the user."""
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        image_gen_registry.register_provider(_MisconfiguredProvider())
        monkeypatch.setattr(
            image_generation_tool,
            "_read_configured_image_provider",
            lambda: "external-provider",
        )
        monkeypatch.setattr(
            plugins_module, "_ensure_plugins_discovered", lambda **kw: None
        )
        monkeypatch.setattr(
            image_generation_tool, "check_fal_api_key", lambda: False
        )

        assert image_generation_tool.check_image_generation_requirements() is True

    def test_discovery_failure_does_not_fall_through_to_another_backend(
        self, monkeypatch
    ):
        from tools import image_generation_tool
        from hermes_cli import plugins as plugins_module

        monkeypatch.setattr(
            image_generation_tool,
            "_read_configured_image_provider",
            lambda: "external-provider",
        )
        monkeypatch.setattr(
            image_generation_tool, "_read_configured_image_model", lambda: None
        )
        monkeypatch.setattr(
            plugins_module,
            "_ensure_plugins_discovered",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("discovery failed")),
        )
        monkeypatch.setattr(
            image_generation_tool,
            "_maybe_route_managed_krea",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("managed Krea fallback must not be called")
            ),
        )
        monkeypatch.setattr(
            image_generation_tool,
            "image_generate_tool",
            lambda **kw: (_ for _ in ()).throw(
                AssertionError("legacy FAL fallback must not be called")
            ),
        )

        payload = json.loads(
            image_generation_tool._handle_image_generate({"prompt": "draw a fox"})
        )

        assert payload["success"] is False
        assert payload["error_type"] == "provider_unavailable"
        assert "no fallback provider was attempted" in payload["error"]
