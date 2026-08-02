"""Regression tests for own-policy open startup gate in gateway/run.py."""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, _own_policy_open_startup_violation


@pytest.mark.asyncio
async def test_unrelated_allow_all_does_not_bypass_yuanbao_open_gate(
    monkeypatch, tmp_path,
):
    """TELEGRAM_ALLOW_ALL_USERS must not satisfy Yuanbao's open-policy opt-in."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("YUANBAO_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")

    config = GatewayConfig(
        platforms={
            Platform.YUANBAO: PlatformConfig(
                enabled=True,
                extra={"dm_policy": "open"},
            ),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)

    ok = await runner.start()

    assert ok is True
    assert runner.should_exit_cleanly is True
    assert "yuanbao" in (runner.exit_reason or "").lower()


class TestGuardHonorsProfileSecretScope:
    """#76574: the guard must resolve the same value the platform adapter
    resolves — a secondary profile's scope, not the sticky process env."""

    @pytest.fixture(autouse=True)
    def _reset_multiplex(self):
        from agent import secret_scope as ss
        ss.set_multiplex_active(False)
        yield
        ss.set_multiplex_active(False)

    def test_scoped_open_without_allow_all_blocks(self, monkeypatch):
        from agent import secret_scope as ss

        monkeypatch.delenv("WHATSAPP_DM_POLICY", raising=False)
        monkeypatch.delenv("WHATSAPP_GROUP_POLICY", raising=False)
        monkeypatch.delenv("WHATSAPP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        config = GatewayConfig(
            platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
        )
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"WHATSAPP_DM_POLICY": "open"})
        try:
            violation = _own_policy_open_startup_violation(config)
        finally:
            ss.reset_secret_scope(token)

        assert violation is not None
        assert "whatsapp" in violation.lower()

    def test_scoped_pairing_ignores_stale_process_env_open(self, monkeypatch):
        """A secondary profile's scoped 'pairing' must not be overridden by a
        stale process-env WHATSAPP_DM_POLICY=open left by another profile."""
        from agent import secret_scope as ss

        monkeypatch.setenv("WHATSAPP_DM_POLICY", "open")
        monkeypatch.delenv("WHATSAPP_GROUP_POLICY", raising=False)

        config = GatewayConfig(
            platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
        )
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"WHATSAPP_DM_POLICY": "pairing"})
        try:
            violation = _own_policy_open_startup_violation(config)
        finally:
            ss.reset_secret_scope(token)

        assert violation is None

    def test_scoped_miss_does_not_inherit_process_allow_all(self, monkeypatch):
        """A scope miss under multiplex must return the 'pairing' default, not
        fall through to the process env (which could be another profile's)."""
        from agent import secret_scope as ss

        monkeypatch.setenv("WHATSAPP_DM_POLICY", "open")
        monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")

        config = GatewayConfig(
            platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
        )
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({})  # scope installed, but empty
        try:
            violation = _own_policy_open_startup_violation(config)
        finally:
            ss.reset_secret_scope(token)

        assert violation is None

    def test_unscoped_default_profile_still_reads_process_env(self, monkeypatch):
        """Outside any profile scope (default-profile startup), the guard must
        keep reading os.environ exactly as before -- zero behavior change."""
        monkeypatch.setenv("WHATSAPP_DM_POLICY", "open")
        monkeypatch.delenv("WHATSAPP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        config = GatewayConfig(
            platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)},
        )

        violation = _own_policy_open_startup_violation(config)

        assert violation is not None
        assert "whatsapp" in violation.lower()


