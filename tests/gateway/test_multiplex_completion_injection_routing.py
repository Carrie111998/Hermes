"""Regression: completion injection must resolve a SECONDARY profile's adapter.

Under ``gateway.multiplex_profiles`` one gateway process serves the default
profile plus every named profile under ``~/.hermes/profiles/``. Only the
default profile's adapters live in ``GatewayRunner.adapters``; each secondary
profile's adapters live in ``_profile_adapters[profile]``.

``_inject_watch_notification`` — the single funnel every background-process and
async-delegation completion passes through — resolved its adapter from
``self.adapters`` only (the alias-aware ``resolve_delivery_transport`` call and
the legacy literal scan both read that one map). For a secondary profile that
map has no entry for the profile's platform, so resolution fell through to a
bare ``return None``: registration proven, drain proven, and then no delivery,
no synthetic turn, and no log line naming the drop.

The kanban notifier already resolves the same problem correctly via
``_adapter_for_source``/``_authorization_adapter``
(``gateway/authz_mixin.py``), which reads ``source.profile`` and fails closed
rather than replying out of the default profile's bot. Contract under test:
the injection path resolves through that same profile-aware resolver, and a
genuine miss is logged rather than swallowed.
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
    """A completion for the alpha profile's Matrix room (served by its own
    ``@alpha-bot:example.org`` connection), as the watcher queues it."""
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
    """The alpha profile's Matrix adapter must receive the completion, even
    though ``self.adapters`` (the default profile's map) has no Matrix entry."""
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
    """Fail closed: when the alpha profile has no Matrix adapter, the default
    profile's Matrix adapter must NOT be used — that answers out of the wrong
    bot."""
    default_matrix = _Adapter("matrix-default")
    runner = _runner({Platform.MATRIX: default_matrix}, {"alpha": {}})

    result = await runner._inject_watch_notification("[task finished]", _alpha_event())

    assert result is None
    assert default_matrix.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_default_profile_injection_unchanged():
    """Control: a default-profile (``agent:main``) completion still resolves
    through ``self.adapters`` exactly as before."""
    matrix = _Adapter("matrix-default")
    runner = _runner({Platform.MATRIX: matrix}, {})
    evt = _alpha_event()
    evt["session_key"] = "agent:main:matrix:group:!room:example.org"

    result = await runner._inject_watch_notification("[task finished]", evt)

    assert result is True
    assert matrix.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_unresolvable_adapter_is_logged_not_silent(caplog):
    """The drop that used to be a bare ``return None`` must name the platform
    and profile it could not resolve."""
    runner = _runner({}, {"alpha": {}})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification("[x]", _alpha_event())

    assert result is None
    assert any(
        "no adapter for" in r.getMessage() and "alpha" in r.getMessage()
        for r in caplog.records
    ), f"expected a WARNING naming platform+profile, got: {[r.getMessage() for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Transport provenance vs runtime namespace
#
# ``source.profile`` is the RUNTIME namespace, not the transport owner. When one
# shared credential serves several routed runtimes, resolving a completion from
# the runtime namespace is wrong in both directions:
#
#   (a) the runtime registers no adapter for the platform -> the completion is
#       dropped although the originating transport is live;
#   (b) the runtime owns its OWN adapter for that platform -> the completion is
#       delivered from a different bot than the one the user spoke to.
#
# So the commissioning turn records WHICH TRANSPORT it arrived on, as a name,
# and the completion re-resolves whatever adapter is live for that name now.
# ---------------------------------------------------------------------------


def _shared_primary_event(**overrides):
    """A completion commissioned on the SHARED PRIMARY Matrix transport by a
    turn routed into the secondary runtime ``alpha``.

    ``profile`` (runtime) and ``transport_profile`` (transport owner) disagree —
    which is the whole point.
    """
    evt = _alpha_event()
    evt["profile"] = "alpha"
    evt["transport_profile"] = "default"
    evt.update(overrides)
    return evt


@pytest.mark.asyncio
async def test_shared_primary_transport_into_runtime_with_no_adapter():
    """(a) Routed runtime ``alpha`` owns NO Matrix adapter, but the turn arrived
    on the shared primary transport, which is live. Deliver on it."""
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
    """(b) Same route, but ``alpha`` ALSO owns its own Matrix adapter. The
    completion must go back out the ORIGINATING (primary) transport — answering
    from ``alpha``'s bot is a wrong-credential delivery to a user who never
    spoke to it."""
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
    """(c) Control — the original topology. ``alpha`` owns the transport it was
    commissioned on, so provenance names ``alpha`` and delivery is unchanged."""
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
    """Provenance is a NAME, never the adapter object captured at commissioning
    time. An adapter that reconnected (or a whole restarted process) must be
    picked up as-is."""
    stale_alpha = _Adapter("matrix-alpha-stale")
    live_alpha = _Adapter("matrix-alpha-live")
    profile_adapters = {"alpha": {Platform.MATRIX: stale_alpha}}
    runner = _runner({}, profile_adapters)
    # Reconnect swaps the registry entry underneath us.
    profile_adapters["alpha"][Platform.MATRIX] = live_alpha

    result = await runner._inject_watch_notification(
        "[task finished]", _shared_primary_event(transport_profile="alpha")
    )

    assert result is True
    assert live_alpha.handle_message.await_count == 1
    assert stale_alpha.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_unresolvable_provenance_fails_closed_with_the_return_address(caplog):
    """A named provenance with no live adapter must drop — never silently, and
    never by falling through to some other profile's bot."""
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


# ---------------------------------------------------------------------------
# Restart / recovery leg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_routes_correctly_after_a_gateway_restart(tmp_path, monkeypatch):
    """(f) Serialize the watcher record, drop ALL in-memory state, reload from
    the checkpoint, and complete: the provenance must survive and still select
    the originating transport."""
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
        watcher_interval=5,
        notify_on_complete=True,
    )
    session.host_start_time = registry._safe_host_start_time(os.getpid())
    registry._running[session.id] = session

    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr_mod, "CHECKPOINT_PATH", checkpoint)
    registry._write_checkpoint()
    assert json.loads(checkpoint.read_text())[0]["watcher_transport_profile"] == "default"

    # --- restart: nothing in memory survives ---
    restarted = pr_mod.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 1
    watcher = next(
        w for w in restarted.pending_watchers if w["session_id"] == "proc_restart_1"
    )
    assert watcher["transport_profile"] == "default"
    assert watcher["chat_type"] == "group"
    assert watcher["chat_id"] == "!room:example.org"

    # --- the completion, built the way _run_process_watcher builds it ---
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
    """Records queued before provenance was captured (or restored from a
    pre-upgrade checkpoint) must keep the runtime-profile behaviour."""
    alpha_matrix = _Adapter("matrix-alpha")
    runner = _runner({}, {"alpha": {Platform.MATRIX: alpha_matrix}})

    evt = _alpha_event()          # no transport_profile at all
    assert "transport_profile" not in evt

    assert await runner._inject_watch_notification("[task finished]", evt) is True
    assert alpha_matrix.handle_message.await_count == 1


# ---------------------------------------------------------------------------
# Async-delegation completions, end to end on the injection path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_delegation_matrix_colon_room_id_routes_to_the_full_id():
    """(d) The captured address is used verbatim: the completion goes to
    ``!room:example.org``, not to the ``!room`` a positional key split yields."""
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
        "status": "completed",
    }

    assert await runner._inject_watch_notification("[delegation done]", evt) is True
    delivered = alpha_matrix.handle_message.await_args[0][0]
    assert delivered.source.chat_id == "!room:example.org"


@pytest.mark.asyncio
async def test_async_delegation_slack_scoped_session_routes_to_the_channel():
    """(e) The workspace scope stays the scope and the channel stays the chat."""
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
        "status": "completed",
    }

    assert await runner._inject_watch_notification("[delegation done]", evt) is True
    delivered = slack.handle_message.await_args[0][0]
    assert delivered.source.chat_id == "C0CHANNEL"
    assert delivered.source.scope_id == "T0WORKSPACE"


@pytest.mark.asyncio
async def test_legacy_async_delegation_enrichment_uses_the_canonical_parser():
    """A legacy event with only a session_key is enriched through the canonical
    parser, so even the fallback keeps the whole Matrix room id."""
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


# ---------------------------------------------------------------------------
# Relay ingress — the receiving adapter is registered under Platform.RELAY
# while the source keeps the logical platform, so the registry check in
# ``_registered_transport_adapter`` cannot see it. Provenance must still be
# recorded, or a relayed turn's completion falls back to runtime-profile
# resolution and answers out of a secondary profile's own bot.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_ingress_records_default_provenance():
    """A relayed turn routed into runtime ``alpha`` records ``default``
    provenance, not nothing."""
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


@pytest.mark.asyncio
async def test_relay_completion_is_not_answered_by_a_secondary_bot():
    """With that provenance the completion goes back out the relay chain, not
    out ``alpha``'s native Slack bot."""
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
        "status": "completed",
    }

    await runner._inject_watch_notification("[task finished]", evt)

    assert alpha_slack.handle_message.await_count == 0, (
        "delivered through the secondary profile's own Slack bot although the "
        "originating turn arrived over the shared relay transport"
    )
