from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import tempfile
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import session_bridge.config as bridge_config
from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)

_DEFAULT_CONFIG_DIRECTORY = tempfile.TemporaryDirectory(
    prefix="session-bridge-config-"
)
_DEFAULT_CONFIG_HOME = Path(_DEFAULT_CONFIG_DIRECTORY.name).resolve()
_default_config_home_token = set_hermes_home_override(_DEFAULT_CONFIG_HOME)
try:
    config_module = importlib.import_module("hermes_cli.config")
    _DEFAULT_CONFIG_SNAPSHOT = config_module.effective_default_config()
finally:
    reset_hermes_home_override(_default_config_home_token)
from session_bridge.config import (
    BridgeConfig,
    ClaudeVisibilityConfig,
    SidebarConfig,
    _ENV_NAMES,
)


def test_bridge_config_explicit_home_scopes_toml_and_yaml_and_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient_home = tmp_path / "ambient"
    config_home = tmp_path / "root"
    ambient_home.mkdir()
    config_home.mkdir()
    (config_home / "session_bridge.toml").write_text(
        "[service]\nport = 8123\n", encoding="utf-8"
    )
    observed: list[Path] = []

    def load_yaml() -> dict[str, object]:
        observed.append(get_hermes_home())
        return {"session_bridge": {"sidebar": {"enabled": True}}}

    monkeypatch.setattr("hermes_cli.config.load_config", load_yaml)
    token = set_hermes_home_override(ambient_home)
    try:
        config = BridgeConfig.load(config_home=config_home, environ={})
        assert get_hermes_home() == ambient_home
    finally:
        reset_hermes_home_override(token)

    assert config.service.port == 8123
    assert config.sidebar.enabled is True
    assert observed == [config_home]


def _installed_default_bridge_config() -> BridgeConfig:
    return BridgeConfig(
        sidebar=SidebarConfig(
            inbox_cwd=str(_DEFAULT_CONFIG_HOME),
            readable_preview_enabled=True,
        )
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
        lambda: deepcopy(_DEFAULT_CONFIG_SNAPSHOT),
    )
    config = _load(
        tmp_path / "missing.toml",
        environ={"HERMES_SESSION_BRIDGE_TOKEN": "x" * 32},
    )

    assert config == _installed_default_bridge_config()
    assert not hasattr(config, "token")


def test_live_characterization_gate_is_allowlisted_but_not_persisted_in_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: deepcopy(_DEFAULT_CONFIG_SNAPSHOT),
    )

    config = _load(
        tmp_path / "missing.toml",
        environ={"HERMES_SESSION_BRIDGE_LIVE_TESTS": "1"},
    )

    assert config == _installed_default_bridge_config()
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
        lambda: deepcopy(_DEFAULT_CONFIG_SNAPSHOT),
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
    "inbox_cwd": None,
    "placement_generation": 1,
    "enabled": False,
    "continuous": False,
    "backfill_days": 30,
    "continuous_batch_limit": 5,
    "manual_batch_limit": 10,
    "lease_seconds": 300,
    "max_attempts": 5,
    "heartbeat_grace_seconds": 120,
    "readable_preview_enabled": True,
    "legacy_hydration_enabled": False,
    "preview_budget_chars": 24_000,
}
_SIDEBAR_CONFIG_DEFAULTS = {
    key: value for key, value in _SIDEBAR_DEFAULTS.items() if key != "inbox_cwd"
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
    assert _DEFAULT_CONFIG_SNAPSHOT["session_bridge"] == {
        "sidebar": {
            **_SIDEBAR_DEFAULTS,
            "inbox_cwd": str(_DEFAULT_CONFIG_HOME),
        }
    }
    assert asdict(SidebarConfig()) == {
        **_SIDEBAR_DEFAULTS,
        "delivery_mode": "desktop_broker",
        "broker_thread_id": None,
        "broker_project_id": None,
        "broker_cwd": None,
        "heartbeat_interval_seconds": 60,
        "oldest_job_alert_seconds": 300,
    }
    assert not any("SIDEBAR" in name for name in _ENV_NAMES)

    config = _load_with_sidebar(monkeypatch, {})

    assert config.sidebar == SidebarConfig()
    assert config.sidebar.enabled is False
    assert config.sidebar.continuous is False


def test_sidebar_preview_budget_rejects_values_below_readable_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="preview_budget_chars must be at least"):
        _load_with_sidebar(
            monkeypatch,
            {
                "preview_budget_chars": (
                    bridge_config.MIN_READABLE_PREVIEW_BUDGET_CHARS - 1
                ),
            },
        )


def test_sidebar_preview_budget_default_is_readable_minimum_or_larger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _load_with_sidebar(monkeypatch, {})

    assert config.sidebar.preview_budget_chars >= (
        bridge_config.MIN_READABLE_PREVIEW_BUDGET_CHARS
    )
    assert _SIDEBAR_DEFAULTS["preview_budget_chars"] >= (
        bridge_config.MIN_READABLE_PREVIEW_BUDGET_CHARS
    )


def test_default_config_import_is_safe_without_a_resolvable_home(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in {"HERMES_HOME", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME"}
    }
    # Pinned explicitly because the spawn below pins cwd. Without it the child
    # would resolve ``hermes_cli.config`` through the editable install, i.e.
    # the shared checkout at ~/.hermes/agent-src, silently testing a different
    # tree than the one under test. Not a home var, so it cannot re-resolve the
    # home this test is proving unavailable.
    environ["PYTHONPATH"] = str(repo_root) + os.pathsep + environ.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.config import DEFAULT_CONFIG; print(DEFAULT_CONFIG['session_bridge']['sidebar']['inbox_cwd'])",
        ],
        capture_output=True,
        check=True,
        env=environ,
        # cwd pinned to tmp_path: run_tests_parallel.py gives every pytest
        # worker cwd=repo_root, and a child stripped of every home var is
        # precisely the shape that falls back to CWD-relative paths -- which
        # would land in the shared checkout root.
        cwd=str(tmp_path),
        text=True,
    )

    assert result.stdout.strip() == "__HERMES_SIDEBAR_INBOX_UNAVAILABLE__"


def test_load_config_refreshes_sidebar_inbox_default_for_current_profile(
    tmp_path: Path,
) -> None:
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    config_module = importlib.import_module("hermes_cli.config")

    profile_a_token = set_hermes_home_override(profile_a)
    try:
        config_module = importlib.reload(config_module)
    finally:
        reset_hermes_home_override(profile_a_token)

    profile_b_token = set_hermes_home_override(profile_b)
    try:
        loaded = config_module.load_config()
    finally:
        reset_hermes_home_override(profile_b_token)
        default_token = set_hermes_home_override(_DEFAULT_CONFIG_HOME)
        try:
            importlib.reload(config_module)
        finally:
            reset_hermes_home_override(default_token)

    assert loaded["session_bridge"]["sidebar"]["inbox_cwd"] == str(profile_b)


def test_load_config_preserves_explicit_sidebar_inbox_cwd_after_profile_switch(
    tmp_path: Path,
) -> None:
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    configured_inbox = tmp_path / "configured-inbox"
    profile_a.mkdir()
    profile_b.mkdir()
    (profile_b / "config.yaml").write_text(
        "session_bridge:\n  sidebar:\n    inbox_cwd: "
        f"{configured_inbox}\n",
        encoding="utf-8",
    )
    config_module = importlib.import_module("hermes_cli.config")

    profile_a_token = set_hermes_home_override(profile_a)
    try:
        config_module = importlib.reload(config_module)
    finally:
        reset_hermes_home_override(profile_a_token)

    profile_b_token = set_hermes_home_override(profile_b)
    try:
        loaded = config_module.load_config()
    finally:
        reset_hermes_home_override(profile_b_token)
        default_token = set_hermes_home_override(_DEFAULT_CONFIG_HOME)
        try:
            importlib.reload(config_module)
        finally:
            reset_hermes_home_override(default_token)

    assert loaded["session_bridge"]["sidebar"]["inbox_cwd"] == str(configured_inbox)


def test_cached_profile_defaults_are_not_written_when_inbox_is_omitted(
    tmp_path: Path,
) -> None:
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    (profile_a / "config.yaml").write_text("model: ''\n", encoding="utf-8")
    (profile_b / "config.yaml").write_text("model: ''\n", encoding="utf-8")
    config_module = importlib.import_module("hermes_cli.config")

    token_a = set_hermes_home_override(profile_a)
    try:
        config_module = importlib.reload(config_module)
        assert config_module.load_config()["session_bridge"]["sidebar"]["inbox_cwd"] == str(profile_a)
    finally:
        reset_hermes_home_override(token_a)
    token_b = set_hermes_home_override(profile_b)
    try:
        first = config_module.load_config()
        second = config_module.load_config()
        assert first["session_bridge"]["sidebar"]["inbox_cwd"] == str(profile_b)
        assert second["session_bridge"]["sidebar"]["inbox_cwd"] == str(profile_b)
        assert config_module.save_config(second)
        raw = config_module.read_raw_config()
    finally:
        reset_hermes_home_override(token_b)
        default_token = set_hermes_home_override(_DEFAULT_CONFIG_HOME)
        try:
            importlib.reload(config_module)
        finally:
            reset_hermes_home_override(default_token)

    assert "inbox_cwd" not in raw.get("session_bridge", {}).get("sidebar", {})


@pytest.mark.parametrize("copy_default", (False, True))
def test_save_config_preserves_explicit_static_inbox_across_profile_switch(
    tmp_path: Path,
    copy_default: bool,
) -> None:
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    config_module = importlib.import_module("hermes_cli.config")
    token_a = set_hermes_home_override(profile_a)
    try:
        config_module = importlib.reload(config_module)
    finally:
        reset_hermes_home_override(token_a)
    token_b = set_hermes_home_override(profile_b)
    try:
        value = (
            deepcopy(config_module.DEFAULT_CONFIG)
            if copy_default
            else config_module.DEFAULT_CONFIG
        )
        assert config_module.save_config(value, strip_defaults=False)
        raw = config_module.read_raw_config()
    finally:
        reset_hermes_home_override(token_b)

    assert raw["session_bridge"]["sidebar"]["inbox_cwd"] == str(profile_a)


@pytest.mark.parametrize("session_bridge", (None, "not-a-mapping"))
def test_save_config_preserves_non_mapping_session_bridge(
    tmp_path: Path,
    session_bridge: object,
) -> None:
    config_module = importlib.import_module("hermes_cli.config")
    token = set_hermes_home_override(tmp_path)
    try:
        assert config_module.save_config({"session_bridge": session_bridge})
        raw = config_module.read_raw_config()
    finally:
        reset_hermes_home_override(token)

    if session_bridge is None:
        assert "session_bridge" not in raw
    else:
        assert raw["session_bridge"] == session_bridge


def test_explicit_sidebar_inbox_survives_profile_default_round_trip(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    configured_inbox = tmp_path / "configured-inbox"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        "session_bridge:\n  sidebar:\n    inbox_cwd: " f"{configured_inbox}\n",
        encoding="utf-8",
    )
    config_module = importlib.import_module("hermes_cli.config")
    token = set_hermes_home_override(profile)
    try:
        loaded = config_module.load_config()
        assert config_module.save_config(loaded)
        raw = config_module.read_raw_config()
    finally:
        reset_hermes_home_override(token)

    assert raw["session_bridge"]["sidebar"]["inbox_cwd"] == str(configured_inbox)


def test_sidebar_config_loads_only_from_config_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = tmp_path / ".hermes"
    configured = {
        "inbox_cwd": str(inbox),
        "placement_generation": 1,
        "enabled": True,
        "continuous": True,
        "delivery_mode": "desktop_broker",
        "broker_thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "broker_project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "broker_cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
        "backfill_days": 14,
        "continuous_batch_limit": 3,
        "manual_batch_limit": 7,
        "lease_seconds": 300,
        "max_attempts": 5,
        "heartbeat_interval_seconds": 60,
        "heartbeat_grace_seconds": 120,
        "oldest_job_alert_seconds": 300,
        "readable_preview_enabled": True,
        "legacy_hydration_enabled": True,
        "preview_budget_chars": 12_000,
    }

    config = _load_with_sidebar(monkeypatch, configured)

    assert asdict(config.sidebar) == configured
    assert config.sidebar.heartbeat_stale_seconds == 180


@pytest.mark.parametrize("field", ("inbox_cwd", "broker_thread_id", "broker_project_id", "broker_cwd"))
@pytest.mark.parametrize("unsafe", ("bad\x00value", "bad\x85value", "bad\u2028value", "bad\u2029value"))
def test_sidebar_broker_configuration_rejects_control_and_unicode_line_separators(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    unsafe: str,
) -> None:
    with pytest.raises(ValueError):
        _load_with_sidebar(monkeypatch, {field: unsafe})


def test_desktop_broker_continuous_delivery_requires_exact_identity_and_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = {
        "enabled": True,
        "continuous": True,
        "delivery_mode": "desktop_broker",
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
        "broker_thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "broker_project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "broker_cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
        "heartbeat_interval_seconds": 60,
        "heartbeat_grace_seconds": 120,
        "oldest_job_alert_seconds": 300,
        "readable_preview_enabled": True,
    }

    config = _load_with_sidebar(monkeypatch, configured)

    assert config.sidebar.delivery_mode == "desktop_broker"
    assert config.sidebar.heartbeat_interval_seconds == 60
    assert config.sidebar.heartbeat_stale_seconds == 180
    assert config.sidebar.oldest_job_alert_seconds == 300
    assert config.service.catalog_scan_seconds <= 60


@pytest.mark.parametrize(
    "field",
    ("inbox_cwd", "broker_thread_id", "broker_project_id", "broker_cwd"),
)
def test_desktop_broker_continuous_delivery_fails_closed_without_identity(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    configured = {
        "enabled": True,
        "continuous": True,
        "delivery_mode": "desktop_broker",
        "inbox_cwd": "C:\\Users\\diego\\.hermes",
        "broker_thread_id": "019f9b71-7109-7ed0-943a-d7291190245c",
        "broker_project_id": "local-453ac85f86839c6d001817cb8480b8ca",
        "broker_cwd": "C:\\Users\\diego\\Developer\\session-sidebar-broker",
        "heartbeat_interval_seconds": 60,
        "heartbeat_grace_seconds": 120,
        "oldest_job_alert_seconds": 300,
        "readable_preview_enabled": True,
    }
    configured.pop(field)

    with pytest.raises(ValueError, match="desktop broker identity"):
        _load_with_sidebar(monkeypatch, configured)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("inbox_cwd", "", "inbox_cwd must be a non-empty string"),
        ("inbox_cwd", 1, "inbox_cwd must be a non-empty string"),
        ("placement_generation", True, "placement_generation must be an integer"),
        (
            "placement_generation",
            2,
            "placement_generation must be exactly 1",
        ),
    ),
)
def test_sidebar_config_rejects_invalid_placement_settings(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        _load_with_sidebar(monkeypatch, {**_SIDEBAR_CONFIG_DEFAULTS, field: value})


def test_unknown_sidebar_config_key_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(
        ValueError,
        match="unknown session_bridge.sidebar configuration key: typo",
    ):
        _load_with_sidebar(monkeypatch, {**_SIDEBAR_CONFIG_DEFAULTS, "typo": True})


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
        (
            "readable_preview_enabled",
            1,
            "readable_preview_enabled must be a boolean",
        ),
        (
            "legacy_hydration_enabled",
            "false",
            "legacy_hydration_enabled must be a boolean",
        ),
        ("preview_budget_chars", 0, "preview_budget_chars must be at least 1"),
        (
            "preview_budget_chars",
            100_001,
            "preview_budget_chars must be at most 100000",
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
            {**_SIDEBAR_CONFIG_DEFAULTS, field: value},
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
    "float_activity": False,
    "archive_idle_chips": False,
    "idle_chip_archive_seconds": 86_400,
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
        "float_activity": True,
        "archive_idle_chips": True,
        "idle_chip_archive_seconds": 43_200,
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
