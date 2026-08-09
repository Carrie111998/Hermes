"""Tests for plugin image_gen providers injecting themselves into the picker.

Covers `_plugin_image_gen_providers`, `_visible_providers`, and
`_toolset_needs_configuration_prompt` handling of plugin providers.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import image_gen_registry
from agent.image_gen_provider import ImageGenProvider


class _FakeProvider(ImageGenProvider):
    def __init__(self, name: str, available: bool = True, schema=None, models=None):
        self._name = name
        self._available = available
        self._schema = schema or {
            "name": name.title(),
            "badge": "test",
            "tag": f"{name} test tag",
            "env_vars": [{"key": f"{name.upper()}_API_KEY", "prompt": f"{name} key"}],
        }
        self._models = models or [
            {"id": f"{name}-model-v1", "display": f"{name} v1",
             "speed": "~5s", "strengths": "test", "price": "$"},
        ]

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def list_models(self):
        return list(self._models)

    def default_model(self):
        return self._models[0]["id"] if self._models else None

    def get_setup_schema(self):
        return dict(self._schema)

    def generate(self, prompt, aspect_ratio="landscape", **kw):
        return {"success": True, "image": f"{self._name}://{prompt}"}


@pytest.fixture(autouse=True)
def _reset_registry():
    image_gen_registry._reset_for_tests()
    yield
    image_gen_registry._reset_for_tests()


class TestPluginPickerInjection:
    def test_plugin_providers_returns_registered(self, monkeypatch):
        from hermes_cli import tools_config

        image_gen_registry.register_provider(_FakeProvider("myimg"))

        rows = tools_config._plugin_image_gen_providers()
        names = [r["name"] for r in rows]
        plugin_names = [r.get("image_gen_plugin_name") for r in rows]

        assert "Myimg" in names
        assert "myimg" in plugin_names


    def test_visible_providers_includes_plugins_for_image_gen(self, monkeypatch):
        from hermes_cli import tools_config

        image_gen_registry.register_provider(_FakeProvider("someimg"))

        cat = tools_config.TOOL_CATEGORIES["image_gen"]
        visible = tools_config._visible_providers(cat, {})
        plugin_names = [p.get("image_gen_plugin_name") for p in visible if p.get("image_gen_plugin_name")]
        assert "someimg" in plugin_names


    def test_post_setup_omitted_when_not_declared(self, monkeypatch):
        from hermes_cli import tools_config

        image_gen_registry.register_provider(_FakeProvider("plain_img"))

        rows = tools_config._plugin_image_gen_providers()
        match = next(r for r in rows if r.get("image_gen_plugin_name") == "plain_img")
        assert "post_setup" not in match


class TestPluginCatalog:
    def test_plugin_catalog_returns_models(self):
        from hermes_cli import tools_config

        image_gen_registry.register_provider(_FakeProvider("catimg"))

        catalog, default = tools_config._plugin_image_gen_catalog("catimg")
        assert "catimg-model-v1" in catalog
        assert default == "catimg-model-v1"


class TestConfigPrompt:
    def test_image_gen_satisfied_by_plugin_provider(self, monkeypatch, tmp_path):
        """When a plugin provider reports is_available(), the picker should
        not force a setup prompt on the user."""
        from hermes_cli import tools_config

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("FAL_KEY", raising=False)

        image_gen_registry.register_provider(_FakeProvider("avail-img", available=True))

        assert tools_config._toolset_needs_configuration_prompt("image_gen", {}) is False


class TestConfigWriting:
    def test_picking_plugin_provider_writes_provider_and_model(self, monkeypatch, tmp_path):
        """When a user picks a plugin-backed image_gen provider with no
        env vars needed, ``_configure_provider`` should write both
        ``image_gen.provider`` and ``image_gen.model``."""
        from hermes_cli import tools_config

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(_FakeProvider("noenv", schema={
            "name": "NoEnv",
            "badge": "free",
            "tag": "",
            "env_vars": [],
        }))

        # Stub out the interactive model picker — no TTY in tests.
        monkeypatch.setattr(tools_config, "_prompt_choice", lambda *a, **kw: 0)

        config: dict = {}
        provider_row = {
            "name": "NoEnv",
            "env_vars": [],
            "image_gen_plugin_name": "noenv",
        }
        tools_config._configure_provider(provider_row, config)

        assert config["image_gen"]["provider"] == "noenv"
        assert config["image_gen"]["model"] == "noenv-model-v1"


    def test_plugin_provider_active_overrides_managed_nous_active_label(self, monkeypatch):
        from hermes_cli import tools_config

        monkeypatch.setattr(
            tools_config,
            "get_nous_subscription_features",
            lambda config, **kwargs: SimpleNamespace(
                features={"image_gen": SimpleNamespace(managed_by_nous=True)}
            ),
        )

        config = {"image_gen": {"provider": "openai", "use_gateway": False}}
        nous_row = {
            "name": "Nous Subscription",
            "managed_nous_feature": "image_gen",
        }
        openai_row = {
            "name": "OpenAI",
            "image_gen_plugin_name": "openai",
        }

        assert tools_config._is_provider_active(openai_row, config) is True
        assert tools_config._is_provider_active(nous_row, config) is False



class TestProviderSetupSchemaPassthrough:
    def test_rows_preserve_config_fields_and_readiness_check(self):
        from hermes_cli import tools_config

        fields = [
            {
                "key": "image_gen.external.endpoint",
                "prompt": "Provider endpoint",
                "required": True,
            },
        ]

        def readiness_check(config, get_secret):
            return "ready"

        image_gen_registry.register_provider(
            _FakeProvider(
                "config-fields-provider",
                schema={
                    "name": "External Image Provider",
                    "env_vars": [{"key": "EXTERNAL_IMAGE_KEY"}],
                    "config_fields": fields,
                    "readiness_check": readiness_check,
                },
            )
        )

        row = next(
            row
            for row in tools_config._plugin_image_gen_providers()
            if row["image_gen_plugin_name"] == "config-fields-provider"
        )

        assert row["config_fields"] == fields
        assert row["env_vars"] == [{"key": "EXTERNAL_IMAGE_KEY"}]
        assert row["readiness_check"] is readiness_check


class TestNonSecretProviderConfigFields:
    def test_setup_writes_normalized_fields_to_active_profile(
        self, monkeypatch, tmp_path
    ):
        from hermes_cli import tools_config
        from hermes_cli.config import read_raw_config

        profile_home = tmp_path / ".hermes" / "profiles" / "external"
        profile_home.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))

        def normalize_endpoint(value):
            if not value.startswith("https://"):
                raise ValueError("Endpoint must use HTTPS.")
            return value.rstrip("/")

        image_gen_registry.register_provider(
            _FakeProvider(
                "external-provider",
                schema={
                    "name": "External Image Provider",
                    "env_vars": [
                        {
                            "key": "EXTERNAL_IMAGE_KEY",
                            "prompt": "External image API key",
                            "password": True,
                            "required": False,
                        },
                    ],
                    "config_fields": [
                        {
                            "key": "image_gen.external.endpoint",
                            "prompt": "Provider endpoint",
                            "required": True,
                            "normalize": normalize_endpoint,
                        },
                        {
                            "key": "image_gen.external.deployment",
                            "prompt": "Image deployment",
                            "required": True,
                        },
                    ],
                },
            )
        )
        provider_row = next(
            row
            for row in tools_config._plugin_image_gen_providers()
            if row.get("image_gen_plugin_name") == "external-provider"
        )

        prompts = []
        values = iter([
            "",                       # optional secret skipped
            "http://invalid.example",  # rejected by normalize
            "https://images.example/",
            "image-deployment",
        ])

        def prompt(question, default=None, password=False):
            prompts.append((question, default, password))
            return next(values)

        monkeypatch.setattr(tools_config, "_prompt", prompt)
        monkeypatch.setattr(tools_config, "get_env_value", lambda key: None)
        monkeypatch.setattr(
            tools_config,
            "save_env_value",
            lambda *a, **kw: pytest.fail(
                "config fields must not use credential storage"
            ),
        )

        config = {}
        tools_config._configure_provider(provider_row, config)
        tools_config.save_config(config)

        saved = read_raw_config()
        assert saved["image_gen"]["external"] == {
            "endpoint": "https://images.example",
            "deployment": "image-deployment",
        }
        assert saved["image_gen"]["provider"] == "external-provider"
        assert prompts[0][2] is True
        assert all(password is False for _, _, password in prompts[1:])

    def test_reconfigure_updates_fields_and_keeps_blank_existing_value(
        self, monkeypatch
    ):
        from hermes_cli import tools_config

        answers = iter(["https://new.example", "", "v2"])
        prompts = []

        def prompt(question, default=None, password=False):
            prompts.append((question, default, password))
            return next(answers)

        monkeypatch.setattr(tools_config, "_prompt", prompt)

        config = {
            "image_gen": {
                "provider": "external-provider",
                "external": {
                    "endpoint": "https://old.example",
                    "deployment": "existing-deployment",
                },
            },
        }
        provider_row = {
            "name": "External Image Provider",
            "env_vars": [],
            "config_fields": [
                {"key": "image_gen.external.endpoint", "required": True},
                {"key": "image_gen.external.deployment", "required": True},
                {"key": "image_gen.external.api_version", "required": False},
            ],
            "image_gen_plugin_name": "external-provider",
        }

        tools_config._reconfigure_provider(provider_row, config)

        external = config["image_gen"]["external"]
        assert external["endpoint"] == "https://new.example"
        assert external["deployment"] == "existing-deployment"
        assert external["api_version"] == "v2"
        assert all(password is False for _, _, password in prompts)


def test_readiness_reports_needs_setup_until_required_fields_present(monkeypatch):
    from hermes_cli import tools_config

    credentials = {"EXTERNAL_IMAGE_KEY": None}
    monkeypatch.setattr(
        tools_config, "get_env_value", lambda key: credentials.get(key)
    )

    def readiness_check(config, get_secret):
        return "ready" if get_secret("EXTERNAL_IMAGE_KEY") else "needs_auth"

    provider = {
        "env_vars": [{"key": "EXTERNAL_IMAGE_KEY", "required": False}],
        "config_fields": [
            {"key": "image_gen.external.endpoint", "required": True},
        ],
        "readiness_check": readiness_check,
    }
    configured = {"image_gen": {"external": {"endpoint": "https://images.example"}}}

    assert tools_config.provider_readiness_status(provider, {}) == "needs_setup"
    assert (
        tools_config.provider_readiness_status(provider, configured) == "needs_auth"
    )

    credentials["EXTERNAL_IMAGE_KEY"] = "secret"
    assert tools_config.provider_readiness_status(provider, configured) == "ready"


def test_readiness_still_reports_needs_keys_for_required_secret(monkeypatch):
    from hermes_cli import tools_config

    monkeypatch.setattr(tools_config, "get_env_value", lambda key: None)

    provider = {"env_vars": [{"key": "EXTERNAL_IMAGE_KEY"}]}

    assert tools_config.provider_readiness_status(provider, {}) == "needs_keys"


def test_readiness_fails_closed_on_invalid_readiness_callback(monkeypatch):
    from hermes_cli import tools_config

    monkeypatch.setattr(tools_config, "get_env_value", lambda key: "set")

    provider = {
        "env_vars": [],
        "config_fields": [],
        "readiness_check": lambda config, get_secret: "unknown-status",
    }
    assert tools_config.provider_readiness_status(provider, {}) == "needs_setup"

    provider["readiness_check"] = lambda config, get_secret: 1 / 0
    assert tools_config.provider_readiness_status(provider, {}) == "needs_setup"
