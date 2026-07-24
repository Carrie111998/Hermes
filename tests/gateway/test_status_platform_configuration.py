"""Parity and safety contracts for import-free status platform evaluation."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest
import yaml


def _write_yaml(home, data) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )


def _write_json(home, data) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "gateway.json").write_text(
        json.dumps(data),
        encoding="utf-8",
    )


def _set_path(target: dict, path: str, value) -> None:
    current = target
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _synthetic_block(spec, *, enabled=True) -> dict:
    block: dict = {"enabled": enabled}
    if spec.any_of:
        for evidence in spec.any_of[0]:
            assert evidence.config_paths
            value = "x" * max(1, evidence.min_length)
            _set_path(block, evidence.config_paths[0], value)
    return block


def test_builtin_specs_cover_every_current_builtin_platform():
    from gateway.config import Platform
    from gateway.platform_configuration import BUILTIN_PLATFORM_SPECS

    expected = {
        member.value for member in Platform.__members__.values()
        if member is not Platform.LOCAL
    }
    expected.update(Platform._scan_bundled_plugin_platforms())

    assert expected <= BUILTIN_PLATFORM_SPECS.keys()


@pytest.mark.parametrize(
    ("name", "block", "env", "expected"),
    [
        ("telegram", {"enabled": True}, {}, "disabled"),
        ("telegram", {"enabled": True, "token": "token"}, {}, "configured"),
        (
            "telegram",
            {"enabled": False, "token": "token"},
            {"TELEGRAM_BOT_TOKEN": "env-token"},
            "disabled",
        ),
        (
            "telegram",
            {"enabled": True, "extra": {"require_mention": True}},
            {},
            "disabled",
        ),
        (
            "matrix",
            {
                "enabled": True,
                "extra": {"homeserver": "https://matrix.example", "password": "pw"},
            },
            {},
            "configured",
        ),
        (
            "api_server",
            {"enabled": True, "extra": {"key": "short"}},
            {},
            "disabled",
        ),
        (
            "api_server",
            {"enabled": True, "extra": {"key": "0123456789abcdef"}},
            {},
            "configured",
        ),
        (
            "api_server",
            {"enabled": True, "extra": {"key": "your_api_key_here"}},
            {},
            "disabled",
        ),
    ],
)
def test_static_contract_rejects_declarations_without_required_evidence(
    name, block, env, expected
):
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    state = evaluate_static_configuration(
        block,
        BUILTIN_PLATFORM_SPECS[name],
        getenv=env.get,
    )

    assert state is StaticConfigurationState(expected)


def test_status_sources_deep_merge_extra_with_canonical_precedence(
    tmp_path, monkeypatch
):
    from gateway.config import Platform, load_gateway_config
    from gateway.platform_configuration import load_platform_blocks

    _write_json(
        tmp_path,
        {
            "platforms": {
                "matrix": {
                    "enabled": True,
                    "extra": {
                        "homeserver": "https://json.example",
                        "nested": {"json": 1, "winner": "json"},
                    },
                }
            }
        },
    )
    _write_yaml(
        tmp_path,
        {
            "gateway": {
                "platforms": {
                    "matrix": {
                        "extra": {
                            "nested": {"gateway": 2, "winner": "gateway"},
                        }
                    }
                }
            },
            "platforms": {
                "matrix": {
                    "extra": {
                        "nested": {"top": 3, "winner": "top"},
                        "password": "pw",
                    }
                }
            },
        },
    )

    loaded = load_platform_blocks(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    full = load_gateway_config()

    assert loaded.blocks["matrix"] == {
        "enabled": True,
        "extra": {
            "homeserver": "https://json.example",
            "password": "pw",
            "nested": {
                "json": 1,
                "gateway": 2,
                "top": 3,
                "winner": "top",
            },
        },
    }
    assert full.platforms[Platform.MATRIX].extra["nested"] == {
        "json": 1,
        "gateway": 2,
        "top": 3,
        "winner": "top",
    }


@pytest.mark.parametrize("broken_name", ["gateway.json", "config.yaml"])
def test_malformed_source_does_not_hide_valid_fallback(tmp_path, broken_name):
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        load_static_platform_states,
    )

    if broken_name == "gateway.json":
        (tmp_path / "gateway.json").write_text("{not-json", encoding="utf-8")
        _write_yaml(
            tmp_path,
            {"platforms": {"telegram": {"enabled": True, "token": "yaml-token"}}},
        )
    else:
        _write_json(
            tmp_path,
            {"platforms": {"telegram": {"enabled": True, "token": "json-token"}}},
        )
        (tmp_path / "config.yaml").write_text("platforms: [unterminated", encoding="utf-8")

    states = load_static_platform_states(
        tmp_path,
        ["telegram"],
        specs=BUILTIN_PLATFORM_SPECS,
        getenv={}.get,
    )

    assert states["telegram"] is StaticConfigurationState.CONFIGURED


def test_unknown_third_party_is_not_promoted_from_runtime_observation(tmp_path):
    from gateway.platform_configuration import (
        StaticConfigurationState,
        load_static_platform_states,
    )

    states = load_static_platform_states(
        tmp_path,
        ["third_party_chat"],
        specs={},
        getenv={}.get,
    )

    assert states == {"third_party_chat": StaticConfigurationState.UNKNOWN}


def test_third_party_requires_complete_declarative_metadata():
    from gateway.platform_configuration import (
        ConfigurationEvidence,
        PlatformConfigurationSpec,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    complete = PlatformConfigurationSpec(
        any_of=(
            (
                ConfigurationEvidence(
                    config_paths=("extra.endpoint",),
                    env_vars=("THIRD_PARTY_ENDPOINT",),
                ),
                ConfigurationEvidence(
                    config_paths=("api_key",),
                    env_vars=("THIRD_PARTY_API_KEY",),
                ),
            ),
        ),
    )
    incomplete = replace(complete, complete=False)
    block = {
        "enabled": True,
        "api_key": "secret",
        "extra": {"endpoint": "https://plugin.example"},
    }

    assert (
        evaluate_static_configuration(block, complete, getenv={}.get)
        is StaticConfigurationState.CONFIGURED
    )
    assert (
        evaluate_static_configuration(block, incomplete, getenv={}.get)
        is StaticConfigurationState.UNKNOWN
    )


def test_registered_third_party_metadata_is_shared_without_callback_resolution(
    tmp_path, monkeypatch
):
    from gateway.config import Platform, load_gateway_config, load_status_platform_states
    from gateway.platform_configuration import (
        ConfigurationEvidence,
        PlatformConfigurationSpec,
        StaticConfigurationState,
    )
    from gateway.platform_registry import PlatformEntry, platform_registry

    spec = PlatformConfigurationSpec(
        any_of=(
            (
                ConfigurationEvidence(env_vars=("THIRD_PARTY_ENDPOINT",)),
                ConfigurationEvidence(env_vars=("THIRD_PARTY_API_KEY",)),
            ),
        ),
    )
    platform_registry.register(
        PlatformEntry(
            name="third_party_chat",
            label="Third Party Chat",
            adapter_factory=lambda config: None,
            check_fn=lambda: True,
            static_configuration=spec,
            source="plugin",
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("THIRD_PARTY_ENDPOINT", "https://plugin.example")
    monkeypatch.setenv("THIRD_PARTY_API_KEY", "secret")
    try:
        full = load_gateway_config()
        states = load_status_platform_states(["third_party_chat"])
        platform = Platform("third_party_chat")

        assert full.get_connected_platforms() == [platform]
        assert states["third_party_chat"] is StaticConfigurationState.CONFIGURED
    finally:
        platform_registry.unregister("third_party_chat")


def test_case_variant_and_removed_plugin_records_remain_unknown(tmp_path):
    from gateway.platform_configuration import (
        StaticConfigurationState,
        load_static_platform_states,
    )

    states = load_static_platform_states(
        tmp_path,
        ["Telegram", "removed_plugin"],
        specs={},
        getenv={}.get,
    )

    assert states == {
        "Telegram": StaticConfigurationState.UNKNOWN,
        "removed_plugin": StaticConfigurationState.UNKNOWN,
    }


def test_builtin_full_loader_and_pure_evaluator_share_semantics(monkeypatch):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    env = {
        "SIGNAL_HTTP_URL": "http://127.0.0.1:8080",
        "SIGNAL_ACCOUNT": "+15555550100",
    }
    monkeypatch.setattr("gateway.config._getenv", env.get)
    config = PlatformConfig(
        enabled=True,
        extra={"http_url": env["SIGNAL_HTTP_URL"], "account": env["SIGNAL_ACCOUNT"]},
    )
    gateway_config = GatewayConfig(platforms={Platform.SIGNAL: config})

    static_state = evaluate_static_configuration(
        config,
        BUILTIN_PLATFORM_SPECS["signal"],
        getenv=env.get,
    )

    assert static_state is StaticConfigurationState.CONFIGURED
    assert gateway_config.get_connected_platforms() == [Platform.SIGNAL]


def test_every_builtin_rejects_credentialless_and_policy_only_declarations():
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    for name, spec in BUILTIN_PLATFORM_SPECS.items():
        if not spec.complete:
            continue
        expected = (
            StaticConfigurationState.CONFIGURED
            if not spec.any_of
            else StaticConfigurationState.DISABLED
        )
        for block in (
            {"enabled": True},
            {"enabled": True, "extra": {"require_mention": True}},
        ):
            assert (
                evaluate_static_configuration(block, spec, getenv={}.get)
                is expected
            ), name


def test_every_builtin_explicit_disable_wins_with_valid_evidence():
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    for name, spec in BUILTIN_PLATFORM_SPECS.items():
        if not spec.complete:
            continue
        block = _synthetic_block(spec, enabled=False)
        assert (
            evaluate_static_configuration(block, spec, getenv={}.get)
            is StaticConfigurationState.DISABLED
        ), name


def test_environment_only_evidence_is_a_static_readiness_projection(monkeypatch):
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    covered = set()
    for name, spec in BUILTIN_PLATFORM_SPECS.items():
        if not spec.any_of:
            continue
        group = next(
            (
                candidate
                for candidate in spec.any_of
                if all(evidence.env_vars for evidence in candidate)
            ),
            None,
        )
        if group is None:
            continue
        env = {}
        for evidence in group:
            env[evidence.env_vars[0]] = "x" * max(1, evidence.min_length)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        pure = evaluate_static_configuration({"enabled": True}, spec, getenv=env.get)

        assert pure is StaticConfigurationState.CONFIGURED, name
        covered.add(name)
        for key in env:
            monkeypatch.delenv(key)

    assert {
        "telegram",
        "matrix",
        "email",
        "teams",
        "google_chat",
        "photon",
    } <= covered


def test_managed_overlay_and_direct_source_follow_full_loader_precedence(
    tmp_path, monkeypatch
):
    from gateway.config import Platform, load_gateway_config
    from gateway.platform_configuration import load_platform_blocks
    from hermes_cli import managed_scope

    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "platforms": {
                    "matrix": {
                        "extra": {
                            "password": "managed-password",
                            "nested": {"managed": 4, "winner": "managed"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    _write_json(
        tmp_path,
        {
            "platforms": {
                "matrix": {
                    "enabled": True,
                    "extra": {
                        "homeserver": "https://json.example",
                        "nested": {"json": 1, "winner": "json"},
                    },
                }
            }
        },
    )
    _write_yaml(
        tmp_path,
        {
            "gateway": {
                "platforms": {
                    "matrix": {
                        "extra": {"nested": {"gateway": 2, "winner": "gateway"}}
                    }
                }
            },
            "platforms": {
                "matrix": {
                    "extra": {"nested": {"top": 3, "winner": "top"}}
                }
            },
            "matrix": {
                "extra": {"nested": {"direct": 5, "winner": "direct"}}
            },
        },
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    managed_scope.invalidate_managed_cache()

    blocks = load_platform_blocks(tmp_path)
    full = load_gateway_config()
    expected_nested = {
        "json": 1,
        "gateway": 2,
        "top": 3,
        "managed": 4,
        "winner": "managed",
    }

    assert blocks.blocks["matrix"]["extra"]["nested"] == expected_nested
    assert full.platforms[Platform.MATRIX].extra["nested"] == expected_nested
    assert blocks.blocks["matrix"]["extra"]["password"] == "managed-password"
    assert full.platforms[Platform.MATRIX].extra["password"] == "managed-password"


def test_status_never_resolves_deferred_plugin_callback(tmp_path):
    from gateway.config import load_status_platform_states
    from gateway.platform_configuration import StaticConfigurationState
    from gateway.platform_registry import platform_registry

    def forbidden_loader():
        raise AssertionError("readiness resolved a deferred plugin callback")

    platform_registry.register_deferred("callback_plugin", forbidden_loader)
    try:
        states = load_status_platform_states(["callback_plugin"])
    finally:
        platform_registry.unregister("callback_plugin")

    assert states["callback_plugin"] is StaticConfigurationState.UNKNOWN


def test_third_party_override_of_builtin_without_metadata_is_unknown():
    from gateway.config import load_status_platform_states
    from gateway.platform_configuration import StaticConfigurationState
    from gateway.platform_registry import PlatformEntry, platform_registry

    original = platform_registry.get_concrete_entry("telegram")
    platform_registry.register(
        PlatformEntry(
            name="telegram",
            label="Custom Telegram Override",
            adapter_factory=lambda config: None,
            check_fn=lambda: True,
            is_connected=lambda config: True,
            source="plugin",
        )
    )
    try:
        states = load_status_platform_states(["telegram"])
    finally:
        platform_registry.unregister("telegram")
        if original is not None:
            platform_registry.register(original)

    assert states["telegram"] is StaticConfigurationState.UNKNOWN


def test_bundled_plugin_registration_inherits_shared_static_spec():
    from gateway.platform_configuration import BUILTIN_PLATFORM_SPECS
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import PluginContext, PluginManifest

    class _Manager:
        _plugin_platform_names: set[str] = set()

    original = platform_registry.get_concrete_entry("telegram")
    context = PluginContext(
        PluginManifest(name="telegram-platform", source="bundled"),
        _Manager(),
    )
    try:
        context.register_platform(
            name="telegram",
            label="Telegram",
            adapter_factory=lambda config: None,
            check_fn=lambda: True,
        )
        entry = platform_registry.get_concrete_entry("telegram")

        assert entry is not None
        assert entry.static_configuration is BUILTIN_PLATFORM_SPECS["telegram"]
    finally:
        platform_registry.unregister("telegram")
        if original is not None:
            platform_registry.register(original)


def test_status_uses_secret_scope_instead_of_process_environment(monkeypatch):
    from agent.secret_scope import (
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )
    from gateway.config import load_status_platform_states
    from gateway.platform_configuration import StaticConfigurationState

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "wrong-profile-token")
    set_multiplex_active(True)
    token = set_secret_scope({"TELEGRAM_BOT_TOKEN": "scoped-token"})
    try:
        states = load_status_platform_states(["telegram"])
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)

    assert states["telegram"] is StaticConfigurationState.CONFIGURED


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", " TRUE "])
def test_platform_enable_env_accepts_shared_truthy_values(truthy):
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    state = evaluate_static_configuration(
        {},
        BUILTIN_PLATFORM_SPECS["webhook"],
        getenv={"WEBHOOK_ENABLED": truthy}.get,
    )

    assert state is StaticConfigurationState.CONFIGURED


def test_explicit_disable_wins_over_environment_enablement():
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    state = evaluate_static_configuration(
        {"enabled": False},
        BUILTIN_PLATFORM_SPECS["webhook"],
        getenv={"WEBHOOK_ENABLED": "true"}.get,
    )

    assert state is StaticConfigurationState.DISABLED


def test_full_loader_preserves_explicit_disable_against_env(
    tmp_path, monkeypatch
):
    from gateway.config import Platform, load_gateway_config

    _write_yaml(
        tmp_path,
        {"platforms": {"webhook": {"enabled": False}}},
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")

    loaded = load_gateway_config()

    assert loaded.platforms[Platform.WEBHOOK].enabled is False
    assert Platform.WEBHOOK not in loaded.get_connected_platforms()


def test_full_loader_preserves_parent_signal_http_url_contract():
    """Parent accepted enabled Signal with a URL; account was not required."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig

    config = GatewayConfig(
        platforms={Platform.SIGNAL: PlatformConfig(enabled=True, extra={"http_url": "http://signal"})}
    )

    assert config.get_connected_platforms() == [Platform.SIGNAL]


def test_readiness_uses_parent_compatible_signal_and_email_minima():
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    assert evaluate_static_configuration(
        {"enabled": True, "extra": {"http_url": "http://signal"}},
        BUILTIN_PLATFORM_SPECS["signal"],
        getenv={}.get,
    ) is StaticConfigurationState.CONFIGURED
    assert evaluate_static_configuration(
        {"enabled": True, "extra": {"address": "user@example.test"}},
        BUILTIN_PLATFORM_SPECS["email"],
        getenv={}.get,
    ) is StaticConfigurationState.CONFIGURED


def test_readiness_returns_unknown_for_whatsapp_callback_contract():
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
        evaluate_static_configuration,
    )

    assert evaluate_static_configuration(
        {"enabled": True, "extra": {"seeded": "value"}},
        BUILTIN_PLATFORM_SPECS["whatsapp"],
        getenv={}.get,
    ) is StaticConfigurationState.UNKNOWN


@pytest.mark.parametrize(
    ("platform_name", "extra"),
    [
        ("email", {"address": "user@example.test"}),
        ("whatsapp", {"seeded": "value"}),
    ],
)
def test_full_loader_never_allows_static_metadata_to_preempt_callback(
    platform_name, extra
):
    """The parent lane invokes its registered callback after built-in checks."""
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platform_configuration import PlatformConfigurationSpec
    from gateway.platform_registry import PlatformEntry, platform_registry

    original = platform_registry.get_concrete_entry(platform_name)
    platform_registry.register(
        PlatformEntry(
            name=platform_name, label="Callback contract", adapter_factory=lambda cfg: None,
            check_fn=lambda: True, is_connected=lambda cfg: bool(cfg.extra),
            static_configuration=PlatformConfigurationSpec(complete=False),
        )
    )
    try:
        loaded = GatewayConfig(
            platforms={Platform(platform_name): PlatformConfig(enabled=True, extra=extra)}
        )
        assert loaded.get_connected_platforms() == [Platform(platform_name)]
    finally:
        platform_registry.unregister(platform_name)
        if original is not None:
            platform_registry.register(original)


def test_forged_unbundled_builtin_metadata_is_unknown(tmp_path, monkeypatch):
    from gateway.config import load_status_platform_states
    from gateway.platform_configuration import (
        BUILTIN_PLATFORM_SPECS,
        StaticConfigurationState,
    )
    from gateway.platform_registry import PlatformEntry, platform_registry

    _write_yaml(tmp_path, {"platforms": {"telegram": {"enabled": True, "token": "token"}}})
    monkeypatch.setattr("gateway.config.get_hermes_home", lambda: tmp_path)
    original = platform_registry.get_concrete_entry("telegram")
    platform_registry.register(
        PlatformEntry(
            name="telegram", label="Forged", adapter_factory=lambda cfg: None,
            check_fn=lambda: True, static_configuration=BUILTIN_PLATFORM_SPECS["telegram"],
            source="plugin", readiness_trusted=False,
        )
    )
    try:
        states = load_status_platform_states(["telegram"])
    finally:
        platform_registry.unregister("telegram")
        if original is not None:
            platform_registry.register(original)

    assert states["telegram"] is StaticConfigurationState.UNKNOWN
