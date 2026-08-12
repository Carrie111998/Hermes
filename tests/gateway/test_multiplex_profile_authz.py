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

    runner._is_user_authorized_for_source = fake_is_user_authorized

    check = runner._make_adapter_auth_check(Platform.WECOM, profile_name="coder")
    assert check("some-user", "dm", "dm-chat") is True
    assert captured["profile"] == "coder"


def test_source_authorization_single_profile_delegates_without_recursion():
    """The scoped helper preserves the legacy single-profile auth path."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._is_user_authorized = MagicMock(return_value=True)
    source = SessionSource(
        platform=Platform.SIGNAL,
        user_id="alice",
        chat_id="dm",
        chat_type="dm",
    )

    assert runner._is_user_authorized_for_source(source) is True
    runner._is_user_authorized.assert_called_once_with(source)


def test_source_authorization_rejects_unserved_profile_before_fallback():
    """An explicit rejected route must never inherit default-profile auth."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._resolve_profile_home_for_source = MagicMock()
    source = SessionSource(
        platform=Platform.SIGNAL,
        user_id="alice",
        chat_id="group:abc",
        chat_type="group",
    )
    source.profile_route_rejected = True

    assert runner._is_user_authorized_for_source(source) is False
    runner._is_user_authorized.assert_not_called()
    runner._resolve_profile_home_for_source.assert_not_called()


@pytest.mark.asyncio
async def test_busy_path_uses_routed_profile_authorization(monkeypatch):
    """An active routed session must authorize under that profile's scope."""
    from agent import secret_scope
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = MagicMock()
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._effective_busy_input_mode = MagicMock(return_value="queue")
    runner._draining = False
    runner._peek_session_state = MagicMock(return_value=None)
    runner._adapter_for_source = MagicMock(return_value=None)
    runner._busy_ack_ts = {}
    runner._pending_messages = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}

    source = SessionSource(
        platform=Platform.SIGNAL,
        user_id="bob",
        chat_id="group:abc",
        chat_type="group",
        profile="profile-b",
    )
    event = SimpleNamespace(source=source)

    secret_scope.set_multiplex_active(True)
    try:
        # Stop immediately after the gate; this assertion targets the auth
        # boundary without coupling to queue-mode mechanics.
        runner._draining = True
        runner._adapter_for_source.return_value = None
        assert await runner._handle_active_session_busy_message(event, "session") is True
    finally:
        secret_scope.set_multiplex_active(False)

    runner._is_user_authorized_for_source.assert_called_once_with(source)


@pytest.mark.asyncio
async def test_entire_busy_path_keeps_routed_profile_scope(tmp_path):
    """Post-auth busy processing must retain the routed profile's secrets."""
    from agent import secret_scope
    from agent.secret_scope import get_secret
    from gateway.run import GatewayRunner

    profile_home = tmp_path / "profile-b"
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "SIGNAL_ALLOWED_USERS=bob\n", encoding="utf-8"
    )
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._resolve_profile_home_for_source = MagicMock(return_value=profile_home)
    observed = []

    async def scoped_inner(_event, _session_key):
        observed.append(get_secret("SIGNAL_ALLOWED_USERS"))
        return True

    runner._handle_active_session_busy_message_scoped = scoped_inner
    source = SessionSource(
        platform=Platform.SIGNAL,
        user_id="bob",
        chat_id="group:abc",
        chat_type="group",
        profile="profile-b",
    )
    event = SimpleNamespace(source=source)

    secret_scope.set_multiplex_active(True)
    try:
        assert await runner._handle_active_session_busy_message(event, "session") is True
    finally:
        secret_scope.set_multiplex_active(False)

    assert observed == ["bob"]


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
