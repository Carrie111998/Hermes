"""Invariant test: a completed webhook delivery closes its session.

Regression guard for the ghost-session leak. Webhook deliveries create a
unique one-shot session from the admitted profile/route/provider and the
server-generated operation trace (deliberately not the provider delivery ID),
but the adapter historically fired ``handle_message`` without ever ending it.
``SessionDB.prune_sessions`` only reaps rows where ``ended_at IS NOT NULL``, so
every webhook session stayed unprunable and state.db grew without bound (this
was the primary driver of the SQLite lock-contention gateway outage).

The invariant asserted here is a *behavior contract*, not a snapshot: once a
webhook delivery's agent run completes, the session row for that delivery must
have ``ended_at`` set — mirroring how a cron run closes its session with
``end_session(..., "cron_complete")``.

CRITICAL: these tests go through the REAL ``handle_message`` →
``_process_message_background`` → ``on_processing_complete`` pipeline (only the
runner-side ``_message_handler`` is stubbed, exactly the seam the live gateway
injects).  ``handle_message`` is fire-and-forget — it spawns the background
task and returns before the run starts — so any close bolted around
``handle_message`` itself runs BEFORE the session row exists and silently
no-ops.  A test that fakes ``handle_message`` to create the row synchronously
masks exactly that bug (the first version of this fix shipped that way).
"""

import asyncio
import threading
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import SessionSource, SessionStore


def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


def _create_app(adapter: WebhookAdapter, *, multiplex: bool = False) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    if multiplex:
        app.router.add_post(
            "/p/{profile}/webhooks/{route_name}",
            adapter._handle_webhook,
        )
    return app


class _FakeRunner:
    """Minimal gateway runner surface the webhook close path depends on.

    Wires a real ``SessionStore`` (which owns a real ``SessionDB``) and reuses
    that same ``SessionDB`` as ``_session_db`` so the row created at routing
    time is the row the close path ends — exactly the wiring the live gateway
    has (``self.session_store`` + ``self._session_db``).
    """

    def __init__(self, store: SessionStore):
        self.session_store = store
        self._session_db = store._db

    def _session_key_for_source(self, source: SessionSource) -> str:
        return self.session_store._generate_session_key(source)


def _make_store(tmp_path, *, multiplex_profiles: bool = False) -> SessionStore:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    config = GatewayConfig(
        multiplex_profiles=multiplex_profiles,
        platforms={Platform.WEBHOOK: PlatformConfig(enabled=True)},
    )
    store = SessionStore(sessions_dir=sessions_dir, config=config)
    assert store._db is not None, "test requires a real SessionDB"
    return store


def _make_event(adapter: WebhookAdapter, delivery_id: str, text: str) -> MessageEvent:
    session_chat_id = f"webhook:alerts:{delivery_id}"
    source = adapter.build_source(
        chat_id=session_chat_id,
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        raw_message={"message": text},
        message_id=delivery_id,
    )


async def _drain_background_tasks(
    adapter: WebhookAdapter, timeout: float = 5.0
) -> None:
    """Wait for the adapter's spawned processing task(s) to finish."""
    deadline = asyncio.get_event_loop().time() + timeout
    while adapter._background_tasks and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    # One extra tick for done-callbacks to run.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_completed_webhook_delivery_closes_its_session(tmp_path):
    """After a webhook run finishes (REAL dispatch path), ended_at is set."""
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)

    adapter = _make_adapter({
        "alerts": {
            "secret": _INSECURE_NO_AUTH,
            "provider": "github",
            "prompt": "Alert: {message}",
            "deliver": "log",
        }
    })
    runner.adapters = {Platform.WEBHOOK: adapter}
    adapter.gateway_runner = runner

    # Stub the RUNNER-side handler (the seam the live gateway injects) — the
    # adapter's own handle_message / _process_message_background pipeline runs
    # for real, including the fire-and-forget task spawn and the
    # on_processing_complete hook.  The handler creates the session row, just
    # like GatewayRunner._handle_message does at routing time.
    created = {}

    async def _message_handler(event: MessageEvent):
        entry = store.get_or_create_session(event.source)
        created["session_id"] = entry.session_id
        return ""  # webhook deliver=log — nothing to send back

    adapter._message_handler = _message_handler

    # Admit through the real HTTP boundary so the event carries the exact
    # durable operation authority required by the processing hooks.
    async with TestClient(TestServer(_create_app(adapter))) as cli:
        response = await cli.post(
            "/webhooks/alerts",
            json={"message": "server on fire"},
            headers={"X-GitHub-Delivery": "alert-close-001"},
        )
        assert response.status == 202
    # handle_message is fire-and-forget: the session must NOT be expected to
    # exist yet.  (Guards against reintroducing a close wrapped around
    # handle_message itself, which ran before the row existed and no-op'd.)
    await asyncio.sleep(0.05)
    await _drain_background_tasks(adapter)

    session_id = created["session_id"]
    row = store._db.get_session(session_id)
    assert row is not None

    # INVARIANT: a completed webhook session must be closed so prune can reap it.
    assert row["ended_at"] is not None, (
        "webhook session was never closed — ended_at is NULL, so "
        "prune_sessions can never reap it (the ghost-session leak)"
    )
    assert row["end_reason"] == "webhook_complete"

    # And the closed row is actually prunable, unlike the pre-fix leak.
    pruned = store._db.prune_sessions(older_than_days=0, source="webhook")
    assert pruned >= 1
    store._db.close()


@pytest.mark.asyncio
async def test_multiplex_completion_closes_the_admitted_profile_db(
    tmp_path, monkeypatch
):
    """The completion hook must resolve its DB inside the source profile scope."""
    import hermes_state

    from gateway.run import (
        GatewayRunner,
        _SESSION_DB_UNPINNED,
        _profile_runtime_scope,
    )
    from gateway.session_db_recovery import RecoverableHandleCache

    root = tmp_path / "hermes"
    profile_home = root / "profiles" / "fitness"
    root.mkdir()
    profile_home.mkdir(parents=True)
    # Route publication freezes grants from the admitted physical profile.
    # A real multiplex runner therefore requires that profile's config file.
    (profile_home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    # The suite fixture deliberately pins DEFAULT_DB_PATH. This regression
    # exercises production's context-local path resolution instead.
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH
    )

    store = _make_store(root, multiplex_profiles=True)
    runner = object.__new__(GatewayRunner)
    runner.config = store.config
    runner.session_store = store
    runner._session_db_pinned = _SESSION_DB_UNPINNED
    runner._session_db_handles = {}
    runner._session_db_handles_lock = threading.Lock()
    runner._session_db_handle_cache = RecoverableHandleCache(
        handles=runner._session_db_handles,
        lock=runner._session_db_handles_lock,
    )
    runner._session_db_init_error = None

    adapter = _make_adapter({
        "alerts": {
            "profile": "fitness",
            "secret": _INSECURE_NO_AUTH,
            "provider": "github",
            "prompt": "Alert: {message}",
            "deliver": "log",
        }
    })
    runner.adapters = {Platform.WEBHOOK: adapter}
    adapter.gateway_runner = runner

    created = {}

    async def _runner_handle_message(event: MessageEvent):
        # The real primary-profile wrapper must have installed the admitted
        # profile before any session state is created.
        created["db_path"] = Path(store._db.db_path)
        entry = store.get_or_create_session(event.source)
        created["session_id"] = entry.session_id
        return ""

    runner._handle_message = _runner_handle_message
    adapter._message_handler = runner._make_default_profile_message_handler()

    try:
        async with TestClient(TestServer(_create_app(adapter, multiplex=True))) as cli:
            response = await cli.post(
                "/p/fitness/webhooks/alerts",
                json={"message": "Profile-scoped alert"},
                headers={"X-GitHub-Delivery": "profile-close-001"},
            )
            assert response.status == 202
        await asyncio.sleep(0.05)
        await _drain_background_tasks(adapter)

        session_id = created["session_id"]
        assert created["db_path"] == profile_home / "state.db"

        with _profile_runtime_scope(profile_home):
            profile_row = store._db.get_session(session_id)
        root_row = store._db.get_session(session_id)

        assert profile_row is not None
        assert profile_row["ended_at"] is not None, (
            "completion resolved the root DB after the routed profile scope "
            "exited, leaving the admitted profile's session open"
        )
        assert profile_row["end_reason"] == "webhook_complete"
        assert root_row is None
    finally:
        store.close_all_db_handles()
        runner.close_all_session_db_handles()
