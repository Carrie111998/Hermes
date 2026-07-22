from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hermes_cli.config import DEFAULT_CONFIG
from session_bridge.config import (
    BridgeConfig,
    ClaudeVisibilityConfig,
    SidebarConfig,
    _ENV_NAMES,
)


def _load(
    path: Path,
    *,
    environ: dict[str, str] | None = None,
) -> BridgeConfig:
    return BridgeConfig.load(path=path, environ={} if environ is None else environ)


@pytest.mark.parametrize(
    "host",
    (
        "127.0.0.2",
        "127.1.2.3",
        "127.255.255.254",
        "0:0:0:0:0:0:0:1",
        "0000:0000:0000:0000:0000:0000:0000:0001",
    ),
)
@pytest.mark.parametrize("from_environment", (False, True))
def test_canonical_loopback_variants_are_accepted_without_a_toml_grant(
    tmp_path: Path,
    host: str,
    from_environment: bool,
) -> None:
    path = tmp_path / "session_bridge.toml"
    if from_environment:
        environ = {"HERMES_SESSION_BRIDGE_HOST": host}
    else:
        path.write_text(f'[service]\nhost = "{host}"\n', encoding="utf-8")
        environ = {}

    config = _load(path, environ=environ)

    assert config.service.host == host.lower()
    assert config.service.allow_non_loopback is False


@pytest.mark.parametrize(
    "host",
    (
        "0.0.0.0",
        "126.255.255.255",
        "128.0.0.0",
        "192.0.2.10",
        "::",
        "2001:db8::1",
        "127.0.0.1.example.com",
    ),
)
@pytest.mark.parametrize("from_environment", (False, True))
def test_non_loopback_hosts_still_require_an_explicit_toml_grant(
    tmp_path: Path,
    host: str,
    from_environment: bool,
) -> None:
    path = tmp_path / "session_bridge.toml"
    if from_environment:
        environ = {"HERMES_SESSION_BRIDGE_HOST": host}
    else:
        path.write_text(f'[service]\nhost = "{host}"\n', encoding="utf-8")
        environ = {}

    with pytest.raises(ValueError, match="non-loopback"):
        _load(path, environ=environ)


@pytest.mark.parametrize(
    "name",
    (
        "HERMES_SESSION_BRIDGE_CATALGO_ENABLED",
        "HERMES_SESSION_BRIDGE_SERVICE_PORT",
        "HERMES_SESSION_BRIDGE_UNSUPPORTED",
    ),
)
def test_unknown_bridge_environment_variables_are_rejected(
    tmp_path: Path,
    name: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(name)):
        _load(tmp_path / "missing.toml", environ={name: "true"})


def test_environment_cannot_grant_non_loopback_access(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicitly in TOML"):
        _load(
            tmp_path / "missing.toml",
            environ={
                "HERMES_SESSION_BRIDGE_HOST": "192.0.2.10",
                "HERMES_SESSION_BRIDGE_ALLOW_NON_LOOPBACK": "true",
            },
        )


def test_mcp_token_is_whitelisted_but_not_persisted_in_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: deepcopy(DEFAULT_CONFIG),
    )
    config = _load(
        tmp_path / "missing.toml",
        environ={"HERMES_SESSION_BRIDGE_TOKEN": "x" * 32},
    )

    assert config == BridgeConfig()
    assert not hasattr(config, "token")


def test_live_characterization_gate_is_allowlisted_but_not_persisted_in_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: deepcopy(DEFAULT_CONFIG),
    )

    config = _load(
        tmp_path / "missing.toml",
        environ={"HERMES_SESSION_BRIDGE_LIVE_TESTS": "1"},
    )

    assert config == BridgeConfig()
    assert not hasattr(config, "live_tests")


@pytest.mark.parametrize(
    "near_match",
    (
        "HERMES_SESSION_BRIDGE_LIVE_TESTS_EXTRA",
        "HERMES_SESSION_BRIDGE_LIVE_TEST",
        "HERMES_SESSION_BRIDGE_LIVE_TESTS_",
    ),
)
def test_live_characterization_gate_rejects_near_match_environment_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    near_match: str,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: deepcopy(DEFAULT_CONFIG),
    )

    with pytest.raises(
        ValueError,
        match=f"unknown session bridge environment variable: {near_match}",
    ):
        _load(
            tmp_path / "missing.toml",
            environ={near_match: "1"},
        )


_SIDEBAR_DEFAULTS = {
    "enabled": False,
    "continuous": False,
    "backfill_days": 30,
    "continuous_batch_limit": 5,
    "manual_batch_limit": 10,
    "lease_seconds": 300,
    "max_attempts": 5,
    "heartbeat_grace_seconds": 120,
}


def _load_with_sidebar(
    monkeypatch: pytest.MonkeyPatch,
    sidebar: object,
) -> BridgeConfig:
    document: dict[str, Any] = {"session_bridge": {"sidebar": sidebar}}
    snapshot = deepcopy(document)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: document,
    )

    config = BridgeConfig.load(environ={})

    assert document == snapshot
    return config


def test_sidebar_config_defaults_are_exact_disabled_and_environment_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DEFAULT_CONFIG["session_bridge"] == {"sidebar": _SIDEBAR_DEFAULTS}
    assert asdict(SidebarConfig()) == _SIDEBAR_DEFAULTS
    assert not any("SIDEBAR" in name for name in _ENV_NAMES)

    config = _load_with_sidebar(monkeypatch, {})

    assert config.sidebar == SidebarConfig()
    assert config.sidebar.enabled is False
    assert config.sidebar.continuous is False


def test_sidebar_config_loads_only_from_config_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = {
        "enabled": True,
        "continuous": True,
        "backfill_days": 14,
        "continuous_batch_limit": 3,
        "manual_batch_limit": 7,
        "lease_seconds": 300,
        "max_attempts": 5,
        "heartbeat_grace_seconds": 45,
    }

    config = _load_with_sidebar(monkeypatch, configured)

    assert asdict(config.sidebar) == configured


def test_unknown_sidebar_config_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        ValueError,
        match="unknown session_bridge.sidebar configuration key: typo",
    ):
        _load_with_sidebar(monkeypatch, {**_SIDEBAR_DEFAULTS, "typo": True})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("enabled", 1, "enabled must be a boolean"),
        ("continuous", "false", "continuous must be a boolean"),
        ("backfill_days", True, "backfill_days must be an integer"),
        ("backfill_days", -1, "backfill_days must be at least 0"),
        ("continuous_batch_limit", 0, "continuous_batch_limit must be at least 1"),
        ("continuous_batch_limit", 11, "continuous_batch_limit must be at most 10"),
        ("manual_batch_limit", True, "manual_batch_limit must be an integer"),
        ("manual_batch_limit", 11, "manual_batch_limit must be at most 10"),
        ("lease_seconds", 299, "lease_seconds must be exactly 300"),
        ("lease_seconds", True, "lease_seconds must be an integer"),
        ("max_attempts", 4, "max_attempts must be exactly 5"),
        ("max_attempts", True, "max_attempts must be an integer"),
        (
            "heartbeat_grace_seconds",
            -1,
            "heartbeat_grace_seconds must be at least 0",
        ),
    ),
)
def test_sidebar_config_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _load_with_sidebar(
            monkeypatch,
            {**_SIDEBAR_DEFAULTS, field: value},
        )


_CLAUDE_VISIBILITY_DEFAULTS = {
    "enabled": False,
    "continuous": False,
    "backfill_days": 30,
    "continuous_batch_limit": 1,
    "manual_batch_limit": 10,
    "lease_seconds": 300,
    "max_attempts": 5,
    "daily_registration_limit": 25,
    "reserved_cost_per_attempt_usd": Decimal("0.02"),
    "emergency_daily_cost_usd": Decimal("0.50"),
    "process_timeout_seconds": 120,
    "discovery_timeout_seconds": 30,
}


def _load_with_claude_visibility(
    monkeypatch: pytest.MonkeyPatch,
    claude_visibility: object,
) -> BridgeConfig:
    document: dict[str, Any] = {
        "session_bridge": {"claude_visibility": claude_visibility}
    }
    snapshot = deepcopy(document)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: document)

    config = BridgeConfig.load(environ={})

    assert document == snapshot
    return config


def test_omitted_claude_visibility_section_is_exact_safe_disabled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document: dict[str, Any] = {"session_bridge": {}}
    snapshot = deepcopy(document)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: document)

    config = BridgeConfig.load(environ={})

    assert document == snapshot
    assert asdict(config.claude_visibility) == _CLAUDE_VISIBILITY_DEFAULTS
    assert config.claude_visibility.enabled is False
    assert config.claude_visibility.continuous is False


def test_claude_visibility_defaults_are_exact_disabled_and_environment_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not any("CLAUDE_VISIBILITY" in name for name in _ENV_NAMES)

    config = _load_with_claude_visibility(monkeypatch, {})

    assert asdict(config.claude_visibility) == _CLAUDE_VISIBILITY_DEFAULTS
    assert config.claude_visibility.enabled is False
    assert config.claude_visibility.continuous is False
    with pytest.raises(FrozenInstanceError):
        config.claude_visibility.enabled = True
    assert isinstance(config.claude_visibility, ClaudeVisibilityConfig)


def test_claude_visibility_config_parses_every_valid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = {
        "enabled": True,
        "continuous": True,
        "backfill_days": 14,
        "continuous_batch_limit": 1,
        "manual_batch_limit": 8,
        "lease_seconds": 600,
        "max_attempts": 7,
        "daily_registration_limit": 40,
        "reserved_cost_per_attempt_usd": "0.03",
        "emergency_daily_cost_usd": "0.75",
        "process_timeout_seconds": 180,
        "discovery_timeout_seconds": 45,
    }

    config = _load_with_claude_visibility(monkeypatch, configured)

    assert asdict(config.claude_visibility) == {
        **configured,
        "reserved_cost_per_attempt_usd": Decimal("0.03"),
        "emergency_daily_cost_usd": Decimal("0.75"),
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("enabled", 1, "enabled must be a boolean"),
        ("continuous", "false", "continuous must be a boolean"),
        ("backfill_days", 0, "backfill_days must be at least 1"),
        ("backfill_days", -1, "backfill_days must be at least 1"),
        ("continuous_batch_limit", 0, "continuous_batch_limit must be at least 1"),
        ("continuous_batch_limit", -1, "continuous_batch_limit must be at least 1"),
        ("continuous_batch_limit", 2, "continuous_batch_limit must be exactly 1"),
        ("manual_batch_limit", 0, "manual_batch_limit must be at least 1"),
        ("manual_batch_limit", -1, "manual_batch_limit must be at least 1"),
        ("lease_seconds", 0, "lease_seconds must be at least 1"),
        ("lease_seconds", -1, "lease_seconds must be at least 1"),
        ("max_attempts", 0, "max_attempts must be at least 1"),
        ("max_attempts", -1, "max_attempts must be at least 1"),
        ("daily_registration_limit", 0, "daily_registration_limit must be at least 1"),
        ("daily_registration_limit", -1, "daily_registration_limit must be at least 1"),
        ("process_timeout_seconds", 0, "process_timeout_seconds must be at least 1"),
        ("process_timeout_seconds", -1, "process_timeout_seconds must be at least 1"),
        (
            "discovery_timeout_seconds",
            0,
            "discovery_timeout_seconds must be at least 1",
        ),
        (
            "discovery_timeout_seconds",
            -1,
            "discovery_timeout_seconds must be at least 1",
        ),
        (
            "reserved_cost_per_attempt_usd",
            "0",
            "reserved_cost_per_attempt_usd must be greater than 0",
        ),
        (
            "reserved_cost_per_attempt_usd",
            "-0.01",
            "reserved_cost_per_attempt_usd must be greater than 0",
        ),
        (
            "emergency_daily_cost_usd",
            "0",
            "emergency_daily_cost_usd must be greater than 0",
        ),
        (
            "emergency_daily_cost_usd",
            "-0.01",
            "emergency_daily_cost_usd must be greater than 0",
        ),
        (
            "reserved_cost_per_attempt_usd",
            "NaN",
            "reserved_cost_per_attempt_usd must be finite",
        ),
        (
            "reserved_cost_per_attempt_usd",
            "Infinity",
            "reserved_cost_per_attempt_usd must be finite",
        ),
        (
            "reserved_cost_per_attempt_usd",
            "not-money",
            "reserved_cost_per_attempt_usd must be a decimal number",
        ),
        (
            "emergency_daily_cost_usd",
            "NaN",
            "emergency_daily_cost_usd must be finite",
        ),
        (
            "emergency_daily_cost_usd",
            "Infinity",
            "emergency_daily_cost_usd must be finite",
        ),
        (
            "emergency_daily_cost_usd",
            "not-money",
            "emergency_daily_cost_usd must be a decimal number",
        ),
        (
            "reserved_cost_per_attempt_usd",
            "0.0000001",
            "reserved_cost_per_attempt_usd supports at most 6 decimal places",
        ),
        (
            "emergency_daily_cost_usd",
            "1000000.000001",
            "emergency_daily_cost_usd cannot exceed 1000000 USD",
        ),
        (
            "emergency_daily_cost_usd",
            "1e1000000",
            "emergency_daily_cost_usd cannot exceed 1000000 USD",
        ),
    ),
)
def test_claude_visibility_config_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _load_with_claude_visibility(
            monkeypatch,
            {**_CLAUDE_VISIBILITY_DEFAULTS, field: value},
        )
