"""Webhook-side contracts for durable messaging session handoff."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.platforms.webhook as webhook_module
from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.base import MessageEvent, ProcessingOutcome, SendResult
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import AsyncSessionStore, SessionStore, build_session_key
from hermes_state import AsyncSessionDB, SessionDB


def _make_adapter(routes) -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "routes": routes},
        )
    )


def _with_admission_lock_api(db, *, acquire=True, has_input=True):
    """Add the crash-released admission fence to a narrow DB test double."""
    db.try_acquire_webhook_delivery_admission_lock = AsyncMock(
        return_value=acquire
    )
    db.release_webhook_delivery_admission_lock = AsyncMock(return_value=True)
    db.ensure_webhook_delivery_admission_lock = AsyncMock(
        return_value=acquire
    )
    db.has_webhook_handoff_input = AsyncMock(return_value=has_input)
    return db


def _admitting_handler(adapter: WebhookAdapter, captured=None):
    """Test double that resolves the adapter's admission future."""
    async def _handle(event):
        if captured is not None:
            captured.append(event)
        adapter._resolve_handoff_admission(event, True)

    return AsyncMock(side_effect=_handle)


def _handoff_routes(**overrides):
    route = {
        "secret": _INSECURE_NO_AUTH,
        "prompt": "{message}",
        "handoff_to": "discord",
    }
    route.update(overrides)
    return {"alerts": route}


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post(
        "/p/{profile}/webhooks/{route_name}", adapter._handle_webhook
    )
    return app


def _delivery_state(
    adapter: WebhookAdapter,
    delivery_id: str,
    *,
    session_id=None,
    platform: str = "discord",
    source_session_key: str | None = None,
    phase: str | None = None,
):
    marker = adapter._handoff_delivery_marker(
        profile=None,
        route_name="alerts",
        delivery_id=delivery_id,
    )
    key = adapter._handoff_delivery_state_key(marker)
    value = adapter._handoff_delivery_state_value(
        marker,
        platform,
        session_id=session_id,
        source_session_key=(
            source_session_key or f"key:webhook:alerts:{delivery_id}"
        ),
        phase=phase,
    )
    return marker, key, value


def _make_event(adapter: WebhookAdapter, delivery_id: str = "delivery-1"):
    chat_id = f"webhook:alerts:{delivery_id}"
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
        message_id=delivery_id,
    )
    from gateway.platforms.base import MessageEvent

    marker, _, _ = _delivery_state(adapter, delivery_id)
    event = MessageEvent(text="alert", source=source, message_id=delivery_id)
    event.metadata.update(
        {
            "_webhook_handoff_to": "discord",
            "_webhook_handoff_delivery": marker,
        }
    )
    return event, marker


def _wire_lifecycle_runner(
    adapter: WebhookAdapter,
    *,
    finalize_result=True,
):
    store = SimpleNamespace(
        peek_session_id=AsyncMock(return_value="session-exact"),
        remove_session_route_and_end=AsyncMock(return_value=finalize_result),
    )
    _marker, _state_key, accepted_state = _delivery_state(
        adapter, "delivery-1"
    )
    db = _with_admission_lock_api(
        SimpleNamespace(
            request_handoff_once=AsyncMock(),
            get_meta=AsyncMock(return_value=accepted_state),
        ),
        has_input=False,
    )
    adapter.gateway_runner = SimpleNamespace(
        async_session_store=store,
        _session_db=db,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
    )
    return store, db


async def _start_and_persist(
    adapter: WebhookAdapter,
    event: MessageEvent,
    *,
    session_key: str,
    session_id: str,
) -> str:
    """Drive the callback-required accepted→durable-input→running path."""
    marker = await adapter.on_agent_run_started(
        event,
        session_key=session_key,
        session_id=session_id,
    )
    assert marker
    store = getattr(adapter.gateway_runner, "session_store", None)
    if store is not None and not store._db.has_webhook_handoff_input(marker):
        store._db.append_message(
            session_id,
            "user",
            event.text,
            platform_message_id=marker,
        )
    await adapter.on_agent_input_persisted(
        event,
        session_key=session_key,
        session_id=session_id,
    )
    return marker


def _real_lifecycle(
    tmp_path,
    monkeypatch,
    *,
    delivery_id: str,
    mark_resume: bool = False,
):
    """Create one admitted webhook run backed by the real routing database."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    sessions_dir = tmp_path / "sessions"
    config = GatewayConfig(write_sessions_json=False)
    store = SessionStore(sessions_dir=sessions_dir, config=config)
    adapter = _make_adapter(_handoff_routes())
    event, marker = _make_event(adapter, delivery_id)
    session_key = build_session_key(event.source)
    entry = store.get_or_create_session(event.source)
    if mark_resume:
        assert store.mark_resume_pending(session_key, "restart_timeout")
    token = store.mark_turn_active(session_key)
    assert token
    event._gateway_active_turn_token = token
    state_key = adapter._handoff_delivery_state_key(marker)
    accepted_state = adapter._handoff_delivery_state_value(
        marker,
        "discord",
        session_id=None,
        source_session_key=session_key,
    )
    assert store._db.set_meta_if_absent(state_key, accepted_state)
    accepted = adapter._parse_handoff_delivery_state(
        accepted_state,
        marker=marker,
        handoff_to="discord",
    )
    admission_owner = f"test-owner:{delivery_id}"
    assert store._db.try_acquire_webhook_delivery_admission_lock(
        state_key,
        accepted["admission_token"],
        accepted["lock_protocol"],
        admission_owner,
    )
    event._webhook_handoff_admission_owner = admission_owner
    adapter.gateway_runner = SimpleNamespace(
        session_store=store,
        async_session_store=AsyncSessionStore(store),
        _session_db=AsyncSessionDB(store._db),
        _session_key_for_source=build_session_key,
    )
    return SimpleNamespace(
        adapter=adapter,
        config=config,
        sessions_dir=sessions_dir,
        store=store,
        event=event,
        marker=marker,
        state_key=state_key,
        session_key=session_key,
        entry=entry,
        token=token,
        accepted_state=accepted_state,
    )


def _durable_route(store: SessionStore, session_key: str) -> dict:
    raw = store._db.load_gateway_routing_entries(
        scope=store._routing_scope()
    )[session_key]
    return json.loads(raw)


def _durable_bindings(db: SessionDB) -> list[dict]:
    with db._lock:
        rows = db._conn.execute(
            "SELECT value FROM state_meta "
            "WHERE key LIKE 'webhook_handoff_route_binding:%'"
        ).fetchall()
    return [json.loads(row["value"]) for row in rows]


class TestHandoffConfiguration:
    def test_discord_is_the_initial_trusted_target(self):
        assert (
            WebhookAdapter._validate_handoff_target(
                "alerts", {"handoff_to": " Discord "}
            )
            == "discord"
        )

    @pytest.mark.parametrize("target", [None, "", "telegram", "{payload.target}", 1])
    def test_invalid_or_untrusted_target_is_rejected(self, target):
        with pytest.raises(ValueError, match="handoff_to"):
            WebhookAdapter._validate_handoff_target(
                "alerts", {"handoff_to": target}
            )

    def test_deliver_only_is_incompatible(self):
        with pytest.raises(ValueError, match="deliver_only=true"):
            WebhookAdapter._validate_handoff_target(
                "alerts",
                {"handoff_to": "discord", "deliver_only": True},
            )

    @pytest.mark.parametrize("profile", ["work", " Work ", "", None, 7])
    def test_named_or_invalid_profile_handoff_is_rejected(self, profile):
        with pytest.raises(ValueError, match="named multiplex profile"):
            WebhookAdapter._validate_handoff_target(
                "alerts",
                {"handoff_to": "discord", "profile": profile},
            )

    def test_explicit_default_profile_handoff_is_allowed(self):
        assert (
            WebhookAdapter._validate_handoff_target(
                "alerts",
                {"handoff_to": "discord", "profile": "default"},
            )
            == "discord"
        )

    def test_route_without_handoff_is_unchanged(self):
        assert WebhookAdapter._validate_handoff_target("alerts", {}) is None


@pytest.mark.asyncio
async def test_handoff_target_is_config_only_not_payload_interpolation():
    adapter = _make_adapter(_handoff_routes(deliver="discord"))
    claim = AsyncMock(return_value=True)
    adapter.gateway_runner = SimpleNamespace(
        _session_db=_with_admission_lock_api(
            SimpleNamespace(set_meta_if_absent=claim)
        ),
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
    )
    captured = []

    adapter.handle_message = _admitting_handler(adapter, captured)
    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "hello", "handoff_to": "telegram"},
            headers={"X-GitHub-Delivery": "trusted-target-1"},
        )
        assert response.status == 202

    await asyncio.sleep(0)
    assert len(captured) == 1
    event = captured[0]
    assert event.metadata["_webhook_handoff_to"] == "discord"
    assert adapter._delivery_info[event.source.chat_id]["handoff_to"] == "discord"
    marker, state_key, accepted_state = _delivery_state(
        adapter, "trusted-target-1"
    )
    assert event.metadata["_webhook_handoff_delivery"] == marker
    claim.assert_awaited_once_with(state_key, accepted_state)


@pytest.mark.asyncio
async def test_runner_without_proxy_reaches_durable_dispatch():
    adapter = _make_adapter(_handoff_routes())
    db = _with_admission_lock_api(
        SimpleNamespace(set_meta_if_absent=AsyncMock(return_value=True))
    )
    runner = MagicMock()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._get_proxy_url = lambda: None
    runner._session_db = db
    runner._session_key_for_source = (
        lambda source: f"key:{source.chat_id}"
    )
    adapter.gateway_runner = runner
    captured = []
    adapter.handle_message = _admitting_handler(adapter, captured)

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "must be admitted locally"},
            headers={"X-GitHub-Delivery": "mock-no-proxy"},
        )

    assert response.status == 202
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_handoff_proxy_mode_fails_before_remote_agent_dispatch():
    adapter = _make_adapter(_handoff_routes())
    claim = AsyncMock(return_value=True)
    db = _with_admission_lock_api(
        SimpleNamespace(set_meta_if_absent=claim)
    )
    adapter.gateway_runner = SimpleNamespace(
        _session_db=db,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
        _get_proxy_url=lambda: "http://remote-gateway:8642",
    )
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "must stay local until durable"},
            headers={"X-GitHub-Delivery": "proxy-handoff"},
        )
        body = await response.json()

    assert response.status == 503
    assert "proxy mode is unsupported" in body["error"]
    adapter.handle_message.assert_not_awaited()
    db.release_webhook_delivery_admission_lock.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_webhook_route_remains_dispatchable_in_proxy_mode():
    adapter = _make_adapter(
        {
            "alerts": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "{message}",
            }
        }
    )
    adapter.gateway_runner = SimpleNamespace(
        _get_proxy_url=lambda: "http://remote-gateway:8642"
    )
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "legacy proxy request"},
            headers={"X-GitHub-Delivery": "legacy-proxy"},
        )

    assert response.status == 202
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_profile_url_aliases_share_one_durable_claim():
    adapter = _make_adapter(_handoff_routes())
    durable = {}

    async def _set_meta_if_absent(key, value):
        if key in durable:
            return False
        durable[key] = value
        return True

    async def _get_meta(key):
        return durable[key]

    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(side_effect=_set_meta_if_absent),
        get_meta=AsyncMock(side_effect=_get_meta),
    ))
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(
            multiplex_profiles=True,
            multiplex_profile_allowlist=[],
        ),
        _session_db=db,
        _profile_name_for_source=lambda _source: None,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
        async_session_store=SimpleNamespace(
            peek_session_id=AsyncMock(return_value=None)
        ),
    )
    async def _admit(event):
        marker = event.metadata["_webhook_handoff_delivery"]
        state_key = adapter._handoff_delivery_state_key(marker)
        state = adapter._parse_handoff_delivery_state(
            durable[state_key],
            marker=marker,
            handoff_to="discord",
        )
        durable[state_key] = adapter._handoff_delivery_state_value(
            marker,
            "discord",
            session_id="session-exact",
            source_session_key=state["source_session_key"],
            phase="running",
            active_turn_token="test-active-turn",
        )
        adapter._resolve_handoff_admission(event, True)

    adapter.handle_message = AsyncMock(side_effect=_admit)

    async with TestClient(TestServer(_create_app(adapter))) as client:
        headers = {"X-GitHub-Delivery": "default-profile-alias"}
        first = await client.post(
            "/webhooks/alerts", json={"message": "same"}, headers=headers
        )
        second = await client.post(
            "/p/default/webhooks/alerts",
            json={"message": "same"},
            headers=headers,
        )
        second_body = await second.json()

    assert first.status == 202
    assert second.status == 200
    assert second_body["status"] == "duplicate"
    assert len(durable) == 1
    assert db.set_meta_if_absent.await_count == 2
    first_claim, second_claim = db.set_meta_if_absent.await_args_list
    assert first_claim.args == second_claim.args
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].source.profile == "default"


@pytest.mark.asyncio
async def test_handoff_send_suppresses_legacy_parent_delivery():
    adapter = _make_adapter({})
    chat_id = "webhook:alerts:no-parent-copy"
    adapter._delivery_info[chat_id] = {
        "deliver": "discord",
        "deliver_extra": {"chat_id": "parent-channel"},
        "handoff_to": "discord",
    }
    adapter._deliver_cross_platform = AsyncMock(
        return_value=SendResult(success=True)
    )

    result = await adapter.send(chat_id, "completed response")

    assert result.success is True
    adapter._deliver_cross_platform.assert_not_awaited()


@pytest.mark.asyncio
async def test_handoff_send_stays_suppressed_after_delivery_snapshot_prunes():
    adapter = _make_adapter(_handoff_routes(deliver="discord"))
    adapter.gateway_runner = SimpleNamespace(
        _session_db=_with_admission_lock_api(
            SimpleNamespace(set_meta_if_absent=AsyncMock(return_value=True))
        )
    )
    captured = []

    adapter.handle_message = _admitting_handler(adapter, captured)
    adapter._deliver_cross_platform = AsyncMock(
        return_value=SendResult(success=True)
    )

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "long-running handoff"},
            headers={"X-GitHub-Delivery": "long-running-handoff"},
        )
        assert response.status == 202

    await asyncio.sleep(0)
    chat_id = captured[0].source.chat_id
    adapter._delivery_info.clear()

    result = await adapter.send(chat_id, "must not reach the legacy target")

    assert result.success is True
    adapter._deliver_cross_platform.assert_not_awaited()


@pytest.mark.asyncio
async def test_started_and_persisted_hooks_use_token_fenced_atomic_phases():
    adapter = _make_adapter({})
    event, marker = _make_event(adapter)
    event._gateway_active_turn_token = "active-turn-1"
    event._webhook_handoff_admission_owner = "admission-owner-1"
    session_key = "key:webhook:alerts:delivery-1"
    state_key = adapter._handoff_delivery_state_key(marker)
    accepted_state = adapter._handoff_delivery_state_value(
        marker,
        "discord",
        session_id=None,
        source_session_key=session_key,
    )
    running_state = adapter._handoff_delivery_state_value(
        marker,
        "discord",
        session_id="session-exact",
        source_session_key=session_key,
        phase="running",
        active_turn_token="active-turn-1",
    )
    succeeded_state = adapter._handoff_delivery_state_value(
        marker,
        "discord",
        session_id="session-exact",
        source_session_key=session_key,
        phase="succeeded",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        get_meta=AsyncMock(
            side_effect=[accepted_state, accepted_state, running_state]
        ),
        bind_webhook_handoff_delivery_to_source_route=AsyncMock(
            return_value=True
        ),
        complete_webhook_handoff_delivery_once=AsyncMock(return_value=True),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    admission = asyncio.get_running_loop().create_future()
    event._webhook_handoff_admission_future = admission

    admitted_marker = await adapter.on_agent_run_started(
        event,
        session_key=session_key,
        session_id="session-exact",
    )
    assert admitted_marker == marker
    assert not admission.done()
    db.bind_webhook_handoff_delivery_to_source_route.assert_not_awaited()

    await adapter.on_agent_input_persisted(
        event,
        session_key=session_key,
        session_id="session-exact",
    )
    assert admission.result() is True
    await adapter.on_agent_run_persisted(
        event,
        session_key=session_key,
        session_id="session-exact",
    )
    await adapter.on_agent_run_persisted(
        event,
        session_key=session_key,
        session_id="session-exact",
    )

    db.bind_webhook_handoff_delivery_to_source_route.assert_awaited_once_with(
        "session-exact",
        session_key,
        state_key,
        accepted_state,
        running_state,
        "active-turn-1",
        "admission-owner-1",
    )
    db.complete_webhook_handoff_delivery_once.assert_awaited_once_with(
        "session-exact",
        session_key,
        state_key,
        running_state,
        succeeded_state,
        "discord",
        "active-turn-1",
        "session-exact",
    )
    assert event.metadata["_webhook_handoff_requested"] is True


@pytest.mark.asyncio
async def test_input_hook_rejects_uncommitted_marker_before_running_cas(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="missing-durable-input",
    )
    admission = asyncio.get_running_loop().create_future()
    run.event._webhook_handoff_admission_future = admission
    try:
        marker = await run.adapter.on_agent_run_started(
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        assert marker == run.marker
        assert not admission.done()

        with pytest.raises(
            RuntimeError,
            match="durable webhook input row is not committed",
        ):
            await run.adapter.on_agent_input_persisted(
                run.event,
                session_key=run.session_key,
                session_id=run.entry.session_id,
            )

        assert admission.result() is False
        assert run.store._db.get_meta(run.state_key) == run.accepted_state
        assert not run.store._db.has_webhook_handoff_input(run.marker)
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_in_place_compaction_preserves_durable_input_marker(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="in-place-compacted-input",
    )
    try:
        marker = await run.adapter.on_agent_run_started(
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        assert marker == run.marker
        run.store._db.archive_and_compact(
            run.entry.session_id,
            [
                {
                    "role": "user",
                    "content": "compacted current webhook input",
                    "_platform_message_id": run.marker,
                    "api_content": "compacted current webhook input",
                }
            ],
        )

        assert run.store._db.has_webhook_handoff_input(run.marker)
        active = run.store.load_transcript(run.entry.session_id)
        assert len(active) == 1
        assert active[0]["role"] == "user"
        assert active[0]["content"] == "compacted current webhook input"
        assert active[0]["api_content"] == "compacted current webhook input"
        assert active[0]["timestamp"]
        assert active[0]["message_id"] == run.marker
        await run.adapter.on_agent_input_persisted(
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        state = run.adapter._parse_handoff_delivery_state(
            run.store._db.get_meta(run.state_key),
            marker=run.marker,
            handoff_to="discord",
        )
        assert state["phase"] == "running"
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_cancelled_offloaded_input_cas_rolls_back_for_provider_retry(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="cancelled-input-cas",
    )
    admission = asyncio.get_running_loop().create_future()
    run.event._webhook_handoff_admission_future = admission
    marker = await run.adapter.on_agent_run_started(
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    run.store._db.append_message(
        run.entry.session_id,
        "user",
        "input survives a cancelled running CAS",
        platform_message_id=marker,
    )

    entered = threading.Event()
    release = threading.Event()
    original_bind = (
        run.store._db.bind_webhook_handoff_delivery_to_source_route
    )
    bind_calls = 0

    def _block_first_bind(*args, **kwargs):
        nonlocal bind_calls
        bind_calls += 1
        if bind_calls == 1:
            entered.set()
            assert release.wait(timeout=3)
        return original_bind(*args, **kwargs)

    monkeypatch.setattr(
        run.store._db,
        "bind_webhook_handoff_delivery_to_source_route",
        _block_first_bind,
    )
    bind_task = asyncio.create_task(
        run.adapter.on_agent_input_persisted(
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
    )
    assert await asyncio.to_thread(entered.wait, 3)
    bind_task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await bind_task

    try:
        assert admission.result() is False
        assert not run.event.metadata.get(
            "_webhook_handoff_ownership_conflict"
        )
        assert run.store._db.get_meta(run.state_key) == run.accepted_state
        assert run.store._db.has_webhook_handoff_input(run.marker)

        run.event.agent_run_failed = True
        await run.adapter.on_processing_complete(
            run.event,
            ProcessingOutcome.CANCELLED,
        )
        assert run.store.peek_session_id(run.session_key) == run.entry.session_id
        assert run.store.clear_turn_active(run.session_key, run.token)

        async def _admit_retry(event):
            entry = run.store.lookup_by_session_key(run.session_key)
            assert entry is not None
            retry_token = run.store.mark_turn_active(run.session_key)
            assert retry_token
            event._gateway_active_turn_token = retry_token
            retry_marker = await run.adapter.on_agent_run_started(
                event,
                session_key=run.session_key,
                session_id=entry.session_id,
            )
            assert retry_marker == run.marker
            await run.adapter.on_agent_input_persisted(
                event,
                session_key=run.session_key,
                session_id=entry.session_id,
            )

        run.adapter.handle_message = AsyncMock(side_effect=_admit_retry)
        run.adapter.gateway_runner._get_proxy_url = lambda: None
        async with TestClient(TestServer(_create_app(run.adapter))) as client:
            response = await client.post(
                "/webhooks/alerts",
                json={"message": "authenticated retry after cancellation"},
                headers={"X-GitHub-Delivery": "cancelled-input-cas"},
            )

        assert response.status == 202
        state = run.adapter._parse_handoff_delivery_state(
            run.store._db.get_meta(run.state_key),
            marker=run.marker,
            handoff_to="discord",
        )
        assert state["phase"] == "running"
        run.adapter.handle_message.assert_awaited_once()
        assert bind_calls == 2
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_detached_unlock_cancellation_cannot_rollback_admitted_run(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="cancelled-detached-unlock",
    )
    admission = asyncio.get_running_loop().create_future()
    run.event._webhook_handoff_admission_future = admission
    marker = await run.adapter.on_agent_run_started(
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    run.store._db.append_message(
        run.entry.session_id,
        "user",
        "input admitted before detached unlock",
        platform_message_id=marker,
    )

    entered = threading.Event()
    release = threading.Event()
    original_release = (
        run.store._db.release_webhook_delivery_admission_lock
    )
    release_calls = 0

    def _block_first_release(*args, **kwargs):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            entered.set()
            assert release.wait(timeout=3)
        return original_release(*args, **kwargs)

    monkeypatch.setattr(
        run.store._db,
        "release_webhook_delivery_admission_lock",
        _block_first_release,
    )
    await run.adapter.on_agent_input_persisted(
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    assert admission.result() is True
    assert await asyncio.to_thread(entered.wait, 3)
    release_task = next(iter(run.adapter._background_tasks))
    release_task.cancel()

    run.adapter.handle_message = AsyncMock()
    run.adapter.gateway_runner._get_proxy_url = lambda: None
    try:
        # The input callback has already returned successfully, so the worker
        # is authorized to enter its primary provider loop. A concurrent retry
        # may observe the durable running duplicate, but cancellation of the
        # detached unlock can no longer send that state back to accepted.
        async with TestClient(TestServer(_create_app(run.adapter))) as client:
            duplicate = await client.post(
                "/webhooks/alerts",
                json={"message": "concurrent provider retry"},
                headers={
                    "X-GitHub-Delivery": "cancelled-detached-unlock"
                },
            )
            duplicate_body = await duplicate.json()

        assert duplicate.status == 200
        assert duplicate_body["status"] == "duplicate"
        run.adapter.handle_message.assert_not_awaited()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await release_task

    try:
        state = run.adapter._parse_handoff_delivery_state(
            run.store._db.get_meta(run.state_key),
            marker=run.marker,
            handoff_to="discord",
        )
        assert state["phase"] == "running"
        assert release_calls >= 2
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_success_commit_before_outer_clear_is_restart_exact_once(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="success-before-outer-clear",
        mark_resume=True,
    )
    adapter = run.adapter
    event = run.event
    event.agent_run_failed = False

    await _start_and_persist(
        adapter,
        event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    running_state = adapter._handoff_delivery_state_value(
        run.marker,
        "discord",
        session_id=run.entry.session_id,
        source_session_key=run.session_key,
        phase="running",
        active_turn_token=run.token,
    )
    assert run.store._db.get_meta(run.state_key) == running_state
    assert _durable_route(run.store, run.session_key)[
        "active_turn_token"
    ] == run.token

    # This is the crash-sensitive commit: the runner has not yet reached its
    # ordinary clear_turn_active() finally block.
    await adapter.on_agent_run_persisted(
        event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    succeeded_state = adapter._handoff_delivery_state_value(
        run.marker,
        "discord",
        session_id=run.entry.session_id,
        source_session_key=run.session_key,
        phase="succeeded",
    )
    assert run.store._db.get_meta(run.state_key) == succeeded_state
    assert run.store._db.get_handoff_state(run.entry.session_id) == {
        "state": "pending",
        "platform": "discord",
        "error": None,
    }
    assert run.store._db.is_webhook_handoff_request(
        run.entry.session_id, "discord"
    )
    route = _durable_route(run.store, run.session_key)
    assert route["active_turn_token"] is None
    assert route["active_turn_started_at"] is None
    assert route["resume_pending"] is False
    assert route["resume_reason"] is None
    assert route["last_resume_marked_at"] is None
    assert _durable_bindings(run.store._db) == [
        {
            "active_session_id": run.entry.session_id,
            "active_session_key": run.session_key,
            "forbidden_routes": [],
            "retired": False,
            "routing_scope": run.store._routing_scope(),
        }
    ]

    # Simulate a hard crash before the outer finally clears its local token.
    run.store.close_all_db_handles()
    restarted_store = SessionStore(
        sessions_dir=run.sessions_dir,
        config=run.config,
    )
    restarted_adapter = _make_adapter(_handoff_routes())
    restarted_adapter.gateway_runner = SimpleNamespace(
        session_store=restarted_store,
        async_session_store=AsyncSessionStore(restarted_store),
        _session_db=AsyncSessionDB(restarted_store._db),
        _session_key_for_source=build_session_key,
    )
    restarted_adapter.handle_message = AsyncMock()
    try:
        assert restarted_store.recover_interrupted_turns() == 0
        restarted = restarted_store.lookup_by_session_key(run.session_key)
        assert restarted is not None
        assert restarted.active_turn_token is None
        assert restarted.resume_pending is False

        async with TestClient(
            TestServer(_create_app(restarted_adapter))
        ) as client:
            response = await client.post(
                "/webhooks/alerts",
                json={"message": "provider replay after committed success"},
                headers={"X-GitHub-Delivery": "success-before-outer-clear"},
            )
            body = await response.json()
        assert response.status == 200
        assert body["status"] == "duplicate"
        restarted_adapter.handle_message.assert_not_awaited()
    finally:
        restarted_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_compression_child_completes_root_delivery(tmp_path, monkeypatch):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="compression-child-success",
    )
    root_id = run.entry.session_id
    await _start_and_persist(
        run.adapter,
        run.event,
        session_key=run.session_key,
        session_id=root_id,
    )
    child_id = "compression-child"
    run.store._db.end_session(root_id, "compression")
    run.store._db.create_session(
        child_id,
        "webhook",
        parent_session_id=root_id,
    )
    assert run.store.advance_compression_session(
        run.session_key,
        root_id,
        child_id,
    ) is not None

    await run.adapter.on_agent_run_persisted(
        run.event,
        session_key=run.session_key,
        session_id=child_id,
    )

    succeeded = run.adapter._handoff_delivery_state_value(
        run.marker,
        "discord",
        session_id=child_id,
        source_session_key=run.session_key,
        phase="succeeded",
    )
    assert run.store._db.get_meta(run.state_key) == succeeded
    assert run.store._db.get_session(root_id)["end_reason"] == "compression"
    assert run.store._db.get_handoff_state(child_id) == {
        "state": "pending",
        "platform": "discord",
        "error": None,
    }
    binding = _durable_bindings(run.store._db)[0]
    assert binding["active_session_id"] == child_id
    assert {
        "session_key": run.session_key,
        "session_id": root_id,
    } in binding["forbidden_routes"]
    run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_preflight_rotation_binds_parent_route_from_marked_child_input(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="preflight-child-input",
    )
    parent_id = run.entry.session_id
    child_id = "preflight-compression-child"
    try:
        marker = await run.adapter.on_agent_run_started(
            run.event,
            session_key=run.session_key,
            session_id=parent_id,
        )
        assert marker == run.marker

        # The agent's preflight compressor can rotate its SessionDB transcript
        # before the worker callback returns to the gateway. The gateway route
        # intentionally still names the captured run-start parent until the
        # executor returns and publishes the split.
        run.store._db.end_session(parent_id, "compression")
        run.store._db.create_session(
            child_id,
            "webhook",
            parent_session_id=parent_id,
        )
        run.store._db.append_message(
            child_id,
            "user",
            "original payload persisted after preflight rotation",
            platform_message_id=run.marker,
        )
        assert _durable_route(run.store, run.session_key)[
            "session_id"
        ] == parent_id

        await run.adapter.on_agent_input_persisted(
            run.event,
            session_key=run.session_key,
            session_id=parent_id,
        )
        running = run.adapter._parse_handoff_delivery_state(
            run.store._db.get_meta(run.state_key),
            marker=run.marker,
            handoff_to="discord",
        )
        assert running["phase"] == "running"
        assert running["session_id"] == parent_id

        assert run.store.advance_compression_session(
            run.session_key,
            parent_id,
            child_id,
        ) is not None
        run.event.agent_run_failed = False
        await run.adapter.on_agent_run_persisted(
            run.event,
            session_key=run.session_key,
            session_id=child_id,
        )

        succeeded = run.adapter._parse_handoff_delivery_state(
            run.store._db.get_meta(run.state_key),
            marker=run.marker,
            handoff_to="discord",
        )
        assert succeeded["phase"] == "succeeded"
        assert succeeded["session_id"] == child_id
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_non_lineage_completion_is_rejected_without_mutation(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="non-lineage-rejected",
    )
    await _start_and_persist(
        run.adapter,
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    running_state = run.store._db.get_meta(run.state_key)
    route_before = _durable_route(run.store, run.session_key)
    bindings_before = _durable_bindings(run.store._db)
    run.store._db.create_session("unrelated-session", "webhook")

    with pytest.raises(RuntimeError, match="publication was rejected"):
        await run.adapter.on_agent_run_persisted(
            run.event,
            session_key=run.session_key,
            session_id="unrelated-session",
        )

    assert run.store._db.get_meta(run.state_key) == running_state
    assert _durable_route(run.store, run.session_key) == route_before
    assert _durable_bindings(run.store._db) == bindings_before
    assert run.store._db.get_handoff_state("unrelated-session") == {
        "state": None,
        "platform": None,
        "error": None,
    }
    run.store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive_target", ["discord", "telegram"])
async def test_concurrent_interactive_handoff_survives_persisted_hook(
    tmp_path, monkeypatch, interactive_target
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id=f"interactive-{interactive_target}",
    )
    await _start_and_persist(
        run.adapter,
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    running_state = run.store._db.get_meta(run.state_key)
    route_before = _durable_route(run.store, run.session_key)
    assert run.store._db.request_handoff(
        run.entry.session_id, interactive_target
    )

    with pytest.raises(RuntimeError, match="interactive request"):
        await run.adapter.on_agent_run_persisted(
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )

    assert run.event.metadata["_webhook_handoff_ownership_conflict"] is True
    assert run.store._db.get_meta(run.state_key) == running_state
    assert _durable_route(run.store, run.session_key) == route_before
    assert run.store._db.get_handoff_state(run.entry.session_id) == {
        "state": "pending",
        "platform": interactive_target,
        "error": None,
    }
    assert not run.store._db.is_webhook_handoff_request(
        run.entry.session_id, interactive_target
    )
    run.store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (ProcessingOutcome.FAILURE, "webhook_handoff_failed"),
        (ProcessingOutcome.CANCELLED, "webhook_handoff_cancelled"),
    ],
)
async def test_failure_or_cancellation_removes_source_and_finalizes(outcome, reason):
    adapter = _make_adapter({})
    store, db = _wire_lifecycle_runner(adapter)
    event, _ = _make_event(adapter)

    await adapter.on_processing_complete(event, outcome)

    store.remove_session_route_and_end.assert_awaited_once_with(
        "key:webhook:alerts:delivery-1", "session-exact", reason
    )
    db.request_handoff_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_with_explicit_agent_failure_still_finalizes():
    adapter = _make_adapter({})
    store, db = _wire_lifecycle_runner(adapter)
    event, _ = _make_event(adapter)
    event.agent_run_failed = True

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    store.remove_session_route_and_end.assert_awaited_once_with(
        "key:webhook:alerts:delivery-1",
        "session-exact",
        "webhook_handoff_failed",
    )
    db.request_handoff_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_with_explicit_agent_success_still_finalizes():
    adapter = _make_adapter({})
    store, db = _wire_lifecycle_runner(adapter)
    event, _ = _make_event(adapter)
    event.agent_run_failed = False

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    store.remove_session_route_and_end.assert_awaited_once_with(
        "key:webhook:alerts:delivery-1",
        "session-exact",
        "webhook_handoff_cancelled",
    )
    db.request_handoff_once.assert_not_awaited()




@pytest.mark.asyncio
async def test_durable_duplicate_after_restart_skips_second_agent_run():
    adapter = _make_adapter(_handoff_routes())
    marker, state_key, bound_state = _delivery_state(
        adapter,
        "restart-duplicate-1",
        session_id="original-session",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=bound_state),
        get_session=AsyncMock(
            return_value={"id": "original-session", "ended_at": None}
        ),
        get_handoff_state=AsyncMock(
            return_value={
                "state": "completed",
                "platform": "discord",
                "error": None,
            }
        ),
        is_webhook_handoff_request=AsyncMock(return_value=True),
        request_handoff_once=AsyncMock(),
    ))
    adapter.gateway_runner = SimpleNamespace(
        _session_db=db,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
    )
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "retry"},
            headers={"X-GitHub-Delivery": "restart-duplicate-1"},
        )
        assert response.status == 200
        assert (await response.json())["status"] == "duplicate"

    adapter.handle_message.assert_not_awaited()
    db.set_meta_if_absent.assert_awaited_once_with(
        state_key,
        adapter._handoff_delivery_state_value(
            marker,
            "discord",
            session_id=None,
            source_session_key="key:webhook:alerts:restart-duplicate-1",
        ),
    )
    db.get_meta.assert_awaited_once_with(state_key)
    db.get_session.assert_awaited_once_with("original-session")
    db.request_handoff_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_state_db_duplicate_replay_survives_adapter_restart(tmp_path):
    """A completed durable claim remains exact-once after reopening SQLite."""
    original_adapter = _make_adapter(_handoff_routes())
    delivery_id = "real-db-restart-duplicate"
    marker, state_key, bound_state = _delivery_state(
        original_adapter,
        delivery_id,
        session_id="original-session",
    )
    db_path = tmp_path / "state.db"
    original_db = SessionDB(db_path=db_path)
    original_db.create_session("original-session", "webhook")
    original_db.set_meta(state_key, bound_state)
    assert original_db.request_handoff_once("original-session", "discord")
    assert original_db.claim_handoff("original-session")
    original_db.complete_handoff("original-session")
    original_db.close()

    restarted_db = SessionDB(db_path=db_path)
    restarted_adapter = _make_adapter(_handoff_routes())
    restarted_adapter.gateway_runner = SimpleNamespace(
        _session_db=AsyncSessionDB(restarted_db)
    )
    restarted_adapter.handle_message = AsyncMock()
    try:
        async with TestClient(TestServer(_create_app(restarted_adapter))) as client:
            response = await client.post(
                "/webhooks/alerts",
                json={"message": "provider replay after restart"},
                headers={"X-GitHub-Delivery": delivery_id},
            )
            body = await response.json()

        assert response.status == 200
        assert body["status"] == "duplicate"
        assert restarted_db.get_meta(state_key) == bound_state
        assert restarted_db.get_handoff_state("original-session") == {
            "state": "completed",
            "platform": "discord",
            "error": None,
        }
        restarted_adapter.handle_message.assert_not_awaited()
    finally:
        restarted_db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive_target", ["discord", "telegram"])
async def test_real_state_db_duplicate_preserves_interactive_handoff(
    tmp_path,
    interactive_target,
):
    adapter = _make_adapter(_handoff_routes())
    delivery_id = f"interactive-owner-{interactive_target}"
    _, state_key, bound_state = _delivery_state(
        adapter,
        delivery_id,
        session_id="interactive-session",
    )
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("interactive-session", "webhook")
    db.set_meta(state_key, bound_state)
    assert db.request_handoff("interactive-session", interactive_target)
    adapter.gateway_runner = SimpleNamespace(_session_db=AsyncSessionDB(db))
    adapter.handle_message = AsyncMock()
    try:
        async with TestClient(TestServer(_create_app(adapter))) as client:
            response = await client.post(
                "/webhooks/alerts",
                json={"message": "provider replay"},
                headers={"X-GitHub-Delivery": delivery_id},
            )
            body = await response.json()

        assert response.status == 200
        assert body == {
            "message": (
                "Delivery ID is bound to a session with an interactive "
                "handoff request"
            ),
            "status": "conflict",
            "reason": "handoff_owned_by_interactive_request",
            "delivery_id": delivery_id,
        }
        assert db.get_handoff_state("interactive-session") == {
            "state": "pending",
            "platform": interactive_target,
            "error": None,
        }
        assert not db.is_webhook_handoff_request(
            "interactive-session", interactive_target
        )
        adapter.handle_message.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_live_accepted_owner_keeps_duplicate_out_of_agent(tmp_path):
    adapter = _make_adapter(_handoff_routes())
    delivery_id = "accepted-before-run"
    marker, state_key, accepted_state = _delivery_state(adapter, delivery_id)
    db_path = tmp_path / "state.db"
    owner_db = SessionDB(db_path=db_path)
    owner_db.set_meta(state_key, accepted_state)
    accepted = adapter._parse_handoff_delivery_state(
        accepted_state,
        marker=marker,
        handoff_to="discord",
    )
    assert owner_db.try_acquire_webhook_delivery_admission_lock(
        state_key,
        accepted["admission_token"],
        accepted["lock_protocol"],
        "live-owner",
    ) is True
    db = SessionDB(db_path=db_path)
    adapter.gateway_runner = SimpleNamespace(
        _session_db=AsyncSessionDB(db),
        _session_key_for_source=build_session_key,
    )
    adapter.handle_message = AsyncMock()
    try:
        async with TestClient(TestServer(_create_app(adapter))) as client:
            first, second = await asyncio.gather(
                client.post(
                    "/webhooks/alerts",
                    json={"message": "retry before agent admission"},
                    headers={"X-GitHub-Delivery": delivery_id},
                ),
                client.post(
                    "/webhooks/alerts",
                    json={"message": "same retry"},
                    headers={"X-GitHub-Delivery": delivery_id},
                ),
            )
            bodies = await asyncio.gather(first.json(), second.json())

        assert first.status == second.status == 503
        assert all("admission in progress" in body["error"] for body in bodies)
        assert db.get_meta(state_key) == accepted_state
        assert db.list_pending_handoffs() == []
        adapter.handle_message.assert_not_awaited()
    finally:
        db.close()
        owner_db.close()


@pytest.mark.asyncio
async def test_crash_left_accepted_delivery_waits_for_provider_retry_before_202(
    tmp_path, monkeypatch
):
    import hermes_state

    db_path = tmp_path / "state.db"
    delivery_id = "accepted-owner-crashed"
    adapter = _make_adapter(_handoff_routes())
    source = adapter.build_source(
        chat_id=f"webhook:alerts:{delivery_id}",
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
        message_id=delivery_id,
    )
    source_session_key = build_session_key(source)
    marker, state_key, accepted_state = _delivery_state(
        adapter,
        delivery_id,
        source_session_key=source_session_key,
    )
    accepted = adapter._parse_handoff_delivery_state(
        accepted_state,
        marker=marker,
        handoff_to="discord",
    )

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", db_path)
    sessions_dir = tmp_path / "sessions"
    config = GatewayConfig(write_sessions_json=False)
    crashed_store = SessionStore(sessions_dir=sessions_dir, config=config)
    crashed_entry = crashed_store.get_or_create_session(source)
    assert crashed_store.mark_turn_active(source_session_key)
    crashed_store._db.set_meta(state_key, accepted_state)
    assert crashed_store._db.try_acquire_webhook_delivery_admission_lock(
        state_key,
        accepted["admission_token"],
        accepted["lock_protocol"],
        "crashed-owner",
    ) is True
    crashed_store.close_all_db_handles()

    store = SessionStore(
        sessions_dir=sessions_dir,
        config=config,
    )
    adapter.gateway_runner = SimpleNamespace(
        _session_db=AsyncSessionDB(store._db),
        session_store=store,
        async_session_store=AsyncSessionStore(store),
        _session_key_for_source=build_session_key,
        _profile_name_for_source=lambda _source: None,
    )

    # Startup recovery can reconstruct the route and provider identity, but it
    # does not possess the process-local admission capability held by the HTTP
    # request that crashed.  It must preserve ``accepted`` for the authenticated
    # provider retry instead of synthesizing a capability and running an empty
    # resume turn.
    assert store.recover_interrupted_turns() == 1
    recovered_entry = store.lookup_by_session_key(source_session_key)
    assert recovered_entry is not None
    resumed_token = store.mark_turn_active(source_session_key)
    assert resumed_token
    resumed_event = MessageEvent(
        text="resume after crash",
        source=recovered_entry.origin,
    )
    resumed_event._gateway_active_turn_token = resumed_token
    with pytest.raises(
        RuntimeError,
        match="accepted webhook delivery has no admission owner",
    ):
        await adapter.on_agent_run_started(
            resumed_event,
            session_key=source_session_key,
            session_id=crashed_entry.session_id,
        )
    assert resumed_event.metadata[
        "_webhook_handoff_ownership_conflict"
    ] is True
    await adapter.on_processing_complete(
        resumed_event,
        ProcessingOutcome.FAILURE,
    )
    assert store._db.get_meta(state_key) == accepted_state
    assert store.peek_session_id(source_session_key) == crashed_entry.session_id

    captured_prompts = []

    async def _bind_reclaimed(event):
        captured_prompts.append(event.text)
        entry = store.get_or_create_session(event.source)
        token = store.mark_turn_active(entry.session_key)
        assert token
        event._gateway_active_turn_token = token
        await _start_and_persist(
            adapter,
            event,
            session_key=entry.session_key,
            session_id=entry.session_id,
        )

    adapter.handle_message = AsyncMock(side_effect=_bind_reclaimed)
    try:
        async with TestClient(TestServer(_create_app(adapter))) as client:
            first = await client.post(
                "/webhooks/alerts",
                json={"message": "retry after crash"},
                headers={"X-GitHub-Delivery": delivery_id},
            )
            replay = await client.post(
                "/webhooks/alerts",
                json={"message": "same delivery"},
                headers={"X-GitHub-Delivery": delivery_id},
            )
            replay_body = await replay.json()

        assert first.status == 202
        assert replay.status == 200
        assert replay_body["status"] == "duplicate"
        state = adapter._parse_handoff_delivery_state(
            store._db.get_meta(state_key),
            marker=marker,
            handoff_to="discord",
        )
        assert state["phase"] == "running"
        assert state["session_id"] == store.peek_session_id(source_session_key)
        assert captured_prompts == ["retry after crash"]
        adapter.handle_message.assert_awaited_once()
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_commit_before_running_cas_retry_reuses_one_marked_user_row(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="input-committed-before-cas",
    )
    marker = await run.adapter.on_agent_run_started(
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    assert marker == run.marker
    run.store._db.append_message(
        run.entry.session_id,
        "user",
        "original prompt committed before the running CAS",
        platform_message_id=run.marker,
        api_content="original prompt committed before the running CAS",
    )
    assert run.store._db.has_webhook_handoff_input(run.marker)

    # Simulate process death after the transcript commit but before the worker
    # callback can bind accepted→running or resolve HTTP 202.
    run.store.close_all_db_handles()
    restarted_store = SessionStore(
        sessions_dir=run.sessions_dir,
        config=run.config,
    )
    restarted_adapter = _make_adapter(_handoff_routes())
    restarted_adapter.gateway_runner = SimpleNamespace(
        session_store=restarted_store,
        async_session_store=AsyncSessionStore(restarted_store),
        _session_db=AsyncSessionDB(restarted_store._db),
        _session_key_for_source=build_session_key,
        _profile_name_for_source=lambda _source: None,
        _get_proxy_url=lambda: None,
    )
    captured_rows = []

    async def _reuse_committed_input(event):
        entry = restarted_store.lookup_by_session_key(run.session_key)
        assert entry is not None
        token = restarted_store.mark_turn_active(run.session_key)
        assert token
        event._gateway_active_turn_token = token
        marker_from_start = await restarted_adapter.on_agent_run_started(
            event,
            session_key=run.session_key,
            session_id=entry.session_id,
        )
        assert marker_from_start == run.marker
        rows = restarted_store.load_transcript(entry.session_id)
        marked = [
            row for row in rows if row.get("message_id") == run.marker
        ]
        assert len(marked) == 1
        captured_rows.extend(marked)
        await restarted_adapter.on_agent_input_persisted(
            event,
            session_key=run.session_key,
            session_id=entry.session_id,
        )

    restarted_adapter.handle_message = AsyncMock(
        side_effect=_reuse_committed_input
    )
    try:
        assert restarted_store._db.has_webhook_handoff_input(run.marker)
        assert len(
            [
                row
                for row in restarted_store.load_transcript(
                    run.entry.session_id
                )
                if row.get("message_id") == run.marker
            ]
        ) == 1
        assert restarted_store.recover_interrupted_turns() == 1
        async with TestClient(
            TestServer(_create_app(restarted_adapter))
        ) as client:
            first = await client.post(
                "/webhooks/alerts",
                json={"message": "authenticated provider retry"},
                headers={
                    "X-GitHub-Delivery": "input-committed-before-cas"
                },
            )
            duplicate = await client.post(
                "/webhooks/alerts",
                json={"message": "same delivery again"},
                headers={
                    "X-GitHub-Delivery": "input-committed-before-cas"
                },
            )
            duplicate_body = await duplicate.json()

        assert first.status == 202
        assert duplicate.status == 200
        assert duplicate_body["status"] == "duplicate"
        assert [row["content"] for row in captured_rows] == [
            "original prompt committed before the running CAS"
        ]
        rows_after = restarted_store.load_transcript(run.entry.session_id)
        assert sum(
            row.get("message_id") == run.marker for row in rows_after
        ) == 1
        restarted_adapter.handle_message.assert_awaited_once()
    finally:
        restarted_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_running_duplicate_does_not_request_or_run_agent(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="running-before-persisted",
    )
    await _start_and_persist(
        run.adapter,
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    running_state = run.adapter._handoff_delivery_state_value(
        run.marker,
        "discord",
        session_id=run.entry.session_id,
        source_session_key=run.session_key,
        phase="running",
        active_turn_token=run.token,
    )
    run.adapter.handle_message = AsyncMock()
    try:
        async with TestClient(TestServer(_create_app(run.adapter))) as client:
            first, second = await asyncio.gather(
                client.post(
                    "/webhooks/alerts",
                    json={"message": "retry while original agent runs"},
                    headers={
                        "X-GitHub-Delivery": "running-before-persisted"
                    },
                ),
                client.post(
                    "/webhooks/alerts",
                    json={"message": "same retry"},
                    headers={
                        "X-GitHub-Delivery": "running-before-persisted"
                    },
                ),
            )
            bodies = await asyncio.gather(first.json(), second.json())

        assert first.status == second.status == 200
        assert [body["status"] for body in bodies] == ["duplicate", "duplicate"]
        assert run.store._db.get_meta(run.state_key) == running_state
        assert run.store._db.get_handoff_state(run.entry.session_id) == {
            "state": None,
            "platform": None,
            "error": None,
        }
        assert _durable_route(run.store, run.session_key)[
            "active_turn_token"
        ] == run.token
        run.adapter.handle_message.assert_not_awaited()
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_running_delivery_rejects_a_different_live_turn_owner(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="running-owner-fence",
    )
    try:
        await _start_and_persist(
            run.adapter,
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        original_running_state = run.store._db.get_meta(run.state_key)
        contender_token = run.store.mark_turn_active(run.session_key)
        assert contender_token and contender_token != run.token
        run.event._gateway_active_turn_token = contender_token

        with pytest.raises(
            RuntimeError,
            match="owned by another agent turn",
        ):
            await run.adapter.on_agent_run_started(
                run.event,
                session_key=run.session_key,
                session_id=run.entry.session_id,
            )

        assert run.store._db.get_meta(run.state_key) == original_running_state
        assert run.store._db.get_handoff_state(run.entry.session_id) == {
            "state": None,
            "platform": None,
            "error": None,
        }
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_losing_running_recovery_cannot_finalize_or_unsuppress_winner(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="running-loser-cleanup-fence",
    )
    try:
        await _start_and_persist(
            run.adapter,
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        winner_state = run.store._db.get_meta(run.state_key)

        loser_event, _marker = _make_event(
            run.adapter,
            "running-loser-cleanup-fence",
        )
        loser_event._gateway_active_turn_token = "losing-turn-token"
        loser_event.agent_run_failed = True
        with pytest.raises(
            RuntimeError,
            match="owned by another agent turn",
        ):
            await run.adapter.on_agent_run_started(
                loser_event,
                session_key=run.session_key,
                session_id=run.entry.session_id,
            )

        assert loser_event.metadata[
            "_webhook_handoff_ownership_conflict"
        ] is True
        await run.adapter.on_processing_complete(
            loser_event,
            ProcessingOutcome.FAILURE,
        )

        assert run.store._db.get_meta(run.state_key) == winner_state
        assert run.store.peek_session_id(run.session_key) == run.entry.session_id
        assert run.store._db.get_session(run.entry.session_id)["ended_at"] is None
        assert _durable_route(run.store, run.session_key)[
            "active_turn_token"
        ] == run.token
        assert (
            run.event.source.chat_id
            in run.adapter._active_handoff_sessions
        )

        # The rejected event's completion hook left the actual owner intact and
        # able to publish the exact handoff.
        await run.adapter.on_agent_run_persisted(
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        assert run.event.metadata["_webhook_handoff_requested"] is True
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_failed_active_turn_admission_cannot_finalize_live_owner(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="pre-hook-admission-loser",
    )
    try:
        loser_event, _marker = _make_event(
            run.adapter,
            "pre-hook-admission-loser",
        )
        loser_event._webhook_handoff_admission_owner = (
            run.event._webhook_handoff_admission_owner
        )
        loser_event.agent_run_failed = True
        loser_event.active_turn_admission_failed = True
        run.adapter._active_handoff_sessions.add(run.event.source.chat_id)

        await run.adapter.on_processing_complete(
            loser_event,
            ProcessingOutcome.FAILURE,
        )

        assert run.store._db.get_meta(run.state_key) == run.accepted_state
        assert run.store.peek_session_id(run.session_key) == run.entry.session_id
        assert run.store._db.get_session(run.entry.session_id)["ended_at"] is None
        assert _durable_route(run.store, run.session_key)[
            "active_turn_token"
        ] == run.token
        assert (
            run.event.source.chat_id
            in run.adapter._active_handoff_sessions
        )
        accepted = run.adapter._parse_handoff_delivery_state(
            run.accepted_state,
            marker=run.marker,
            handoff_to="discord",
        )
        assert run.store._db.try_acquire_webhook_delivery_admission_lock(
            run.state_key,
            accepted["admission_token"],
            accepted["lock_protocol"],
            "provider-retry-owner",
        )
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_startup_admission_loser_without_metadata_preserves_live_owner(
    tmp_path, monkeypatch
):
    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="startup-admission-loser",
    )
    try:
        await _start_and_persist(
            run.adapter,
            run.event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        winner_state = run.store._db.get_meta(run.state_key)
        synthetic_event = MessageEvent(
            text="",
            source=run.entry.origin,
            internal=True,
            agent_run_failed=True,
            active_turn_admission_failed=True,
        )

        await run.adapter.on_processing_complete(
            synthetic_event,
            ProcessingOutcome.FAILURE,
        )

        assert synthetic_event.metadata == {}
        assert run.store._db.get_meta(run.state_key) == winner_state
        assert run.store.peek_session_id(run.session_key) == run.entry.session_id
        assert run.store._db.get_session(run.entry.session_id)["ended_at"] is None
        assert _durable_route(run.store, run.session_key)[
            "active_turn_token"
        ] == run.token
        assert (
            run.event.source.chat_id
            in run.adapter._active_handoff_sessions
        )
    finally:
        run.store.close_all_db_handles()


@pytest.mark.asyncio
async def test_legacy_webhook_admission_failure_still_closes_session():
    adapter = _make_adapter(
        {
            "alerts": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "{message}",
            }
        }
    )
    store = SimpleNamespace(peek_session_id=MagicMock(return_value="legacy-session"))
    db = SimpleNamespace(end_session=AsyncMock())
    adapter.gateway_runner = SimpleNamespace(
        session_store=store,
        _session_db=db,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
    )
    source = adapter.build_source(
        chat_id="webhook:alerts:legacy-delivery",
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
    )
    event = MessageEvent(
        text="legacy alert",
        source=source,
        message_id="legacy-delivery",
        agent_run_failed=True,
        active_turn_admission_failed=True,
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    db.end_session.assert_awaited_once_with(
        "legacy-session",
        "webhook_complete",
    )


@pytest.mark.asyncio
async def test_restart_identity_restores_running_delivery_suppression(
    tmp_path, monkeypatch
):
    from gateway.platforms.base import MessageEvent

    run = _real_lifecycle(
        tmp_path,
        monkeypatch,
        delivery_id="restart-running-identity",
    )
    await _start_and_persist(
        run.adapter,
        run.event,
        session_key=run.session_key,
        session_id=run.entry.session_id,
    )
    run.store.close_all_db_handles()

    restarted_store = SessionStore(
        sessions_dir=run.sessions_dir,
        config=run.config,
    )
    restarted_entry = restarted_store.lookup_by_session_key(run.session_key)
    assert restarted_entry is not None
    assert restarted_entry.origin is not None
    assert restarted_entry.origin.message_id == "restart-running-identity"
    assert restarted_store.recover_interrupted_turns() == 1
    resumed_token = restarted_store.mark_turn_active(run.session_key)
    assert resumed_token
    resumed_event = MessageEvent(
        text="resume after restart",
        source=restarted_entry.origin,
    )
    resumed_event._gateway_active_turn_token = resumed_token
    restarted_adapter = _make_adapter(_handoff_routes())
    restarted_adapter.gateway_runner = SimpleNamespace(
        session_store=restarted_store,
        async_session_store=AsyncSessionStore(restarted_store),
        _session_db=AsyncSessionDB(restarted_store._db),
        _session_key_for_source=build_session_key,
    )
    restarted_adapter._deliver_cross_platform = AsyncMock(
        return_value=SendResult(success=True)
    )
    try:
        await restarted_adapter.on_agent_run_started(
            resumed_event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        resumed_state = restarted_adapter._parse_handoff_delivery_state(
            restarted_store._db.get_meta(run.state_key),
            marker=run.marker,
            handoff_to="discord",
        )
        assert resumed_state["phase"] == "running"
        assert resumed_state["active_turn_token"] == resumed_token
        durable_route = _durable_route(restarted_store, run.session_key)
        assert durable_route["resume_pending"] is False
        assert durable_route["resume_reason"] is None
        assert durable_route["last_resume_marked_at"] is None
        assert resumed_event.metadata["_webhook_handoff_to"] == "discord"
        assert resumed_event.metadata["_webhook_handoff_delivery"] == run.marker
        assert (
            resumed_event.source.chat_id
            in restarted_adapter._active_handoff_sessions
        )
        send_result = await restarted_adapter.send(
            resumed_event.source.chat_id,
            "must stay off the legacy delivery target",
        )
        assert send_result.success is True
        restarted_adapter._deliver_cross_platform.assert_not_awaited()

        await restarted_adapter.on_agent_run_persisted(
            resumed_event,
            session_key=run.session_key,
            session_id=run.entry.session_id,
        )
        assert resumed_event.metadata["_webhook_handoff_requested"] is True
        assert restarted_store.clear_turn_active(
            run.session_key, resumed_token
        )
    finally:
        restarted_store.close_all_db_handles()


@pytest.mark.asyncio
async def test_legacy_retry_without_source_message_id_ignores_old_tombstone(
    tmp_path, monkeypatch
):
    import hermes_state
    from gateway.platforms.base import MessageEvent

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig(write_sessions_json=False)
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    adapter = _make_adapter(_handoff_routes())
    delivery_id = "legacy-collides-with-old-delivery"
    source = adapter.build_source(
        chat_id=f"webhook:alerts:{delivery_id}",
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
        message_id=None,
    )
    session_key = build_session_key(source)
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(session_key)
    assert token
    marker, state_key, succeeded_state = _delivery_state(
        adapter,
        delivery_id,
        session_id=entry.session_id,
        source_session_key=session_key,
        phase="succeeded",
    )
    store._db.set_meta(state_key, succeeded_state)
    adapter.gateway_runner = SimpleNamespace(
        session_store=store,
        async_session_store=AsyncSessionStore(store),
        _session_db=AsyncSessionDB(store._db),
        _session_key_for_source=build_session_key,
    )
    event = MessageEvent(
        text="legacy retry",
        source=source,
        message_id=delivery_id,
    )
    event._gateway_active_turn_token = token
    try:
        await adapter.on_agent_run_started(
            event,
            session_key=session_key,
            session_id=entry.session_id,
        )
        await adapter.on_agent_run_persisted(
            event,
            session_key=session_key,
            session_id=entry.session_id,
        )

        assert event.metadata == {"_webhook_handoff_completion_attempted": True}
        assert adapter._active_handoff_sessions == set()
        assert store._db.get_meta(state_key) == succeeded_state
        assert store._db.get_handoff_state(entry.session_id) == {
            "state": None,
            "platform": None,
            "error": None,
        }
        assert _durable_bindings(store._db) == []
        assert _durable_route(store, session_key)["active_turn_token"] == token
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle", "session_row", "handoff_state"),
    [
        (
            "failed",
            {"id": "original-session", "ended_at": None},
            {"state": "failed", "platform": "discord", "error": "send failed"},
        ),
        (
            "ended",
            {"id": "original-session", "ended_at": 1, "end_reason": "agent_close"},
            None,
        ),
        (
            "reset",
            {"id": "original-session", "ended_at": 1, "end_reason": "session_reset"},
            None,
        ),
        (
            "compression",
            {"id": "original-session", "ended_at": 1, "end_reason": "compression"},
            None,
        ),
    ],
)
async def test_duplicate_tombstone_uses_original_identity_after_lifecycle_change(
    lifecycle,
    session_row,
    handoff_state,
):
    adapter = _make_adapter(_handoff_routes())
    delivery_id = f"duplicate-after-{lifecycle}"
    _, _, bound_state = _delivery_state(
        adapter,
        delivery_id,
        session_id="original-session",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=bound_state),
        get_session=AsyncMock(return_value=session_row),
        get_handoff_state=AsyncMock(return_value=handoff_state),
        is_webhook_handoff_request=AsyncMock(return_value=True),
        request_handoff_once=AsyncMock(),
    ))
    adapter.gateway_runner = SimpleNamespace(
        _session_db=db,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
        async_session_store=SimpleNamespace(
            peek_session_id=AsyncMock(return_value=None)
        ),
    )
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "same provider delivery"},
            headers={"X-GitHub-Delivery": delivery_id},
        )
        body = await response.json()

    assert response.status == 200
    assert body["status"] == "duplicate"
    db.get_session.assert_awaited_once_with("original-session")
    if lifecycle == "failed":
        db.get_handoff_state.assert_awaited_once_with("original-session")
    else:
        db.get_handoff_state.assert_not_awaited()
    db.request_handoff_once.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_recovers_crash_between_binding_and_request():
    adapter = _make_adapter(_handoff_routes())
    _, _, bound_state = _delivery_state(
        adapter,
        "bound-before-request",
        session_id="bound-session",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=bound_state),
        get_session=AsyncMock(
            return_value={
                "id": "bound-session",
                "ended_at": None,
                "session_key": "source-bound-session",
            }
        ),
        get_handoff_state=AsyncMock(
            return_value={"state": None, "platform": None, "error": None}
        ),
        is_webhook_handoff_request=AsyncMock(return_value=True),
        request_handoff_once=AsyncMock(return_value=True),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "retry after crash"},
            headers={"X-GitHub-Delivery": "bound-before-request"},
        )
        body = await response.json()

    assert response.status == 200
    assert body["status"] == "duplicate"
    db.request_handoff_once.assert_awaited_once_with(
        "bound-session",
        "discord",
        source_session_key="source-bound-session",
    )
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_with_mismatched_durable_target_is_terminal_conflict():
    adapter = _make_adapter(_handoff_routes())
    _, _, conflicting_state = _delivery_state(
        adapter,
        "mismatched-target",
        session_id="bound-session",
        platform="telegram",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=conflicting_state),
        get_session=AsyncMock(),
        request_handoff_once=AsyncMock(),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        first, second = await asyncio.gather(
            client.post(
                "/webhooks/alerts",
                json={"message": "conflict"},
                headers={"X-GitHub-Delivery": "mismatched-target"},
            ),
            client.post(
                "/webhooks/alerts",
                json={"message": "conflict replay"},
                headers={"X-GitHub-Delivery": "mismatched-target"},
            ),
        )
        first_body, second_body = await asyncio.gather(
            first.json(), second.json()
        )

    assert first.status == second.status == 200
    assert first_body == second_body == {
        "message": "Delivery ID already claimed for a different handoff target",
        "status": "conflict",
        "reason": "handoff_target_changed",
        "delivery_id": "mismatched-target",
    }
    assert db.set_meta_if_absent.await_count == 2
    assert db.get_meta.await_count == 2
    db.get_session.assert_not_awaited()
    db.request_handoff_once.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_retargeted_duplicate_recovers_only_first_claimed_target(
    monkeypatch,
):
    monkeypatch.setattr(
        webhook_module,
        "_SUPPORTED_HANDOFF_TARGETS",
        frozenset({"discord", "telegram"}),
    )
    adapter = _make_adapter(_handoff_routes(handoff_to="telegram"))
    _, _, first_target_state = _delivery_state(
        adapter,
        "retargeted-bound-delivery",
        session_id="bound-session",
        platform="discord",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=first_target_state),
        get_session=AsyncMock(
            return_value={
                "id": "bound-session",
                "ended_at": None,
                "session_key": "source-bound-session",
            }
        ),
        get_handoff_state=AsyncMock(
            return_value={"state": None, "platform": None, "error": None}
        ),
        is_webhook_handoff_request=AsyncMock(return_value=True),
        request_handoff_once=AsyncMock(return_value=True),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "replay after retarget"},
            headers={"X-GitHub-Delivery": "retargeted-bound-delivery"},
        )
        body = await response.json()

    assert response.status == 200
    assert body["status"] == "conflict"
    assert body["reason"] == "handoff_target_changed"
    db.request_handoff_once.assert_awaited_once_with(
        "bound-session",
        "discord",
        source_session_key="source-bound-session",
    )
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_retargeted_duplicate_preserves_interactive_owner(monkeypatch):
    monkeypatch.setattr(
        webhook_module,
        "_SUPPORTED_HANDOFF_TARGETS",
        frozenset({"discord", "telegram"}),
    )
    adapter = _make_adapter(_handoff_routes(handoff_to="telegram"))
    _, _, first_target_state = _delivery_state(
        adapter,
        "retargeted-interactive-owner",
        session_id="bound-session",
        platform="discord",
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=first_target_state),
        get_session=AsyncMock(
            return_value={"id": "bound-session", "ended_at": None}
        ),
        get_handoff_state=AsyncMock(
            return_value={
                "state": "pending",
                "platform": "discord",
                "error": None,
            }
        ),
        is_webhook_handoff_request=AsyncMock(return_value=False),
        request_handoff_once=AsyncMock(),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "interactive replay after retarget"},
            headers={"X-GitHub-Delivery": "retargeted-interactive-owner"},
        )
        body = await response.json()

    assert response.status == 200
    assert body["status"] == "conflict"
    assert body["reason"] == "handoff_target_changed"
    db.request_handoff_once.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_target", [None, "", 7])
async def test_malformed_durable_target_remains_retryable_failure(stored_target):
    adapter = _make_adapter(_handoff_routes())
    marker, _, _ = _delivery_state(adapter, "malformed-target")
    malformed_state = json.dumps(
        {
            "marker": marker,
            "platform": stored_target,
            "session_id": "bound-session",
        }
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=malformed_state),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "malformed tombstone"},
            headers={"X-GitHub-Delivery": "malformed-target"},
        )

    assert response.status == 503
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mismatched_target_with_malformed_session_id_remains_retryable_failure():
    adapter = _make_adapter(_handoff_routes())
    marker, _, _ = _delivery_state(adapter, "malformed-retarget-session")
    malformed_state = json.dumps(
        {
            "marker": marker,
            "platform": "telegram",
            "session_id": 7,
        }
    )
    db = _with_admission_lock_api(SimpleNamespace(
        set_meta_if_absent=AsyncMock(return_value=False),
        get_meta=AsyncMock(return_value=malformed_state),
    ))
    adapter.gateway_runner = SimpleNamespace(_session_db=db)
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "malformed retarget tombstone"},
            headers={"X-GitHub-Delivery": "malformed-retarget-session"},
        )

    assert response.status == 503
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_claim_failure_returns_503_without_consuming_retry():
    adapter = _make_adapter(_handoff_routes())
    claim = AsyncMock(side_effect=[RuntimeError("db down"), True])
    adapter.gateway_runner = SimpleNamespace(
        _session_db=_with_admission_lock_api(
            SimpleNamespace(set_meta_if_absent=claim)
        )
    )
    adapter.handle_message = _admitting_handler(adapter)

    async with TestClient(TestServer(_create_app(adapter))) as client:
        headers = {"X-GitHub-Delivery": "retry-after-store-failure"}
        first = await client.post(
            "/webhooks/alerts", json={"message": "retry"}, headers=headers
        )
        second = await client.post(
            "/webhooks/alerts", json={"message": "retry"}, headers=headers
        )

    assert first.status == 503
    assert second.status == 202
    assert claim.await_count == 2
    await asyncio.sleep(0)
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_duplicate_claim_starts_exactly_one_agent_run():
    adapter = _make_adapter(_handoff_routes())
    durable = {"value": None}
    lock_held = False
    duplicate_reached_fence = asyncio.Event()

    async def _set_meta_if_absent(_key, value):
        if durable["value"] is not None:
            return False
        durable["value"] = value
        return True

    async def _get_meta(_key):
        return durable["value"]

    async def _acquire(*_args):
        nonlocal lock_held
        if lock_held:
            duplicate_reached_fence.set()
            return False
        lock_held = True
        return True

    async def _release(*_args):
        nonlocal lock_held
        lock_held = False
        return True

    db = SimpleNamespace(
        set_meta_if_absent=AsyncMock(side_effect=_set_meta_if_absent),
        get_meta=AsyncMock(side_effect=_get_meta),
        try_acquire_webhook_delivery_admission_lock=AsyncMock(
            side_effect=_acquire
        ),
        release_webhook_delivery_admission_lock=AsyncMock(
            side_effect=_release
        ),
        get_session=AsyncMock(
            side_effect=AssertionError("an unbound duplicate has no session to recover")
        ),
    )
    adapter.gateway_runner = SimpleNamespace(
        _session_db=db,
        _session_key_for_source=lambda source: f"key:{source.chat_id}",
        async_session_store=SimpleNamespace(
            peek_session_id=AsyncMock(return_value=None)
        ),
    )

    async def _admit(event):
        await duplicate_reached_fence.wait()
        state = adapter._parse_handoff_delivery_state(
            durable["value"],
            marker=event.metadata["_webhook_handoff_delivery"],
            handoff_to="discord",
        )
        durable["value"] = adapter._handoff_delivery_state_value(
            state["marker"],
            "discord",
            session_id="session-exact",
            source_session_key=state["source_session_key"],
            phase="running",
            active_turn_token="test-active-turn",
        )
        await db.release_webhook_delivery_admission_lock(
            adapter._handoff_delivery_state_key(state["marker"]),
            state["admission_token"],
            event._webhook_handoff_admission_owner,
        )
        adapter._resolve_handoff_admission(event, True)

    adapter.handle_message = AsyncMock(side_effect=_admit)

    async with TestClient(TestServer(_create_app(adapter))) as client:
        headers = {"X-GitHub-Delivery": "concurrent-claim"}
        first, second = await asyncio.gather(
            client.post(
                "/webhooks/alerts", json={"message": "same"}, headers=headers
            ),
            client.post(
                "/webhooks/alerts", json={"message": "same"}, headers=headers
            ),
        )
        first_body, second_body = await asyncio.gather(
            first.json(), second.json()
        )

    bodies = [first_body, second_body]
    statuses = [first.status, second.status]
    assert statuses.count(202) == 1
    assert next(status for status in statuses if status != 202) in {200, 503}
    assert sum(body.get("status") == "accepted" for body in bodies) == 1
    nonaccepted = next(body for body in bodies if body.get("status") != "accepted")
    assert nonaccepted.get("status") == "duplicate" or (
        "admission in progress" in nonaccepted.get("error", "")
    )
    assert db.set_meta_if_absent.await_count in {1, 2}
    db.get_meta.assert_awaited_once()
    db.get_session.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_route_does_not_consult_durable_handoff_index():
    adapter = _make_adapter(
        {
            "alerts": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "{message}",
                "deliver": "log",
            }
        }
    )
    claim = AsyncMock(
        side_effect=AssertionError("legacy route must not use state_meta claim")
    )
    adapter.gateway_runner = SimpleNamespace(
        _session_db=SimpleNamespace(set_meta_if_absent=claim)
    )
    adapter.handle_message = AsyncMock()

    async with TestClient(TestServer(_create_app(adapter))) as client:
        response = await client.post(
            "/webhooks/alerts",
            json={"message": "legacy"},
            headers={"X-GitHub-Delivery": "legacy-1"},
        )
        assert response.status == 202

    await asyncio.sleep(0)
    claim.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
