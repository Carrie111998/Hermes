from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading

import pytest

from plugins.skyai_customer import dev_gateway, discord_delivery


class SimulatedProcessCrash(BaseException):
    pass


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeDiscordTransport:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nonce_history: dict[
            tuple[str, str],
            list[tuple[str, str]],
        ] = {}
        self._nonce_enforcement: dict[
            tuple[str, str],
            tuple[str, str],
        ] = {}
        self._threads: dict[tuple[str, str], str] = {}
        self.message_counter = 0
        self.thread_counter = 0
        self.fail_posts = 0
        self.crash_after_thread_create = False
        self.crash_after_chunk_post = False
        self.posted_contents: list[tuple[str, str, str]] = []

    def find_threads_by_exact_name(
        self,
        configured_channel_id: str,
        exact_name: str,
    ) -> list[str]:
        with self._lock:
            thread_id = self._threads.get((configured_channel_id, exact_name))
            return [thread_id] if thread_id is not None else []

    def find_message_ids_by_exact_nonce(
        self,
        channel_id: str,
        nonce: str,
    ) -> list[str]:
        with self._lock:
            return [
                message_id
                for _content, message_id in self._nonce_history.get(
                    (channel_id, nonce),
                    [],
                )
            ]

    def expire_nonce_enforcement(self) -> None:
        with self._lock:
            self._nonce_enforcement.clear()

    def post_message(
        self,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> str:
        with self._lock:
            if self.fail_posts > 0:
                self.fail_posts -= 1
                raise OSError("discord unavailable")
            nonce_key = (channel_id, nonce)
            existing = self._nonce_enforcement.get(nonce_key)
            if existing is not None:
                existing_content, message_id = existing
                if existing_content != content:
                    raise RuntimeError("nonce was reused with different exact content")
                return message_id
            self.message_counter += 1
            message_id = f"message-{self.message_counter}"
            self._nonce_enforcement[nonce_key] = (content, message_id)
            self._nonce_history.setdefault(nonce_key, []).append(
                (content, message_id)
            )
            self.posted_contents.append((channel_id, content, nonce))
            if (
                self.crash_after_chunk_post
                and channel_id.startswith("thread-")
            ):
                self.crash_after_chunk_post = False
                raise SimulatedProcessCrash()
            return message_id

    def start_thread_from_message(
        self,
        configured_channel_id: str,
        starter_message_id: str,
        exact_name: str,
    ) -> str:
        with self._lock:
            key = (configured_channel_id, exact_name)
            existing = self._threads.get(key)
            if existing is not None:
                return existing
            self.thread_counter += 1
            thread_id = f"thread-{self.thread_counter}"
            self._threads[key] = thread_id
            if self.crash_after_thread_create:
                self.crash_after_thread_create = False
                raise SimulatedProcessCrash()
            return thread_id


class RecordingRepository(
    discord_delivery.InMemoryDiscordDeliveryRepository
):
    def __init__(
        self,
        state: discord_delivery.InMemoryDiscordDeliveryState,
    ) -> None:
        super().__init__(state)
        self.claim_limits: list[int] = []
        self.claim_tokens: list[str] = []
        self.renewed: list[tuple[str, str]] = []

    def claim_due(self, **kwargs):
        self.claim_limits.append(kwargs["limit"])
        self.claim_tokens.append(kwargs["lease_token"])
        return super().claim_due(**kwargs)

    def renew_lease(self, delivery_id: str, **kwargs) -> None:
        self.renewed.append((delivery_id, kwargs["lease_token"]))
        super().renew_lease(delivery_id, **kwargs)


def make_worker(
    state: discord_delivery.InMemoryDiscordDeliveryState,
    transport: FakeDiscordTransport,
    clock: MutableClock,
    worker_id: str,
) -> discord_delivery.DiscordDeliveryWorker:
    return discord_delivery.DiscordDeliveryWorker(
        discord_delivery.InMemoryDiscordDeliveryRepository(state),
        transport,
        worker_id=worker_id,
        lease_seconds=5,
        batch_size=10,
        base_backoff_seconds=2,
        max_backoff_seconds=8,
        clock=clock,
    )


def enqueue_text(
    worker: discord_delivery.DiscordDeliveryWorker,
    *,
    delivery_id: str,
    conversation_id: str = "conversation-1",
    content: str = "exact mirror payload",
) -> str:
    return worker.enqueue(
        key=discord_delivery.MirrorKey(
            surface="chat",
            configured_channel_id=(
                dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
            ),
            conversation_id=conversation_id,
        ),
        content=content,
        chunks=tuple(dev_gateway._split_discord_message(content)),
        delivery_id=delivery_id,
    )


@pytest.mark.asyncio
async def test_outage_is_queued_then_retried_without_losing_payload() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    transport.fail_posts = 1
    worker = make_worker(state, transport, clock, "worker-a")
    enqueue_text(worker, delivery_id="delivery-outage")

    first = await worker.attempt("delivery-outage")

    assert first.state == "retry"
    assert first.attempt_count == 1
    assert first.next_chunk_index == 0
    clock.advance(2)

    retried = await worker.run_once()

    assert len(retried) == 1
    assert retried[0].state == "delivered"
    assert retried[0].attempt_count == 2
    assert [content for _channel, content, _nonce in transport.posted_contents][-1] == (
        "exact mirror payload"
    )


@pytest.mark.asyncio
async def test_retry_backlog_is_exposed_as_raw_degraded_fact() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    transport.fail_posts = 1
    worker = make_worker(state, transport, clock, "worker-backlog")
    enqueue_text(worker, delivery_id="delivery-backlog")

    snapshots = await worker.run_once()

    assert snapshots[0].state == "retry"
    assert worker.last_backlog == discord_delivery.DeliveryBacklog(
        pending_count=0,
        leased_count=0,
        retry_count=1,
        delivered_count=0,
        oldest_undelivered_at=clock.value,
        max_undelivered_attempt_count=1,
        latest_error_type="OSError",
    )
    assert worker.last_backlog.has_retry_backlog is True


@pytest.mark.asyncio
async def test_run_once_claims_each_delivery_only_when_it_is_ready_to_run() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    repository = RecordingRepository(state)
    transport = FakeDiscordTransport()
    worker = discord_delivery.DiscordDeliveryWorker(
        repository,
        transport,
        worker_id="claim-one-worker",
        lease_seconds=5,
        batch_size=3,
        clock=clock,
    )
    for index in range(3):
        enqueue_text(
            worker,
            delivery_id=f"delivery-{index}",
            conversation_id=f"conversation-{index}",
        )

    snapshots = await worker.run_once()

    assert [snapshot.state for snapshot in snapshots] == [
        "delivered",
        "delivered",
        "delivered",
    ]
    assert repository.claim_limits == [1, 1, 1]
    assert len(set(repository.claim_tokens)) == 3
    assert all(repository.renewed)
    assert worker.last_backlog == discord_delivery.DeliveryBacklog(
        pending_count=0,
        leased_count=0,
        retry_count=0,
        delivered_count=3,
        oldest_undelivered_at=None,
        max_undelivered_attempt_count=0,
        latest_error_type=None,
    )


@pytest.mark.asyncio
async def test_crash_after_thread_create_recovers_exact_single_thread() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    transport.crash_after_thread_create = True
    first_worker = make_worker(state, transport, clock, "worker-before-crash")
    enqueue_text(first_worker, delivery_id="delivery-thread-crash")

    with pytest.raises(SimulatedProcessCrash):
        await first_worker.attempt("delivery-thread-crash")

    assert transport.thread_counter == 1
    clock.advance(5)
    restarted_worker = make_worker(
        state,
        transport,
        clock,
        "worker-after-restart",
    )

    snapshots = await restarted_worker.run_once()

    assert snapshots[0].state == "delivered"
    assert snapshots[0].thread_id == "thread-1"
    assert transport.thread_counter == 1


@pytest.mark.asyncio
async def test_concurrent_first_turns_share_one_exact_thread() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    worker_a = make_worker(state, transport, clock, "worker-a")
    worker_b = make_worker(state, transport, clock, "worker-b")
    enqueue_text(worker_a, delivery_id="delivery-concurrent-a")
    enqueue_text(worker_b, delivery_id="delivery-concurrent-b")

    first, second = await asyncio.gather(
        worker_a.attempt("delivery-concurrent-a"),
        worker_b.attempt("delivery-concurrent-b"),
    )

    assert first.state == "delivered"
    assert second.state == "delivered"
    assert first.thread_id == second.thread_id == "thread-1"
    assert transport.thread_counter == 1
    starter = next(
        content
        for channel_id, content, _nonce in transport.posted_contents
        if channel_id == dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
    )
    assert "conversation-1" not in starter
    assert discord_delivery.MirrorKey(
        "chat",
        dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
        "conversation-1",
    ).conversation_hash in starter


@pytest.mark.asyncio
async def test_process_restart_reuses_persisted_retry_state() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    transport.fail_posts = 1
    before_restart = make_worker(state, transport, clock, "worker-before")
    enqueue_text(before_restart, delivery_id="delivery-restart")
    assert (await before_restart.attempt("delivery-restart")).state == "retry"

    clock.advance(2)
    after_restart = make_worker(state, transport, clock, "worker-after")
    result = await after_restart.run_once()

    assert result[0].state == "delivered"
    assert result[0].attempt_count == 2


@pytest.mark.asyncio
async def test_history_reconciliation_prevents_duplicate_after_nonce_expiry_and_crash() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    transport.crash_after_chunk_post = True
    before_crash = make_worker(state, transport, clock, "worker-before")
    enqueue_text(
        before_crash,
        delivery_id="delivery-chunk-crash",
        content="authored once",
    )

    with pytest.raises(SimulatedProcessCrash):
        await before_crash.attempt("delivery-chunk-crash")

    authored_posts = [
        content
        for _channel, content, _nonce in transport.posted_contents
        if content == "authored once"
    ]
    assert authored_posts == ["authored once"]
    transport.expire_nonce_enforcement()
    clock.advance(5)

    after_crash = make_worker(state, transport, clock, "worker-after")
    result = await after_crash.run_once()

    assert result[0].state == "delivered"
    authored_posts = [
        content
        for _channel, content, _nonce in transport.posted_contents
        if content == "authored once"
    ]
    assert authored_posts == ["authored once"]


@pytest.mark.asyncio
async def test_more_than_2000_utf16_units_reconstructs_exactly() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    worker = make_worker(state, transport, clock, "worker-long")
    exact = " \tначало\n" + ("аб🙂\n" * 1300) + "\nкрай "
    enqueue_text(
        worker,
        delivery_id="delivery-long",
        content=exact,
    )

    snapshot = await worker.attempt("delivery-long")

    assert snapshot.state == "delivered"
    posted_chunks = [
        content
        for channel_id, content, _nonce in transport.posted_contents
        if channel_id == "thread-1"
    ]
    assert len(posted_chunks) > 1
    assert "".join(posted_chunks) == exact
    assert all(
        len(chunk.encode("utf-16-le", errors="surrogatepass")) // 2 <= 2000
        for chunk in posted_chunks
    )


@pytest.mark.asyncio
async def test_stable_delivery_id_dedupes_only_exact_envelope_replay() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    worker = make_worker(state, transport, clock, "worker-replay")
    enqueue_text(worker, delivery_id="caller-turn-1", content="same")
    first = await worker.attempt("caller-turn-1")
    clock.advance(1)
    enqueue_text(worker, delivery_id="caller-turn-1", content="same")
    replay = await worker.attempt("caller-turn-1")

    assert first.state == replay.state == "delivered"
    assert replay.attempt_count == 1
    with pytest.raises(
        RuntimeError,
        match="different exact envelope",
    ):
        enqueue_text(worker, delivery_id="caller-turn-1", content="different")

    enqueue_text(worker, delivery_id="caller-turn-2", content="same")
    second_legitimate_turn = await worker.attempt("caller-turn-2")
    assert second_legitimate_turn.state == "delivered"


@pytest.mark.asyncio
async def test_retention_redacts_only_successfully_delivered_rows() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    worker = make_worker(state, transport, clock, "worker-retention")
    enqueue_text(worker, delivery_id="delivery-complete")
    enqueue_text(worker, delivery_id="delivery-pending")
    assert (await worker.attempt("delivery-complete")).state == "delivered"

    clock.advance(604801)
    await worker.run_once()

    assert (
        state.deliveries["delivery-complete"].payload_redacted_at
        == clock.value
    )
    assert state.deliveries["delivery-pending"].payload_redacted_at is None


@pytest.mark.asyncio
async def test_durable_request_enqueues_without_waiting_for_discord() -> None:
    clock = MutableClock(datetime(2026, 7, 30, tzinfo=timezone.utc))
    state = discord_delivery.InMemoryDiscordDeliveryState()
    transport = FakeDiscordTransport()
    transport.fail_posts = 1
    worker = make_worker(state, transport, clock, "worker-decoupled")
    settings = dev_gateway.CanarySettings(
        profile_home=Path("/tmp/skyai-test-profile"),
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url="postgresql://mirror.invalid/skyai",
        discord_mirror_durable_required=True,
    )

    result = await dev_gateway.mirror_to_discord_durably(
        {
            "delivery_id": "delivery-decoupled",
            "conversation_id": "conversation-decoupled",
            "message": "Здравей",
        },
        {
            "status": "ok",
            "conversation_id": "conversation-decoupled",
            "reply": "Здравей!",
            "trace": {},
        },
        settings,
        worker,
        surface="chat",
    )

    assert result["status"] == "queued"
    assert result["delivery_state"] == "pending"
    assert result["attempt_count"] == 0
    assert transport.message_counter == 0
    assert state.deliveries["delivery-decoupled"].envelope.content


@pytest.mark.asyncio
async def test_app_worker_drains_durable_outbox_after_startup(
    tmp_path: Path,
) -> None:
    state = discord_delivery.InMemoryDiscordDeliveryState()
    repository = discord_delivery.InMemoryDiscordDeliveryRepository(state)
    transport = FakeDiscordTransport()
    settings = dev_gateway.CanarySettings(
        profile_home=tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url="postgresql://mirror.invalid/skyai",
        discord_mirror_durable_required=True,
        discord_mirror_worker_poll_seconds=0.001,
    )
    app = dev_gateway.create_app(
        settings,
        delivery_repository=repository,
        discord_transport=transport,
    )

    for startup_callback in app.on_startup:
        await startup_callback(app)
    try:
        key = discord_delivery.MirrorKey(
            "chat",
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            "conversation-background-worker",
        )
        repository.enqueue(
            discord_delivery.MirrorEnvelope(
                delivery_id="delivery-background-worker",
                key=key,
                content="exact mirror payload",
                chunks=("exact mirror payload",),
                created_at=datetime.now(timezone.utc),
            )
        )

        async def wait_until_delivered() -> None:
            while (
                repository.snapshot(
                    "delivery-background-worker"
                ).state
                != "delivered"
            ):
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_until_delivered(), timeout=1.0)

        snapshot = repository.snapshot("delivery-background-worker")
        assert snapshot.state == "delivered"
        assert snapshot.thread_id == "thread-1"
        assert snapshot.message_ids == ("message-2",)
        assert transport.message_counter == 2
    finally:
        for cleanup_callback in reversed(app.on_cleanup):
            await cleanup_callback(app)


def test_exact_lookalike_conversation_ids_have_distinct_hashes_and_names() -> None:
    keys = [
        discord_delivery.MirrorKey(
            "chat",
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            conversation_id,
        )
        for conversation_id in ("Case", "case", " Case", "Сase")
    ]

    assert len({key.conversation_hash for key in keys}) == 4
    assert len({discord_delivery.deterministic_thread_name(key) for key in keys}) == 4
    for key in keys:
        name = discord_delivery.deterministic_thread_name(key)
        assert key.recovery_digest in name
        assert key.conversation_id not in name


def test_conversation_id_is_exact_and_bounded_without_truncation() -> None:
    exact = " \tCase\n"
    key = discord_delivery.MirrorKey(
        "chat",
        dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
        exact,
    )

    assert key.conversation_id == exact
    with pytest.raises(ValueError, match="256-byte"):
        discord_delivery.MirrorKey(
            "chat",
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID,
            "x" * 257,
        )
    with pytest.raises(ValueError, match="delivery_id exceeds"):
        discord_delivery.MirrorEnvelope(
            delivery_id="x" * 257,
            key=key,
            content="payload",
            chunks=("payload",),
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )


def test_postgres_schema_enforces_exact_identity_and_progress_bounds() -> None:
    sql = Path(
        "plugins/skyai_customer/schema/discord_mirror_delivery_v1.sql"
    ).read_text(encoding="utf-8")

    assert "octet_length(conversation_id) BETWEEN 1 AND 256" in sql
    assert "octet_length(delivery_id) BETWEEN 1 AND 256" in sql
    assert "char_length(recovery_name) <= 100" in sql
    assert "jsonb_array_length(message_ids) = next_chunk_index" in sql


class FailingEnqueueRepository(
    discord_delivery.InMemoryDiscordDeliveryRepository
):
    def enqueue(self, envelope: discord_delivery.MirrorEnvelope) -> None:
        raise OSError("dedicated mirror database unavailable")


class FakeRequest:
    headers: dict[str, str] = {}

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def json(self) -> dict[str, object]:
        return self.payload


@pytest.mark.asyncio
async def test_durable_required_handler_fails_closed_when_enqueue_fails(
    tmp_path: Path,
) -> None:
    settings = dev_gateway.CanarySettings(
        profile_home=tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url="postgresql://mirror-only.invalid/skyai",
        discord_mirror_durable_required=True,
    )
    app = dev_gateway.create_app(
        settings,
        delivery_repository=FailingEnqueueRepository(),
        discord_transport=FakeDiscordTransport(),
    )
    route = next(
        route
        for route in app.router.routes()
        if route.method == "POST"
        and route.resource.canonical == "/chatkit/dev-message"
    )

    response = await route.handler(
        FakeRequest(
            {
                "delivery_id": "caller-turn-db-down",
                "conversation_id": "conversation-db-down",
                "message": "Здравей",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["error"] == "discord_mirror_enqueue_failed"
    assert "database unavailable" in payload["reason"]


@pytest.mark.asyncio
async def test_health_exposes_typed_durable_posture_without_secrets(
    tmp_path: Path,
) -> None:
    database_url = "postgresql://mirror-user:mirror-secret@db.invalid/skyai"
    settings = dev_gateway.CanarySettings(
        profile_home=tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="bot-secret",
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url=database_url,
        discord_mirror_durable_required=True,
    )
    app = dev_gateway.create_app(
        settings,
        delivery_repository=(
            discord_delivery.InMemoryDiscordDeliveryRepository()
        ),
        discord_transport=FakeDiscordTransport(),
    )
    route = next(
        route
        for route in app.router.routes()
        if route.method == "GET" and route.resource.canonical == "/health"
    )

    response = await route.handler(None)
    payload = json.loads(response.text)

    assert payload["discord_mirror"] == {
        "enabled": True,
        "durable_required": True,
        "durable_store": "postgres",
        "worker_configured": True,
        "worker_running": False,
        "first_database_poll_succeeded": False,
        "worker_last_cycle_ok": None,
        "worker_last_error_type": None,
        "delivery_contract": {
            "persistence": "persist_before_http_response",
            "retry": "at_least_once",
            "remote_reconciliation": "exact_nonce_history",
            "exactly_once_claimed": False,
        },
        "backlog": None,
        "delivery_degraded": False,
        "payload_retention_seconds": 604800,
    }
    assert "mirror-secret" not in response.text
    assert "bot-secret" not in response.text


@pytest.mark.asyncio
async def test_readiness_waits_for_first_successful_database_poll(
    tmp_path: Path,
) -> None:
    settings = dev_gateway.CanarySettings(
        profile_home=tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url="postgresql://mirror.invalid/skyai",
        discord_mirror_durable_required=True,
        discord_mirror_worker_poll_seconds=0.001,
    )
    app = dev_gateway.create_app(
        settings,
        delivery_repository=(
            discord_delivery.InMemoryDiscordDeliveryRepository()
        ),
        discord_transport=FakeDiscordTransport(),
    )
    ready_route = next(
        route
        for route in app.router.routes()
        if route.method == "GET" and route.resource.canonical == "/ready"
    )

    before_start = await ready_route.handler(None)
    assert before_start.status == 503
    assert (
        json.loads(before_start.text)["discord_mirror"][
            "first_database_poll_succeeded"
        ]
        is False
    )

    for startup_callback in app.on_startup:
        await startup_callback(app)
    try:
        async def wait_until_ready() -> None:
            while (await ready_route.handler(None)).status != 200:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_until_ready(), timeout=1.0)
        after_poll = await ready_route.handler(None)
        facts = json.loads(after_poll.text)["discord_mirror"]
        assert facts["first_database_poll_succeeded"] is True
        assert facts["worker_last_cycle_ok"] is True
        assert facts["backlog"]["undelivered_count"] == 0
    finally:
        for cleanup_callback in reversed(app.on_cleanup):
            await cleanup_callback(app)


@pytest.mark.asyncio
async def test_durable_handler_rejects_missing_stable_delivery_id_before_model(
    tmp_path: Path,
) -> None:
    calls = 0

    async def agent_runner(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"final_response": "must not run", "cards": []}

    settings = dev_gateway.CanarySettings(
        profile_home=tmp_path,
        discord_mirror_enabled=True,
        discord_mirror_bot_token="token",
        discord_mirror_channel_id=(
            dev_gateway.REQUIRED_DISCORD_MIRROR_CHANNEL_ID
        ),
        discord_mirror_create_threads=True,
        discord_mirror_database_url="postgresql://mirror.invalid/skyai",
        discord_mirror_durable_required=True,
    )
    app = dev_gateway.create_app(
        settings,
        agent_runner=agent_runner,
        delivery_repository=(
            discord_delivery.InMemoryDiscordDeliveryRepository()
        ),
        discord_transport=FakeDiscordTransport(),
    )
    route = next(
        route
        for route in app.router.routes()
        if route.method == "POST"
        and route.resource.canonical == "/chatkit/message"
    )

    response = await route.handler(
        FakeRequest(
            {
                "conversation_id": "conversation-no-delivery-id",
                "message": "Здравей",
            }
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["reason"] == (
        "delivery_id is required for durable Discord mirroring"
    )
    assert calls == 0
