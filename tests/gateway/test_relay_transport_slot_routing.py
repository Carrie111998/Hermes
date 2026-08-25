"""Regression: the ingress transport identity, not only its owner map.

``transport_profile`` names which adapter map owned the receiving adapter.
Inside one map a logical platform can be served by two live transports: a
native adapter and a Relay adapter that fronts the same platform (a supported
mixed deployment — ``GATEWAY_RELAY_ALLOW_DIRECT_PLATFORMS``). ``default``
provenance alone therefore sends the completion through
``resolve_delivery_transport``, whose documented contract is native-wins, so a
turn that arrived over Relay is answered by a different bot and credential.
Contract under test: the commissioning turn also records which adapter slot
received it as one atomic pair with the owner map, and completion either
resolves that exact adapter, falls back for a record carrying neither field, or
drops a record carrying a partial or contradictory pair.
"""

import json
import logging
import os
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


class _Adapter:
    """Minimal push-capable adapter stub."""

    def __init__(self, name):
        self.name = name
        self.handle_message = AsyncMock()


class _RelayAdapter(_Adapter):
    """Relay stub that advertises a logical platform, as the real one does."""

    def __init__(self, name="relay", fronted=(Platform.SLACK,)):
        super().__init__(name)
        self._fronted = tuple(fronted)

    def fronts_platform(self, platform):
        return platform in self._fronted


def _runner(default_adapters, profile_adapters=None):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = default_adapters
    runner._profile_adapters = profile_adapters or {}
    runner.config = SimpleNamespace(platforms={})
    return runner


def _dropped_with_the_return_address(caplog):
    """Whether a drop WARNING named the damaged provenance and the return address."""
    return any(
        "incomplete or contradictory" in r.getMessage()
        and "C0CHANNEL" in r.getMessage()
        and "transport provenance" in r.getMessage()
        for r in caplog.records
    )


def _slack_event(**overrides):
    """A Slack completion as the watcher queues it."""
    evt = {
        "type": "process_completed",
        "session_id": "proc_slot_1",
        "session_key": "agent:main:slack:group:T0WORKSPACE:C0CHANNEL",
        "platform": "slack",
        "chat_type": "group",
        "chat_id": "C0CHANNEL",
        "scope_id": "T0WORKSPACE",
        "profile": "",
        "status": "completed",
    }
    evt.update(overrides)
    return evt


def _relayed_event(**overrides):
    """The same completion carrying exact relay provenance."""
    return _slack_event(
        transport_profile="default", transport_slot="relay", **overrides
    )


# Capture side: the slot is read off the receiving adapter.


def test_relay_ingress_records_the_relay_slot():
    """A Slack turn arriving over Relay records ``relay``, not ``slack``."""
    relay = _RelayAdapter()
    runner = _runner({Platform.SLACK: _Adapter("slack-native"), Platform.RELAY: relay})
    source = SimpleNamespace(
        platform=Platform.SLACK,
        profile="",
        delivered_via_upstream_relay=True,
    )
    source._transport_adapter_ref = weakref.ref(relay)

    assert runner._ingress_transport_slot(source) == "relay"
    assert runner._transport_owner_profile(source) == "default"
    assert runner._capture_transport_provenance(source) == ("default", "relay")


def test_native_ingress_records_its_own_slot():
    """A turn on the native adapter records that platform's slot."""
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: _RelayAdapter()})
    source = SimpleNamespace(
        platform=Platform.SLACK,
        profile="",
        delivered_via_upstream_relay=False,
    )
    source._transport_adapter_ref = weakref.ref(native)

    assert runner._ingress_transport_slot(source) == "slack"
    assert runner._capture_transport_provenance(source) == ("default", "slack")


def test_no_provenance_source_records_no_slot():
    """A hand-built or restored source keeps legacy alias resolution."""
    runner = _runner({Platform.SLACK: _Adapter("slack-native")})
    source = SimpleNamespace(platform=Platform.SLACK, profile="")

    assert runner._ingress_transport_slot(source) is None
    assert runner._capture_transport_provenance(source) is None, (
        "an unidentifiable transport must record neither field, so the "
        "completion is classified legacy rather than partial"
    )


# Completion side.


@pytest.mark.asyncio
async def test_relay_only_completion_is_delivered_through_the_relay():
    """Relay is the only transport: the completion succeeds through it."""
    relay = _RelayAdapter()
    runner = _runner({Platform.RELAY: relay})

    result = await runner._inject_watch_notification(
        "[task finished]", _relayed_event()
    )

    assert result is True, (
        f"injection returned {result!r} — the completion was dropped although "
        "the recorded ingress transport is live"
    )
    assert relay.handle_message.await_count == 1
    delivered = relay.handle_message.await_args[0][0]
    assert delivered.source.platform == Platform.SLACK, (
        "the logical platform must survive: the relay sends for slack"
    )


@pytest.mark.asyncio
async def test_mixed_map_relay_ingress_is_not_answered_by_the_native_adapter():
    """Both transports front Slack; a relayed turn returns over the relay."""
    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})

    result = await runner._inject_watch_notification(
        "[task finished]", _relayed_event()
    )

    assert result is True
    assert relay.handle_message.await_count == 1
    assert native.handle_message.await_count == 0, (
        "answered out of the native Slack bot although the originating turn "
        "arrived over the relay — a different credential than the user spoke to"
    )


@pytest.mark.asyncio
async def test_mixed_map_native_ingress_is_not_answered_by_the_relay():
    """Same topology, native-origin turn: the native adapter stays the route."""
    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})

    result = await runner._inject_watch_notification(
        "[task finished]",
        _slack_event(transport_profile="default", transport_slot="slack"),
    )

    assert result is True
    assert native.handle_message.await_count == 1
    assert relay.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_dead_ingress_transport_fails_closed_with_the_return_address(caplog):
    """The recorded slot is gone: drop loudly rather than pick the survivor."""
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification(
            "[x]", _relayed_event()
        )

    assert result is None
    assert native.handle_message.await_count == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "no longer live" in m and "'relay'" in m and "C0CHANNEL" in m
        for m in messages
    ), f"expected a WARNING naming the slot and the return address, got: {messages}"


@pytest.mark.asyncio
async def test_legacy_record_without_provenance_keeps_alias_resolution():
    """Control: a record carrying neither field still uses the alias router."""
    relay = _RelayAdapter()
    runner = _runner({Platform.RELAY: relay})

    result = await runner._inject_watch_notification("[task finished]", _slack_event())

    assert result is True
    assert relay.handle_message.await_count == 1


# Restart / recovery leg: the discriminator is durable.


@pytest.mark.asyncio
async def test_relay_slot_survives_a_gateway_restart(tmp_path, monkeypatch):
    """Checkpoint round-trip keeps the slot, so the relay still answers."""
    from tools import process_registry as pr_mod

    registry = pr_mod.ProcessRegistry()
    session = pr_mod.ProcessSession(
        id="proc_slot_restart",
        command="sleep 1",
        session_key="agent:main:slack:group:T0WORKSPACE:C0CHANNEL",
        pid=os.getpid(),
        started_at=1.0,
        watcher_platform="slack",
        watcher_chat_id="C0CHANNEL",
        watcher_chat_type="group",
        watcher_scope_id="T0WORKSPACE",
        watcher_profile="",
        watcher_transport_profile="default",
        watcher_transport_slot="relay",
        watcher_interval=5,
        notify_on_complete=True,
    )
    session.host_start_time = registry._safe_host_start_time(os.getpid())
    registry._running[session.id] = session

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr_mod, "CHECKPOINT_PATH", checkpoint)
    registry._write_checkpoint()
    assert json.loads(checkpoint.read_text())[0]["watcher_transport_slot"] == "relay"

    restarted = pr_mod.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 1
    watcher = next(
        w for w in restarted.pending_watchers if w["session_id"] == "proc_slot_restart"
    )
    assert watcher["transport_slot"] == "relay"

    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})
    evt = _slack_event(
        session_id=watcher["session_id"],
        session_key=watcher["session_key"],
        transport_profile=watcher["transport_profile"],
        transport_slot=watcher["transport_slot"],
    )

    assert await runner._inject_watch_notification("[task finished]", evt) is True
    assert relay.handle_message.await_count == 1
    assert native.handle_message.await_count == 0


# Malformed records: exactly one field present, or a slot the turn could not
# have arrived on. Owner and slot only identify a transport together, so a
# damaged pair is a return address that cannot be trusted, not a legacy record.


@pytest.mark.asyncio
async def test_a_contradictory_slot_drops_without_touching_either_adapter(caplog):
    """A slot the Slack turn could not have arrived on is dropped, not re-guessed."""
    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification(
            "[task finished]",
            _slack_event(transport_profile="default", transport_slot="discord"),
        )

    assert result is None, (
        "exact provenance was contradicted, yet another transport was chosen "
        "and the completion delivered"
    )
    assert native.handle_message.await_count == 0
    assert relay.handle_message.await_count == 0
    assert _dropped_with_the_return_address(caplog)


@pytest.mark.asyncio
async def test_a_slot_without_an_owner_drops_and_never_reaches_a_default_bot(caplog):
    """A slot with no owner must not be resolved against the default map."""
    default_slack = _Adapter("slack-default")
    alpha_slack = _Adapter("slack-alpha")
    runner = _runner(
        {Platform.SLACK: default_slack}, {"alpha": {Platform.SLACK: alpha_slack}}
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification(
            "[task finished]", _slack_event(profile="alpha", transport_slot="slack")
        )

    assert result is None, (
        "a partial record resolved to owner=default and answered through the "
        "default bot"
    )
    assert default_slack.handle_message.await_count == 0
    assert alpha_slack.handle_message.await_count == 0
    assert _dropped_with_the_return_address(caplog)


@pytest.mark.asyncio
async def test_an_owner_without_a_slot_drops_and_never_reaches_alias_resolution(caplog):
    """An owner with no slot is incomplete, not a legacy record."""
    default_slack = _Adapter("slack-default")
    alpha_slack = _Adapter("slack-alpha")
    runner = _runner(
        {Platform.SLACK: default_slack}, {"alpha": {Platform.SLACK: alpha_slack}}
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification(
            "[task finished]", _slack_event(profile="alpha", transport_profile="default")
        )

    assert result is None
    assert default_slack.handle_message.await_count == 0
    assert alpha_slack.handle_message.await_count == 0
    assert _dropped_with_the_return_address(caplog)


@pytest.mark.asyncio
async def test_a_damaged_record_still_drops_after_a_process_restart(
    tmp_path, monkeypatch, caplog
):
    """A checkpoint holding half a pair is dropped once it is replayed."""
    from tools import process_registry as pr_mod

    registry = pr_mod.ProcessRegistry()
    session = pr_mod.ProcessSession(
        id="proc_slot_damaged",
        command="sleep 1",
        session_key="agent:main:slack:group:T0WORKSPACE:C0CHANNEL",
        pid=os.getpid(),
        started_at=1.0,
        watcher_platform="slack",
        watcher_chat_id="C0CHANNEL",
        watcher_chat_type="group",
        watcher_scope_id="T0WORKSPACE",
        watcher_profile="",
        watcher_transport_profile="default",
        watcher_interval=5,
        notify_on_complete=True,
    )
    session.host_start_time = registry._safe_host_start_time(os.getpid())
    registry._running[session.id] = session

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr_mod, "CHECKPOINT_PATH", checkpoint)
    registry._write_checkpoint()

    restarted = pr_mod.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 1
    watcher = next(
        w for w in restarted.pending_watchers if w["session_id"] == "proc_slot_damaged"
    )

    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})
    evt = _slack_event(
        session_id=watcher["session_id"],
        transport_profile=watcher["transport_profile"],
        transport_slot=watcher["transport_slot"],
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification("[task finished]", evt)

    assert result is None
    assert native.handle_message.await_count == 0
    assert relay.handle_message.await_count == 0
    assert _dropped_with_the_return_address(caplog)


@pytest.mark.asyncio
async def test_a_damaged_delegation_record_still_drops_after_a_restart(
    tmp_path, monkeypatch, caplog
):
    """The async-delegation replay leg carries the damage through, and it drops."""
    from tools import async_delegation as ad

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")

    record = {
        "delegation_id": "del_damaged",
        "session_key": "agent:main:slack:group:T0WORKSPACE:C0CHANNEL",
        "goal": "g",
        "dispatched_at": 1.0,
        "platform": "slack",
        "chat_type": "group",
        "chat_id": "C0CHANNEL",
        "scope_id": "T0WORKSPACE",
        "transport_slot": "relay",
    }
    ad._persist_dispatch(record)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    assert ad.recover_abandoned_delegations() >= 1

    with ad._connect() as conn:
        event_json = conn.execute(
            "SELECT event_json FROM async_delegations WHERE delegation_id=?",
            ("del_damaged",),
        ).fetchone()[0]
    evt = json.loads(event_json)
    assert evt["transport_slot"] == "relay"
    assert not evt.get("transport_profile"), (
        "the replay invented an owner for a record that never carried one"
    )

    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification("[delegation done]", evt)

    assert result is None
    assert relay.handle_message.await_count == 0
    assert native.handle_message.await_count == 0
    assert _dropped_with_the_return_address(caplog)


def test_relay_ingress_outranks_a_native_registration_on_the_same_source():
    """The relay flag decides the slot, not the order of the lookups."""
    relay = _RelayAdapter()
    native = _Adapter("slack-native")
    runner = _runner({Platform.SLACK: native, Platform.RELAY: relay})
    source = SimpleNamespace(
        platform=Platform.SLACK,
        profile="",
        delivered_via_upstream_relay=True,
    )
    source._transport_adapter_ref = weakref.ref(native)

    assert runner._ingress_transport_slot(source) == "relay"
