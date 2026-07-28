"""Security-boundary regression tests for gateway profile isolation."""

from __future__ import annotations

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
import gateway.run as gateway_run


def _multiplex_config(tmp_path) -> GatewayConfig:
    return GatewayConfig(
        multiplex_profiles=True,
        sessions_dir=tmp_path / "sessions",
        platforms={
            Platform.API_SERVER: PlatformConfig(enabled=True),
        },
    )


def test_runner_refuses_multiplex_before_any_downstream_side_effect(
    monkeypatch,
    tmp_path,
):
    """The direct constructor gate precedes every process/profile resource."""

    calls: list[str] = []

    def forbidden(name):
        def _fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must remain unreachable")

        return _fail

    monkeypatch.setattr(
        "gateway.canonical_writer_boundary.harden_gateway_process_for_writer_boundary",
        forbidden("writer_hardening"),
    )
    monkeypatch.setattr(
        gateway_run,
        "_configure_gateway_provider_discovery",
        forbidden("provider_discovery"),
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_freeze_served_profile_inventory",
        forbidden("profile_inventory_marker"),
    )
    monkeypatch.setattr(
        "tools.async_delegation.register_frozen_event_delivery_inventory",
        forbidden("event_delivery_registration"),
    )
    monkeypatch.setattr(
        "tools.process_registry.process_registry.bind_checkpoint_path",
        forbidden("process_registry_checkpoint"),
    )
    monkeypatch.setattr(gateway_run, "SessionStore", forbidden("session_store"))
    monkeypatch.setattr(
        gateway_run,
        "AsyncSessionStore",
        forbidden("async_session_store"),
    )
    monkeypatch.setattr(gateway_run, "DeliveryRouter", forbidden("delivery_router"))
    monkeypatch.setattr(
        gateway_run,
        "_reload_runtime_env_preserving_config_authority",
        forbidden("terminal_env_bridge"),
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_create_adapter",
        forbidden("adapter_or_listener"),
    )

    with pytest.raises(
        gateway_run.MultiplexConfigError,
        match="one Hermes gateway process per profile",
    ):
        gateway_run.GatewayRunner(_multiplex_config(tmp_path))

    assert calls == []
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path / "sessions").exists()


@pytest.mark.asyncio
async def test_start_gateway_refuses_multiplex_before_runner_or_startup_services(
    monkeypatch,
    tmp_path,
    capsys,
):
    """The public startup path fails before locks, tools, stores, or listeners."""

    calls: list[str] = []

    def forbidden(name):
        def _fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"{name} must remain unreachable")

        return _fail

    monkeypatch.setattr(
        gateway_run,
        "GatewayRunner",
        forbidden("runner_constructor"),
    )
    monkeypatch.setattr(
        "gateway.code_skew.record_boot_fingerprint",
        forbidden("boot_fingerprint"),
    )
    monkeypatch.setattr(
        "gateway.canonical_writer_boundary.harden_gateway_process_for_writer_boundary",
        forbidden("writer_hardening"),
    )
    monkeypatch.setattr(
        "gateway.status.acquire_gateway_runtime_lock",
        forbidden("runtime_listener_lock"),
    )
    monkeypatch.setattr(
        "tools.skills_sync.sync_skills",
        forbidden("tool_or_skill_startup"),
    )
    monkeypatch.setattr(
        "hermes_logging.setup_logging",
        forbidden("listener_logging"),
    )

    started = await gateway_run.start_gateway(
        config=_multiplex_config(tmp_path),
        verbosity=None,
    )

    assert started is False
    assert calls == []
    diagnostic = capsys.readouterr().out
    assert "one Hermes gateway process per profile" in diagnostic
    assert "route it to isolated per-profile processes" in diagnostic
