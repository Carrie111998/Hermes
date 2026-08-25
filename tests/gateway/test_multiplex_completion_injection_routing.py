"""Regression: completion injection must resolve a secondary profile's adapter.

Under ``gateway.multiplex_profiles`` only the default profile's adapters live in
``GatewayRunner.adapters``; a secondary profile's live in
``_profile_adapters[profile]``. Contract under test: ``_inject_watch_notification``
resolves through the profile-aware resolver rather than ``self.adapters`` alone,
and logs a genuine miss rather than swallowing it.
"""

import logging
import os
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


def _runner(default_adapters, profile_adapters):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = default_adapters
    runner._profile_adapters = profile_adapters
    runner.config = SimpleNamespace(platforms={})
    return runner


def _alpha_event():
    """A completion for the alpha profile's Matrix room, as the watcher queues it."""
    return {
        "type": "process_completed",
        "session_id": "proc_alpha_1",
        "session_key": "agent:alpha:matrix:group:!room:example.org",
        "platform": "matrix",
        "chat_type": "group",
        "chat_id": "!room:example.org",
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_injection_resolves_secondary_profile_adapter():
    """Alpha's Matrix adapter is used although the default map has no Matrix entry."""
    alpha_adapter = _Adapter("matrix-alpha")
    telegram = _Adapter("telegram-default")
    runner = _runner(
        {Platform.TELEGRAM: telegram},
        {"alpha": {Platform.MATRIX: alpha_adapter}},
    )

    result = await runner._inject_watch_notification("[task finished]", _alpha_event())

    assert result is True, (
        f"injection returned {result!r} — the alpha completion was dropped at the "
        "bare `return None`, exactly the multiplex symptom this fixes"
    )
    assert alpha_adapter.handle_message.await_count == 1
    assert telegram.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_injection_does_not_leak_to_default_profile_adapter():
    """Fail closed rather than answering out of the default profile's bot."""
    default_matrix = _Adapter("matrix-default")
    runner = _runner({Platform.MATRIX: default_matrix}, {"alpha": {}})

    result = await runner._inject_watch_notification("[task finished]", _alpha_event())

    assert result is None
    assert default_matrix.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_default_profile_injection_unchanged():
    """Control: an ``agent:main`` completion still resolves through ``self.adapters``."""
    matrix = _Adapter("matrix-default")
    runner = _runner({Platform.MATRIX: matrix}, {})
    evt = _alpha_event()
    evt["session_key"] = "agent:main:matrix:group:!room:example.org"

    result = await runner._inject_watch_notification("[task finished]", evt)

    assert result is True
    assert matrix.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_unresolvable_adapter_is_logged_not_silent(caplog):
    """An unresolvable drop names the platform and profile it could not resolve."""
    runner = _runner({}, {"alpha": {}})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification("[x]", _alpha_event())

    assert result is None
    assert any(
        "no adapter for" in r.getMessage() and "alpha" in r.getMessage()
        for r in caplog.records
    ), f"expected a WARNING naming platform+profile, got: {[r.getMessage() for r in caplog.records]}"


# Transport provenance vs runtime namespace: when one shared credential serves
# several routed runtimes, resolving from ``source.profile`` either drops a
# completion whose transport is live, or answers from a different bot than the
# user spoke to.


def _shared_primary_event(**overrides):
    """A shared-primary Matrix completion whose runtime and transport disagree."""
    evt = _alpha_event()
    evt["profile"] = "alpha"
    # Capture always stamps the pair, so every well-formed record carries both.
    evt["transport_profile"] = "default"
    evt["transport_slot"] = "matrix"
    evt.update(overrides)
    return evt


@pytest.mark.asyncio
async def test_shared_primary_transport_into_runtime_with_no_adapter():
    """Runtime ``alpha`` owns no Matrix adapter; deliver on the live transport."""
    primary_matrix = _Adapter("matrix-primary")
    runner = _runner({Platform.MATRIX: primary_matrix}, {"alpha": {}})

    result = await runner._inject_watch_notification(
        "[task finished]", _shared_primary_event()
    )

    assert result is True, (
        "resolving from the runtime namespace finds _profile_adapters['alpha'] "
        "== {} and drops the completion, although the transport it was "
        "commissioned on is live"
    )
    assert primary_matrix.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_shared_primary_transport_is_not_switched_for_a_secondary_bot():
    """With ``alpha`` also owning a Matrix adapter, the originating one still wins."""
    primary_matrix = _Adapter("matrix-primary")
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner(
        {Platform.MATRIX: primary_matrix},
        {"alpha": {Platform.MATRIX: alpha_matrix}},
    )

    result = await runner._inject_watch_notification(
        "[task finished]", _shared_primary_event()
    )

    assert result is True
    assert primary_matrix.handle_message.await_count == 1
    assert alpha_matrix.handle_message.await_count == 0, (
        "delivered through the secondary bot although the originating turn "
        "arrived through the shared primary bot"
    )


@pytest.mark.asyncio
async def test_dedicated_per_profile_transport_still_uses_the_secondary_bot():
    """Control: ``alpha`` owns the transport it was commissioned on."""
    primary_matrix = _Adapter("matrix-primary")
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner(
        {Platform.MATRIX: primary_matrix},
        {"alpha": {Platform.MATRIX: alpha_matrix}},
    )

    result = await runner._inject_watch_notification(
        "[task finished]", _shared_primary_event(transport_profile="alpha")
    )

    assert result is True
    assert alpha_matrix.handle_message.await_count == 1
    assert primary_matrix.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_provenance_is_re_resolved_to_the_current_live_adapter():
    """Provenance is a name, so a reconnected adapter is picked up as-is."""
    stale_alpha = _Adapter("matrix-alpha-stale")
    live_alpha = _Adapter("matrix-alpha-live")
    profile_adapters = {"alpha": {Platform.MATRIX: stale_alpha}}
    runner = _runner({}, profile_adapters)
    # Reconnect swaps the registry entry after commissioning.
    profile_adapters["alpha"][Platform.MATRIX] = live_alpha

    result = await runner._inject_watch_notification(
        "[task finished]", _shared_primary_event(transport_profile="alpha")
    )

    assert result is True
    assert live_alpha.handle_message.await_count == 1
    assert stale_alpha.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_unresolvable_provenance_fails_closed_with_the_return_address(caplog):
    """A named provenance with no live adapter drops loudly, without falling through."""
    primary_matrix = _Adapter("matrix-primary")
    runner = _runner({Platform.MATRIX: primary_matrix}, {"alpha": {}})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification(
            "[x]", _shared_primary_event(transport_profile="alpha")
        )

    assert result is None
    assert primary_matrix.handle_message.await_count == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "transport provenance" in m and "'alpha'" in m and "!room:example.org" in m
        for m in messages
    ), f"expected a WARNING naming the provenance and the return address, got: {messages}"


# Restart / recovery leg.


@pytest.mark.asyncio
async def test_completion_routes_correctly_after_a_gateway_restart(tmp_path, monkeypatch):
    """Provenance survives a checkpoint round-trip and still selects the transport."""
    import json

    from tools import process_registry as pr_mod

    registry = pr_mod.ProcessRegistry()
    session = pr_mod.ProcessSession(
        id="proc_restart_1",
        command="sleep 1",
        session_key="agent:alpha:matrix:group:!room:example.org",
        pid=os.getpid(),
        started_at=1.0,
        watcher_platform="matrix",
        watcher_chat_id="!room:example.org",
        watcher_chat_type="group",
        watcher_user_id="@user:example.org",
        watcher_thread_id="",
        watcher_message_id="$evt",
        watcher_profile="alpha",
        watcher_transport_profile="default",
        watcher_transport_slot="matrix",
        watcher_interval=5,
        notify_on_complete=True,
    )
    session.host_start_time = registry._safe_host_start_time(os.getpid())
    registry._running[session.id] = session

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr_mod, "CHECKPOINT_PATH", checkpoint)
    registry._write_checkpoint()
    assert json.loads(checkpoint.read_text())[0]["watcher_transport_profile"] == "default"

    # Restart: nothing in memory survives.
    restarted = pr_mod.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 1
    watcher = next(
        w for w in restarted.pending_watchers if w["session_id"] == "proc_restart_1"
    )
    assert watcher["transport_profile"] == "default"
    assert watcher["transport_slot"] == "matrix"
    assert watcher["chat_type"] == "group"
    assert watcher["chat_id"] == "!room:example.org"

    # The completion, built the way _run_process_watcher builds it.
    primary_matrix = _Adapter("matrix-primary")
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner(
        {Platform.MATRIX: primary_matrix},
        {"alpha": {Platform.MATRIX: alpha_matrix}},
    )
    evt = {
        "type": "completion",
        "session_id": watcher["session_id"],
        "session_key": watcher["session_key"],
        "platform": watcher["platform"],
        "chat_type": watcher["chat_type"],
        "chat_id": watcher["chat_id"],
        "scope_id": watcher["scope_id"],
        "thread_id": watcher["thread_id"],
        "user_id": watcher["user_id"],
        "profile": watcher["profile"],
        "transport_profile": watcher["transport_profile"],
        "transport_slot": watcher["transport_slot"],
        "status": "completed",
    }

    assert await runner._inject_watch_notification("[task finished]", evt) is True
    assert primary_matrix.handle_message.await_count == 1
    assert alpha_matrix.handle_message.await_count == 0

    delivered = primary_matrix.handle_message.await_args[0][0]
    assert delivered.source.chat_id == "!room:example.org"
    assert delivered.source.chat_type == "group"


@pytest.mark.asyncio
async def test_legacy_record_without_provenance_keeps_runtime_resolution():
    """Records without provenance keep the runtime-profile behaviour."""
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner({}, {"alpha": {Platform.MATRIX: alpha_matrix}})

    evt = _alpha_event()          # no transport_profile at all
    assert "transport_profile" not in evt

    assert await runner._inject_watch_notification("[task finished]", evt) is True
    assert alpha_matrix.handle_message.await_count == 1


# Async-delegation completions, end to end on the injection path.


@pytest.mark.asyncio
async def test_async_delegation_matrix_colon_room_id_routes_to_the_full_id():
    """The captured address is used verbatim, keeping the whole Matrix room id."""
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner({}, {"alpha": {Platform.MATRIX: alpha_matrix}})

    evt = {
        "type": "async_delegation",
        "delegation_id": "del_1",
        "session_key": "agent:alpha:matrix:group:!room:example.org",
        "platform": "matrix",
        "chat_type": "group",
        "chat_id": "!room:example.org",
        "user_id": "@user:example.org",
        "profile": "alpha",
        "transport_profile": "alpha",
        "transport_slot": "matrix",
        "status": "completed",
    }

    assert await runner._inject_watch_notification("[delegation done]", evt) is True
    delivered = alpha_matrix.handle_message.await_args[0][0]
    assert delivered.source.chat_id == "!room:example.org"


@pytest.mark.asyncio
async def test_async_delegation_slack_scoped_session_routes_to_the_channel():
    """The workspace scope stays the scope and the channel stays the chat."""
    slack = _Adapter("slack-default")
    runner = _runner({Platform.SLACK: slack}, {})

    evt = {
        "type": "async_delegation",
        "delegation_id": "del_2",
        "session_key": "agent:main:slack:group:T0WORKSPACE:C0CHANNEL:U0USER",
        "platform": "slack",
        "chat_type": "group",
        "chat_id": "C0CHANNEL",
        "scope_id": "T0WORKSPACE",
        "user_id": "U0USER",
        "transport_profile": "default",
        "transport_slot": "slack",
        "status": "completed",
    }

    assert await runner._inject_watch_notification("[delegation done]", evt) is True
    delivered = slack.handle_message.await_args[0][0]
    assert delivered.source.chat_id == "C0CHANNEL"
    assert delivered.source.scope_id == "T0WORKSPACE"


@pytest.mark.asyncio
async def test_legacy_async_delegation_enrichment_uses_the_canonical_parser():
    """Session-key-only enrichment goes through the canonical parser."""
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner({}, {"alpha": {Platform.MATRIX: alpha_matrix}})

    evt = {
        "type": "async_delegation",
        "delegation_id": "del_legacy",
        "session_key": "agent:alpha:matrix:group:!room:example.org",
        "status": "completed",
    }
    runner._enrich_async_delegation_routing(evt)
    assert evt["chat_id"] == "!room:example.org"

    assert await runner._inject_watch_notification("[delegation done]", evt) is True
    delivered = alpha_matrix.handle_message.await_args[0][0]
    assert delivered.source.chat_id == "!room:example.org"


# Relay ingress registers the adapter under ``Platform.RELAY`` while the source
# keeps the logical platform, so ``_registered_transport_adapter`` cannot see it
# and provenance has to be recorded another way.


@pytest.mark.asyncio
async def test_relay_ingress_records_default_provenance():
    """A relayed turn routed into runtime ``alpha`` records ``default`` provenance."""
    import weakref

    relay = _Adapter("relay")
    alpha_slack = _Adapter("slack-alpha")
    runner = _runner(
        {Platform.RELAY: relay}, {"alpha": {Platform.SLACK: alpha_slack}},
    )
    source = SimpleNamespace(
        platform=Platform.SLACK,
        profile="alpha",
        delivered_via_upstream_relay=True,
    )
    source._transport_adapter_ref = weakref.ref(relay)

    assert runner._transport_owner_profile(source) == "default", (
        "a relayed turn recorded no transport provenance, so its completion "
        "would fall back to the runtime profile's own adapter"
    )
    assert runner._ingress_transport_slot(source) == "relay", (
        "the owner map alone cannot tell relay ingress from native ingress "
        "when both front the same logical platform"
    )
    assert runner._capture_transport_provenance(source) == ("default", "relay"), (
        "the pair must be captured atomically — a half record is dropped at "
        "delivery, not resolved"
    )


@pytest.mark.asyncio
async def test_relay_completion_is_delivered_through_the_relay():
    """That provenance sends the completion back out the relay chain.

    The relay stub deliberately does not advertise ``fronts_platform``: the
    recorded slot is exact provenance, so delivery must not depend on the
    alias resolver being able to re-derive it.
    """
    relay = _Adapter("relay")
    alpha_slack = _Adapter("slack-alpha")
    runner = _runner(
        {Platform.RELAY: relay}, {"alpha": {Platform.SLACK: alpha_slack}},
    )
    evt = {
        "type": "process_completed",
        "session_id": "proc_relay_1",
        "session_key": "agent:alpha:slack:group:T0WORKSPACE:C0CHANNEL",
        "platform": "slack",
        "chat_type": "group",
        "chat_id": "C0CHANNEL",
        "scope_id": "T0WORKSPACE",
        "profile": "alpha",
        "transport_profile": "default",
        "transport_slot": "relay",
        "status": "completed",
    }

    result = await runner._inject_watch_notification("[task finished]", evt)

    assert result is True, (
        f"injection returned {result!r} — the completion was dropped, which a "
        "'not the other bot' assertion alone would have passed"
    )
    assert relay.handle_message.await_count == 1, (
        "the completion did not leave through the relay the turn arrived on"
    )
    assert alpha_slack.handle_message.await_count == 0, (
        "delivered through the secondary profile's own Slack bot although the "
        "originating turn arrived over the shared relay transport"
    )
