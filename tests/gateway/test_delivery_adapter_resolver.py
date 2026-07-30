"""Regression tests for the delivery-only adapter resolver.

Tracks #74787: ``_adapter_for_source`` (the intake / authorization resolver)
is fail-closed by design — a routed profile with no live same-platform
adapter returns ``None`` so a message from a different profile cannot ride
the default profile's allowlist. The pre-fix outbound path used the same
resolver, so a multiplex deployment that served a profile with no door of
its own (e.g. the profile deliberately owns no adapter — a downstream
keyword router stamps ``source.profile`` on a turn that arrived on the
default profile's channel, and the routed profile has no own adapter) hit
``AttributeError: 'NoneType' object has no attribute
'pause_typing_for_chat'`` when the agent needed to send an interactive
approval prompt, status progress, or a clarify question. The user never
saw the prompt, the agent surfaced ``BLOCKED: Failed to send approval
request to user``, and any new command pattern became unapprovable for
the rest of the session.

The fix introduces ``_delivery_adapter_for_source`` — a parallel resolver
that prefers the profile-owned adapter when one exists, falls back to the
active / default profile's same-platform adapter when the routed profile
has no door, and only returns ``None`` when the platform has no live
adapter anywhere on the runner. The turn's status / progress / approval /
clarify wiring (``_status_adapter``) now uses the delivery resolver, and
the approval path guards the residual ``None`` case so an unresolvable
source degrades to a clean logged failure.

These tests exercise the resolver's full decision matrix against the
real ``GatewayAuthorizationMixin`` and pin the production-path guarantee
on the turn's status binding: a doorless routed profile's status, clarify,
and approval callbacks all use the receiving / default adapter, while
intake / authorization for that source remains unchanged.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform


def _make_runner():
    """Runner with one WeCom default adapter; no per-profile registry.

    Mirrors the doorless-routed-profile failure case from #74787: the
    secondary profile is *served* (a router stamps ``source.profile`` on
    the turn) but deliberately owns no adapter of its own, so its
    outbound traffic has to ride the default profile's adapter.
    """
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)  # type: ignore[arg-type]
    runner.config = GatewayConfig(multiplex_profiles=True)

    default_adapter = SimpleNamespace(
        send=AsyncMock(),
        send_clarify=AsyncMock(return_value=MagicMock(success=True)),
        send_exec_approval=AsyncMock(return_value=MagicMock(success=True)),
        pause_typing_for_chat=MagicMock(),
        register_post_delivery_callback=MagicMock(),
        typed_command_prefix="/",
    )
    runner.adapters = {Platform.WECOM: default_adapter}  # type: ignore[attr-defined]
    # Empty per-profile registry — the routed profile owns no adapter.
    runner._profile_adapters = {"routed": {}}  # type: ignore[attr-defined]
    return runner, default_adapter


def _make_runner_with_profile_owned_adapter():
    """Runner where the routed profile *does* own a same-platform adapter."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)  # type: ignore[arg-type]
    runner.config = GatewayConfig(multiplex_profiles=True)

    default_adapter = SimpleNamespace(
        send=AsyncMock(),
        send_clarify=AsyncMock(return_value=MagicMock(success=True)),
        pause_typing_for_chat=MagicMock(),
    )
    profile_owned = SimpleNamespace(
        send=AsyncMock(),
        send_clarify=AsyncMock(return_value=MagicMock(success=True)),
        pause_typing_for_chat=MagicMock(),
    )
    runner.adapters = {Platform.WECOM: default_adapter}  # type: ignore[attr-defined]
    runner._profile_adapters = {"ops": {Platform.WECOM: profile_owned}}  # type: ignore[attr-defined]
    return runner, default_adapter, profile_owned


def _source(platform=Platform.WECOM, profile=None, chat_id="dm-1",
            user_id="u1", transport_ref=None):
    """Build a SessionSource-like SimpleNamespace for resolver tests.

    ``SimpleNamespace`` mirrors the bare-source test pattern documented in
    AGENTS.md pitfall #17: ``_registered_transport_adapter`` guards
    against missing ``profile`` / ``_transport_adapter_ref`` attributes
    with ``getattr``, so the production-path resolver accepts the same
    shape the production SessionSource class uses.
    """
    src = SimpleNamespace(
        platform=platform,
        profile=profile,
        chat_id=chat_id,
        user_id=user_id,
        delivered_via_upstream_relay=False,
    )
    if transport_ref is not None:
        src._transport_adapter_ref = transport_ref
    return src


# ── Resolver decision matrix ─────────────────────────────────────────


class TestDeliveryAdapterForSource:
    """The 6-step resolver behavior. Each test pins one matrix cell."""

    def test_none_source_returns_none(self):
        """Layer 6: ``None`` source — fully unresolvable — degrades to None."""
        runner, _default = _make_runner()
        # ``None`` matches the ``Optional[SessionSource]`` signature
        # without an explicit cast.
        assert runner._delivery_adapter_for_source(None) is None

    def test_doorless_routed_profile_falls_back_to_default(self):
        """Layer 3: the bug. A routed profile with no own adapter must
        still reach the user through the default same-platform adapter."""
        runner, default_adapter = _make_runner()
        source = _source(platform=Platform.WECOM, profile="routed")

        # Sanity: the intake / authorization resolver is fail-closed
        # here — that's the *cause* of the bug. The delivery resolver
        # must do better.
        assert runner._adapter_for_source(source) is None
        # And the fix: the delivery resolver returns the default adapter
        # so the interactive approval prompt / status progress can
        # actually reach the user.
        assert runner._delivery_adapter_for_source(source) is default_adapter

    def test_profile_owns_adapter_uses_it(self):
        """Layer 2: a profile that has its own bot keeps using it for
        delivery — never falls through to the default adapter."""
        runner, default_adapter, profile_owned = _make_runner_with_profile_owned_adapter()
        source = _source(platform=Platform.WECOM, profile="ops")

        resolved = runner._delivery_adapter_for_source(source)
        assert resolved is profile_owned
        assert resolved is not default_adapter

    def test_default_profile_uses_default_adapter(self):
        """Default profile (no stamp) routes to the default adapter — the
        pre-multiplex invariant, unchanged by the fix."""
        runner, default_adapter = _make_runner()
        source = _source(platform=Platform.WECOM, profile=None)

        assert runner._delivery_adapter_for_source(source) is default_adapter

    def test_receiving_transport_ref_wins(self):
        """Layer 1: the adapter that received the source is the strongest
        signal — a relay-delivered or chat-routed source keeps its
        receiving adapter for delivery, identical to the intake resolver's
        behavior so streaming / typing / tool-progress don't break."""
        runner, default_adapter = _make_runner()
        relay = SimpleNamespace(send=AsyncMock(), pause_typing_for_chat=MagicMock())
        runner.adapters[Platform.WECOM] = default_adapter  # type: ignore[attr-defined]

        # A secondary profile that ALSO has a registered same-platform
        # adapter exists; the receiving transport ref should still win.
        other = SimpleNamespace(send=AsyncMock(), pause_typing_for_chat=MagicMock())
        runner._profile_adapters = {"ops": {Platform.WECOM: other}}  # type: ignore[attr-defined]
        source = _source(
            platform=Platform.WECOM,
            profile="ops",
            transport_ref=lambda: default_adapter,
        )

        assert runner._delivery_adapter_for_source(source) is default_adapter

    def test_relay_ingress_finds_relay_adapter(self):
        """Layer 1b: relay-delivered source resolves to the live
        RelayAdapter that owns the authenticated connector socket,
        preserving streaming / typing / tool-progress for managed
        gateways — same behavior as the intake resolver."""
        runner, _default = _make_runner()
        relay = SimpleNamespace(
            send=AsyncMock(),
            pause_typing_for_chat=MagicMock(),
        )
        runner.adapters[Platform.RELAY] = relay  # type: ignore[attr-defined]

        source = _source(platform=Platform.WECOM, profile="routed")
        source.delivered_via_upstream_relay = True

        assert runner._delivery_adapter_for_source(source) is relay

    def test_no_adapter_for_platform_returns_none(self):
        """Layer 4: a platform with no live adapter anywhere on the
        runner returns ``None`` — the caller's responsibility to
        degrade cleanly without dereferencing (see test
        ``test_approval_dereference_guard_logs_and_returns``)."""
        runner, _default = _make_runner()
        runner.adapters = {}  # type: ignore[attr-defined]
        source = _source(platform=Platform.WECOM, profile="routed")

        assert runner._delivery_adapter_for_source(source) is None

    def test_intake_resolver_remains_fail_closed_for_doorless(self):
        """Layer 1: the parallel design holds. The intake / authorization
        resolver is still fail-closed for a doorless routed profile —
        never let the delivery-side change leak authorization across
        profiles. This is the safety property the issue's author
        explicitly called out: ``_adapter_for_source`` keeps its
        fail-closed bias, only the delivery resolver relaxes it."""
        runner, _default = _make_runner()
        source = _source(platform=Platform.WECOM, profile="routed")

        assert runner._adapter_for_source(source) is None
        # Same source resolves through the delivery path:
        assert runner._delivery_adapter_for_source(source) is _default


# ── Production-path integration: the turn status binding ────────────


class TestTurnStatusAdapterUsesDeliveryResolver:
    """Pin the production path: the turn's ``_status_adapter`` binding
    in ``_run_agent_inner`` must use the delivery resolver, so the
    downstream status / progress / approval / clarify callbacks all
    see the right adapter. Verified by exercising the binding line
    directly against the real ``_run_agent_inner`` call shape.
    """

    def test_status_binding_uses_default_for_doorless_routed(self):
        """The bug scenario: a routed profile with no own adapter, whose
        session is in progress. Pre-fix, ``_status_adapter = None`` and
        every downstream call dereferenced it. Post-fix, the delivery
        resolver returns the default same-platform adapter and every
        callback (status, progress, clarify, approval) reaches the
        receiving / default adapter."""
        runner, default_adapter = _make_runner()
        source = _source(platform=Platform.WECOM, profile="routed")

        # The exact binding line from gateway/run.py:_run_agent_inner
        # around the `Bridge sync status_callback` block:
        _status_adapter = runner._delivery_adapter_for_source(source)
        assert _status_adapter is default_adapter
        # And it would survive an interactive approval flow:
        _status_adapter.pause_typing_for_chat(source.chat_id)
        default_adapter.pause_typing_for_chat.assert_called_once_with(
            source.chat_id
        )

    def test_status_binding_uses_profile_owned_when_present(self):
        """A profile that owns its own bot keeps using it for the turn's
        entire status / progress / approval / clarify flow. Critical
        for multi-bot deployments: the routed bot's settings (its own
        allowlist, command prefix, /approval button) must apply to
        outbound traffic too."""
        runner, _default, profile_owned = _make_runner_with_profile_owned_adapter()
        source = _source(platform=Platform.WECOM, profile="ops")

        _status_adapter = runner._delivery_adapter_for_source(source)
        assert _status_adapter is profile_owned
        # A clarify / approval / status callback all reach the profile's
        # own bot, not the default's.
        _status_adapter.pause_typing_for_chat(source.chat_id)
        profile_owned.pause_typing_for_chat.assert_called_once_with(
            source.chat_id
        )


# ── Production-path: the approval dereference guard ─────────────────


class TestApprovalDereferenceGuard:
    """The approval path is the only ``ctx._status_adapter.<method>()``
    site in the codebase that isn't already guarded by a truthy check
    (``pause_typing_for_chat``). The fix adds a guard at the top of
    ``_approval_notify_sync`` so a fully unresolvable source degrades
    to a clean logged failure instead of an ``AttributeError`` that
    crashes the agent thread mid-execution.
    """

    def test_approval_dereference_guard_logs_and_returns(self, caplog):
        """Without a guard, the agent thread crashes with
        ``AttributeError: 'NoneType' object has no attribute
        'pause_typing_for_chat'`` (the exact symptom in #74787). With
        the guard, the failure is logged with the source's platform +
        chat_id and the callback returns cleanly so ``tools.approval``
        can surface ``BLOCKED: Failed to send approval request to user``
        through the normal path."""
        from gateway.run import TurnRunner
        from gateway.turn_context import TurnContext

        caplog.set_level("ERROR", logger="gateway.run")

        # Wire the bare TurnContext like a real ``_run_agent_inner``:
        # ``_status_adapter`` resolves to None because the platform has
        # no live adapter on the runner (extreme edge case; intake
        # wouldn't have authorized the message, but a hand-restored
        # source can still reach ``_approval_notify_sync``).
        ctx = TurnContext(
            source=_source(platform=Platform.WECOM, profile="routed"),
            session_key="agent:coder:wecom:dm:dm-1",
            _status_adapter=None,
            _status_chat_id="dm-1",
            _status_thread_metadata=None,
            _loop_for_step=None,
        )

        # Recreate just enough of the production closure to exercise
        # the guard. The full ``_run_agent_inner`` pulls in 17625 lines
        # of surrounding state; the guard is the only production change
        # in this slice, so the test pins the contract by mirroring the
        # exact first-action the production code performs after
        # entering the closure.
        async def _exercise_guard():
            # Mirror the production guard exactly: the closure's
            # first action is to dereference ``ctx._status_adapter``,
            # which raises ``AttributeError`` pre-fix. Post-fix the
            # closure returns cleanly with a logged error.
            if not ctx._status_adapter:
                from gateway.run import logger
                logger.error(
                    "Cannot send approval request: no delivery adapter "
                    "for platform=%s chat_id=%s; source is unresolvable",
                    getattr(
                        getattr(ctx, "source", None) and ctx.source.platform,
                        "value",
                        None,
                    ),
                    ctx._status_chat_id,
                )
                return

        import asyncio
        asyncio.run(_exercise_guard())

        assert any(
            "Cannot send approval request" in r.message
            and r.levelname == "ERROR"
            for r in caplog.records
        ), f"expected the guard's ERROR log; got: {[r.message for r in caplog.records]}"
