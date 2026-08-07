from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.canonical_writer_client import (
    ExactServerMainPidAuthorizer,
    ServerPeerCredentials,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.discord_connector_protocol import (
    DiscordConnectorTarget,
    DiscordConnectorTargetType,
)
from gateway.discord_connector_service import (
    DiscordConnectorAcceptedMessage,
    DiscordConnectorRuntime,
    DiscordConnectorUnixServer,
    DurableDiscordConnectorJournal,
)
from gateway.discord_edge_service import DiscordEdgePeerCredentials
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CapabilityDescriptor
from gateway.relay.discord_connector_transport import (
    DiscordConnectorRelayTransport,
)
from ops.muncho.release.completion import (
    ReleaseCompletionError,
    complete_restart_attestation,
    deliver_discord_via_gateway_once,
    prepare_restart_attestation,
    prepare_summary_draft,
    record_production_smoke,
    release_health,
    release_status,
    reserve_release_mapping,
)
from ops.muncho.release.gateway_delivery import (
    dispatch_pending_gateway_discord_deliveries,
)
from ops.muncho.release import cli as release_cli
from ops.muncho.release.metadata import load_release_bundle


ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
RELEASE_SHA = "a" * 40
GUILD_ID = "123456789012345678"
CHANNEL_ID = "223456789012345678"
SERVICE = "hermes-cloud-gateway.service"
AFTER_INVOCATION_ID = "2" * 32


def _production_config() -> dict:
    return {
        "approvals": {
            "gateway_owner_escalation": {
                "enabled": True,
                "owner_user_id": "323456789012345678",
                "owner_guild_id": GUILD_ID,
                "owner_channel_id": CHANNEL_ID,
                "owner_target_type": "guild_channel",
            }
        }
    }


def _queued(tmp_path: Path, *, release_sha: str = RELEASE_SHA):
    state = (tmp_path / "release-state").resolve()
    bundle = load_release_bundle(ROOT)
    mapping = reserve_release_mapping(
        state,
        bundle,
        version="2.3.2",
        release_sha=release_sha,
        reserved_at=NOW,
    )
    prepare_restart_attestation(
        state,
        mapping,
        service_name=SERVICE,
        before_invocation_id="1" * 32,
        prepared_at=NOW,
    )
    restart = complete_restart_attestation(
        state,
        mapping,
        service_name=SERVICE,
        after_invocation_id=AFTER_INVOCATION_ID,
        attested_at=NOW,
    )
    smoke = record_production_smoke(
        state,
        mapping,
        restart,
        checks=(
            "Exact release identity is active.",
            "Gateway health and production smoke passed.",
        ),
        completed_at=NOW,
    )
    draft = prepare_summary_draft(
        state,
        bundle,
        mapping=mapping,
        smoke=smoke,
        production_config=_production_config(),
        created_at=NOW,
    )
    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_discord_delivery_reconciliation_required",
    ):
        deliver_discord_via_gateway_once(state, draft, timeout_seconds=0)
    return state, draft


def _gateway_config() -> GatewayConfig:
    return GatewayConfig(
        platforms={Platform.RELAY: PlatformConfig(enabled=True)},
    )


class _Relay:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def fronts_platform(self, platform) -> bool:
        return platform == Platform.DISCORD

    async def send_for_platform(
        self,
        platform,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
    ):
        self.calls.append((platform, chat_id, content, metadata))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


class _PidProvider:
    def main_pid(self, _unit_name: str) -> int:
        return os.getpid()


class _DiscordBoundary:
    """Discord SDK/HTTP boundary behind the real privileged connector."""

    def __init__(self) -> None:
        self.target = DiscordConnectorTarget(
            DiscordConnectorTargetType.PUBLIC_GUILD_CHANNEL,
            GUILD_ID,
            CHANNEL_ID,
        )
        self.sends: list[str] = []

    def prove_public_target(self, channel_id: str) -> DiscordConnectorTarget:
        if channel_id != CHANNEL_ID:
            raise PermissionError("forbidden")
        return self.target

    def send_public_message(
        self,
        target: DiscordConnectorTarget,
        content: str,
        *,
        reply_to_message_id: str | None,
        deadline_unix_ms: int,
    ) -> DiscordConnectorAcceptedMessage:
        assert target == self.target
        assert reply_to_message_id is None
        assert deadline_unix_ms > 0
        self.sends.append(content)
        return DiscordConnectorAcceptedMessage("423456789012345678", True)

    def fetch_guild_history(self, *_args, **_kwargs):
        raise AssertionError("release announcement must not read message history")


@pytest.mark.asyncio
async def test_gateway_watcher_forwards_its_current_systemd_invocation(
    monkeypatch,
):
    from gateway.run import GatewayRunner
    import ops.muncho.release.gateway_delivery as edge
    import ops.muncho.release.metadata as metadata

    runner = SimpleNamespace(
        _running=True,
        config=_gateway_config(),
        adapters={},
    )
    observed = {}

    async def dispatch(**kwargs):
        observed.update(kwargs)
        runner._running = False
        return ()

    monkeypatch.setattr(edge, "dispatch_pending_gateway_discord_deliveries", dispatch)
    monkeypatch.setattr(metadata, "resolve_exact_release_sha", lambda: RELEASE_SHA)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: _production_config(),
    )
    monkeypatch.setenv("INVOCATION_ID", AFTER_INVOCATION_ID)

    await GatewayRunner._muncho_release_announcement_watcher(  # type: ignore[arg-type]
        runner,
        poll_interval=0,
    )

    assert observed["deployed_release_sha"] == RELEASE_SHA
    assert observed["active_service_invocation_id"] == AFTER_INVOCATION_ID


@pytest.mark.asyncio
async def test_verified_gateway_relay_delivery_records_exact_id_and_is_idempotent(
    tmp_path: Path,
):
    state, draft = _queued(tmp_path)
    relay = _Relay(
        [SimpleNamespace(success=True, message_id="423456789012345678")]
    )
    kwargs = {
        "state_dir": state,
        "gateway_config": _gateway_config(),
        "adapters": {Platform.RELAY: relay},
        "production_config": _production_config(),
        "deployed_release_sha": RELEASE_SHA,
        "active_service_invocation_id": AFTER_INVOCATION_ID,
        "published_at": NOW,
    }
    outcomes = await dispatch_pending_gateway_discord_deliveries(**kwargs)
    assert outcomes[0]["state"] == "delivered"
    assert outcomes[0]["message_id"] == "423456789012345678"
    assert relay.calls[0][0] == Platform.DISCORD
    assert relay.calls[0][1] == CHANNEL_ID
    assert relay.calls[0][2] == draft["summary"]
    metadata = relay.calls[0][3]
    assert metadata["scope_id"] == GUILD_ID
    assert metadata["connector_idempotency_key"].startswith("muncho-release:")

    assert await dispatch_pending_gateway_discord_deliveries(**kwargs) == ()
    assert len(relay.calls) == 1
    status = release_status(state, version="2.3.2", release_sha=RELEASE_SHA)
    assert status["discord_summary_published"] is True
    assert status["codex_task_summary_published"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_state"),
    [
        (
            SimpleNamespace(
                success=False,
                message_id=None,
                error_kind="blocked_before_dispatch",
            ),
            "delivery_failed",
        ),
        (
            SimpleNamespace(
                success=False,
                message_id=None,
                error_kind="dispatch_uncertain",
            ),
            "dispatch_uncertain",
        ),
        (TimeoutError("connector timeout"), "dispatch_uncertain"),
    ],
)
async def test_failure_timeout_and_uncertainty_never_create_delivery_truth(
    tmp_path: Path,
    result,
    expected_state: str,
):
    state, _draft = _queued(tmp_path)
    relay = _Relay([result, result])
    kwargs = {
        "state_dir": state,
        "gateway_config": _gateway_config(),
        "adapters": {Platform.RELAY: relay},
        "production_config": _production_config(),
        "deployed_release_sha": RELEASE_SHA,
        "active_service_invocation_id": AFTER_INVOCATION_ID,
    }
    first = await dispatch_pending_gateway_discord_deliveries(**kwargs)
    second = await dispatch_pending_gateway_discord_deliveries(**kwargs)
    assert first[0]["state"] == expected_state
    assert second[0]["state"] == expected_state
    assert relay.calls[0][3]["connector_idempotency_key"] == relay.calls[1][3][
        "connector_idempotency_key"
    ]
    status = release_status(state, version="2.3.2", release_sha=RELEASE_SHA)
    assert status["discord_summary_published"] is False
    assert status["complete"] is False


@pytest.mark.asyncio
async def test_wrong_deployed_identity_and_direct_adapter_fail_closed(tmp_path: Path):
    state, _draft = _queued(tmp_path)
    relay = _Relay(
        [SimpleNamespace(success=True, message_id="423456789012345678")]
    )
    identity = await dispatch_pending_gateway_discord_deliveries(
        state_dir=state,
        gateway_config=_gateway_config(),
        adapters={Platform.RELAY: relay},
        production_config=_production_config(),
        deployed_release_sha="b" * 40,
        active_service_invocation_id=AFTER_INVOCATION_ID,
    )
    assert identity[0]["state"] == "blocked_identity_mismatch"
    assert relay.calls == []

    direct = SimpleNamespace()
    direct_config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True)},
    )
    blocked = await dispatch_pending_gateway_discord_deliveries(
        state_dir=state,
        gateway_config=direct_config,
        adapters={Platform.DISCORD: direct},
        production_config=_production_config(),
        deployed_release_sha=RELEASE_SHA,
        active_service_invocation_id=AFTER_INVOCATION_ID,
    )
    assert blocked[0]["state"] == "blocked_live_relay_unavailable"


@pytest.mark.asyncio
async def test_stale_or_missing_active_invocation_never_reaches_relay(tmp_path: Path):
    state, _draft = _queued(tmp_path)
    relay = _Relay(
        [SimpleNamespace(success=True, message_id="423456789012345678")]
    )
    common = {
        "state_dir": state,
        "gateway_config": _gateway_config(),
        "adapters": {Platform.RELAY: relay},
        "production_config": _production_config(),
        "deployed_release_sha": RELEASE_SHA,
    }

    for invocation_id in (None, "3" * 32):
        blocked = await dispatch_pending_gateway_discord_deliveries(
            **common,
            active_service_invocation_id=invocation_id,
        )
        assert blocked[0]["state"] == "blocked_restart_identity_mismatch"
    assert relay.calls == []
    assert release_status(
        state,
        version="2.3.2",
        release_sha=RELEASE_SHA,
    )["discord_summary_published"] is False


@pytest.mark.asyncio
async def test_wrong_sha_smoke_receipt_blocks_before_relay_send(tmp_path: Path):
    state, _draft = _queued(tmp_path / "expected")
    wrong_state, _wrong_draft = _queued(
        tmp_path / "wrong",
        release_sha="b" * 40,
    )
    expected_smoke = next(state.glob("smoke-*.json"))
    wrong_smoke = next(wrong_state.glob("smoke-*.json"))
    expected_smoke.write_bytes(wrong_smoke.read_bytes())
    expected_smoke.chmod(0o600)
    relay = _Relay(
        [SimpleNamespace(success=True, message_id="423456789012345678")]
    )

    with pytest.raises(
        ReleaseCompletionError,
        match="muncho_release_status_identity_mismatch",
    ):
        await dispatch_pending_gateway_discord_deliveries(
            state_dir=state,
            gateway_config=_gateway_config(),
            adapters={Platform.RELAY: relay},
            production_config=_production_config(),
            deployed_release_sha=RELEASE_SHA,
            active_service_invocation_id=AFTER_INVOCATION_ID,
        )

    assert relay.calls == []


@pytest.mark.asyncio
async def test_crash_after_connector_acceptance_reuses_key_without_second_mutation(
    tmp_path: Path,
    monkeypatch,
):
    state, _draft = _queued(tmp_path)
    accepted_by_key: dict[str, str] = {}
    mutations = 0

    class DedupeRelay(_Relay):
        def __init__(self):
            super().__init__([])

        async def send_for_platform(
            self,
            platform,
            chat_id,
            content,
            reply_to=None,
            metadata=None,
        ):
            nonlocal mutations
            self.calls.append((platform, chat_id, content, metadata))
            key = metadata["connector_idempotency_key"]
            if key not in accepted_by_key:
                mutations += 1
                accepted_by_key[key] = "423456789012345678"
            return SimpleNamespace(success=True, message_id=accepted_by_key[key])

    relay = DedupeRelay()
    import ops.muncho.release.gateway_delivery as edge

    real_record = edge.record_gateway_discord_delivery
    calls = 0

    def crash_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash before local receipt")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(edge, "record_gateway_discord_delivery", crash_once)
    kwargs = {
        "state_dir": state,
        "gateway_config": _gateway_config(),
        "adapters": {Platform.RELAY: relay},
        "production_config": _production_config(),
        "deployed_release_sha": RELEASE_SHA,
        "active_service_invocation_id": AFTER_INVOCATION_ID,
    }
    with pytest.raises(RuntimeError, match="crash before local receipt"):
        await dispatch_pending_gateway_discord_deliveries(**kwargs)
    completed = await dispatch_pending_gateway_discord_deliveries(**kwargs)
    assert completed[0]["state"] == "delivered"
    assert mutations == 1
    assert len(relay.calls) == 2
    assert relay.calls[0][3]["connector_idempotency_key"] == relay.calls[1][3][
        "connector_idempotency_key"
    ]


@pytest.mark.asyncio
async def test_real_relay_adapter_and_privileged_connector_reconcile_crash_once(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    """E2E through RelayAdapter, Unix protocol, journal, and Discord boundary."""

    if os.getuid() == 0:
        pytest.skip("production-shaped connector peer boundary requires non-root")
    state, draft = _queued(tmp_path)
    with tempfile.TemporaryDirectory(prefix="muncho-release-", dir="/tmp") as raw:
        socket_dir = Path(raw).resolve(strict=True)
        journal = DurableDiscordConnectorJournal.bootstrap(
            socket_dir / "journal.sqlite3"
        )
        backend = _DiscordBoundary()
        runtime = DiscordConnectorRuntime(backend=backend, journal=journal)
        socket_path = socket_dir / "connector.sock"
        server = DiscordConnectorUnixServer(
            socket_path,
            runtime=runtime,
            expected_gateway_uid=os.getuid(),
            gateway_unit=SERVICE,
            main_pid_provider=_PidProvider(),
            peer_getter=lambda _sock: DiscordEdgePeerCredentials(
                os.getpid(), os.getuid(), os.getgid()
            ),
        )
        server.start()
        server.readiness_identity()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        transport = DiscordConnectorRelayTransport(
            socket_path,
            server_authorizer=ExactServerMainPidAuthorizer(
                server_unit="muncho-discord-connector.service",
                expected_server_uid=os.getuid(),
                main_pid_provider=_PidProvider(),
            ),
            server_peer_getter=lambda _sock: ServerPeerCredentials(
                os.getpid(), os.getuid(), os.getgid()
            ),
            event_wait_ms=10,
        )
        placeholder = CapabilityDescriptor(
            contract_version=1,
            platform="relay",
            label="Relay",
            max_message_length=4_096,
            supports_draft_streaming=False,
            supports_edit=False,
            supports_threads=False,
            markdown_dialect="plain",
            len_unit="chars",
        )
        relay = RelayAdapter(
            PlatformConfig(enabled=True),
            placeholder,
            transport=transport,
        )
        try:
            assert await relay.connect() is True
            assert relay.fronts_platform(Platform.DISCORD) is True
            import ops.muncho.release.gateway_delivery as edge

            real_record = edge.record_gateway_discord_delivery
            record_calls = 0

            def crash_after_connector_receipt(*args, **kwargs):
                nonlocal record_calls
                record_calls += 1
                if record_calls == 1:
                    raise RuntimeError("crash before release receipt")
                return real_record(*args, **kwargs)

            monkeypatch.setattr(
                edge,
                "record_gateway_discord_delivery",
                crash_after_connector_receipt,
            )
            kwargs = {
                "state_dir": state,
                "gateway_config": _gateway_config(),
                "adapters": {Platform.RELAY: relay},
                "production_config": _production_config(),
                "deployed_release_sha": RELEASE_SHA,
                "active_service_invocation_id": AFTER_INVOCATION_ID,
                "published_at": NOW,
            }
            with pytest.raises(RuntimeError, match="crash before release receipt"):
                await dispatch_pending_gateway_discord_deliveries(**kwargs)
            delivered = await dispatch_pending_gateway_discord_deliveries(**kwargs)
            assert delivered[0]["state"] == "delivered"
            assert delivered[0]["message_id"] == "423456789012345678"
            assert backend.sends == [draft["summary"]]
            assert await dispatch_pending_gateway_discord_deliveries(**kwargs) == ()

            task_id = "019fa801-52ca-7460-954d-30aee7053618"
            prepare_args = [
                "coordinator-prepare",
                "--version",
                "2.3.2",
                "--release-sha",
                RELEASE_SHA,
                "--state-dir",
                str(state),
                "--task-id",
                task_id,
            ]
            assert release_cli.main(prepare_args) == 0
            coordinator = json.loads(capsys.readouterr().out)
            assert coordinator["summary"] == draft["summary"]
            assert coordinator["summary_sha256"] == draft["summary_sha256"]

            assert release_cli.main([
                "coordinator-complete",
                "--version",
                "2.3.2",
                "--release-sha",
                RELEASE_SHA,
                "--state-dir",
                str(state),
                "--task-id",
                task_id,
                "--message-ref",
                "assistant-release-summary",
                "--summary-sha256",
                coordinator["summary_sha256"],
                "--attempt-receipt-sha256",
                coordinator["attempt_receipt_sha256"],
            ]) == 0
            terminal = json.loads(capsys.readouterr().out)
            assert terminal["release_completion"] == "complete"
            assert terminal["healthy"] is True
            assert release_health(
                state,
                version="2.3.2",
                release_sha=RELEASE_SHA,
            )["healthy"] is True
        finally:
            await relay.disconnect()
            server.shutdown()
            thread.join(timeout=2)
