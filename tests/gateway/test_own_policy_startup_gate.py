"""Regression tests for own-policy open startup gate in gateway/run.py."""

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _neutralize_eventbus_startup(monkeypatch):
    """Keep ``GatewayRunner.start()`` off the canonical ~/.hermes event bus.

    ``start()`` calls ``events.gateway_integration.startup()`` inline and
    synchronously. That does real I/O against the **canonical** ~/.hermes event
    bus (13 subscribers, tracker-intent-applier rehydrate, a jobops :4100
    probe); notification state is cross-profile, so the ``tmp_path``
    HERMES_HOME above does not redirect it.

    ``test_gateway_allow_all_satisfies_yuanbao_open_gate`` reaches that call
    (its adapter stub returns None, so startup falls through to the running
    path) and paid **173s** on a loaded box for a policy-gate assertion that
    says nothing about the event bus. Its sibling exits at the gate first and
    ran in 0.7s — the whole difference is live-bus I/O.
    """
    import events.gateway_integration as _ebi

    monkeypatch.setattr(_ebi, "startup", lambda *a, **k: None)


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


@pytest.mark.asyncio
async def test_gateway_allow_all_satisfies_yuanbao_open_gate(monkeypatch, tmp_path):
    """GATEWAY_ALLOW_ALL_USERS is the intended global open-policy opt-in."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    monkeypatch.delenv("YUANBAO_ALLOW_ALL_USERS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOW_ALL_USERS", raising=False)

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
    monkeypatch.setattr(runner, "_create_adapter", lambda platform, cfg: None)

    ok = await runner.start()

    assert ok is True
    assert runner.should_exit_cleanly is False