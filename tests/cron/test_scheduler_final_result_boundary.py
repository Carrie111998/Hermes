"""End-to-end Core seam coverage for #1478 final-result delivery."""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import TimeoutError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.output_provenance import ProvenanceStore


class _Hooks:
    def __init__(self, decision: dict):
        self.decision = decision
        self.events: list[str] = []
        self.contexts: list[dict] = []

    async def emit_collect_strict(self, event: str, context: dict):
        self.events.append(event)
        self.contexts.append(dict(context))
        return [self.decision] if event == "outbound:before_send" else []


def _config():
    from gateway.config import Platform

    pconfig = MagicMock()
    pconfig.enabled = True
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: pconfig}
    return config


def _job():
    return {
        "id": "final-result-job",
        "name": "daily report",
        "deliver": "origin",
        "origin": {"platform": "telegram", "chat_id": "123"},
        "next_run_at": "2026-07-27T09:00:00+00:00",
    }


def _ledger(store: ProvenanceStore) -> dict:
    return json.loads(store.ledger_path.read_text(encoding="utf-8"))


def _pinned_live_transport(monkeypatch, *, result: object) -> tuple[object, AsyncMock]:
    """Install one resolved native transport and synchronously run its coroutine."""
    from gateway.config import Platform
    from gateway.delivery import DeliveryTransport

    adapter = SimpleNamespace(send=AsyncMock(return_value=result))
    transport = DeliveryTransport(
        adapter=adapter,
        config=_config().platforms[Platform.TELEGRAM],
        transport_platform=Platform.TELEGRAM,
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    monkeypatch.setattr("gateway.delivery.resolve_delivery_transport", lambda *_args: transport)
    monkeypatch.setattr(
        "agent.async_utils.safe_schedule_threadsafe",
        lambda coro, _loop: _Future(result=asyncio.run(coro)),
    )
    return loop, adapter.send


def test_protected_final_result_claims_then_sends_once(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    loop, send = _pinned_live_transport(monkeypatch, result={"success": True, "delivered": True})
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            _job(),
            "business-safe final result",
            protect_final_result=True,
            outbound_hooks=hooks,
            provenance_store=store,
            loop=loop,
        )

    assert result is None
    send.assert_awaited_once()
    assert hooks.contexts[0]["source_kind"] == "gateway_reply"
    assert hooks.events == ["outbound:before_send", "outbound:after_send"]
    assert hooks.contexts[1]["success"] is True
    targets = next(iter(_ledger(store)["occurrences"].values()))["targets"]
    assert list(targets.values())[0]["state"] == "sent"


@pytest.mark.parametrize("cancel_result", [True, False], ids=["cancel-accepted", "cancel-rejected"])
def test_protected_final_result_timeout_is_indeterminate_without_resend(
    tmp_path, monkeypatch, cancel_result,
):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    send = AsyncMock(return_value={"success": True, "delivered": True})
    transport = SimpleNamespace(
        adapter=SimpleNamespace(),
        config=_config().platforms[Platform.TELEGRAM],
        transport_platform=Platform.TELEGRAM,
        send=send,
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    future = _Future(error=TimeoutError(), cancel_result=cancel_result)
    schedule_count = 0

    def schedule(coro, _loop):
        nonlocal schedule_count
        schedule_count += 1
        coro.close()
        return future

    standalone_send = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("gateway.delivery.resolve_delivery_transport", lambda *_args: transport)
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", schedule)
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "tools.send_message_tool._send_to_platform", new=standalone_send,
    ):
        first = _deliver_result(
            _job(), "business-safe final result", protect_final_result=True,
            outbound_hooks=hooks, provenance_store=store, loop=loop,
        )
        second = _deliver_result(
            _job(), "business-safe final result", protect_final_result=True,
            outbound_hooks=hooks, provenance_store=store, loop=loop,
        )

    assert first == "protected final-result delivery is indeterminate telegram:123"
    assert "already issued" in second
    assert schedule_count == 1
    assert future.cancel_calls == 1
    send.assert_called_once()
    standalone_send.assert_not_awaited()
    target = next(iter(next(iter(_ledger(store)["occurrences"].values()))["targets"].values()))
    assert target["state"] == "indeterminate"
    assert target["post_send_repair"]["context"]["send_result"] == {"error": "in_flight_timeout"}


def test_post_send_repair_replays_only_persisted_observer_context(tmp_path):
    from cron.scheduler import repair_protected_final_result_after_send

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = store.issue(
        profile_id="profile", job_id="job", occurrence_id="occurrence", target_id="target",
        route_digest="sha256:route", raw_body=b"body", template_digest="sha256:template", producer_class="llm_final",
    )
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    store.begin_send(
        capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=b"body", rendered_body=b"body", route_digest="sha256:route",
    )
    context = {"source_kind": "gateway_reply", "content": "body", "send_result": {"success": True}}
    store.complete_claim(
        capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent",
        post_send_error="frame unavailable", post_send_repair_context=context,
    )
    hooks = _Hooks({"decision": "allow"})

    assert repair_protected_final_result_after_send(
        provenance_store=store, hooks=hooks, capability_id=claim["capability_id"],
    ) is None
    assert hooks.events == ["outbound:after_send"]
    assert hooks.contexts == [{
        **context,
        "observer_event_id": f"after-send:{claim['capability_id']}:{claim['claim_id']}:sent",
    }]
    assert store.pending_post_send_repairs() == []


def test_post_send_repair_failure_remains_pending_without_transport(tmp_path):
    from cron.scheduler import repair_protected_final_result_after_send

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = store.issue(profile_id="profile", job_id="job", occurrence_id="occurrence", target_id="target", route_digest="sha256:route", raw_body=b"body", template_digest="sha256:template", producer_class="llm_final")
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=b"body", rendered_body=b"body", route_digest="sha256:route")
    store.complete_claim(capability_id=claim["capability_id"], claim_id=claim["claim_id"], result="sent", post_send_error="broken", post_send_repair_context={"content": "body"})
    class BrokenHooks:
        async def emit_collect_strict(self, *_args, **_kwargs):
            raise RuntimeError("observer still broken")
    result = repair_protected_final_result_after_send(provenance_store=store, hooks=BrokenHooks(), capability_id=claim["capability_id"])
    assert "repair failed" in result
    assert store.pending_post_send_repairs()[0]["error"] == "observer still broken"


def test_next_protected_delivery_drains_a_stale_send_started_without_resending(tmp_path, monkeypatch):
    from cron.scheduler import recover_protected_final_result_repairs

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = store.issue(profile_id="profile", job_id="job", occurrence_id="occurrence", target_id="target", route_digest="sha256:route", raw_body=b"body", template_digest="sha256:template", producer_class="llm_final")
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=b"body", rendered_body=b"body", route_digest="sha256:route", post_send_repair_context={"content": "body"})
    ledger = _ledger(store)
    target = next(iter(next(iter(ledger["occurrences"].values()))["targets"].values()))
    target["send_started_at"] = "invalid"
    store.ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    hooks = _Hooks({"decision": "allow"})

    assert recover_protected_final_result_repairs(provenance_store=store, hooks=hooks) == []
    assert hooks.events == ["outbound:after_send"]
    assert store.pending_post_send_repairs() == []


def test_fresh_send_started_is_immediately_visible_to_the_restart_hard_gate(tmp_path):
    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    issued = store.issue(profile_id="profile", job_id="job", occurrence_id="occurrence", target_id="target", route_digest="sha256:route", raw_body=b"body", template_digest="sha256:template", producer_class="llm_final")
    claim = store.verify_and_claim(proof=issued["proof"], raw_body_b64=issued["raw_body_b64"], decision="allow")
    store.begin_send(capability_id=claim["capability_id"], claim_id=claim["claim_id"], body=b"body", rendered_body=b"body", route_digest="sha256:route")

    assert store.pending_post_send_repairs() == [{
        "capability_id": claim["capability_id"],
        "error": "send_started_recovery_pending",
        "context": {},
    }]


def test_recovery_reports_malformed_or_failed_durable_repairs(monkeypatch):
    from cron.scheduler import recover_protected_final_result_repairs

    class Store:
        def pending_post_send_repairs(self):
            return [{}, {"capability_id": "cap-1"}]

    monkeypatch.setattr("cron.scheduler.repair_protected_final_result_after_send", lambda **_kwargs: "still pending")
    assert recover_protected_final_result_repairs(provenance_store=Store(), hooks=object()) == [
        "protected final-result repair has no capability id", "still pending",
    ]
    assert recover_protected_final_result_repairs(provenance_store=object(), hooks=object()) == []


def test_scheduler_sweep_recovers_without_a_new_delivery(monkeypatch):
    from cron.scheduler import sweep_protected_final_result_repairs

    hooks = object()
    store = object()
    monkeypatch.setattr("gateway.outbound_boundary.load_installed_outbound_hooks", lambda _home: hooks)
    monkeypatch.setattr("cron.output_provenance.ProvenanceStore", lambda _home: store)
    monkeypatch.setattr("cron.scheduler.recover_protected_final_result_repairs", lambda **kwargs: ["recovered"] if kwargs == {"provenance_store": store, "hooks": hooks} else [])
    assert sweep_protected_final_result_repairs() == ["recovered"]


def test_profile_recovery_uses_the_requested_home_without_the_cron_jobs_lock(monkeypatch, tmp_path):
    from cron.scheduler import recover_protected_final_result_repairs_for_home

    hooks = object()
    store = object()
    monkeypatch.setattr("gateway.outbound_boundary.load_installed_outbound_hooks", lambda home: hooks if home == tmp_path else None)
    monkeypatch.setattr("cron.output_provenance.ProvenanceStore", lambda home: store if home == tmp_path else None)
    monkeypatch.setattr(
        "cron.scheduler.recover_protected_final_result_repairs",
        lambda **kwargs: ["recovered"] if kwargs == {"provenance_store": store, "hooks": hooks} else [],
    )

    assert recover_protected_final_result_repairs_for_home(tmp_path) == ["recovered"]


def test_scheduler_sweep_skips_when_no_activated_hook(monkeypatch):
    from cron.scheduler import sweep_protected_final_result_repairs
    monkeypatch.setattr("gateway.outbound_boundary.load_installed_outbound_hooks", lambda _home: None)
    assert sweep_protected_final_result_repairs() == []


def test_tick_logs_and_continues_when_recovery_sweep_fails(tmp_path, monkeypatch):
    from cron.scheduler import tick
    monkeypatch.setattr("cron.scheduler._get_lock_paths", lambda: (tmp_path, tmp_path / ".tick.lock"))
    monkeypatch.setattr("cron.scheduler.sweep_protected_final_result_repairs", lambda: (_ for _ in ()).throw(RuntimeError("bad sweep")))
    monkeypatch.setattr("cron.scheduler.get_due_jobs", lambda: [])
    assert tick(verbose=False) == 0


def test_tick_reports_pending_recovery_before_empty_due_list(tmp_path, monkeypatch):
    from cron.scheduler import tick
    monkeypatch.setattr("cron.scheduler._get_lock_paths", lambda: (tmp_path, tmp_path / ".tick.lock"))
    monkeypatch.setattr("cron.scheduler.sweep_protected_final_result_repairs", lambda: ["still pending"])
    monkeypatch.setattr("cron.scheduler.get_due_jobs", lambda: [])
    assert tick(verbose=False) == 0


def test_protected_delivery_stops_when_prior_durable_recovery_is_unresolved(monkeypatch):
    from cron.scheduler import _deliver_result

    store = _Store()
    monkeypatch.setattr("cron.scheduler.recover_protected_final_result_repairs", lambda **_kwargs: ["observer still pending"])
    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            _job(), "safe", protect_final_result=True,
            outbound_hooks=_Hooks({"decision": "allow"}), provenance_store=store,
        )
    assert result == "protected final-result recovery pending: observer still pending"


@pytest.mark.parametrize(
    ("is_home", "expected_metadata"),
    [
        (True, {"_relay_logical_platform": "telegram", "user_id": "u-1", "scope_id": "s-1"}),
        (False, {"_relay_logical_platform": "telegram", "scope_id": "cached-s"}),
    ],
)
def test_protected_relay_binds_exact_logical_target_before_proof(
    tmp_path, monkeypatch, is_home, expected_metadata,
):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform
    from gateway.delivery import DeliveryTransport

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    adapter = SimpleNamespace(
        send_for_platform=AsyncMock(return_value={"success": True, "delivered": True}),
        bound_outbound_metadata=lambda _chat_id, metadata: metadata if metadata.get("scope_id") else metadata | {"scope_id": "cached-s"},
    )
    config = _config()
    config.get_home_channel.return_value = SimpleNamespace(
        chat_id="123" if is_home else "other", user_id="u-1", scope_id="s-1",
    )
    transport = DeliveryTransport(
        adapter=adapter, config=config.platforms[Platform.TELEGRAM], transport_platform=Platform.RELAY,
    )
    loop = MagicMock()
    loop.is_running.return_value = True
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.delivery.resolve_delivery_transport", lambda *_args: transport)
    monkeypatch.setattr(
        "agent.async_utils.safe_schedule_threadsafe", lambda coro, _loop: _Future(result=asyncio.run(coro)),
    )

    with patch("gateway.config.load_gateway_config", return_value=config):
        assert _deliver_result(
            _job(), "business-safe final result", protect_final_result=True,
            outbound_hooks=hooks, provenance_store=store, loop=loop,
        ) is None

    sent_metadata = adapter.send_for_platform.await_args.kwargs["metadata"]
    for key, value in expected_metadata.items():
        assert sent_metadata[key] == value
    target = next(iter(next(iter(_ledger(store)["occurrences"].values()))["targets"].values()))
    assert target["route_digest"] != ""
    assert target["proof"]["route_digest"] == target["route_digest"]


def test_protected_final_result_deny_never_reaches_transport(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "deny", "reason": "private_runtime_context"})
    loop, send = _pinned_live_transport(monkeypatch, result={"success": True, "delivered": True})
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            _job(),
            "private final result",
            protect_final_result=True,
            outbound_hooks=hooks,
            provenance_store=store,
            loop=loop,
        )

    assert result is not None
    assert "boundary denied" in result
    send.assert_not_awaited()
    targets = next(iter(_ledger(store)["occurrences"].values()))["targets"]
    assert list(targets.values())[0]["state"] == "blocked"


def test_protected_final_result_rewrite_sends_only_replacement_bytes(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "rewrite", "reason": "projection", "content": "safe projected result"})
    loop, send = _pinned_live_transport(monkeypatch, result={"success": True, "delivered": True})
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            _job(),
            "raw internal trace /Users/alice/.hermes/private.log",
            protect_final_result=True,
            outbound_hooks=hooks,
            provenance_store=store,
            loop=loop,
        )

    assert result is None
    sent = send.await_args.args[1]
    assert "safe projected result" in sent
    assert "private.log" not in sent


def test_protected_final_result_blocks_private_wrapper_bytes(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    send = AsyncMock(return_value={"success": True})
    job = _job() | {"name": "/Users/alice/.hermes/private-job"}
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "tools.send_message_tool._send_to_platform", new=send
    ):
        result = _deliver_result(
            job,
            "business-safe final result",
            protect_final_result=True,
            outbound_hooks=hooks,
            provenance_store=store,
        )

    assert result is not None
    assert "private rendered bytes" in result
    send.assert_not_awaited()


def test_protected_final_result_blocks_bearer_value_in_wrapper_field(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    send = AsyncMock(return_value={"success": True})
    job = _job() | {"name": "Bearer opaque-secret"}
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "tools.send_message_tool._send_to_platform", new=send
    ):
        result = _deliver_result(job, "business-safe final result", protect_final_result=True, outbound_hooks=hooks, provenance_store=store)

    assert "private rendered bytes" in result
    send.assert_not_awaited()


def test_protected_final_result_rejects_media_before_hook_or_transport(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    send = AsyncMock(return_value={"success": True})
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "gateway.platforms.base.BasePlatformAdapter.extract_media",
        lambda _content: ([("/tmp/private.pdf", False)], "business-safe final result"),
    )
    monkeypatch.setattr(
        "gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
        lambda media: media,
    )

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "tools.send_message_tool._send_to_platform", new=send
    ):
        result = _deliver_result(
            _job(),
            "business-safe final result",
            protect_final_result=True,
            outbound_hooks=hooks,
            provenance_store=store,
        )

    assert result is not None
    assert "MEDIA attachments" in result
    assert hooks.contexts == []
    send.assert_not_awaited()


def test_protected_delivery_uses_resolved_relay_adapter(monkeypatch):
    from cron.scheduler import _deliver_result

    relay_adapter = MagicMock(name="relay-adapter")
    seen = {}
    monkeypatch.setattr(
        "gateway.delivery.resolve_delivery_transport",
        lambda *_args: SimpleNamespace(config=_config().platforms[next(iter(_config().platforms))], adapter=relay_adapter),
    )
    monkeypatch.setattr(
        "cron.scheduler._deliver_protected_final_result",
        lambda **kwargs: seen.setdefault("adapter", kwargs["runtime_adapter"]) and "blocked for test",
    )

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(_job(), "safe", protect_final_result=True, outbound_hooks=_Hooks({"decision": "allow"}), provenance_store=_Store())

    assert "blocked for test" in result
    assert seen["adapter"] is relay_adapter


def test_protected_delivery_preserves_mirror_after_success(monkeypatch):
    from cron.scheduler import _deliver_result

    seen = []
    monkeypatch.setattr("cron.scheduler._cron_mirror_delivery_enabled", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._target_matches_origin", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._deliver_protected_final_result", lambda **_kwargs: None)
    monkeypatch.setattr("cron.scheduler._maybe_mirror_cron_delivery", lambda *args, **kwargs: seen.append((args, kwargs)))

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(_job(), "safe", protect_final_result=True, outbound_hooks=_Hooks({"decision": "allow"}), provenance_store=_Store())

    assert result is None
    assert seen and seen[0][1]["enabled"] is True


def test_delivery_disables_mirroring_when_its_config_hook_raises(monkeypatch):
    from cron.scheduler import _deliver_result

    monkeypatch.setattr(
        "cron.scheduler._cron_mirror_delivery_enabled",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("bad mirror config")),
    )
    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            _job(), "safe", protect_final_result=True,
            outbound_hooks=_Hooks({"decision": "deny", "reason": "test"}),
            provenance_store=_Store(),
        )

    assert "requires a live pinned transport" in result


def test_protected_live_delivery_seeds_opened_thread_after_confirmed_send(monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    adapter = MagicMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    seeded = []
    monkeypatch.setattr("cron.scheduler._cron_mirror_delivery_enabled", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._target_matches_origin", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._open_continuable_cron_thread", lambda *_args: "thread-1")
    monkeypatch.setattr("cron.scheduler._deliver_protected_final_result", lambda **_kwargs: None)
    monkeypatch.setattr("cron.scheduler._seed_cron_thread_session", lambda *args, **kwargs: seeded.append((args, kwargs)))
    monkeypatch.setattr("cron.scheduler._maybe_mirror_cron_delivery", lambda *_args, **_kwargs: None)

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "gateway.delivery.resolve_delivery_transport",
        return_value=SimpleNamespace(config=_config().platforms[Platform.TELEGRAM], adapter=adapter, is_relay=False),
    ):
        assert _deliver_result(_job(), "safe", adapters={Platform.TELEGRAM: adapter}, loop=loop, protect_final_result=True, outbound_hooks=_Hooks({"decision": "allow"}), provenance_store=_Store()) is None

    assert seeded and seeded[0][0][4] == "thread-1"


def test_protected_live_delivery_keeps_origin_when_thread_open_returns_none(monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    adapter = MagicMock()
    loop = MagicMock()
    loop.is_running.return_value = True
    seen = {}
    monkeypatch.setattr("cron.scheduler._cron_mirror_delivery_enabled", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._target_matches_origin", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._open_continuable_cron_thread", lambda *_args: None)
    monkeypatch.setattr(
        "cron.scheduler._deliver_protected_final_result",
        lambda **kwargs: seen.setdefault("thread_id", kwargs["thread_id"]),
    )
    monkeypatch.setattr("cron.scheduler._maybe_mirror_cron_delivery", lambda *_args, **_kwargs: None)

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "gateway.delivery.resolve_delivery_transport",
        return_value=SimpleNamespace(
            config=_config().platforms[Platform.TELEGRAM], adapter=adapter, is_relay=False
        ),
    ):
        assert _deliver_result(
            _job(), "safe", adapters={Platform.TELEGRAM: adapter}, loop=loop,
            protect_final_result=True, outbound_hooks=_Hooks({"decision": "allow"}),
            provenance_store=_Store(),
        ) is None

    assert seen["thread_id"] is None


def test_protected_live_delivery_seeds_inchannel_session_after_confirmed_send(monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    adapter = MagicMock(supports_inchannel_continuable=True)
    loop = MagicMock()
    loop.is_running.return_value = True
    pconfig = _config().platforms[Platform.TELEGRAM]
    pconfig.extra = {"cron_continuable_surface": "in_channel"}
    seeded = []
    monkeypatch.setattr("cron.scheduler._cron_mirror_delivery_enabled", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._target_matches_origin", lambda *_args: True)
    monkeypatch.setattr("cron.scheduler._deliver_protected_final_result", lambda **_kwargs: None)
    monkeypatch.setattr("cron.scheduler._seed_cron_channel_session", lambda *args, **kwargs: seeded.append((args, kwargs)) or True)
    monkeypatch.setattr("cron.scheduler._maybe_mirror_cron_delivery", lambda *_args, **_kwargs: None)

    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "gateway.delivery.resolve_delivery_transport",
        return_value=SimpleNamespace(config=pconfig, adapter=adapter, is_relay=False),
    ):
        assert _deliver_result(_job(), "safe", adapters={Platform.TELEGRAM: adapter}, loop=loop, protect_final_result=True, outbound_hooks=_Hooks({"decision": "allow"}), provenance_store=_Store()) is None

    assert seeded


def test_unprotected_relay_never_falls_back_to_native_sender(monkeypatch):
    from cron.scheduler import _deliver_result
    from gateway.config import Platform

    relay = SimpleNamespace(config=_config().platforms[Platform.TELEGRAM], adapter=MagicMock(), is_relay=True)
    monkeypatch.setattr("gateway.delivery.resolve_delivery_transport", lambda *_args: relay)
    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(_job(), "safe")

    assert result == "relay delivery to telegram:123 failed"


def test_protected_final_result_unconfirmed_stays_indeterminate(tmp_path, monkeypatch):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    hooks = _Hooks({"decision": "allow", "reason": "safe"})
    loop, send = _pinned_live_transport(monkeypatch, result={"success": False, "delivered": False})
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            _job(),
            "business-safe final result",
            protect_final_result=True,
            outbound_hooks=hooks,
            provenance_store=store,
            loop=loop,
        )

    assert result is not None
    assert "unconfirmed" in result
    targets = next(iter(_ledger(store)["occurrences"].values()))["targets"]
    assert list(targets.values())[0]["state"] == "indeterminate"
    assert hooks.contexts[1]["success"] is False


def test_protected_live_route_preserves_telegram_channel_dm_topic(monkeypatch):
    from gateway.config import Platform
    from cron.scheduler import _protected_live_route

    monkeypatch.setattr("cron.scheduler._is_channel_dm_topic", lambda *_args: True)
    loop = MagicMock()
    loop.is_running.return_value = True

    route, target, metadata = _protected_live_route(
        job={"id": "topic-job"},
        platform=Platform.TELEGRAM,
        platform_name="telegram",
        chat_id="123456",
        thread_id="7",
        adapter=MagicMock(),
        loop=loop,
    )

    assert target.thread_id is None
    assert metadata == {"job_id": "topic-job", "direct_messages_topic_id": "7"}
    assert route["direct_messages_topic_id"] == "7"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"fire_claim": {"occurrence_id": "claim-id", "at": "old"}}, "claim-id"),
        ({"fire_claim": {"at": "2026-07-27T10:00:00Z"}}, "external:2026-07-27T10:00:00Z"),
        ({"fire_claim": {}, "next_run_at": "2026-07-27T09:30:00Z"}, "scheduled:2026-07-27T09:30:00Z"),
        ({"next_run_at": "2026-07-27T10:00:00Z"}, "scheduled:2026-07-27T10:00:00Z"),
    ],
)
def test_provenance_occurrence_prefers_persisted_firing_identity(job, expected):
    from cron.scheduler import _provenance_occurrence_id

    assert _provenance_occurrence_id(job) == expected


def test_provenance_occurrence_manual_runs_are_distinct(monkeypatch):
    from cron.scheduler import _provenance_occurrence_id

    values = iter(["one", "two"])
    monkeypatch.setattr("cron.scheduler.uuid.uuid4", lambda: MagicMock(hex=next(values)))
    assert _provenance_occurrence_id({}) == "manual:one"
    assert _provenance_occurrence_id({}) == "manual:two"


def test_final_rendered_byte_screen_rejects_non_utf8_and_private_values():
    from cron.scheduler import _final_delivery_bytes_safe

    assert _final_delivery_bytes_safe(b"business summary") is True
    assert _final_delivery_bytes_safe(b"api_key=secret") is False
    assert _final_delivery_bytes_safe(b"\xff") is False


class _Store:
    def __init__(self, *, decision="allow"):
        self.decision = decision
        self.events = []

    def issue(self, **_kwargs):
        self.events.append("issue")
        return {"proof": {"capability_id": "cap"}, "raw_body_b64": "c2FmZQ=="}

    def verify_and_claim(self, **_kwargs):
        self.events.append("claim")
        return {"decision": self.decision, "capability_id": "cap", "claim_id": "claim", "body_b64": "c2FmZQ=="}

    def begin_send(self, **_kwargs):
        self.events.append("begin")

    def block_claim(self, **_kwargs):
        self.events.append("blocked")

    def complete_claim(self, **kwargs):
        self.events.append(("complete", kwargs["result"]))


def _direct_protected_call(
    monkeypatch, store, *, hooks=None, adapters=None, loop=None, gate_mode="enforce",
):
    from cron.scheduler import _deliver_protected_final_result
    from gateway.config import Platform

    monkeypatch.setattr(
        "cron.scheduler._protected_live_route",
        lambda **_kwargs: (
            {"platform": "telegram", "chat_id": "123", "thread_id": "", "direct_messages_topic_id": ""},
            MagicMock(),
            {"job_id": "final-result-job"},
        ),
    )
    runtime_adapter = (adapters or {}).get(Platform.TELEGRAM)
    runtime_transport = None
    if runtime_adapter is not None:
        runtime_transport = SimpleNamespace(
            transport_platform=Platform.TELEGRAM,
            send=AsyncMock(return_value={"success": True, "delivered": True}),
        )
    return _deliver_protected_final_result(
        job=_job(), content="safe", platform=Platform.TELEGRAM, platform_name="telegram", chat_id="123",
        thread_id=None,
        pconfig=MagicMock(),
        adapters=adapters,
        runtime_adapter=runtime_adapter,
        runtime_transport=runtime_transport,
        loop=loop,
        hooks=hooks or _Hooks({"decision": "allow"}), provenance_store=store,
        wrap_response=False, occurrence_id="occurrence", gate_mode=gate_mode,
    )


@pytest.mark.parametrize("gate_mode", ["enforce", "observe", "warn", "downgrade"])
def test_protected_final_result_gate_mode_reaches_outbound_context(monkeypatch, gate_mode):
    hooks = _Hooks({"decision": "allow"})

    result = _direct_protected_call(monkeypatch, _Store(), hooks=hooks, gate_mode=gate_mode)

    assert "requires a live pinned transport" in result
    assert hooks.contexts[0]["boundary_enabled"] is True
    assert hooks.contexts[0]["gate_mode"] == gate_mode


def test_protected_delivery_provenance_or_boundary_failure_never_sends(monkeypatch):
    class BrokenStore(_Store):
        def issue(self, **_kwargs):
            raise ValueError("bad proof")

    result = _direct_protected_call(monkeypatch, BrokenStore())
    assert result == "protected final-result boundary denied telegram:123: bad proof"


def test_protected_delivery_private_render_blocks_and_survives_after_hook_failure(monkeypatch):
    store = _Store()

    class BrokenAfterHooks(_Hooks):
        async def emit_collect_strict(self, event, context):
            if event == "outbound:after_send":
                raise RuntimeError("audit unavailable")
            return await super().emit_collect_strict(event, context)

    monkeypatch.setattr("cron.scheduler._render_cron_delivery_content", lambda *_args, **_kwargs: "api_key=secret")
    result = _direct_protected_call(monkeypatch, store, hooks=BrokenAfterHooks({"decision": "allow"}))
    assert "blocked private rendered bytes" in result
    assert store.events == ["issue", "claim", "blocked"]


def test_protected_delivery_without_live_transport_blocks_before_standalone_send(monkeypatch):
    store = _Store()
    send = AsyncMock(side_effect=RuntimeError("network lost"))
    with patch("tools.send_message_tool._send_to_platform", new=send):
        result = _direct_protected_call(monkeypatch, store)
    assert "requires a live pinned transport" in result
    send.assert_not_awaited()
    assert store.events == ["issue", "claim", "begin", ("complete", "blocked")]


def test_protected_delivery_without_live_transport_never_attempts_standalone(monkeypatch):
    store = _Store()
    send = AsyncMock(return_value="not a receipt")
    with patch("tools.send_message_tool._send_to_platform", new=send):
        result = _direct_protected_call(monkeypatch, store)
    assert "requires a live pinned transport" in result
    send.assert_not_awaited()
    assert store.events[-1] == ("complete", "blocked")


def test_protected_delivery_tolerates_after_send_observer_failure(monkeypatch):
    store = _Store()

    class BrokenAfterHooks(_Hooks):
        async def emit_collect_strict(self, event, context):
            if event == "outbound:after_send":
                raise RuntimeError("audit unavailable")
            return await super().emit_collect_strict(event, context)

    loop = MagicMock()
    loop.is_running.return_value = True
    from gateway.config import Platform
    def scheduled(coro, _loop):
        coro.close()
        return _Future(result={"success": True, "delivered": True})

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", scheduled)
    result = _direct_protected_call(
        monkeypatch, store, hooks=BrokenAfterHooks({"decision": "allow"}),
        adapters={Platform.TELEGRAM: MagicMock()}, loop=loop,
    )
    assert result is None
    assert store.events[-1] == ("complete", "sent")


def test_protected_delivery_never_runs_observer_before_terminal_persistence(monkeypatch):
    store = _Store()
    observer_calls = []

    def reject_terminal(**_kwargs):
        raise OSError("ledger unavailable")

    store.complete_claim = reject_terminal
    store.claim_post_send_repair = lambda **_kwargs: observer_calls.append("claimed")
    loop = MagicMock()
    loop.is_running.return_value = True
    from gateway.config import Platform

    def scheduled(coro, _loop):
        coro.close()
        return _Future(result={"success": True, "delivered": True})

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", scheduled)
    result = _direct_protected_call(
        monkeypatch, store, adapters={Platform.TELEGRAM: MagicMock()}, loop=loop,
    )

    assert "indeterminate" in result
    assert observer_calls == []


def test_protected_delivery_keeps_persisted_observer_pending_when_worker_raises(monkeypatch):
    store = _Store()
    store.claim_post_send_repair = lambda **_kwargs: {"repair_id": "repair"}
    loop = MagicMock()
    loop.is_running.return_value = True
    from gateway.config import Platform

    def scheduled(coro, _loop):
        coro.close()
        return _Future(result={"success": True, "delivered": True})

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", scheduled)
    monkeypatch.setattr("cron.scheduler.repair_protected_final_result_after_send", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("observer unavailable")))
    result = _direct_protected_call(
        monkeypatch, store, adapters={Platform.TELEGRAM: MagicMock()}, loop=loop,
    )

    assert result is None
    assert store.events[-1] == ("complete", "sent")


def test_protected_delivery_live_non_dict_receipt_uses_adapter_confirmation(monkeypatch):
    store = _Store()
    loop = MagicMock()
    loop.is_running.return_value = True
    from gateway.config import Platform
    def scheduled(coro, _loop):
        coro.close()
        return _Future(result="adapter receipt")

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", scheduled)
    monkeypatch.setattr("cron.scheduler._confirm_adapter_delivery", lambda _receipt: True)
    with patch("gateway.config.load_gateway_config", return_value=_config()), patch("gateway.delivery.DeliveryRouter"):
        result = _direct_protected_call(monkeypatch, store, adapters={Platform.TELEGRAM: MagicMock()}, loop=loop)
    assert result is None
    assert store.events[-1] == ("complete", "sent")


def test_protected_live_delivery_bypasses_router_re_resolution(monkeypatch):
    store = _Store()
    loop = MagicMock()
    loop.is_running.return_value = True
    from gateway.config import Platform
    router = MagicMock()

    def scheduled(coro, _loop):
        coro.close()
        return _Future(result={"success": True, "delivered": True})

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", scheduled)
    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "gateway.delivery.DeliveryRouter", return_value=router
    ):
        assert _direct_protected_call(monkeypatch, store, adapters={Platform.TELEGRAM: MagicMock()}, loop=loop) is None

    router._deliver_to_platform.assert_not_called()


def test_protected_delivery_rejects_router_oversize_before_claimed_send(monkeypatch):
    from gateway.delivery import MAX_PLATFORM_OUTPUT

    store = _Store()
    monkeypatch.setattr("cron.scheduler._render_cron_delivery_content", lambda *_args, **_kwargs: "x" * (MAX_PLATFORM_OUTPUT + 1))
    result = _direct_protected_call(monkeypatch, store)

    assert "oversized rendered bytes" in result
    assert store.events == ["issue", "claim", "blocked"]


def test_protected_standalone_sender_is_never_used(monkeypatch):
    store = _Store()
    send = AsyncMock(return_value={"success": True})
    with patch("tools.send_message_tool._send_to_platform", new=send):
        result = _direct_protected_call(monkeypatch, store)

    assert "requires a live pinned transport" in result
    send.assert_not_awaited()


def test_protected_delivery_preserves_indeterminate_when_completion_fails(monkeypatch):
    class CompletionBrokenStore(_Store):
        def complete_claim(self, **_kwargs):
            raise RuntimeError("ledger unavailable")

    with patch("tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=RuntimeError("network lost"))):
        result = _direct_protected_call(monkeypatch, CompletionBrokenStore())
    assert "indeterminate" in result


def test_protected_delivery_loader_paths_fail_closed(monkeypatch, tmp_path):
    from cron.scheduler import _deliver_result

    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.outbound_boundary.load_installed_outbound_hooks", lambda _home: (_ for _ in ()).throw(RuntimeError("handler missing")))
    result = _deliver_result(_job(), "safe", protect_final_result=True)
    assert result == "protected final-result boundary unavailable: handler missing"

    monkeypatch.setattr("gateway.outbound_boundary.load_installed_outbound_hooks", lambda _home: None)
    assert _deliver_result(_job(), "safe", protect_final_result=True) == (
        "protected final-result boundary unavailable: required outbound hook is not installed"
    )


def test_protected_delivery_rejects_job_profile_id_that_differs_from_active_home(monkeypatch, tmp_path):
    from cron.scheduler import _deliver_result

    store = ProvenanceStore(tmp_path)
    store.bootstrap()
    job = _job()
    job["profile_id"] = "yuange"
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)

    with patch("gateway.config.load_gateway_config", return_value=_config()):
        result = _deliver_result(
            job, "safe", protect_final_result=True, outbound_hooks=_Hooks({"decision": "allow"}),
            provenance_store=store,
        )

    assert result == "protected final-result boundary denied telegram:123: profile identity mismatch"


def test_legacy_standalone_fallback_runs_in_a_fresh_thread_when_loop_is_active(monkeypatch):
    from cron.scheduler import _deliver_result

    send = AsyncMock(return_value={"success": True})

    async def invoke():
        with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
            "tools.send_message_tool._send_to_platform", new=send
        ):
            return _deliver_result(_job(), "safe")

    assert asyncio.run(invoke()) is None
    assert send.await_count == 1


def test_legacy_delivery_reports_interpreter_shutdown_as_skipped(monkeypatch):
    from cron.scheduler import _deliver_result

    monkeypatch.setattr("cron.scheduler._interpreter_shutting_down", lambda *_args: True)
    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=RuntimeError("shutdown"))
    ):
        result = _deliver_result(_job(), "safe")

    assert "skipped" in result
    assert "interpreter is shutting down" in result


def test_thread_pool_fallback_shutdown_is_skipped_without_escaping(monkeypatch):
    from cron.scheduler import _deliver_result

    class BrokenPool:
        def __init__(self, **_kwargs):
            pass

        def submit(self, _fn):
            raise RuntimeError("pool shutdown")

        def shutdown(self, **_kwargs):
            pass

    monkeypatch.setattr("cron.scheduler.concurrent.futures.ThreadPoolExecutor", BrokenPool)
    monkeypatch.setattr("cron.scheduler._interpreter_shutting_down", lambda exc=None: str(exc) == "pool shutdown")

    async def invoke():
        with patch("gateway.config.load_gateway_config", return_value=_config()):
            return _deliver_result(_job(), "safe")

    result = asyncio.run(invoke())
    assert "skipped" in result
    assert "interpreter is shutting down" in result


class _Future:
    def __init__(self, *, result=None, error=None, cancel_result=False):
        self._result = result
        self._error = error
        self._cancel_result = cancel_result
        self.cancel_calls = 0

    def result(self, timeout):
        assert timeout == 60
        if self._error:
            raise self._error
        return self._result

    def cancel(self):
        self.cancel_calls += 1
        return self._cancel_result


@pytest.mark.parametrize(
    ("future", "expected", "terminal"),
    [
        (None, "could not schedule", "blocked"),
        (_Future(result={"success": False}), "unconfirmed", "indeterminate"),
        (_Future(result={"success": True, "delivered": True}), None, "sent"),
    ],
)
def test_protected_live_delivery_fences_scheduler_dispositions(monkeypatch, future, expected, terminal):
    store = _Store()
    loop = MagicMock()
    loop.is_running.return_value = True
    adapters = {object(): MagicMock()}
    from gateway.config import Platform
    adapters = {Platform.TELEGRAM: MagicMock()}

    def scheduled(coro, _loop):
        coro.close()
        return future

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", scheduled)
    with patch("gateway.config.load_gateway_config", return_value=_config()), patch(
        "gateway.delivery.DeliveryRouter"
    ):
        result = _direct_protected_call(monkeypatch, store, adapters=adapters, loop=loop)

    assert store.events[-1] == ("complete", terminal)
    if expected is None:
        assert result is None
    else:
        assert expected in result
