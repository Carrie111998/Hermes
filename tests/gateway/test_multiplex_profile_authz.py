"""Regression tests for multiplex profile-aware own-policy authorization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "WECOM_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "WECOM_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_multiplex_runner(monkeypatch):
    """Runner with default allowlist WeCom and secondary open-policy WeCom."""
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    default_adapter = SimpleNamespace(
        send=AsyncMock(),
        enforces_own_access_policy=True,
        _dm_policy="allowlist",
        _group_policy="pairing",
    )
    secondary_adapter = SimpleNamespace(
        send=AsyncMock(),
        enforces_own_access_policy=True,
        _dm_policy="open",
        _group_policy="open",
    )

    runner.adapters = {Platform.WECOM: default_adapter}
    runner._profile_adapters = {
        "coder": {Platform.WECOM: secondary_adapter},
    }
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner, default_adapter, secondary_adapter


def test_default_profile_still_trusts_own_allowlist(monkeypatch):
    """Default-profile allowlist trust is unchanged when profile is unstamped."""
    runner, _default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)

    source = SessionSource(
        platform=Platform.WECOM,
        user_id="allowed-user",
        chat_id="dm-chat",
        user_name="allowed-user",
        chat_type="dm",
        profile=None,
    )

    assert runner._is_user_authorized(source) is True


def test_active_profile_stamp_resolves_primary_adapter(monkeypatch):
    """A single-profile gateway stamps its active profile but stores adapters as primary."""
    runner, default_adapter, _secondary_adapter = _make_multiplex_runner(monkeypatch)
    runner._active_profile_name = lambda: "dev"

    assert runner._authorization_adapter(Platform.WECOM, profile="dev") is default_adapter


def test_secondary_allowlist_dm_behavior_ignores_unauthorized(monkeypatch):
    """Unauthorized-DM behavior must read the secondary adapter's dm_policy."""
    runner, _default_adapter, secondary_adapter = _make_multiplex_runner(monkeypatch)
    secondary_adapter._dm_policy = "allowlist"

    assert runner._get_unauthorized_dm_behavior(
        Platform.WECOM,
        profile="coder",
    ) == "ignore"
    assert runner._get_unauthorized_dm_behavior(Platform.WECOM) == "ignore"


def test_adapter_auth_check_stamps_secondary_profile(monkeypatch):
    """The adapter auth-check callback must stamp its own secondary profile.

    Regression for the gap where ``_make_adapter_auth_check`` built a
    profile-less ``SessionSource``, so a secondary adapter's external-context
    authorization (e.g. Slack/Discord thread-reply lookups) silently
    resolved the *active* profile's allowlist scope instead of its own.
    """
    from gateway.run import GatewayRunner

    _clear_auth_env(monkeypatch)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    captured: dict = {}

    def fake_is_user_authorized(source):
        captured["profile"] = source.profile
        return True

    runner._is_user_authorized = fake_is_user_authorized

    check = runner._make_adapter_auth_check(Platform.WECOM, profile_name="coder")
    assert check("some-user", "dm", "dm-chat") is True
    assert captured["profile"] == "coder"


def test_primary_adapter_auth_check_uses_active_profile_scope(tmp_path, monkeypatch):
    """The active adapter must remain authorized after multiplex fail-closed mode."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    default_home = tmp_path / "default"
    default_home.mkdir()
    (default_home / ".env").write_text(
        "TELEGRAM_ALLOWED_USERS=primary-user\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "foreign-process-user")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: default_home,
    )

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_stores = {"default": runner.pairing_store}
    check = runner._make_adapter_auth_check(Platform.TELEGRAM)

    ss.set_multiplex_active(True)
    try:
        assert check("primary-user", "dm", "primary-user") is True
        assert check("foreign-process-user", "dm", "foreign-process-user") is False
    finally:
        ss.set_multiplex_active(False)


def test_adapter_auth_check_reads_secondary_profile_allowlist(tmp_path, monkeypatch):
    """A profile-bound callback must not fall back to the primary allowlist."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    default_home = tmp_path / "default"
    personal_home = tmp_path / "personal"
    default_home.mkdir()
    personal_home.mkdir()
    (personal_home / ".env").write_text(
        "TELEGRAM_ALLOWED_USERS=personal-user\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "primary-user")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: personal_home,
    )

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_stores = {}
    check = runner._make_adapter_auth_check(
        Platform.TELEGRAM,
        profile_name="personal",
    )

    ss.set_multiplex_active(True)
    try:
        assert check("personal-user", "dm", "personal-user") is True
        assert check("primary-user", "dm", "primary-user") is False
    finally:
        ss.set_multiplex_active(False)


def test_auth_env_never_falls_back_to_process_env_in_multiplex(monkeypatch):
    from agent import secret_scope as ss
    from gateway.authz_mixin import _auth_env

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "primary-user")
    ss.set_multiplex_active(True)
    try:
        assert _auth_env("TELEGRAM_ALLOWED_USERS") == ""

        token = ss.set_secret_scope({})
        try:
            assert _auth_env("TELEGRAM_ALLOWED_USERS") == ""
        finally:
            ss.reset_secret_scope(token)

        token = ss.set_secret_scope({"TELEGRAM_ALLOWED_USERS": "profile-user"})
        try:
            assert _auth_env("TELEGRAM_ALLOWED_USERS") == "profile-user"
        finally:
            ss.reset_secret_scope(token)
    finally:
        ss.set_multiplex_active(False)


def test_group_chat_allowlist_never_falls_back_to_process_env_in_multiplex(
    monkeypatch,
):
    """A secondary profile must not inherit the primary profile's chat grant."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "-100-primary")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_stores = {}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-100-primary",
        chat_type="group",
        profile="personal",
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert runner._is_user_authorized(source) is False
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_bot_allowance_never_falls_back_to_process_env_in_multiplex(monkeypatch):
    """A secondary profile must not inherit the primary profile's bot policy."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    monkeypatch.setenv("TELEGRAM_ALLOW_BOTS", "all")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_stores = {}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-100-personal",
        chat_type="group",
        profile="personal",
        is_bot=True,
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert runner._is_user_authorized(source) is False
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_bot_allowance_uses_transport_profile_config(monkeypatch):
    """Profile-local YAML policy must work without a process-global env bridge."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    monkeypatch.delenv("TELEGRAM_ALLOW_BOTS", raising=False)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    personal_adapter = MagicMock()
    personal_adapter.config = PlatformConfig(extra={"allow_bots": "all"})
    runner._profile_adapters = {
        "personal": {Platform.TELEGRAM: personal_adapter}
    }
    runner.pairing_stores = {}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="-100-personal",
        chat_type="group",
        profile="personal",
        is_bot=True,
        _transport_profile="personal",
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert runner._is_user_authorized(source) is True
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_unauthorized_dm_behavior_never_inherits_process_allowlist(monkeypatch):
    """A primary allowlist must not force secondary unknown DMs into ignore mode."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "primary-user")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert runner._get_unauthorized_dm_behavior(
            Platform.TELEGRAM,
            profile="personal",
        ) == "pair"
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_routed_source_uses_transport_pairing_store():
    """A topic route changes runtime profile, not the receiving bot's whitelist."""
    import dataclasses
    import weakref
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    primary_adapter = MagicMock()
    routed_adapter = MagicMock()
    runner.adapters = {Platform.TELEGRAM: primary_adapter}
    runner._profile_adapters = {
        "x100": {Platform.TELEGRAM: routed_adapter}
    }
    runner._active_profile_name = lambda: "default"
    runner.pairing_store = MagicMock(name="primary-pairing")
    routed_pairing = MagicMock(name="routed-profile-pairing")
    runner.pairing_stores = {
        "default": runner.pairing_store,
        "x100": routed_pairing,
    }
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="owner",
        profile="x100",
    )
    setattr(source, "_transport_adapter_ref", weakref.ref(primary_adapter))
    source._transport_profile = "default"
    source = dataclasses.replace(source, thread_id="recovered-topic")

    assert runner._pairing_store_for(source) is runner.pairing_store
    assert runner._adapter_for_source(source) is primary_adapter
    assert "_transport_adapter_ref" not in source.to_dict()
    assert "transport_profile" not in source.to_dict()

    persisted = source.to_dict(include_transport=True)
    assert persisted["transport_profile"] == "default"
    restored = SessionSource.from_dict(persisted)
    assert restored._transport_adapter_ref is None
    assert runner._pairing_store_for(restored) is runner.pairing_store
    assert runner._adapter_for_source(restored) is primary_adapter


def test_unknown_explicit_profile_does_not_fall_back_to_global_home(
    tmp_path, monkeypatch
):
    """A stale or malformed route must not execute in the primary profile."""
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)

    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path / name,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: False,
    )
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: tmp_path / "primary",
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="owner",
        chat_id="chat",
        chat_type="group",
        profile="missing-profile",
    )

    with pytest.raises(RuntimeError, match="missing-profile"):
        runner._resolve_profile_home_for_source(source)


def test_unknown_explicit_profile_keeps_legacy_fallback_outside_multiplex(
    tmp_path, monkeypatch
):
    from gateway.run import GatewayRunner
    from gateway.config import GatewayConfig, Platform
    from gateway.session import SessionSource

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    primary_home = tmp_path / "primary"

    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path / name,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profile_exists",
        lambda name: False,
    )
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: primary_home,
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="owner",
        chat_id="chat",
        chat_type="group",
        profile="missing-profile",
    )

    assert runner._resolve_profile_home_for_source(source) == primary_home


def test_secondary_pairing_never_falls_back_to_primary_store(monkeypatch):
    """Missing secondary pairing state must deny, not reuse the primary store."""
    from agent import secret_scope as ss
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner.pairing_stores = {}
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="primary-paired-user",
        chat_id="primary-paired-user",
        chat_type="dm",
        profile="personal",
    )

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert runner._is_user_authorized(source) is False
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_primary_open_policy_guard_uses_active_profile_scope(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    default_home = tmp_path / "default"
    default_home.mkdir()
    (default_home / ".env").write_text(
        "GATEWAY_ALLOW_ALL_USERS=true\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: default_home,
    )

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {
        Platform.WECOM: PlatformConfig(
            enabled=True,
            extra={"dm_policy": "open"},
        ),
    }
    runner._active_profile_name = lambda: "default"

    assert runner._primary_open_policy_violation() is None


def test_secondary_open_policy_ignores_primary_allow_all(monkeypatch):
    """Primary allow-all must not satisfy a secondary profile's startup guard."""
    from agent import secret_scope as ss
    from gateway.run import _own_policy_open_startup_violation

    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    secondary_cfg = GatewayConfig(multiplex_profiles=True)
    secondary_cfg.platforms = {
        Platform.WECOM: PlatformConfig(
            enabled=True,
            extra={"dm_policy": "open"},
        ),
    }

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert _own_policy_open_startup_violation(secondary_cfg) is not None
    finally:
        ss.reset_secret_scope(token)

    token = ss.set_secret_scope({"GATEWAY_ALLOW_ALL_USERS": "true"})
    try:
        assert _own_policy_open_startup_violation(secondary_cfg) is None
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_secondary_open_policy_fails_startup_guard(monkeypatch):
    """Secondary profiles must pass the same open-policy startup guard."""
    from gateway.run import _own_policy_open_startup_violation

    _clear_auth_env(monkeypatch)

    secondary_cfg = GatewayConfig(multiplex_profiles=True)
    secondary_cfg.platforms = {
        Platform.WECOM: PlatformConfig(
            enabled=True,
            extra={"dm_policy": "open"},
        ),
    }

    violation = _own_policy_open_startup_violation(secondary_cfg)
    assert violation is not None
    assert "wecom" in violation
    assert "open policy" in violation
