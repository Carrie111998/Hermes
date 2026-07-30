from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import threading

import pytest

from plugins.skyai_customer import discord_delivery


TEST_DSN_ENV = "SKYAI_TEST_POSTGRES_DSN"
SCHEMA_PATH = Path(
    "plugins/skyai_customer/schema/discord_mirror_delivery_v1.sql"
)


def _test_dsn() -> str:
    value = os.getenv(TEST_DSN_ENV)
    if value is None:
        pytest.skip(
            f"{TEST_DSN_ENV} is not configured for the disposable Postgres test"
        )
    if type(value) is not str or not value:
        pytest.fail(f"{TEST_DSN_ENV} must be a nonempty string when configured")
    return value


@pytest.fixture()
def postgres_repository():
    psycopg = pytest.importorskip(
        "psycopg",
        reason="psycopg is required for the disposable Postgres integration test",
    )
    dsn = _test_dsn()
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(schema_sql)
            cursor.execute(
                """
                TRUNCATE
                    skyai_discord_mirror.deliveries,
                    skyai_discord_mirror.threads
                """
            )
    repository = discord_delivery.PostgresDiscordDeliveryRepository(
        dsn,
        connect=psycopg.connect,
    )
    yield repository
    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                TRUNCATE
                    skyai_discord_mirror.deliveries,
                    skyai_discord_mirror.threads
                """
            )


def _envelope(
    delivery_id: str,
    conversation_id: str,
    created_at: datetime,
) -> discord_delivery.MirrorEnvelope:
    content = f"payload:{delivery_id}"
    return discord_delivery.MirrorEnvelope(
        delivery_id=delivery_id,
        key=discord_delivery.MirrorKey(
            surface="chat",
            configured_channel_id="1510888721614901358",
            conversation_id=conversation_id,
        ),
        content=content,
        chunks=(content,),
        created_at=created_at,
    )


def test_postgres_migration_leases_advisory_lock_progress_and_retention(
    postgres_repository,
) -> None:
    repository = postgres_repository
    second_repository = discord_delivery.PostgresDiscordDeliveryRepository(
        repository.dsn,
        connect=repository._connect,
    )
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    first = _envelope("pg-delivery-1", "pg-conversation-1", now)
    second = _envelope("pg-delivery-2", "pg-conversation-2", now)
    repository.enqueue(first)
    repository.enqueue(second)

    first_lease = repository.claim_due(
        lease_token="worker-a",
        now=now,
        lease_seconds=30,
        limit=1,
    )
    second_lease = second_repository.claim_due(
        lease_token="worker-b",
        now=now,
        lease_seconds=30,
        limit=1,
    )

    assert len(first_lease) == len(second_lease) == 1
    assert (
        first_lease[0].envelope.delivery_id
        != second_lease[0].envelope.delivery_id
    )
    repository.renew_lease(
        first_lease[0].envelope.delivery_id,
        lease_token=first_lease[0].lease_token,
        now=now + timedelta(seconds=20),
        lease_seconds=30,
    )
    assert repository.backlog().leased_count == 2

    thread_key = first_lease[0].envelope.key
    recovery_name = discord_delivery.deterministic_thread_name(thread_key)
    resolver_calls = 0
    resolver_lock = threading.Lock()

    def resolve() -> str:
        nonlocal resolver_calls

        def remote_resolver(exact_name: str) -> str:
            nonlocal resolver_calls
            assert exact_name == recovery_name
            with resolver_lock:
                resolver_calls += 1
            return "discord-thread-1"

        return repository.resolve_thread(
            thread_key,
            recovery_name,
            remote_resolver,
            now=now,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        resolved = list(pool.map(lambda _index: resolve(), range(2)))

    assert resolved == ["discord-thread-1", "discord-thread-1"]
    assert resolver_calls == 1

    lease = first_lease[0]
    repository.record_chunk(
        lease.envelope.delivery_id,
        lease_token=lease.lease_token,
        chunk_index=0,
        message_id="discord-message-1",
        thread_id="discord-thread-1",
        now=now + timedelta(seconds=1),
    )
    repository.mark_delivered(
        lease.envelope.delivery_id,
        lease_token=lease.lease_token,
        thread_id="discord-thread-1",
        now=now + timedelta(seconds=2),
    )
    assert repository.snapshot(lease.envelope.delivery_id).state == "delivered"
    assert repository.redact_delivered_payloads(
        delivered_before=now + timedelta(seconds=3),
        now=now + timedelta(seconds=4),
        limit=10,
    ) == 1
