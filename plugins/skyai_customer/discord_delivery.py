"""Durable, SkyAI-only Discord mirror delivery.

This module is an operational plugin edge.  It does not read or write Hermes
memory, Canonical Brain, or a generic application ``DATABASE_URL``.  Callers
must provide the dedicated SkyAI Discord mirror DSN explicitly.

The model remains the sole semantic authority.  Everything here is limited to
exact schema validation, persistence, leases, idempotent transport, and
configured destination delivery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import uuid
from typing import Any, Protocol
from urllib.parse import urlparse


MIRROR_SURFACES = ("chat", "voice")
DELIVERY_STATES = ("pending", "leased", "retry", "delivered")
DISCORD_NONCE_MAX_LENGTH = 25
DISCORD_THREAD_NAME_MAX_LENGTH = 100
MAX_CONVERSATION_ID_BYTES = 256
MAX_DELIVERY_ID_BYTES = 256


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_exact_nonempty_string(value: Any, field_name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be a nonempty string")
    return value


def _require_exact_positive_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _exact_key_bytes(*values: str) -> bytes:
    encoded = bytearray()
    for value in values:
        raw = value.encode("utf-8", errors="surrogatepass")
        encoded.extend(len(raw).to_bytes(8, "big", signed=False))
        encoded.extend(raw)
    return bytes(encoded)


@dataclass(frozen=True)
class MirrorKey:
    surface: str
    configured_channel_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        if type(self.surface) is not str or self.surface not in MIRROR_SURFACES:
            raise ValueError(
                f"surface must exactly equal one of {MIRROR_SURFACES!r}"
            )
        _require_exact_nonempty_string(
            self.configured_channel_id,
            "configured_channel_id",
        )
        _require_exact_nonempty_string(self.conversation_id, "conversation_id")
        if (
            len(
                self.conversation_id.encode(
                    "utf-8",
                    errors="surrogatepass",
                )
            )
            > MAX_CONVERSATION_ID_BYTES
        ):
            raise ValueError(
                "conversation_id exceeds the 256-byte durable mirror limit"
            )

    @property
    def advisory_lock_key(self) -> int:
        digest = hashlib.sha256(
            _exact_key_bytes(
                self.surface,
                self.configured_channel_id,
                self.conversation_id,
            )
        ).digest()
        return int.from_bytes(digest[:8], "big", signed=True)

    @property
    def recovery_digest(self) -> str:
        return hashlib.sha256(
            _exact_key_bytes(
                self.surface,
                self.configured_channel_id,
                self.conversation_id,
            )
        ).hexdigest()

    @property
    def conversation_hash(self) -> str:
        return hashlib.sha256(
            self.conversation_id.encode("utf-8", errors="surrogatepass")
        ).hexdigest()


def deterministic_thread_name(key: MirrorKey) -> str:
    """Return an exact, collision-resistant Discord recovery name."""

    prefix = "Voice SkyAI" if key.surface == "voice" else "SkyAI v2"
    result = f"{prefix} · #{key.recovery_digest}"
    if len(result) > DISCORD_THREAD_NAME_MAX_LENGTH:  # pragma: no cover
        raise RuntimeError("Discord recovery thread name exceeds the limit")
    return result


def deterministic_nonce(*values: str) -> str:
    for index, value in enumerate(values):
        _require_exact_nonempty_string(value, f"nonce component {index}")
    nonce = hashlib.sha256(_exact_key_bytes(*values)).hexdigest()[
        :DISCORD_NONCE_MAX_LENGTH
    ]
    if not nonce:  # pragma: no cover - sha256 is never empty
        raise RuntimeError("failed to construct Discord nonce")
    return nonce


@dataclass(frozen=True)
class MirrorEnvelope:
    delivery_id: str
    key: MirrorKey
    content: str
    chunks: tuple[str, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_exact_nonempty_string(self.delivery_id, "delivery_id")
        if (
            len(
                self.delivery_id.encode(
                    "utf-8",
                    errors="surrogatepass",
                )
            )
            > MAX_DELIVERY_ID_BYTES
        ):
            raise ValueError("delivery_id exceeds the 256-byte limit")
        if not isinstance(self.key, MirrorKey):
            raise ValueError("key must be a MirrorKey")
        _require_exact_nonempty_string(self.content, "content")
        if type(self.chunks) is not tuple or not self.chunks:
            raise ValueError("chunks must be a nonempty tuple")
        for index, chunk in enumerate(self.chunks):
            _require_exact_nonempty_string(chunk, f"chunks[{index}]")
        if "".join(self.chunks) != self.content:
            raise ValueError("chunks must reconstruct content exactly")
        if not isinstance(self.created_at, datetime):
            raise ValueError("created_at must be a datetime")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True)
class DeliveryLease:
    envelope: MirrorEnvelope
    lease_token: str
    lease_expires_at: datetime
    attempt_count: int
    next_chunk_index: int
    message_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, MirrorEnvelope):
            raise ValueError("envelope must be a MirrorEnvelope")
        _require_exact_nonempty_string(self.lease_token, "lease_token")
        if not isinstance(self.lease_expires_at, datetime):
            raise ValueError("lease_expires_at must be a datetime")
        _require_exact_positive_int(self.attempt_count, "attempt_count")
        if type(self.next_chunk_index) is not int:
            raise ValueError("next_chunk_index must be an integer")
        if not 0 <= self.next_chunk_index <= len(self.envelope.chunks):
            raise ValueError("next_chunk_index is outside the chunk range")
        if type(self.message_ids) is not tuple:
            raise ValueError("message_ids must be a tuple")
        if len(self.message_ids) != self.next_chunk_index:
            raise ValueError(
                "message_ids length must exactly equal next_chunk_index"
            )
        for index, message_id in enumerate(self.message_ids):
            _require_exact_nonempty_string(message_id, f"message_ids[{index}]")


@dataclass(frozen=True)
class DeliverySnapshot:
    delivery_id: str
    state: str
    attempt_count: int
    next_chunk_index: int
    thread_id: str | None
    message_ids: tuple[str, ...]
    available_at: datetime
    lease_expires_at: datetime | None
    last_error: str | None

    def __post_init__(self) -> None:
        _require_exact_nonempty_string(self.delivery_id, "delivery_id")
        if type(self.state) is not str or self.state not in DELIVERY_STATES:
            raise ValueError(f"state must exactly equal one of {DELIVERY_STATES!r}")
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise ValueError("attempt_count must be a nonnegative integer")
        if type(self.next_chunk_index) is not int or self.next_chunk_index < 0:
            raise ValueError("next_chunk_index must be a nonnegative integer")
        if self.thread_id is not None:
            _require_exact_nonempty_string(self.thread_id, "thread_id")
        if type(self.message_ids) is not tuple:
            raise ValueError("message_ids must be a tuple")
        if len(self.message_ids) != self.next_chunk_index:
            raise ValueError(
                "message_ids length must exactly equal next_chunk_index"
            )
        for index, message_id in enumerate(self.message_ids):
            _require_exact_nonempty_string(message_id, f"message_ids[{index}]")
        if not isinstance(self.available_at, datetime):
            raise ValueError("available_at must be a datetime")
        if self.lease_expires_at is not None and not isinstance(
            self.lease_expires_at,
            datetime,
        ):
            raise ValueError("lease_expires_at must be a datetime or None")
        if self.last_error is not None and type(self.last_error) is not str:
            raise ValueError("last_error must be a string or None")


@dataclass(frozen=True)
class DeliveryBacklog:
    pending_count: int
    leased_count: int
    retry_count: int
    delivered_count: int
    oldest_undelivered_at: datetime | None
    max_undelivered_attempt_count: int
    latest_error_type: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "pending_count",
            "leased_count",
            "retry_count",
            "delivered_count",
            "max_undelivered_attempt_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if self.oldest_undelivered_at is not None and not isinstance(
            self.oldest_undelivered_at,
            datetime,
        ):
            raise ValueError(
                "oldest_undelivered_at must be a datetime or None"
            )
        if self.latest_error_type is not None:
            _require_exact_nonempty_string(
                self.latest_error_type,
                "latest_error_type",
            )

    @property
    def undelivered_count(self) -> int:
        return self.pending_count + self.leased_count + self.retry_count

    @property
    def has_retry_backlog(self) -> bool:
        return self.retry_count > 0


class DiscordDeliveryRepository(Protocol):
    def enqueue(self, envelope: MirrorEnvelope) -> None: ...

    def claim_one(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> DeliveryLease | None: ...

    def claim_due(
        self,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[DeliveryLease]: ...

    def renew_lease(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> None: ...

    def resolve_thread(
        self,
        key: MirrorKey,
        recovery_name: str,
        resolver: Callable[[str], str],
        *,
        now: datetime,
    ) -> str: ...

    def record_chunk(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        chunk_index: int,
        message_id: str,
        thread_id: str,
        now: datetime,
    ) -> None: ...

    def mark_delivered(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        thread_id: str,
        now: datetime,
    ) -> None: ...

    def mark_retry(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        available_at: datetime,
        last_error: str,
        now: datetime,
    ) -> None: ...

    def snapshot(self, delivery_id: str) -> DeliverySnapshot: ...

    def redact_delivered_payloads(
        self,
        *,
        delivered_before: datetime,
        now: datetime,
        limit: int,
    ) -> int: ...

    def backlog(self) -> DeliveryBacklog: ...


class DiscordTransport(Protocol):
    def find_threads_by_exact_name(
        self,
        configured_channel_id: str,
        exact_name: str,
    ) -> Sequence[str]: ...

    def find_message_ids_by_exact_nonce(
        self,
        channel_id: str,
        nonce: str,
    ) -> Sequence[str]: ...

    def post_message(
        self,
        channel_id: str,
        content: str,
        nonce: str,
    ) -> str: ...

    def start_thread_from_message(
        self,
        configured_channel_id: str,
        starter_message_id: str,
        exact_name: str,
    ) -> str: ...


@dataclass
class _MemoryDelivery:
    envelope: MirrorEnvelope
    state: str = "pending"
    attempt_count: int = 0
    available_at: datetime = field(default_factory=utc_now)
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    thread_id: str | None = None
    next_chunk_index: int = 0
    message_ids: list[str] = field(default_factory=list)
    last_error: str | None = None
    delivered_at: datetime | None = None
    payload_redacted_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class InMemoryDiscordDeliveryState:
    """Shared fake state used to exercise process-restart behavior."""

    deliveries: dict[str, _MemoryDelivery] = field(default_factory=dict)
    threads: dict[MirrorKey, tuple[str, str]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    thread_locks: dict[MirrorKey, threading.RLock] = field(default_factory=dict)


class InMemoryDiscordDeliveryRepository:
    """Exact fake of the persistent boundary for E2E tests."""

    def __init__(
        self,
        state: InMemoryDiscordDeliveryState | None = None,
    ) -> None:
        self.state = state or InMemoryDiscordDeliveryState()

    def enqueue(self, envelope: MirrorEnvelope) -> None:
        if not isinstance(envelope, MirrorEnvelope):
            raise ValueError("envelope must be a MirrorEnvelope")
        with self.state.lock:
            existing = self.state.deliveries.get(envelope.delivery_id)
            if existing is not None:
                if (
                    existing.envelope.delivery_id != envelope.delivery_id
                    or existing.envelope.key != envelope.key
                    or existing.envelope.content != envelope.content
                    or existing.envelope.chunks != envelope.chunks
                ):
                    raise RuntimeError(
                        "delivery_id already exists with a different exact envelope"
                    )
                return
            self.state.deliveries[envelope.delivery_id] = _MemoryDelivery(
                envelope=envelope,
                available_at=envelope.created_at,
                updated_at=envelope.created_at,
            )

    def _claim_record(
        self,
        record: _MemoryDelivery,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> DeliveryLease | None:
        eligible = (
            record.state in ("pending", "retry") and record.available_at <= now
        ) or (
            record.state == "leased"
            and record.lease_expires_at is not None
            and record.lease_expires_at <= now
        )
        if not eligible:
            return None
        record.state = "leased"
        record.lease_token = lease_token
        record.lease_expires_at = now + timedelta(seconds=lease_seconds)
        record.attempt_count += 1
        record.updated_at = now
        return DeliveryLease(
            envelope=record.envelope,
            lease_token=lease_token,
            lease_expires_at=record.lease_expires_at,
            attempt_count=record.attempt_count,
            next_chunk_index=record.next_chunk_index,
            message_ids=tuple(record.message_ids),
        )

    def claim_one(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> DeliveryLease | None:
        _require_exact_nonempty_string(delivery_id, "delivery_id")
        _require_exact_nonempty_string(lease_token, "lease_token")
        _require_exact_positive_int(lease_seconds, "lease_seconds")
        with self.state.lock:
            record = self.state.deliveries.get(delivery_id)
            if record is None:
                raise KeyError(delivery_id)
            return self._claim_record(
                record,
                lease_token=lease_token,
                now=now,
                lease_seconds=lease_seconds,
            )

    def claim_due(
        self,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[DeliveryLease]:
        _require_exact_nonempty_string(lease_token, "lease_token")
        _require_exact_positive_int(lease_seconds, "lease_seconds")
        _require_exact_positive_int(limit, "limit")
        with self.state.lock:
            ordered = sorted(
                self.state.deliveries.values(),
                key=lambda item: (
                    item.envelope.created_at,
                    item.envelope.delivery_id,
                ),
            )
            leases: list[DeliveryLease] = []
            for record in ordered:
                if len(leases) >= limit:
                    break
                lease = self._claim_record(
                    record,
                    lease_token=lease_token,
                    now=now,
                    lease_seconds=lease_seconds,
                )
                if lease is not None:
                    leases.append(lease)
            return leases

    def renew_lease(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        _require_exact_nonempty_string(delivery_id, "delivery_id")
        _require_exact_nonempty_string(lease_token, "lease_token")
        if not isinstance(now, datetime):
            raise ValueError("now must be a datetime")
        _require_exact_positive_int(lease_seconds, "lease_seconds")
        with self.state.lock:
            record = self._leased_record(delivery_id, lease_token)
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.updated_at = now

    def resolve_thread(
        self,
        key: MirrorKey,
        recovery_name: str,
        resolver: Callable[[str], str],
        *,
        now: datetime,
    ) -> str:
        if not isinstance(key, MirrorKey):
            raise ValueError("key must be a MirrorKey")
        _require_exact_nonempty_string(recovery_name, "recovery_name")
        if not callable(resolver):
            raise ValueError("resolver must be callable")
        with self.state.lock:
            key_lock = self.state.thread_locks.setdefault(key, threading.RLock())
        with key_lock:
            with self.state.lock:
                existing = self.state.threads.get(key)
                if existing is not None:
                    existing_name, thread_id = existing
                    if existing_name != recovery_name:
                        raise RuntimeError(
                            "stored Discord recovery name does not match exact key"
                        )
                    return thread_id
            thread_id = resolver(recovery_name)
            _require_exact_nonempty_string(thread_id, "thread_id")
            with self.state.lock:
                existing = self.state.threads.get(key)
                if existing is not None:
                    existing_name, existing_thread_id = existing
                    if (
                        existing_name != recovery_name
                        or existing_thread_id != thread_id
                    ):
                        raise RuntimeError(
                            "concurrent Discord thread resolution disagreed"
                        )
                    return existing_thread_id
                self.state.threads[key] = (recovery_name, thread_id)
            return thread_id

    def _leased_record(
        self,
        delivery_id: str,
        lease_token: str,
    ) -> _MemoryDelivery:
        record = self.state.deliveries.get(delivery_id)
        if record is None:
            raise KeyError(delivery_id)
        if record.state != "leased" or record.lease_token != lease_token:
            raise RuntimeError("delivery lease is not owned by this worker")
        return record

    def record_chunk(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        chunk_index: int,
        message_id: str,
        thread_id: str,
        now: datetime,
    ) -> None:
        _require_exact_nonempty_string(message_id, "message_id")
        _require_exact_nonempty_string(thread_id, "thread_id")
        if type(chunk_index) is not int or chunk_index < 0:
            raise ValueError("chunk_index must be a nonnegative integer")
        with self.state.lock:
            record = self._leased_record(delivery_id, lease_token)
            if chunk_index != record.next_chunk_index:
                raise RuntimeError("chunk_index does not exactly match delivery progress")
            if record.thread_id is not None and record.thread_id != thread_id:
                raise RuntimeError("delivery thread_id changed")
            record.thread_id = thread_id
            record.message_ids.append(message_id)
            record.next_chunk_index += 1
            record.updated_at = now

    def mark_delivered(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        thread_id: str,
        now: datetime,
    ) -> None:
        _require_exact_nonempty_string(thread_id, "thread_id")
        with self.state.lock:
            record = self._leased_record(delivery_id, lease_token)
            if record.next_chunk_index != len(record.envelope.chunks):
                raise RuntimeError("cannot deliver before every exact chunk is recorded")
            if record.thread_id != thread_id:
                raise RuntimeError("delivered thread_id does not match recorded thread")
            record.state = "delivered"
            record.lease_token = None
            record.lease_expires_at = None
            record.last_error = None
            record.delivered_at = now
            record.updated_at = now

    def mark_retry(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        available_at: datetime,
        last_error: str,
        now: datetime,
    ) -> None:
        if type(last_error) is not str:
            raise ValueError("last_error must be a string")
        with self.state.lock:
            record = self._leased_record(delivery_id, lease_token)
            record.state = "retry"
            record.available_at = available_at
            record.lease_token = None
            record.lease_expires_at = None
            record.last_error = last_error
            record.updated_at = now

    def snapshot(self, delivery_id: str) -> DeliverySnapshot:
        _require_exact_nonempty_string(delivery_id, "delivery_id")
        with self.state.lock:
            record = self.state.deliveries.get(delivery_id)
            if record is None:
                raise KeyError(delivery_id)
            return DeliverySnapshot(
                delivery_id=delivery_id,
                state=record.state,
                attempt_count=record.attempt_count,
                next_chunk_index=record.next_chunk_index,
                thread_id=record.thread_id,
                message_ids=tuple(record.message_ids),
                available_at=record.available_at,
                lease_expires_at=record.lease_expires_at,
                last_error=record.last_error,
            )

    def redact_delivered_payloads(
        self,
        *,
        delivered_before: datetime,
        now: datetime,
        limit: int,
    ) -> int:
        if not isinstance(delivered_before, datetime):
            raise ValueError("delivered_before must be a datetime")
        if not isinstance(now, datetime):
            raise ValueError("now must be a datetime")
        _require_exact_positive_int(limit, "limit")
        redacted = 0
        with self.state.lock:
            ordered = sorted(
                self.state.deliveries.values(),
                key=lambda item: (
                    item.delivered_at or datetime.max.replace(tzinfo=timezone.utc),
                    item.envelope.delivery_id,
                ),
            )
            for record in ordered:
                if redacted >= limit:
                    break
                if (
                    record.state == "delivered"
                    and record.delivered_at is not None
                    and record.delivered_at < delivered_before
                    and record.payload_redacted_at is None
                ):
                    # The fake records the same state transition as Postgres.
                    # Its immutable envelope remains in test memory only so
                    # assertions can diagnose an exact failed fixture.
                    record.payload_redacted_at = now
                    record.updated_at = now
                    redacted += 1
        return redacted

    def backlog(self) -> DeliveryBacklog:
        with self.state.lock:
            records = tuple(self.state.deliveries.values())
            undelivered = tuple(
                record
                for record in records
                if record.state != "delivered"
            )
            errored = tuple(
                record
                for record in undelivered
                if record.last_error is not None
            )
            latest_error = (
                max(errored, key=lambda record: record.updated_at).last_error
                if errored
                else None
            )
            return DeliveryBacklog(
                pending_count=sum(
                    record.state == "pending" for record in records
                ),
                leased_count=sum(
                    record.state == "leased" for record in records
                ),
                retry_count=sum(
                    record.state == "retry" for record in records
                ),
                delivered_count=sum(
                    record.state == "delivered" for record in records
                ),
                oldest_undelivered_at=(
                    min(
                        record.envelope.created_at
                        for record in undelivered
                    )
                    if undelivered
                    else None
                ),
                max_undelivered_attempt_count=max(
                    (
                        record.attempt_count
                        for record in undelivered
                    ),
                    default=0,
                ),
                latest_error_type=latest_error,
            )


class PostgresDiscordDeliveryRepository:
    """Dedicated Postgres implementation.

    ``psycopg`` is imported only when this plugin-edge repository is selected.
    No fallback DSN is read here.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.dsn = _require_exact_nonempty_string(
            dsn,
            "SkyAI Discord mirror Postgres DSN",
        )
        parsed_dsn = urlparse(self.dsn)
        if (
            parsed_dsn.scheme != "postgresql"
            or not parsed_dsn.netloc
            or any(character.isspace() for character in self.dsn)
        ):
            raise ValueError(
                "SkyAI Discord mirror Postgres DSN must be an exact "
                "postgresql URL"
            )
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - deployment guard
                raise RuntimeError(
                    "SkyAI Discord mirror persistence requires "
                    "psycopg[binary]==3.2.9"
                ) from exc
            connect = psycopg.connect
        if not callable(connect):
            raise ValueError("connect must be callable")
        self._connect = connect

    @staticmethod
    def _chunks_json(chunks: tuple[str, ...]) -> str:
        return json.dumps(
            list(chunks),
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def enqueue(self, envelope: MirrorEnvelope) -> None:
        if not isinstance(envelope, MirrorEnvelope):
            raise ValueError("envelope must be a MirrorEnvelope")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO skyai_discord_mirror.deliveries (
                        delivery_id,
                        surface,
                        configured_channel_id,
                        conversation_hash,
                        conversation_id,
                        content,
                        chunks,
                        state,
                        available_at,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s::jsonb,
                        'pending', %s, %s, %s
                    )
                    ON CONFLICT (delivery_id) DO NOTHING
                    """,
                    (
                        envelope.delivery_id,
                        envelope.key.surface,
                        envelope.key.configured_channel_id,
                        envelope.key.conversation_hash,
                        envelope.key.conversation_id,
                        envelope.content,
                        self._chunks_json(envelope.chunks),
                        envelope.created_at,
                        envelope.created_at,
                        envelope.created_at,
                    ),
                )
                cursor.execute(
                    """
                    SELECT
                        surface,
                        configured_channel_id,
                        conversation_hash,
                        conversation_id,
                        content,
                        chunks
                    FROM skyai_discord_mirror.deliveries
                    WHERE delivery_id = %s
                    """,
                    (envelope.delivery_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("enqueued delivery could not be read back")
                exact = (
                    envelope.key.surface,
                    envelope.key.configured_channel_id,
                    envelope.key.conversation_hash,
                    envelope.key.conversation_id,
                    envelope.content,
                    list(envelope.chunks),
                )
                if tuple(row) != exact:
                    raise RuntimeError(
                        "delivery_id already exists with a different exact envelope"
                    )

    @staticmethod
    def _lease_from_row(row: Sequence[Any]) -> DeliveryLease:
        (
            delivery_id,
            surface,
            configured_channel_id,
            conversation_hash,
            conversation_id,
            content,
            chunks,
            created_at,
            lease_token,
            lease_expires_at,
            attempt_count,
            next_chunk_index,
            message_ids,
        ) = row
        if type(chunks) is not list:
            raise RuntimeError("stored delivery chunks must be a JSON array")
        if type(message_ids) is not list:
            raise RuntimeError("stored message_ids must be a JSON array")
        envelope = MirrorEnvelope(
            delivery_id=delivery_id,
            key=MirrorKey(
                surface=surface,
                configured_channel_id=configured_channel_id,
                conversation_id=conversation_id,
            ),
            content=content,
            chunks=tuple(chunks),
            created_at=created_at,
        )
        if conversation_hash != envelope.key.conversation_hash:
            raise RuntimeError(
                "stored conversation_hash does not match exact conversation_id"
            )
        return DeliveryLease(
            envelope=envelope,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            attempt_count=attempt_count,
            next_chunk_index=next_chunk_index,
            message_ids=tuple(message_ids),
        )

    def _claim(
        self,
        *,
        delivery_id: str | None,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[DeliveryLease]:
        _require_exact_nonempty_string(lease_token, "lease_token")
        _require_exact_positive_int(lease_seconds, "lease_seconds")
        _require_exact_positive_int(limit, "limit")
        if delivery_id is not None:
            _require_exact_nonempty_string(delivery_id, "delivery_id")
        expires_at = now + timedelta(seconds=lease_seconds)
        id_clause = "AND delivery_id = %s" if delivery_id is not None else ""
        params: list[Any] = [now]
        if delivery_id is not None:
            params.append(delivery_id)
        params.extend([limit, lease_token, expires_at, now])
        query = f"""
            WITH claimable AS (
                SELECT delivery_id
                FROM skyai_discord_mirror.deliveries
                WHERE (
                    (state IN ('pending', 'retry') AND available_at <= %s)
                    OR
                    (state = 'leased' AND lease_expires_at <= %s)
                )
                {id_clause}
                ORDER BY created_at, delivery_id
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            UPDATE skyai_discord_mirror.deliveries AS delivery
            SET
                state = 'leased',
                lease_token = %s,
                lease_expires_at = %s,
                attempt_count = delivery.attempt_count + 1,
                updated_at = %s
            FROM claimable
            WHERE delivery.delivery_id = claimable.delivery_id
            RETURNING
                delivery.delivery_id,
                delivery.surface,
                delivery.configured_channel_id,
                delivery.conversation_hash,
                delivery.conversation_id,
                delivery.content,
                delivery.chunks,
                delivery.created_at,
                delivery.lease_token,
                delivery.lease_expires_at,
                delivery.attempt_count,
                delivery.next_chunk_index,
                delivery.message_ids
        """
        # The eligibility expression uses ``now`` twice.
        params.insert(1, now)
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, tuple(params))
                rows = cursor.fetchall()
        return [self._lease_from_row(row) for row in rows]

    def claim_one(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> DeliveryLease | None:
        leases = self._claim(
            delivery_id=delivery_id,
            lease_token=lease_token,
            now=now,
            lease_seconds=lease_seconds,
            limit=1,
        )
        return leases[0] if leases else None

    def claim_due(
        self,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[DeliveryLease]:
        return self._claim(
            delivery_id=None,
            lease_token=lease_token,
            now=now,
            lease_seconds=lease_seconds,
            limit=limit,
        )

    def renew_lease(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        _require_exact_nonempty_string(delivery_id, "delivery_id")
        _require_exact_nonempty_string(lease_token, "lease_token")
        if not isinstance(now, datetime):
            raise ValueError("now must be a datetime")
        _require_exact_positive_int(lease_seconds, "lease_seconds")
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE skyai_discord_mirror.deliveries
                    SET
                        lease_expires_at = %s,
                        updated_at = %s
                    WHERE
                        delivery_id = %s
                        AND state = 'leased'
                        AND lease_token = %s
                    """,
                    (
                        expires_at,
                        now,
                        delivery_id,
                        lease_token,
                    ),
                )
                self._expect_single_updated_row(cursor, "renew_lease")

    def resolve_thread(
        self,
        key: MirrorKey,
        recovery_name: str,
        resolver: Callable[[str], str],
        *,
        now: datetime,
    ) -> str:
        if not isinstance(key, MirrorKey):
            raise ValueError("key must be a MirrorKey")
        _require_exact_nonempty_string(recovery_name, "recovery_name")
        if not callable(resolver):
            raise ValueError("resolver must be callable")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (key.advisory_lock_key,),
                )
                cursor.execute(
                    """
                    SELECT recovery_name, discord_thread_id
                    FROM skyai_discord_mirror.threads
                    WHERE
                        surface = %s
                        AND configured_channel_id = %s
                        AND conversation_hash = %s
                    FOR UPDATE
                    """,
                    (
                        key.surface,
                        key.configured_channel_id,
                        key.conversation_hash,
                    ),
                )
                row = cursor.fetchone()
                if row is not None:
                    if row[0] != recovery_name:
                        raise RuntimeError(
                            "stored Discord recovery name does not match exact key"
                        )
                    return _require_exact_nonempty_string(
                        row[1],
                        "discord_thread_id",
                    )
                thread_id = _require_exact_nonempty_string(
                    resolver(recovery_name),
                    "discord_thread_id",
                )
                cursor.execute(
                    """
                    INSERT INTO skyai_discord_mirror.threads (
                        surface,
                        configured_channel_id,
                        conversation_hash,
                        conversation_id,
                        recovery_name,
                        discord_thread_id,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        key.surface,
                        key.configured_channel_id,
                        key.conversation_hash,
                        key.conversation_id,
                        recovery_name,
                        thread_id,
                        now,
                        now,
                    ),
                )
                return thread_id

    @staticmethod
    def _expect_single_updated_row(cursor: Any, action: str) -> None:
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"{action} requires one exact live delivery lease"
            )

    def record_chunk(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        chunk_index: int,
        message_id: str,
        thread_id: str,
        now: datetime,
    ) -> None:
        _require_exact_nonempty_string(delivery_id, "delivery_id")
        _require_exact_nonempty_string(lease_token, "lease_token")
        _require_exact_nonempty_string(message_id, "message_id")
        _require_exact_nonempty_string(thread_id, "thread_id")
        if type(chunk_index) is not int or chunk_index < 0:
            raise ValueError("chunk_index must be a nonnegative integer")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE skyai_discord_mirror.deliveries
                    SET
                        thread_id = %s,
                        message_ids = message_ids || jsonb_build_array(%s::text),
                        next_chunk_index = next_chunk_index + 1,
                        updated_at = %s
                    WHERE
                        delivery_id = %s
                        AND state = 'leased'
                        AND lease_token = %s
                        AND next_chunk_index = %s
                        AND (thread_id IS NULL OR thread_id = %s)
                    """,
                    (
                        thread_id,
                        message_id,
                        now,
                        delivery_id,
                        lease_token,
                        chunk_index,
                        thread_id,
                    ),
                )
                self._expect_single_updated_row(cursor, "record_chunk")

    def mark_delivered(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        thread_id: str,
        now: datetime,
    ) -> None:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE skyai_discord_mirror.deliveries
                    SET
                        state = 'delivered',
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = NULL,
                        delivered_at = %s,
                        updated_at = %s
                    WHERE
                        delivery_id = %s
                        AND state = 'leased'
                        AND lease_token = %s
                        AND thread_id = %s
                        AND next_chunk_index = jsonb_array_length(chunks)
                    """,
                    (now, now, delivery_id, lease_token, thread_id),
                )
                self._expect_single_updated_row(cursor, "mark_delivered")

    def redact_delivered_payloads(
        self,
        *,
        delivered_before: datetime,
        now: datetime,
        limit: int,
    ) -> int:
        """Redact only successfully delivered payloads after retention.

        Pending, leased, and retry rows are structurally excluded so cleanup
        cannot destroy data required for eventual delivery.
        """

        if not isinstance(delivered_before, datetime):
            raise ValueError("delivered_before must be a datetime")
        if not isinstance(now, datetime):
            raise ValueError("now must be a datetime")
        _require_exact_positive_int(limit, "limit")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH redactable AS (
                        SELECT delivery_id
                        FROM skyai_discord_mirror.deliveries
                        WHERE
                            state = 'delivered'
                            AND delivered_at < %s
                            AND payload_redacted_at IS NULL
                        ORDER BY delivered_at, delivery_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE skyai_discord_mirror.deliveries AS delivery
                    SET
                        content = NULL,
                        chunks = NULL,
                        payload_redacted_at = %s,
                        updated_at = %s
                    FROM redactable
                    WHERE delivery.delivery_id = redactable.delivery_id
                    """,
                    (delivered_before, limit, now, now),
                )
                return cursor.rowcount

    def mark_retry(
        self,
        delivery_id: str,
        *,
        lease_token: str,
        available_at: datetime,
        last_error: str,
        now: datetime,
    ) -> None:
        if type(last_error) is not str:
            raise ValueError("last_error must be a string")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE skyai_discord_mirror.deliveries
                    SET
                        state = 'retry',
                        available_at = %s,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error = %s,
                        updated_at = %s
                    WHERE
                        delivery_id = %s
                        AND state = 'leased'
                        AND lease_token = %s
                    """,
                    (
                        available_at,
                        last_error,
                        now,
                        delivery_id,
                        lease_token,
                    ),
                )
                self._expect_single_updated_row(cursor, "mark_retry")

    def snapshot(self, delivery_id: str) -> DeliverySnapshot:
        _require_exact_nonempty_string(delivery_id, "delivery_id")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        state,
                        attempt_count,
                        next_chunk_index,
                        thread_id,
                        message_ids,
                        available_at,
                        lease_expires_at,
                        last_error
                    FROM skyai_discord_mirror.deliveries
                    WHERE delivery_id = %s
                    """,
                    (delivery_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise KeyError(delivery_id)
        message_ids = row[4]
        if type(message_ids) is not list:
            raise RuntimeError("stored message_ids must be a JSON array")
        return DeliverySnapshot(
            delivery_id=delivery_id,
            state=row[0],
            attempt_count=row[1],
            next_chunk_index=row[2],
            thread_id=row[3],
            message_ids=tuple(message_ids),
            available_at=row[5],
            lease_expires_at=row[6],
            last_error=row[7],
        )

    def backlog(self) -> DeliveryBacklog:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE state = 'pending'),
                        COUNT(*) FILTER (WHERE state = 'leased'),
                        COUNT(*) FILTER (WHERE state = 'retry'),
                        COUNT(*) FILTER (WHERE state = 'delivered'),
                        MIN(created_at) FILTER (
                            WHERE state IN ('pending', 'leased', 'retry')
                        ),
                        COALESCE(
                            MAX(attempt_count) FILTER (
                                WHERE state IN ('pending', 'leased', 'retry')
                            ),
                            0
                        ),
                        (
                            SELECT latest.last_error
                            FROM skyai_discord_mirror.deliveries AS latest
                            WHERE
                                latest.state IN ('pending', 'leased', 'retry')
                                AND latest.last_error IS NOT NULL
                            ORDER BY latest.updated_at DESC, latest.delivery_id
                            LIMIT 1
                        )
                    FROM skyai_discord_mirror.deliveries
                    """
                )
                row = cursor.fetchone()
        if row is None:  # pragma: no cover - aggregate always returns one row
            raise RuntimeError("Discord delivery backlog query returned no row")
        return DeliveryBacklog(
            pending_count=row[0],
            leased_count=row[1],
            retry_count=row[2],
            delivered_count=row[3],
            oldest_undelivered_at=row[4],
            max_undelivered_attempt_count=row[5],
            latest_error_type=row[6],
        )


class DiscordDeliveryWorker:
    def __init__(
        self,
        repository: DiscordDeliveryRepository,
        transport: DiscordTransport,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        batch_size: int = 10,
        base_backoff_seconds: int = 2,
        max_backoff_seconds: int = 300,
        payload_retention_seconds: int = 604800,
        cleanup_batch_size: int = 100,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.transport = transport
        self.worker_id = _require_exact_nonempty_string(worker_id, "worker_id")
        self.lease_seconds = _require_exact_positive_int(
            lease_seconds,
            "lease_seconds",
        )
        self.batch_size = _require_exact_positive_int(batch_size, "batch_size")
        if self.batch_size > 100:
            raise ValueError("batch_size must not exceed 100")
        self.base_backoff_seconds = _require_exact_positive_int(
            base_backoff_seconds,
            "base_backoff_seconds",
        )
        self.max_backoff_seconds = _require_exact_positive_int(
            max_backoff_seconds,
            "max_backoff_seconds",
        )
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be >= base_backoff_seconds"
            )
        self.payload_retention_seconds = _require_exact_positive_int(
            payload_retention_seconds,
            "payload_retention_seconds",
        )
        self.cleanup_batch_size = _require_exact_positive_int(
            cleanup_batch_size,
            "cleanup_batch_size",
        )
        if self.cleanup_batch_size > 1000:
            raise ValueError("cleanup_batch_size must not exceed 1000")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.clock = clock
        self.last_cycle_succeeded_at: datetime | None = None
        self.last_cycle_error_type: str | None = None
        self.last_backlog: DeliveryBacklog | None = None

    def enqueue(
        self,
        *,
        key: MirrorKey,
        content: str,
        chunks: tuple[str, ...],
        delivery_id: str | None = None,
    ) -> str:
        if delivery_id is None:
            delivery_id = str(uuid.uuid4())
        now = self.clock()
        envelope = MirrorEnvelope(
            delivery_id=delivery_id,
            key=key,
            content=content,
            chunks=chunks,
            created_at=now,
        )
        self.repository.enqueue(envelope)
        return delivery_id

    def _lease_token(self) -> str:
        return f"{self.worker_id}:{uuid.uuid4()}"

    def _renew_lease(self, lease: DeliveryLease) -> None:
        self.repository.renew_lease(
            lease.envelope.delivery_id,
            lease_token=lease.lease_token,
            now=self.clock(),
            lease_seconds=self.lease_seconds,
        )

    @contextmanager
    def _lease_heartbeat(
        self,
        lease: DeliveryLease,
    ):
        """Keep one owned lease alive during bounded blocking Discord I/O."""

        stop = threading.Event()
        errors: list[Exception] = []
        interval_seconds = max(0.25, self.lease_seconds / 3)

        def heartbeat() -> None:
            while not stop.wait(interval_seconds):
                try:
                    self._renew_lease(lease)
                except Exception as exc:
                    errors.append(exc)
                    stop.set()
                    return

        thread = threading.Thread(
            target=heartbeat,
            name=f"skyai-discord-lease-{lease.envelope.delivery_id}",
            daemon=True,
        )
        thread.start()

        def check() -> None:
            if errors:
                raise RuntimeError(
                    "Discord delivery lease heartbeat failed"
                ) from errors[0]

        try:
            yield check
            check()
        finally:
            stop.set()
            thread.join(timeout=interval_seconds + 1)

    def _find_exact_message_id(
        self,
        channel_id: str,
        nonce: str,
    ) -> str | None:
        self_matches = list(
            self.transport.find_message_ids_by_exact_nonce(
                channel_id,
                nonce,
            )
        )
        for index, match in enumerate(self_matches):
            _require_exact_nonempty_string(
                match,
                f"message nonce matches[{index}]",
            )
        unique_matches = tuple(dict.fromkeys(self_matches))
        if len(unique_matches) > 1:
            raise RuntimeError(
                "Discord exact nonce history reconciliation is ambiguous"
            )
        return unique_matches[0] if unique_matches else None

    def _resolve_thread(self, lease: DeliveryLease) -> str:
        key = lease.envelope.key
        recovery_name = deterministic_thread_name(key)

        def resolver(exact_name: str) -> str:
            self._renew_lease(lease)
            matches = list(
                self.transport.find_threads_by_exact_name(
                    key.configured_channel_id,
                    exact_name,
                )
            )
            for index, match in enumerate(matches):
                _require_exact_nonempty_string(
                    match,
                    f"thread matches[{index}]",
                )
            unique_matches = tuple(dict.fromkeys(matches))
            if len(unique_matches) > 1:
                raise RuntimeError(
                    "Discord exact thread recovery is ambiguous"
                )
            if len(unique_matches) == 1:
                return unique_matches[0]

            starter_label = (
                "🎙️ Voice SkyAI разговор"
                if key.surface == "voice"
                else "SkyAI v2 разговор"
            )
            starter_content = (
                f"{starter_label} · conversation_hash="
                f"`{key.conversation_hash}`"
            )
            starter_nonce = deterministic_nonce(
                "skyai-thread-starter",
                key.surface,
                key.configured_channel_id,
                key.conversation_id,
            )
            self._renew_lease(lease)
            starter_message_id = self._find_exact_message_id(
                key.configured_channel_id,
                starter_nonce,
            )
            if starter_message_id is None:
                self._renew_lease(lease)
                starter_message_id = self.transport.post_message(
                    key.configured_channel_id,
                    starter_content,
                    starter_nonce,
                )
            _require_exact_nonempty_string(
                starter_message_id,
                "starter_message_id",
            )
            self._renew_lease(lease)
            return _require_exact_nonempty_string(
                self.transport.start_thread_from_message(
                    key.configured_channel_id,
                    starter_message_id,
                    exact_name,
                ),
                "discord_thread_id",
            )

        return self.repository.resolve_thread(
            key,
            recovery_name,
            resolver,
            now=self.clock(),
        )

    def _backoff_seconds(self, attempt_count: int) -> int:
        _require_exact_positive_int(attempt_count, "attempt_count")
        value = self.base_backoff_seconds
        for _ in range(attempt_count - 1):
            if value >= self.max_backoff_seconds:
                return self.max_backoff_seconds
            value = min(value * 2, self.max_backoff_seconds)
        return value

    def _deliver_lease(self, lease: DeliveryLease) -> DeliverySnapshot:
        try:
            self._renew_lease(lease)
            with self._lease_heartbeat(lease) as check_heartbeat:
                thread_id = self._resolve_thread(lease)
                check_heartbeat()
                for chunk_index in range(
                    lease.next_chunk_index,
                    len(lease.envelope.chunks),
                ):
                    chunk = lease.envelope.chunks[chunk_index]
                    nonce = deterministic_nonce(
                        "skyai-mirror-chunk",
                        lease.envelope.delivery_id,
                        str(chunk_index),
                    )
                    self._renew_lease(lease)
                    message_id = self._find_exact_message_id(
                        thread_id,
                        nonce,
                    )
                    check_heartbeat()
                    if message_id is None:
                        self._renew_lease(lease)
                        message_id = _require_exact_nonempty_string(
                            self.transport.post_message(
                                thread_id,
                                chunk,
                                nonce,
                            ),
                            "message_id",
                        )
                    check_heartbeat()
                    self._renew_lease(lease)
                    self.repository.record_chunk(
                        lease.envelope.delivery_id,
                        lease_token=lease.lease_token,
                        chunk_index=chunk_index,
                        message_id=message_id,
                        thread_id=thread_id,
                        now=self.clock(),
                    )
                check_heartbeat()
                self._renew_lease(lease)
            self._renew_lease(lease)
            self.repository.mark_delivered(
                lease.envelope.delivery_id,
                lease_token=lease.lease_token,
                thread_id=thread_id,
                now=self.clock(),
            )
        except Exception as exc:
            now = self.clock()
            available_at = now + timedelta(
                seconds=self._backoff_seconds(lease.attempt_count)
            )
            self.repository.mark_retry(
                lease.envelope.delivery_id,
                lease_token=lease.lease_token,
                available_at=available_at,
                last_error=type(exc).__name__,
                now=now,
            )
        return self.repository.snapshot(lease.envelope.delivery_id)

    async def attempt(self, delivery_id: str) -> DeliverySnapshot:
        now = self.clock()
        lease = await asyncio.to_thread(
            self.repository.claim_one,
            delivery_id,
            lease_token=self._lease_token(),
            now=now,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return await asyncio.to_thread(self.repository.snapshot, delivery_id)
        return await asyncio.to_thread(self._deliver_lease, lease)

    async def run_once(self) -> list[DeliverySnapshot]:
        snapshots: list[DeliverySnapshot] = []
        for _ in range(self.batch_size):
            leases = await asyncio.to_thread(
                self.repository.claim_due,
                lease_token=self._lease_token(),
                now=self.clock(),
                lease_seconds=self.lease_seconds,
                limit=1,
            )
            if not leases:
                break
            if len(leases) != 1:
                raise RuntimeError("claim_due(limit=1) returned multiple leases")
            snapshots.append(
                await asyncio.to_thread(self._deliver_lease, leases[0])
            )
        cleanup_now = self.clock()
        await asyncio.to_thread(
            self.repository.redact_delivered_payloads,
            delivered_before=(
                cleanup_now
                - timedelta(seconds=self.payload_retention_seconds)
            ),
            now=cleanup_now,
            limit=self.cleanup_batch_size,
        )
        self.last_backlog = await asyncio.to_thread(
            self.repository.backlog,
        )
        return snapshots

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_seconds: float,
    ) -> None:
        if type(poll_seconds) is not float or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a positive float")
        while not stop_event.is_set():
            try:
                await self.run_once()
                self.last_cycle_succeeded_at = self.clock()
                self.last_cycle_error_type = None
            except Exception as exc:
                # The durable queue is the source of truth. A database/network
                # outage must leave the worker alive so the next bounded poll
                # can resume without losing the queued exact payload.
                self.last_cycle_error_type = type(exc).__name__
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass
