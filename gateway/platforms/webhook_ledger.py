"""Durable authority and settlement ledger for inbound webhook operations.

The webhook HTTP adapter has two facts that must survive an adapter replacement
or a process restart:

* the provider delivery identity that already owns an execution; and
* the exact grant and delivery target selected at admission time.

This module owns those facts.  It deliberately contains no HTTP, adapter, or
agent orchestration.  Callers must durably prepare an operation before they
dispatch it and must cross the target-attempt gate before invoking an external
delivery primitive.

The ledger is a guest of the stable Hermes-root ``state.db`` shared across
profile and multiplex-mode adapters. Like the durable async delegation
registry, guest connections preserve the journal mode selected by ``SessionDB``
and apply only the per-connection durability barriers. Every write uses
``BEGIN IMMEDIATE`` and every connection is closed explicitly.

There is no in-memory fallback.  If SQLite cannot establish or commit the
authority record, the caller must fail closed before dispatching an effect.

``max_storage_bytes`` is a conservative logical allocation bound for this
ledger's rows and indexes. The ledger shares ``state.db`` with other Hermes
state, so it is not a physical cap on that database file or its journal/WAL.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Optional

from hermes_constants import get_hermes_home


DEFAULT_MAX_RECORDS = 4096
MAXIMUM_MAX_RECORDS = 1_000_000
DEFAULT_MAX_STORAGE_BYTES = 1024 * 1024 * 1024
MAXIMUM_MAX_STORAGE_BYTES = 64 * 1024 * 1024 * 1024
DEFAULT_TERMINAL_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_LOCAL_BYPASS_REPLAY_RETENTION_SECONDS = 60 * 60
_SCHEMA_NAME = "webhook_operation_ledger"
_SCHEMA_VERSION = 5
_MAX_ERROR_CHARS = 1024
_MAX_ERROR_UTF8_BYTES = _MAX_ERROR_CHARS * 4
_MAX_EXTERNAL_ID_UTF8_BYTES = 512 * 4
_MAX_LEDGER_TEXT_BYTES = 1024
_MAX_EVENT_JSON_BYTES = 2 * 1024 * 1024
_MAX_AUTHORITY_JSON_BYTES = 64 * 1024
_MAX_SCOPE_CAPACITY_RESERVE = 64
_MAX_PRUNE_BATCH = 128
# Recovery is intentionally much smaller than the configurable durable-record
# ceiling.  One authority can contain multiple MiB of canonical JSON, so even
# a seemingly modest unbounded recovery query can exhaust the gateway process.
DEFAULT_RECOVERY_BATCH_SIZE = 8
MAXIMUM_RECOVERY_BATCH_SIZE = 16
MAXIMUM_RECOVERY_PROFILES = 256
# Admission reserves the worst-case durable payload before any effect can run:
# one event snapshot, one staged delivery, two authority snapshots, bounded
# scalar/index storage, and SQLite page overhead. Tombstones hold only compact
# replay proof fields and their indexes. Fixed reservations let admission check
# total ledger growth in O(1) without scanning permanent history.
_OPERATION_STORAGE_RESERVATION_BYTES = 5 * 1024 * 1024
_TOMBSTONE_STORAGE_RESERVATION_BYTES = 16 * 1024
_AUTH_BINDING_STORAGE_RESERVATION_BYTES = 16 * 1024
# Authentication-key ownership is permanent evidence, but it must not compete
# with operation carriers: otherwise a full operation budget can strand a key
# rotation and leave the superseded verifier live.  These fixed logical caps
# bound that evidence independently, while the scope cap prevents one route's
# rotation history from consuming the shared binding reserve.
_AUTH_BINDING_GLOBAL_LIMIT_BYTES = 16 * 1024 * 1024
_AUTH_BINDING_SCOPE_LIMIT_BYTES = 1 * 1024 * 1024
_MAX_AUTH_BINDINGS_PER_AUTHORITY_CHECK = 8
# A valid v4 operation can carry multiple MiB of JSON. Keep migration
# validation bounded independently of the configured record/storage ceiling.
_V4_MIGRATION_VALIDATION_BATCH_SIZE = 8
MINIMUM_MAX_STORAGE_BYTES = _OPERATION_STORAGE_RESERVATION_BYTES
_BOUNDED_REPLAY_PREFIXES = ("local_bypass_body_sha256:",)


def _scope_storage_limit(max_storage_bytes: int) -> int:
    """Reserve global room that no single webhook authority scope can consume."""

    if max_storage_bytes <= _OPERATION_STORAGE_RESERVATION_BYTES:
        return _OPERATION_STORAGE_RESERVATION_BYTES
    reserve = max(
        _OPERATION_STORAGE_RESERVATION_BYTES,
        max_storage_bytes // 4,
    )
    return max(
        _OPERATION_STORAGE_RESERVATION_BYTES,
        max_storage_bytes - reserve,
    )


class WebhookLedgerError(RuntimeError):
    """The durable webhook authority store could not answer safely."""


class WebhookLedgerTransitionError(WebhookLedgerError):
    """A caller attempted a state transition it does not own."""


class WebhookLedgerConfigurationError(WebhookLedgerError):
    """Configured limits conflict with deterministic persisted authority."""


class WebhookLedgerCapacityError(WebhookLedgerError):
    """A durable authority quota is exhausted and cannot recover by retry."""


class WebhookLedgerCorruptionError(WebhookLedgerError):
    """Persisted webhook authority data violates the ledger contract."""


class OperationState(str, Enum):
    PREPARING = "preparing"
    READY = "ready"
    RUNNING = "running"
    DELIVERY_READY = "delivery_ready"
    DELIVERING = "delivering"
    SETTLED = "settled"
    INDETERMINATE = "indeterminate"


class TargetState(str, Enum):
    PENDING = "pending"
    ATTEMPTING = "attempting"
    CONFIRMED = "confirmed"
    SUPPRESSED = "suppressed"
    INDETERMINATE = "indeterminate"


class AdmitDisposition(str, Enum):
    ACCEPTED = "accepted"
    ACTIVE = "active"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    INDETERMINATE = "indeterminate"
    SATURATED = "saturated"


class AdmitSaturationReason(str, Enum):
    GLOBAL_RECORD_LIMIT = "global_record_limit"
    SCOPE_RECORD_LIMIT = "scope_record_limit"
    GLOBAL_STORAGE_LIMIT = "global_storage_limit"
    SCOPE_STORAGE_LIMIT = "scope_storage_limit"


class TargetAttemptDisposition(str, Enum):
    STARTED = "started"
    IN_PROGRESS = "in_progress"
    CACHED = "cached"
    INDETERMINATE = "indeterminate"


class SettlementKind(str, Enum):
    CONFIRMED = "confirmed"
    SUPPRESSED = "suppressed"
    PRE_EFFECT_FAILED = "pre_effect_failed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class Settlement:
    """Typed knowledge produced by one exact target attempt."""

    kind: SettlementKind
    external_id: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class OperationAuthority:
    """Immutable durable carrier returned to the execution layer."""

    operation_id: str
    generation: int
    session_key: str
    profile: str
    route: str
    provider: str
    replay_id: str
    body_sha256: str
    event_type: str
    state: OperationState
    owner_instance: str
    target_id: Optional[str]
    target_state: Optional[TargetState]
    event_snapshot: Optional[Mapping[str, Any]]
    target_snapshot: Optional[Mapping[str, Any]]
    grant_snapshot: Optional[Mapping[str, Any]]
    delivery: Optional["StagedDelivery"]


@dataclass(frozen=True)
class StagedDelivery:
    """Exact rendered outbound effect persisted before its mutation gate."""

    content: str
    carrier: Mapping[str, Any]
    content_sha256: str
    delivery_sha256: str


@dataclass(frozen=True)
class AdmitResult:
    disposition: AdmitDisposition
    authority: Optional[OperationAuthority] = None
    tombstone: Optional["DeliveryTombstone"] = None
    saturation: Optional[AdmitSaturationReason] = None


@dataclass(frozen=True)
class DeliveryTombstone:
    """Compact replay verdict for one terminal provider identity."""

    profile: str
    route: str
    provider: str
    replay_id: str
    body_sha256: str
    operation_id: str
    state: OperationState
    settled_at: float
    expires_at: Optional[float]


@dataclass(frozen=True)
class TargetAttempt:
    disposition: TargetAttemptDisposition
    operation_id: str
    generation: int
    target_id: str
    content_sha256: str
    delivery_sha256: str
    delivery: Optional[StagedDelivery] = None
    attempt_token: Optional[str] = None
    owner_instance: Optional[str] = None


@dataclass(frozen=True, order=True)
class RecoveryCursor:
    """Stable keyset cursor for one bounded recovery scan."""

    created_at: float
    operation_id: str


@dataclass(frozen=True)
class RecoveryBatch:
    """One strictly bounded durable-recovery page.

    ``scanned_count`` includes live-owner rows skipped while looking for dead
    work.  Callers must therefore use ``has_more``/``next_cursor`` rather than
    infer exhaustion from the number of returned authorities.  The cursor is
    a keyset continuation over immutable ``(created_at, operation_id)``.
    """

    event_ready: tuple[OperationAuthority, ...] = ()
    delivery_ready: tuple[OperationAuthority, ...] = ()
    released: tuple[str, ...] = ()
    indeterminate: tuple[str, ...] = ()
    scanned_count: int = 0
    has_more: bool = False
    next_cursor: Optional[RecoveryCursor] = None

    @property
    def ready(self) -> tuple[OperationAuthority, ...]:
        """Compatibility alias for pre-agent work only."""

        return self.event_ready


def _normalize_recovery_batch_limit(limit: Any) -> int:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > MAXIMUM_RECOVERY_BATCH_SIZE
    ):
        raise ValueError(
            f"recovery batch limit must be between 1 and {MAXIMUM_RECOVERY_BATCH_SIZE}"
        )
    return limit


def _normalize_recovery_cursor(
    cursor: Optional[RecoveryCursor],
) -> Optional[RecoveryCursor]:
    if cursor is None:
        return None
    if not isinstance(cursor, RecoveryCursor):
        raise ValueError("recovery cursor must be a RecoveryCursor")
    try:
        created_at = float(cursor.created_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("recovery cursor timestamp must be finite") from exc
    if not math.isfinite(created_at):
        raise ValueError("recovery cursor timestamp must be finite")
    try:
        operation_id = _normalize_nonempty(
            cursor.operation_id,
            label="recovery cursor operation_id",
        )
    except WebhookLedgerError as exc:
        raise ValueError(str(exc)) from exc
    if created_at != cursor.created_at or operation_id != cursor.operation_id:
        raise ValueError("recovery cursor must be canonical")
    return RecoveryCursor(created_at=created_at, operation_id=operation_id)


def _normalize_recovery_profiles(
    profiles: Optional[Iterable[str]],
) -> Optional[tuple[str, ...]]:
    if profiles is None:
        return None
    if isinstance(profiles, (str, bytes, Mapping)) or not isinstance(
        profiles, Iterable
    ):
        raise ValueError("recovery profiles must be an iterable of profile names")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_profile in profiles:
        if len(normalized) >= MAXIMUM_RECOVERY_PROFILES:
            raise ValueError(
                "recovery profiles exceed the supported profile-count limit"
            )
        if not isinstance(raw_profile, str):
            raise ValueError("recovery profile must be a canonical string")
        try:
            profile = _normalize_nonempty(
                raw_profile,
                label="recovery profile",
            )
        except WebhookLedgerError as exc:
            raise ValueError(str(exc)) from exc
        if profile != raw_profile:
            raise ValueError("recovery profile must be canonical")
        normalized.append(profile)
        seen.add(profile)
    return tuple(sorted(seen))


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """Return whether an owner stamp still names the same live process."""

    if not pid:
        return False
    try:
        normalized_pid = int(pid)
    except (TypeError, ValueError):
        return False
    if normalized_pid == os.getpid():
        # The current interpreter is definitive evidence that its PID is live,
        # even in constrained PID namespaces where psutil cannot enumerate it.
        current_started_at = _owner_stamp()[1]
        if started_at is None or current_started_at is None:
            return True
        try:
            return int(current_started_at) == int(started_at)
        except (TypeError, ValueError):
            return True
    try:
        from gateway.status import _pid_exists, get_process_start_time

        if not _pid_exists(normalized_pid):
            return False
        current_start = get_process_start_time(normalized_pid)
    except Exception:
        # A liveness probe that cannot establish death must not make work
        # stealable.  On POSIX, retain the conservative EPERM-means-alive
        # behavior; never use os.kill(pid, 0) on Windows.
        if os.name == "nt":
            return True
        try:
            os.kill(normalized_pid, 0)  # windows-footgun: ok - POSIX-only branch
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True
    if started_at is None or current_start is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def _safe_error(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    return text[:_MAX_ERROR_CHARS]


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _plain_json(value: Any) -> Any:
    """Detach supported JSON containers, including immutable projections."""

    if isinstance(value, Mapping):
        plain: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            plain[key] = _plain_json(item)
        return plain
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_json(value: Mapping[str, Any], *, label: str, max_bytes: int) -> str:
    if not isinstance(value, Mapping):
        raise WebhookLedgerError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            _plain_json(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise WebhookLedgerError(f"{label} is not canonical JSON") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise WebhookLedgerError(f"{label} exceeds its durable size limit")
    return encoded


def _decode_json(
    value: Optional[str], *, label: str, max_bytes: int
) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > max_bytes:
        raise WebhookLedgerCorruptionError(
            f"stored {label} exceeds its durable size limit"
        )

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON number {token!r}")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        decoded_object: dict[str, Any] = {}
        for key, item in pairs:
            if key in decoded_object:
                raise ValueError(f"duplicate JSON key {key!r}")
            decoded_object[key] = item
        return decoded_object

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise WebhookLedgerCorruptionError(f"stored {label} is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise WebhookLedgerCorruptionError(f"stored {label} is not an object")
    try:
        canonical = json.dumps(
            decoded,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise WebhookLedgerCorruptionError(
            f"stored {label} is not canonical JSON"
        ) from exc
    if canonical != value:
        raise WebhookLedgerCorruptionError(
            f"stored {label} is not the canonical authority snapshot"
        )
    return _freeze_json(decoded)


def _decode_staged_delivery(
    value: Optional[str],
    content_digest: Optional[str],
    delivery_digest: Optional[str],
) -> Optional[StagedDelivery]:
    decoded = _decode_json(
        value,
        label="staged delivery",
        max_bytes=_MAX_EVENT_JSON_BYTES,
    )
    if decoded is None:
        if content_digest is not None or delivery_digest is not None:
            raise WebhookLedgerCorruptionError(
                "stored delivery digest has no staged delivery"
            )
        return None
    if set(decoded) != {"carrier", "content"}:
        raise WebhookLedgerCorruptionError(
            "stored staged delivery has an invalid shape"
        )
    content = decoded["content"]
    carrier = decoded["carrier"]
    if not isinstance(content, str) or not isinstance(carrier, Mapping):
        raise WebhookLedgerCorruptionError(
            "stored staged delivery has invalid field types"
        )
    try:
        normalized_digest = _normalize_sha256(
            content_digest, label="stored content_sha256"
        )
    except WebhookLedgerError as exc:
        raise WebhookLedgerCorruptionError(
            "stored staged delivery digest is invalid"
        ) from exc
    actual_digest = content_sha256(content)
    if normalized_digest != actual_digest:
        raise WebhookLedgerCorruptionError(
            "stored staged delivery content does not match its digest"
        )
    try:
        normalized_delivery_digest = _normalize_sha256(
            delivery_digest, label="stored delivery_sha256"
        )
    except WebhookLedgerError as exc:
        raise WebhookLedgerCorruptionError(
            "stored staged delivery authority digest is invalid"
        ) from exc
    actual_delivery_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    if normalized_delivery_digest != actual_delivery_digest:
        raise WebhookLedgerCorruptionError(
            "stored staged delivery carrier does not match its digest"
        )
    return StagedDelivery(
        content=content,
        carrier=carrier,
        content_sha256=normalized_digest,
        delivery_sha256=normalized_delivery_digest,
    )


def _normalize_nonempty(value: Any, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise WebhookLedgerError(f"{label} must be non-empty")
    if len(normalized.encode("utf-8")) > _MAX_LEDGER_TEXT_BYTES:
        raise WebhookLedgerError(
            f"{label} exceeds {_MAX_LEDGER_TEXT_BYTES} UTF-8 bytes"
        )
    return normalized


def _normalize_sha256(value: Any, *, label: str) -> str:
    normalized = _normalize_nonempty(value, label=label).lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise WebhookLedgerError(f"{label} must be a SHA-256 hex digest")
    return normalized


def _normalize_authentication_binding(
    binding: object,
) -> tuple[str, str, str, str, str, str]:
    if isinstance(binding, (str, bytes)) or not isinstance(binding, Iterable):
        raise WebhookLedgerError("authentication key binding must contain six fields")
    values = tuple(binding)
    if len(values) != 6:
        raise WebhookLedgerError("authentication key binding must contain six fields")
    (
        fingerprint,
        profile,
        route,
        provider,
        signature_mode,
        policy_sha256,
    ) = values
    return (
        _normalize_sha256(
            fingerprint,
            label="authentication key fingerprint",
        ),
        _normalize_nonempty(profile, label="authentication key profile"),
        _normalize_nonempty(route, label="authentication key route"),
        _normalize_nonempty(provider, label="authentication key provider"),
        _normalize_nonempty(
            signature_mode,
            label="authentication key signature mode",
        ),
        _normalize_sha256(
            policy_sha256,
            label="authentication policy fingerprint",
        ),
    )


class WebhookOperationLedger:
    """SQLite-backed webhook admission, authority, and target mutation gate."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_storage_bytes: int = DEFAULT_MAX_STORAGE_BYTES,
        terminal_retention_seconds: int = DEFAULT_TERMINAL_RETENTION_SECONDS,
        local_bypass_replay_retention_seconds: int = (
            DEFAULT_LOCAL_BYPASS_REPLAY_RETENTION_SECONDS
        ),
        instance_id: Optional[str] = None,
        _adopt_persisted_operation_limits: bool = False,
    ) -> None:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records < 1
            or max_records > MAXIMUM_MAX_RECORDS
        ):
            raise ValueError("max_records is outside the supported record range")
        if (
            not isinstance(max_storage_bytes, int)
            or isinstance(max_storage_bytes, bool)
            or max_storage_bytes < MINIMUM_MAX_STORAGE_BYTES
            or max_storage_bytes > MAXIMUM_MAX_STORAGE_BYTES
        ):
            raise ValueError("max_storage_bytes is outside the supported storage range")
        if (
            isinstance(terminal_retention_seconds, bool)
            or int(terminal_retention_seconds) < 1
        ):
            raise ValueError("terminal_retention_seconds must be positive")
        if (
            isinstance(local_bypass_replay_retention_seconds, bool)
            or int(local_bypass_replay_retention_seconds) < 1
        ):
            raise ValueError("local_bypass_replay_retention_seconds must be positive")
        self.db_path = (
            Path(db_path) if db_path is not None else get_hermes_home() / "state.db"
        )
        self.max_records = int(max_records)
        self.max_storage_bytes = int(max_storage_bytes)
        self.terminal_retention_seconds = int(terminal_retention_seconds)
        self.local_bypass_replay_retention_seconds = int(
            local_bypass_replay_retention_seconds
        )
        self._adopt_persisted_operation_limits = bool(_adopt_persisted_operation_limits)
        self.instance_id = _normalize_nonempty(
            instance_id or uuid.uuid4().hex,
            label="instance_id",
        )
        self._lock = threading.RLock()
        self._initialize_schema()

    @classmethod
    def for_authentication_bindings(
        cls,
        db_path: Optional[Path] = None,
    ) -> "WebhookOperationLedger":
        """Open root key authority without renegotiating operation quotas.

        Named profiles share authentication-key ownership at the Hermes root,
        while their own operation ledgers may use different record/storage
        limits.  An existing root ledger therefore remains authoritative for
        its operation limits; this handle adopts and validates those values.
        """

        return cls(
            db_path,
            _adopt_persisted_operation_limits=True,
        )

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10,
            isolation_level=None,
        )
        try:
            from hermes_state import apply_durability_barriers

            apply_durability_barriers(conn)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _storage_failure(exc: BaseException) -> WebhookLedgerError:
        """Classify SQLite/filesystem failures at the durable-store boundary."""

        error_name = getattr(exc, "sqlite_errorname", None)
        error_code = getattr(exc, "sqlite_errorcode", None)
        if error_name:
            detail = str(error_name)
        elif error_code is not None:
            detail = f"SQLite error {error_code}"
        else:
            detail = type(exc).__name__
        return WebhookLedgerError(f"durable webhook storage failed ({detail})")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            yield conn
        except WebhookLedgerError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise self._storage_failure(exc) from exc
        finally:
            if conn is not None:
                conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            from hermes_cli.sqlite_util import write_txn

            with write_txn(conn):
                yield conn

    @staticmethod
    def _prepare_v4_migration(conn: sqlite3.Connection) -> bool:
        """Validate and park one canonical v4 ledger inside this transaction."""

        table_names = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        webhook_tables = {
            "webhook_operations",
            "webhook_targets",
            "webhook_delivery_tombstones",
            "webhook_ledger_meta",
            "webhook_ledger_usage",
            "webhook_ledger_scope_usage",
            "webhook_auth_key_bindings",
        }
        if "webhook_ledger_meta" not in table_names:
            if table_names & webhook_tables:
                raise WebhookLedgerCorruptionError(
                    "webhook ledger metadata is unavailable"
                )
            return False

        meta_info = conn.execute("PRAGMA table_info(webhook_ledger_meta)").fetchall()
        if tuple(row["name"] for row in meta_info) != (
            "schema_name",
            "schema_version",
        ) or [int(row["pk"]) for row in meta_info] != [1, 0]:
            raise WebhookLedgerCorruptionError(
                "webhook ledger metadata schema is incompatible"
            )
        metadata = conn.execute(
            "SELECT schema_name, schema_version FROM webhook_ledger_meta"
        ).fetchall()
        if len(metadata) != 1 or metadata[0]["schema_name"] != _SCHEMA_NAME:
            raise WebhookLedgerCorruptionError("webhook ledger metadata is unavailable")
        try:
            version = int(metadata[0]["schema_version"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebhookLedgerCorruptionError(
                "webhook operation ledger schema version is invalid"
            ) from exc
        if version == _SCHEMA_VERSION:
            required_v5_tables = webhook_tables
            if not required_v5_tables.issubset(table_names):
                raise WebhookLedgerCorruptionError(
                    "webhook v5 ledger structure is incomplete"
                )
            return False
        if version != 4:
            raise WebhookLedgerCorruptionError(
                "webhook operation ledger schema version is unsupported"
            )

        required_v4_tables = {
            "webhook_operations",
            "webhook_targets",
            "webhook_delivery_tombstones",
            "webhook_ledger_meta",
        }
        forbidden_v5_tables = {
            "webhook_ledger_usage",
            "webhook_ledger_scope_usage",
            "webhook_auth_key_bindings",
        }
        if (
            not required_v4_tables.issubset(table_names)
            or table_names & forbidden_v5_tables
            or table_names
            & {
                "webhook_operations_v4",
                "webhook_targets_v4",
                "webhook_delivery_tombstones_v4",
            }
        ):
            raise WebhookLedgerCorruptionError(
                "webhook v4 ledger structure is incompatible"
            )

        def normalized_sql(sql: object) -> str:
            return "".join(str(sql).lower().split())

        expected_table_sql = {
            "webhook_operations": """
                CREATE TABLE webhook_operations (
                    operation_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    route TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    replay_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'preparing','ready','running','delivery_ready',
                            'delivering','settled','indeterminate'
                        )
                    ),
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    owner_pid INTEGER,
                    owner_started_at INTEGER,
                    owner_instance TEXT NOT NULL,
                    event_json TEXT,
                    target_json TEXT,
                    grant_json TEXT,
                    script_started INTEGER NOT NULL DEFAULT 0 CHECK (
                        script_started IN (0,1)
                    ),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    settled_at REAL,
                    last_error TEXT
                )
            """,
            "webhook_targets": """
                CREATE TABLE webhook_targets (
                    operation_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'pending','attempting','confirmed','suppressed',
                            'indeterminate'
                        )
                    ),
                    attempt_token TEXT,
                    content_sha256 TEXT,
                    delivery_json TEXT,
                    delivery_sha256 TEXT,
                    external_id TEXT,
                    owner_pid INTEGER,
                    owner_started_at INTEGER,
                    owner_instance TEXT,
                    started_at REAL,
                    settled_at REAL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    PRIMARY KEY(operation_id, target_id),
                    FOREIGN KEY(operation_id)
                        REFERENCES webhook_operations(operation_id)
                        ON DELETE CASCADE
                )
            """,
            "webhook_delivery_tombstones": """
                CREATE TABLE webhook_delivery_tombstones (
                    profile TEXT NOT NULL,
                    route TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    replay_id TEXT NOT NULL,
                    body_sha256 TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('settled','indeterminate')
                    ),
                    settled_at REAL NOT NULL,
                    expires_at REAL,
                    PRIMARY KEY(profile, route, provider, replay_id)
                )
            """,
            "webhook_ledger_meta": """
                CREATE TABLE webhook_ledger_meta (
                    schema_name TEXT PRIMARY KEY CHECK (
                        schema_name='webhook_operation_ledger'
                    ),
                    schema_version INTEGER NOT NULL CHECK (schema_version=4)
                )
            """,
        }
        actual_table_sql = {
            str(row["name"]): normalized_sql(row["sql"])
            for row in conn.execute(
                """SELECT name, sql FROM sqlite_master
                    WHERE type='table' AND name IN (?, ?, ?, ?)""",
                tuple(expected_table_sql),
            )
        }
        if actual_table_sql != {
            name: normalized_sql(sql) for name, sql in expected_table_sql.items()
        }:
            raise WebhookLedgerCorruptionError(
                "webhook v4 ledger table definitions are incompatible"
            )

        expected_index_sql = {
            "idx_webhook_operations_replay_identity": """
                CREATE UNIQUE INDEX idx_webhook_operations_replay_identity
                    ON webhook_operations(profile, route, provider, replay_id)
            """,
            "idx_webhook_operations_state_updated": """
                CREATE INDEX idx_webhook_operations_state_updated
                    ON webhook_operations(state, updated_at)
            """,
            "idx_webhook_tombstones_expires_at": """
                CREATE INDEX idx_webhook_tombstones_expires_at
                    ON webhook_delivery_tombstones(expires_at)
                    WHERE expires_at IS NOT NULL
            """,
        }
        actual_v4_objects = {
            (str(row["type"]), str(row["name"])): (
                str(row["tbl_name"]),
                normalized_sql(row["sql"]),
            )
            for row in conn.execute(
                """SELECT type, name, tbl_name, sql FROM sqlite_master
                    WHERE type IN ('index','trigger')
                      AND tbl_name IN (?, ?, ?, ?)
                      AND sql IS NOT NULL""",
                tuple(expected_table_sql),
            )
        }
        expected_v4_objects = {
            ("index", name): (
                "webhook_delivery_tombstones"
                if name == "idx_webhook_tombstones_expires_at"
                else "webhook_operations",
                normalized_sql(sql),
            )
            for name, sql in expected_index_sql.items()
        }
        if actual_v4_objects != expected_v4_objects:
            raise WebhookLedgerCorruptionError(
                "webhook v4 ledger indexes or triggers are incompatible"
            )

        expected_operation_columns = (
            "operation_id",
            "profile",
            "route",
            "provider",
            "replay_id",
            "body_sha256",
            "event_type",
            "session_key",
            "state",
            "generation",
            "owner_pid",
            "owner_started_at",
            "owner_instance",
            "event_json",
            "target_json",
            "grant_json",
            "script_started",
            "created_at",
            "updated_at",
            "settled_at",
            "last_error",
        )
        expected_target_columns = (
            "operation_id",
            "target_id",
            "state",
            "attempt_token",
            "content_sha256",
            "delivery_json",
            "delivery_sha256",
            "external_id",
            "owner_pid",
            "owner_started_at",
            "owner_instance",
            "started_at",
            "settled_at",
            "updated_at",
            "last_error",
        )
        expected_tombstone_columns = (
            "profile",
            "route",
            "provider",
            "replay_id",
            "body_sha256",
            "operation_id",
            "state",
            "settled_at",
            "expires_at",
        )
        operation_info = conn.execute(
            "PRAGMA table_info(webhook_operations)"
        ).fetchall()
        target_info = conn.execute("PRAGMA table_info(webhook_targets)").fetchall()
        tombstone_info = conn.execute(
            "PRAGMA table_info(webhook_delivery_tombstones)"
        ).fetchall()
        if (
            tuple(row["name"] for row in operation_info) != expected_operation_columns
            or [int(row["pk"]) for row in operation_info]
            != [1] + [0] * (len(operation_info) - 1)
            or tuple(row["name"] for row in target_info) != expected_target_columns
            or [int(row["pk"]) for row in target_info]
            != [1, 2] + [0] * (len(target_info) - 2)
            or tuple(row["name"] for row in tombstone_info)
            != expected_tombstone_columns
            or [int(row["pk"]) for row in tombstone_info] != [1, 2, 3, 4] + [0] * 5
        ):
            raise WebhookLedgerCorruptionError(
                "webhook v4 ledger structure is incompatible"
            )

        operation_indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(webhook_operations)")
        }
        replay_index = operation_indexes.get("idx_webhook_operations_replay_identity")
        replay_columns = (
            tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA index_info(idx_webhook_operations_replay_identity)"
                )
            )
            if replay_index is not None
            else ()
        )
        session_unique = any(
            bool(index["unique"])
            and not bool(index["partial"])
            and tuple(
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM pragma_index_info(?)",
                    (index["name"],),
                )
            )
            == ("session_key",)
            for index in operation_indexes.values()
        )
        if (
            replay_index is None
            or not bool(replay_index["unique"])
            or bool(replay_index["partial"])
            or replay_columns != ("profile", "route", "provider", "replay_id")
            or not session_unique
        ):
            raise WebhookLedgerCorruptionError(
                "webhook v4 uniqueness authority is unavailable"
            )

        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(webhook_targets)"
        ).fetchall()
        duplicate_target = conn.execute(
            """SELECT operation_id FROM webhook_targets
                GROUP BY operation_id HAVING COUNT(*) > 1 LIMIT 1"""
        ).fetchone()
        overlapping_identity = conn.execute(
            """SELECT 1
                 FROM webhook_operations AS operation
                 JOIN webhook_delivery_tombstones AS tombstone
                   ON tombstone.profile=operation.profile
                  AND tombstone.route=operation.route
                  AND tombstone.provider=operation.provider
                  AND tombstone.replay_id=operation.replay_id
                LIMIT 1"""
        ).fetchone()
        if (
            not any(
                row["table"] == "webhook_operations"
                and row["from"] == "operation_id"
                and row["to"] == "operation_id"
                and str(row["on_delete"]).upper() == "CASCADE"
                for row in foreign_keys
            )
            or duplicate_target is not None
            or overlapping_identity is not None
            or conn.execute("PRAGMA foreign_key_check(webhook_targets)").fetchone()
            is not None
        ):
            raise WebhookLedgerCorruptionError(
                "webhook v4 ledger contents are incompatible"
            )

        ambiguous_default_evidence = conn.execute(
            """SELECT 1 FROM webhook_operations WHERE profile='default'
               UNION ALL
               SELECT 1 FROM webhook_delivery_tombstones
                WHERE profile='default'
               LIMIT 1"""
        ).fetchone()
        if ambiguous_default_evidence is not None:
            raise WebhookLedgerConfigurationError(
                "webhook v4 profile='default' evidence cannot be migrated: "
                "its physical authority profile is ambiguous; preserve the "
                "database and remove or explicitly re-home the unpublished "
                "v4 evidence before retrying"
            )

        for index_name in (
            "idx_webhook_operations_replay_identity",
            "idx_webhook_operations_state_updated",
            "idx_webhook_tombstones_expires_at",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")
        conn.execute("ALTER TABLE webhook_operations RENAME TO webhook_operations_v4")
        conn.execute("ALTER TABLE webhook_targets RENAME TO webhook_targets_v4")
        conn.execute(
            """ALTER TABLE webhook_delivery_tombstones
               RENAME TO webhook_delivery_tombstones_v4"""
        )
        conn.execute("DROP TABLE webhook_ledger_meta")
        return True

    def _restore_v4_rows(self, conn: sqlite3.Connection) -> None:
        """Copy parked v4 authority through v5 constraints and counters."""

        operation_columns = (
            "operation_id, profile, route, provider, replay_id, body_sha256, "
            "event_type, session_key, state, generation, owner_pid, "
            "owner_started_at, owner_instance, event_json, target_json, "
            "grant_json, script_started, created_at, updated_at, settled_at, "
            "last_error"
        )
        target_columns = (
            "operation_id, target_id, state, attempt_token, content_sha256, "
            "delivery_json, delivery_sha256, external_id, owner_pid, "
            "owner_started_at, owner_instance, started_at, settled_at, "
            "updated_at, last_error"
        )
        tombstone_columns = (
            "profile, route, provider, replay_id, body_sha256, operation_id, "
            "state, settled_at, expires_at"
        )
        conn.execute(
            f"""INSERT INTO webhook_operations ({operation_columns})
                SELECT {operation_columns} FROM webhook_operations_v4"""
        )
        conn.execute(
            f"""INSERT INTO webhook_targets ({target_columns})
                SELECT {target_columns} FROM webhook_targets_v4"""
        )
        conn.execute(
            f"""INSERT INTO webhook_delivery_tombstones ({tombstone_columns})
                SELECT {tombstone_columns}
                  FROM webhook_delivery_tombstones_v4"""
        )
        if conn.execute("PRAGMA foreign_key_check(webhook_targets)").fetchone():
            raise WebhookLedgerCorruptionError(
                "webhook v4 migration produced invalid target ownership"
            )

        def require_exact_text(row: sqlite3.Row, key: str, label: str) -> None:
            try:
                normalized = _normalize_nonempty(row[key], label=label)
            except WebhookLedgerError as exc:
                raise WebhookLedgerCorruptionError(
                    f"stored {label} is invalid"
                ) from exc
            if row[key] != normalized:
                raise WebhookLedgerCorruptionError(f"stored {label} is not canonical")

        def require_sha256(row: sqlite3.Row, key: str, label: str) -> None:
            try:
                normalized = _normalize_sha256(row[key], label=label)
            except WebhookLedgerError as exc:
                raise WebhookLedgerCorruptionError(
                    f"stored {label} is invalid"
                ) from exc
            if row[key] != normalized:
                raise WebhookLedgerCorruptionError(f"stored {label} is not canonical")

        def require_finite_time(
            row: sqlite3.Row,
            key: str,
            label: str,
            *,
            optional: bool = False,
        ) -> None:
            value = row[key]
            if optional and value is None:
                return
            try:
                finite = math.isfinite(float(value))
            except (TypeError, ValueError, OverflowError):
                finite = False
            if not finite:
                raise WebhookLedgerCorruptionError(f"stored {label} is invalid")

        operation_cursor = conn.execute(
            "SELECT * FROM webhook_operations ORDER BY operation_id"
        )
        while rows := operation_cursor.fetchmany(_V4_MIGRATION_VALIDATION_BATCH_SIZE):
            for row in rows:
                for key in (
                    "operation_id",
                    "profile",
                    "route",
                    "provider",
                    "replay_id",
                    "event_type",
                    "session_key",
                    "owner_instance",
                ):
                    require_exact_text(row, key, f"operation {key}")
                require_sha256(row, "body_sha256", "operation body_sha256")
                if (
                    not isinstance(row["generation"], int)
                    or isinstance(row["generation"], bool)
                    or int(row["generation"]) < 1
                    or row["script_started"] not in (0, 1)
                ):
                    raise WebhookLedgerCorruptionError(
                        "stored operation generation or script gate is invalid"
                    )
                # This validates operation/target states, canonical authority JSON,
                # and staged-delivery content/digests as one joined authority.
                authority = self._authority_from_row(conn, row)
                snapshot_presence = tuple(
                    row[key] is not None
                    for key in ("event_json", "target_json", "grant_json")
                )
                if any(snapshot_presence) and not all(snapshot_presence):
                    raise WebhookLedgerCorruptionError(
                        "stored operation authority snapshot is incomplete"
                    )
                if (authority.target_id is not None) != bool(row["target_json"]):
                    raise WebhookLedgerCorruptionError(
                        "stored operation target ownership is incomplete"
                    )
                if row["target_json"] is not None:
                    expected_target_id = hashlib.sha256(
                        str(row["target_json"]).encode("utf-8")
                    ).hexdigest()[:32]
                    if authority.target_id != expected_target_id:
                        raise WebhookLedgerCorruptionError(
                            "stored operation target identity is invalid"
                        )
                require_finite_time(row, "created_at", "operation created_at")
                require_finite_time(row, "updated_at", "operation updated_at")
                require_finite_time(
                    row,
                    "settled_at",
                    "operation settled_at",
                    optional=True,
                )

        for row in conn.execute("SELECT * FROM webhook_targets"):
            require_exact_text(row, "operation_id", "target operation_id")
            require_exact_text(row, "target_id", "target target_id")
            for key in ("attempt_token", "owner_instance"):
                if row[key] is not None:
                    require_exact_text(row, key, f"target {key}")
            for key in ("started_at", "settled_at"):
                require_finite_time(
                    row,
                    key,
                    f"target {key}",
                    optional=True,
                )
            require_finite_time(row, "updated_at", "target updated_at")

        for row in conn.execute("SELECT * FROM webhook_delivery_tombstones"):
            self._tombstone_from_row(row)

        conn.execute("DROP TABLE webhook_targets_v4")
        conn.execute("DROP TABLE webhook_delivery_tombstones_v4")
        conn.execute("DROP TABLE webhook_operations_v4")

    def _initialize_schema(self) -> None:
        with self._lock, self._transaction() as conn:
            migrate_v4 = self._prepare_v4_migration(conn)
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS webhook_operations (
                    operation_id TEXT PRIMARY KEY CHECK (
                        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    profile TEXT NOT NULL CHECK (
                        length(CAST(profile AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    route TEXT NOT NULL CHECK (
                        length(CAST(route AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    provider TEXT NOT NULL CHECK (
                        length(CAST(provider AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    replay_id TEXT NOT NULL CHECK (
                        length(CAST(replay_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    body_sha256 TEXT NOT NULL CHECK (
                        length(CAST(body_sha256 AS BLOB))=64
                    ),
                    event_type TEXT NOT NULL CHECK (
                        length(CAST(event_type AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    session_key TEXT NOT NULL UNIQUE CHECK (
                        length(CAST(session_key AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'preparing','ready','running','delivery_ready',
                            'delivering','settled','indeterminate'
                        )
                    ),
                    generation INTEGER NOT NULL CHECK (generation >= 1),
                    owner_pid INTEGER,
                    owner_started_at INTEGER,
                    owner_instance TEXT NOT NULL CHECK (
                        length(CAST(owner_instance AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    event_json TEXT CHECK (
                        event_json IS NULL OR
                        length(CAST(event_json AS BLOB)) <= {_MAX_EVENT_JSON_BYTES}
                    ),
                    target_json TEXT CHECK (
                        target_json IS NULL OR
                        length(CAST(target_json AS BLOB)) <= {_MAX_AUTHORITY_JSON_BYTES}
                    ),
                    grant_json TEXT CHECK (
                        grant_json IS NULL OR
                        length(CAST(grant_json AS BLOB)) <= {_MAX_AUTHORITY_JSON_BYTES}
                    ),
                    script_started INTEGER NOT NULL DEFAULT 0 CHECK (
                        script_started IN (0,1)
                    ),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    settled_at REAL,
                    last_error TEXT CHECK (
                        last_error IS NULL OR
                        length(CAST(last_error AS BLOB)) <= {_MAX_ERROR_UTF8_BYTES}
                    )
                )"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_webhook_operations_replay_identity
                    ON webhook_operations(profile, route, provider, replay_id)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_webhook_operations_state_updated
                    ON webhook_operations(state, updated_at)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_scope_state_updated
                    ON webhook_operations(
                        profile, route, provider, state, updated_at
                    )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_recovery_order
                    ON webhook_operations(created_at, operation_id)
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_owner_recovery_order
                    ON webhook_operations(
                        owner_instance, created_at, operation_id
                    )
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_profile_recovery_order
                    ON webhook_operations(
                        profile, created_at, operation_id
                    )
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_owner_delivery_ready
                    ON webhook_operations(
                        owner_instance, created_at, operation_id
                    )
                    WHERE state='delivery_ready'"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_owner_profile_delivery_ready
                    ON webhook_operations(
                        owner_instance, profile, created_at, operation_id
                    )
                    WHERE state='delivery_ready'"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_owner_current_recovery
                    ON webhook_operations(
                        owner_instance, created_at, operation_id
                    )
                    WHERE state='delivery_ready'
                       OR (state='ready' AND generation>=2)"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_owner_profile_current_recovery
                    ON webhook_operations(
                        owner_instance, profile, created_at, operation_id
                    )
                    WHERE state='delivery_ready'
                       OR (state='ready' AND generation>=2)"""
            )
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS webhook_targets (
                    operation_id TEXT NOT NULL CHECK (
                        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    target_id TEXT NOT NULL CHECK (
                        length(CAST(target_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','attempting','confirmed','suppressed','indeterminate')
                    ),
                    attempt_token TEXT CHECK (
                        attempt_token IS NULL OR
                        length(CAST(attempt_token AS BLOB)) <= 1024
                    ),
                    content_sha256 TEXT CHECK (
                        content_sha256 IS NULL OR
                        length(CAST(content_sha256 AS BLOB))=64
                    ),
                    delivery_json TEXT CHECK (
                        delivery_json IS NULL OR
                        length(CAST(delivery_json AS BLOB)) <= {_MAX_EVENT_JSON_BYTES}
                    ),
                    delivery_sha256 TEXT CHECK (
                        delivery_sha256 IS NULL OR
                        length(CAST(delivery_sha256 AS BLOB))=64
                    ),
                    external_id TEXT CHECK (
                        external_id IS NULL OR
                        length(CAST(external_id AS BLOB)) <= {_MAX_EXTERNAL_ID_UTF8_BYTES}
                    ),
                    owner_pid INTEGER,
                    owner_started_at INTEGER,
                    owner_instance TEXT CHECK (
                        owner_instance IS NULL OR
                        length(CAST(owner_instance AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    started_at REAL,
                    settled_at REAL,
                    updated_at REAL NOT NULL,
                    last_error TEXT CHECK (
                        last_error IS NULL OR
                        length(CAST(last_error AS BLOB)) <= {_MAX_ERROR_UTF8_BYTES}
                    ),
                    PRIMARY KEY(operation_id),
                    FOREIGN KEY(operation_id) REFERENCES webhook_operations(operation_id)
                        ON DELETE CASCADE
                )"""
            )
            bounded_replay_prefix = _BOUNDED_REPLAY_PREFIXES[0]
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS webhook_delivery_tombstones (
                    profile TEXT NOT NULL CHECK (
                        length(CAST(profile AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    route TEXT NOT NULL CHECK (
                        length(CAST(route AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    provider TEXT NOT NULL CHECK (
                        length(CAST(provider AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    replay_id TEXT NOT NULL CHECK (
                        length(CAST(replay_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    body_sha256 TEXT NOT NULL CHECK (
                        length(CAST(body_sha256 AS BLOB))=64
                    ),
                    operation_id TEXT NOT NULL CHECK (
                        length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    state TEXT NOT NULL CHECK (
                        state IN ('settled','indeterminate')
                    ),
                    settled_at REAL NOT NULL,
                    expires_at REAL CHECK (
                        expires_at IS NULL OR (
                            state='settled' AND
                            substr(replay_id, 1, {len(bounded_replay_prefix)})=
                                '{bounded_replay_prefix}'
                        )
                    ),
                    PRIMARY KEY(profile, route, provider, replay_id)
                )"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_webhook_tombstones_expires_at
                    ON webhook_delivery_tombstones(expires_at)
                    WHERE expires_at IS NOT NULL"""
            )
            conn.execute(
                f"""CREATE INDEX IF NOT EXISTS
                    idx_webhook_operations_bounded_settled_expiry
                    ON webhook_operations(COALESCE(settled_at, updated_at))
                    WHERE state='settled'
                      AND substr(replay_id, 1, {len(bounded_replay_prefix)})=
                          '{bounded_replay_prefix}'"""
            )
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS webhook_ledger_usage (
                    schema_name TEXT PRIMARY KEY CHECK (
                        schema_name='webhook_operation_ledger'
                    ),
                    reserved_bytes INTEGER NOT NULL CHECK (reserved_bytes >= 0),
                    proof_count INTEGER NOT NULL CHECK (proof_count >= 0),
                    active_record_count INTEGER NOT NULL CHECK (
                        active_record_count >= 0
                    ),
                    settled_operation_count INTEGER NOT NULL CHECK (
                        settled_operation_count >= 0
                    ),
                    indeterminate_operation_count INTEGER NOT NULL CHECK (
                        indeterminate_operation_count >= 0
                    ),
                    auth_binding_reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (
                        auth_binding_reserved_bytes >= 0
                    ),
                    auth_binding_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        auth_binding_count >= 0
                    ),
                    max_records INTEGER NOT NULL CHECK (
                        max_records BETWEEN 1 AND {MAXIMUM_MAX_RECORDS}
                    ),
                    max_storage_bytes INTEGER NOT NULL CHECK (
                        max_storage_bytes >= {_OPERATION_STORAGE_RESERVATION_BYTES}
                        AND max_storage_bytes <= {MAXIMUM_MAX_STORAGE_BYTES}
                    ),
                    scope_limit_bytes INTEGER NOT NULL CHECK (
                        scope_limit_bytes >= {_OPERATION_STORAGE_RESERVATION_BYTES}
                        AND scope_limit_bytes <= max_storage_bytes
                    ),
                    operation_limits_provisional INTEGER NOT NULL CHECK (
                        operation_limits_provisional IN (0,1)
                    ),
                    auth_binding_limit_bytes INTEGER NOT NULL CHECK (
                        auth_binding_limit_bytes=
                            {_AUTH_BINDING_GLOBAL_LIMIT_BYTES}
                    ),
                    auth_binding_scope_limit_bytes INTEGER NOT NULL CHECK (
                        auth_binding_scope_limit_bytes=
                            {_AUTH_BINDING_SCOPE_LIMIT_BYTES}
                        AND auth_binding_scope_limit_bytes <=
                            auth_binding_limit_bytes
                    ),
                    CHECK (
                        active_record_count + settled_operation_count
                            + indeterminate_operation_count <= proof_count
                    ),
                    CHECK (
                        auth_binding_reserved_bytes=
                            auth_binding_count*
                                {_AUTH_BINDING_STORAGE_RESERVATION_BYTES}
                        AND auth_binding_reserved_bytes <=
                            auth_binding_limit_bytes
                    )
                )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO webhook_ledger_usage (
                       schema_name, reserved_bytes, proof_count,
                       active_record_count, settled_operation_count,
                       indeterminate_operation_count,
                       auth_binding_reserved_bytes, auth_binding_count,
                       max_records, max_storage_bytes, scope_limit_bytes,
                       operation_limits_provisional,
                       auth_binding_limit_bytes,
                       auth_binding_scope_limit_bytes
                   ) VALUES (?, 0, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?)""",
                (
                    _SCHEMA_NAME,
                    self.max_records,
                    self.max_storage_bytes,
                    _scope_storage_limit(self.max_storage_bytes),
                    int(self._adopt_persisted_operation_limits and not migrate_v4),
                    _AUTH_BINDING_GLOBAL_LIMIT_BYTES,
                    _AUTH_BINDING_SCOPE_LIMIT_BYTES,
                ),
            )
            current_usage = conn.execute(
                """SELECT reserved_bytes, max_records, max_storage_bytes,
                          proof_count, active_record_count,
                          settled_operation_count,
                          indeterminate_operation_count, scope_limit_bytes,
                          operation_limits_provisional,
                          auth_binding_limit_bytes,
                          auth_binding_scope_limit_bytes
                     FROM webhook_ledger_usage
                   WHERE schema_name=?""",
                (_SCHEMA_NAME,),
            ).fetchone()
            if current_usage is None:
                raise WebhookLedgerCorruptionError(
                    "webhook ledger usage authority is unavailable"
                )
            if not self._adopt_persisted_operation_limits and bool(
                current_usage["operation_limits_provisional"]
            ):
                has_operation_evidence = (
                    int(current_usage["reserved_bytes"]) != 0
                    or int(current_usage["proof_count"]) != 0
                    or int(current_usage["active_record_count"]) != 0
                    or int(current_usage["settled_operation_count"]) != 0
                    or int(current_usage["indeterminate_operation_count"]) != 0
                )
                requested_limits_match = (
                    int(current_usage["max_records"]) == self.max_records
                    and int(current_usage["max_storage_bytes"])
                    == self.max_storage_bytes
                    and int(current_usage["scope_limit_bytes"])
                    == _scope_storage_limit(self.max_storage_bytes)
                )
                if has_operation_evidence and not requested_limits_match:
                    raise WebhookLedgerConfigurationError(
                        "provisional webhook operation limits cannot change "
                        "after durable operation evidence exists"
                    )
                if not has_operation_evidence:
                    conn.execute(
                        """UPDATE webhook_ledger_usage
                              SET max_records=?, max_storage_bytes=?,
                                  scope_limit_bytes=?,
                                  operation_limits_provisional=0
                            WHERE schema_name=?
                              AND operation_limits_provisional=1
                              AND reserved_bytes=0 AND proof_count=0
                              AND active_record_count=0
                              AND settled_operation_count=0
                              AND indeterminate_operation_count=0""",
                        (
                            self.max_records,
                            self.max_storage_bytes,
                            _scope_storage_limit(self.max_storage_bytes),
                            _SCHEMA_NAME,
                        ),
                    )
                else:
                    conn.execute(
                        """UPDATE webhook_ledger_usage
                              SET operation_limits_provisional=0
                            WHERE schema_name=?
                              AND operation_limits_provisional=1""",
                        (_SCHEMA_NAME,),
                    )
                current_usage = conn.execute(
                    """SELECT reserved_bytes, max_records, max_storage_bytes,
                              proof_count, active_record_count,
                              settled_operation_count,
                              indeterminate_operation_count, scope_limit_bytes,
                              operation_limits_provisional,
                              auth_binding_limit_bytes,
                              auth_binding_scope_limit_bytes
                         FROM webhook_ledger_usage
                       WHERE schema_name=?""",
                    (_SCHEMA_NAME,),
                ).fetchone()
                if current_usage is None:  # pragma: no cover - singleton update
                    raise WebhookLedgerCorruptionError(
                        "webhook ledger usage authority is unavailable"
                    )
            if self._adopt_persisted_operation_limits:
                # The singleton row already existed (or was initialized above
                # with canonical defaults).  Never rewrite it from a named
                # profile's unrelated operation settings.
                self.max_records = int(current_usage["max_records"])
                self.max_storage_bytes = int(current_usage["max_storage_bytes"])
            if int(current_usage["reserved_bytes"]) > int(
                current_usage["max_storage_bytes"]
            ):
                raise WebhookLedgerConfigurationError(
                    "persisted webhook storage limit is below reserved evidence"
                )
            if int(current_usage["reserved_bytes"]) > self.max_storage_bytes:
                raise WebhookLedgerConfigurationError(
                    "configured webhook storage limit is below reserved evidence"
                )
            if not self._adopt_persisted_operation_limits and (
                int(current_usage["max_records"]) != self.max_records
                or int(current_usage["max_storage_bytes"]) != self.max_storage_bytes
                or int(current_usage["scope_limit_bytes"])
                != _scope_storage_limit(self.max_storage_bytes)
                or int(current_usage["auth_binding_limit_bytes"])
                != _AUTH_BINDING_GLOBAL_LIMIT_BYTES
                or int(current_usage["auth_binding_scope_limit_bytes"])
                != _AUTH_BINDING_SCOPE_LIMIT_BYTES
            ):
                raise WebhookLedgerConfigurationError(
                    "configured webhook ledger limits do not match persisted authority"
                )

            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS webhook_ledger_scope_usage (
                    profile TEXT NOT NULL CHECK (
                        length(CAST(profile AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    route TEXT NOT NULL CHECK (
                        length(CAST(route AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    provider TEXT NOT NULL CHECK (
                        length(CAST(provider AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    reserved_bytes INTEGER NOT NULL CHECK (reserved_bytes >= 0),
                    proof_count INTEGER NOT NULL CHECK (proof_count >= 0),
                    active_record_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        active_record_count >= 0 AND
                        active_record_count <= proof_count
                    ),
                    settled_operation_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        settled_operation_count >= 0 AND
                        active_record_count + settled_operation_count <=
                            proof_count
                    ),
                    auth_binding_reserved_bytes INTEGER NOT NULL DEFAULT 0 CHECK (
                        auth_binding_reserved_bytes >= 0
                    ),
                    auth_binding_count INTEGER NOT NULL DEFAULT 0 CHECK (
                        auth_binding_count >= 0 AND
                        auth_binding_reserved_bytes=
                            auth_binding_count*
                                {_AUTH_BINDING_STORAGE_RESERVATION_BYTES} AND
                        auth_binding_reserved_bytes <=
                            {_AUTH_BINDING_SCOPE_LIMIT_BYTES}
                    ),
                    PRIMARY KEY(profile, route, provider)
                ) WITHOUT ROWID"""
            )
            # Recreate the counter triggers on every open so a shadowed
            # trigger cannot silently disable the O(1) storage authority.
            for trigger_name in (
                "trg_webhook_operations_budget_insert",
                "trg_webhook_operations_usage_insert",
                "trg_webhook_operations_usage_delete",
                "trg_webhook_operations_usage_state",
                "trg_webhook_tombstones_budget_insert",
                "trg_webhook_tombstones_usage_insert",
                "trg_webhook_tombstones_usage_delete",
                "trg_webhook_auth_bindings_budget_insert",
                "trg_webhook_auth_bindings_usage_insert",
                "trg_webhook_auth_bindings_immutable_update",
                "trg_webhook_auth_bindings_immutable_delete",
            ):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_operations_budget_insert
                    BEFORE INSERT ON webhook_operations
                    WHEN NOT EXISTS (
                             SELECT 1 FROM webhook_ledger_usage
                              WHERE schema_name='{_SCHEMA_NAME}'
                                AND reserved_bytes <= max_storage_bytes-
                                        {_OPERATION_STORAGE_RESERVATION_BYTES}
                         )
                         OR COALESCE((
                                SELECT reserved_bytes
                                  FROM webhook_ledger_scope_usage
                                 WHERE profile=NEW.profile
                                   AND route=NEW.route
                                   AND provider=NEW.provider
                            ), 0) > (
                                SELECT scope_limit_bytes-
                                       {_OPERATION_STORAGE_RESERVATION_BYTES}
                                  FROM webhook_ledger_usage
                                 WHERE schema_name='{_SCHEMA_NAME}'
                            )
                    BEGIN
                        SELECT RAISE(ABORT, 'webhook_ledger_full');
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_operations_usage_insert
                    AFTER INSERT ON webhook_operations
                    BEGIN
                        UPDATE webhook_ledger_usage
                           SET reserved_bytes=reserved_bytes+
                                   {_OPERATION_STORAGE_RESERVATION_BYTES},
                               proof_count=proof_count+1,
                               active_record_count=active_record_count+
                                   CASE WHEN NEW.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END,
                               settled_operation_count=
                                   settled_operation_count+
                                   CASE WHEN NEW.state='settled'
                                        THEN 1 ELSE 0 END,
                               indeterminate_operation_count=
                                   indeterminate_operation_count+
                                   CASE WHEN NEW.state='indeterminate'
                                        THEN 1 ELSE 0 END
                         WHERE schema_name='{_SCHEMA_NAME}';
                        INSERT INTO webhook_ledger_scope_usage (
                            profile, route, provider, reserved_bytes, proof_count,
                            active_record_count, settled_operation_count
                        ) VALUES (
                            NEW.profile, NEW.route, NEW.provider,
                            {_OPERATION_STORAGE_RESERVATION_BYTES}, 1,
                            CASE WHEN NEW.state NOT IN (
                                'settled','indeterminate'
                            ) THEN 1 ELSE 0 END,
                            CASE WHEN NEW.state='settled' THEN 1 ELSE 0 END
                        ) ON CONFLICT(profile, route, provider) DO UPDATE SET
                            reserved_bytes=reserved_bytes+
                                {_OPERATION_STORAGE_RESERVATION_BYTES},
                            proof_count=proof_count+1,
                            active_record_count=active_record_count+
                                CASE WHEN NEW.state NOT IN (
                                    'settled','indeterminate'
                                ) THEN 1 ELSE 0 END,
                            settled_operation_count=settled_operation_count+
                                CASE WHEN NEW.state='settled' THEN 1 ELSE 0 END;
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_operations_usage_delete
                    AFTER DELETE ON webhook_operations
                    BEGIN
                        UPDATE webhook_ledger_usage
                           SET reserved_bytes=reserved_bytes-
                                   {_OPERATION_STORAGE_RESERVATION_BYTES},
                               proof_count=proof_count-1,
                               active_record_count=active_record_count-
                                   CASE WHEN OLD.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END,
                               settled_operation_count=
                                   settled_operation_count-
                                   CASE WHEN OLD.state='settled'
                                        THEN 1 ELSE 0 END,
                               indeterminate_operation_count=
                                   indeterminate_operation_count-
                                   CASE WHEN OLD.state='indeterminate'
                                        THEN 1 ELSE 0 END
                         WHERE schema_name='{_SCHEMA_NAME}';
                        UPDATE webhook_ledger_scope_usage
                           SET reserved_bytes=reserved_bytes-
                                   {_OPERATION_STORAGE_RESERVATION_BYTES},
                               proof_count=proof_count-1,
                               active_record_count=active_record_count-
                                   CASE WHEN OLD.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END,
                               settled_operation_count=settled_operation_count-
                                   CASE WHEN OLD.state='settled'
                                        THEN 1 ELSE 0 END
                         WHERE profile=OLD.profile AND route=OLD.route
                           AND provider=OLD.provider;
                        DELETE FROM webhook_ledger_scope_usage
                         WHERE profile=OLD.profile AND route=OLD.route
                           AND provider=OLD.provider AND proof_count=0
                           AND auth_binding_count=0;
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_operations_usage_state
                    AFTER UPDATE OF state ON webhook_operations
                    WHEN OLD.state != NEW.state
                    BEGIN
                        UPDATE webhook_ledger_usage
                           SET active_record_count=active_record_count
                                   - CASE WHEN OLD.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END
                                   + CASE WHEN NEW.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END,
                               settled_operation_count=
                                   settled_operation_count
                                   - CASE WHEN OLD.state='settled'
                                        THEN 1 ELSE 0 END
                                   + CASE WHEN NEW.state='settled'
                                        THEN 1 ELSE 0 END,
                               indeterminate_operation_count=
                                   indeterminate_operation_count
                                   - CASE WHEN OLD.state='indeterminate'
                                        THEN 1 ELSE 0 END
                                   + CASE WHEN NEW.state='indeterminate'
                                        THEN 1 ELSE 0 END
                         WHERE schema_name='{_SCHEMA_NAME}';
                        UPDATE webhook_ledger_scope_usage
                           SET active_record_count=active_record_count
                                   - CASE WHEN OLD.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END
                                   + CASE WHEN NEW.state NOT IN (
                                       'settled','indeterminate'
                                   ) THEN 1 ELSE 0 END
                               , settled_operation_count=
                                   settled_operation_count
                                   - CASE WHEN OLD.state='settled'
                                        THEN 1 ELSE 0 END
                                   + CASE WHEN NEW.state='settled'
                                        THEN 1 ELSE 0 END
                         WHERE profile=OLD.profile AND route=OLD.route
                           AND provider=OLD.provider;
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_tombstones_budget_insert
                    BEFORE INSERT ON webhook_delivery_tombstones
                    WHEN NOT EXISTS (
                             SELECT 1 FROM webhook_ledger_usage
                              WHERE schema_name='{_SCHEMA_NAME}'
                                AND reserved_bytes <= max_storage_bytes-
                                        {_TOMBSTONE_STORAGE_RESERVATION_BYTES}
                         )
                         OR COALESCE((
                                SELECT reserved_bytes
                                  FROM webhook_ledger_scope_usage
                                 WHERE profile=NEW.profile
                                   AND route=NEW.route
                                   AND provider=NEW.provider
                            ), 0) > (
                                SELECT scope_limit_bytes-
                                       {_TOMBSTONE_STORAGE_RESERVATION_BYTES}
                                  FROM webhook_ledger_usage
                                 WHERE schema_name='{_SCHEMA_NAME}'
                            )
                    BEGIN
                        SELECT RAISE(ABORT, 'webhook_ledger_full');
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_tombstones_usage_insert
                    AFTER INSERT ON webhook_delivery_tombstones
                    BEGIN
                        UPDATE webhook_ledger_usage
                           SET reserved_bytes=reserved_bytes+
                                   {_TOMBSTONE_STORAGE_RESERVATION_BYTES},
                               proof_count=proof_count+1
                         WHERE schema_name='{_SCHEMA_NAME}';
                        INSERT INTO webhook_ledger_scope_usage (
                            profile, route, provider, reserved_bytes, proof_count
                        ) VALUES (
                            NEW.profile, NEW.route, NEW.provider,
                            {_TOMBSTONE_STORAGE_RESERVATION_BYTES}, 1
                        ) ON CONFLICT(profile, route, provider) DO UPDATE SET
                            reserved_bytes=reserved_bytes+
                                {_TOMBSTONE_STORAGE_RESERVATION_BYTES},
                            proof_count=proof_count+1;
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_tombstones_usage_delete
                    AFTER DELETE ON webhook_delivery_tombstones
                    BEGIN
                        UPDATE webhook_ledger_usage
                           SET reserved_bytes=reserved_bytes-
                                   {_TOMBSTONE_STORAGE_RESERVATION_BYTES},
                               proof_count=proof_count-1
                         WHERE schema_name='{_SCHEMA_NAME}';
                        UPDATE webhook_ledger_scope_usage
                           SET reserved_bytes=reserved_bytes-
                                   {_TOMBSTONE_STORAGE_RESERVATION_BYTES},
                               proof_count=proof_count-1
                         WHERE profile=OLD.profile AND route=OLD.route
                           AND provider=OLD.provider;
                        DELETE FROM webhook_ledger_scope_usage
                         WHERE profile=OLD.profile AND route=OLD.route
                           AND provider=OLD.provider AND proof_count=0
                           AND auth_binding_count=0;
                    END"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS webhook_ledger_meta (
                    schema_name TEXT PRIMARY KEY CHECK (
                        schema_name='webhook_operation_ledger'
                    ),
                    schema_version INTEGER NOT NULL CHECK (schema_version=5)
                )"""
            )
            conn.execute(
                """INSERT OR IGNORE INTO webhook_ledger_meta (
                       schema_name, schema_version
                   ) VALUES (?, ?)""",
                (_SCHEMA_NAME, _SCHEMA_VERSION),
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS webhook_auth_key_bindings (
                    key_fingerprint TEXT PRIMARY KEY CHECK (
                        length(key_fingerprint)=64 AND
                        key_fingerprint NOT GLOB '*[^0-9a-f]*'
                    ),
                    profile TEXT NOT NULL CHECK (
                        length(CAST(profile AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    route TEXT NOT NULL CHECK (
                        length(CAST(route AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    provider TEXT NOT NULL CHECK (
                        length(CAST(provider AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    signature_mode TEXT NOT NULL CHECK (
                        length(CAST(signature_mode AS BLOB)) BETWEEN 1 AND 1024
                    ),
                    policy_sha256 TEXT NOT NULL CHECK (
                        length(policy_sha256)=64 AND
                        policy_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    bound_at REAL NOT NULL
                )"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_auth_bindings_budget_insert
                    BEFORE INSERT ON webhook_auth_key_bindings
                    WHEN NOT EXISTS (
                        SELECT 1 FROM webhook_ledger_usage
                         WHERE schema_name='{_SCHEMA_NAME}'
                           AND auth_binding_reserved_bytes <=
                               auth_binding_limit_bytes-
                               {_AUTH_BINDING_STORAGE_RESERVATION_BYTES}
                    )
                    OR COALESCE((
                           SELECT auth_binding_reserved_bytes
                             FROM webhook_ledger_scope_usage
                            WHERE profile=NEW.profile
                              AND route=NEW.route
                              AND provider=NEW.provider
                       ), 0) > (
                           SELECT auth_binding_scope_limit_bytes-
                                  {_AUTH_BINDING_STORAGE_RESERVATION_BYTES}
                             FROM webhook_ledger_usage
                            WHERE schema_name='{_SCHEMA_NAME}'
                       )
                    BEGIN
                        SELECT RAISE(ABORT, 'webhook_ledger_full');
                    END"""
            )
            conn.execute(
                f"""CREATE TRIGGER trg_webhook_auth_bindings_usage_insert
                    AFTER INSERT ON webhook_auth_key_bindings
                    BEGIN
                        UPDATE webhook_ledger_usage
                           SET auth_binding_reserved_bytes=
                                   auth_binding_reserved_bytes+
                                   {_AUTH_BINDING_STORAGE_RESERVATION_BYTES},
                               auth_binding_count=auth_binding_count+1
                         WHERE schema_name='{_SCHEMA_NAME}';
                        INSERT INTO webhook_ledger_scope_usage (
                            profile, route, provider, reserved_bytes, proof_count,
                            active_record_count, settled_operation_count,
                            auth_binding_reserved_bytes, auth_binding_count
                        ) VALUES (
                            NEW.profile, NEW.route, NEW.provider,
                            0, 0, 0, 0,
                            {_AUTH_BINDING_STORAGE_RESERVATION_BYTES}, 1
                        ) ON CONFLICT(profile, route, provider) DO UPDATE SET
                            auth_binding_reserved_bytes=
                                auth_binding_reserved_bytes+
                                {_AUTH_BINDING_STORAGE_RESERVATION_BYTES},
                            auth_binding_count=auth_binding_count+1;
                    END"""
            )
            conn.execute(
                """CREATE TRIGGER trg_webhook_auth_bindings_immutable_update
                    BEFORE UPDATE ON webhook_auth_key_bindings
                    BEGIN
                        SELECT RAISE(ABORT, 'webhook_auth_binding_immutable');
                    END"""
            )
            conn.execute(
                """CREATE TRIGGER trg_webhook_auth_bindings_immutable_delete
                    BEFORE DELETE ON webhook_auth_key_bindings
                    BEGIN
                        SELECT RAISE(ABORT, 'webhook_auth_binding_immutable');
                    END"""
            )
            if migrate_v4:
                try:
                    self._restore_v4_rows(conn)
                except sqlite3.IntegrityError as exc:
                    if "webhook_ledger_full" in str(exc):
                        raise WebhookLedgerCapacityError(
                            "webhook v4 migration exceeds the configured "
                            "storage capacity; increase "
                            "idempotency_max_storage_bytes"
                        ) from exc
                    raise WebhookLedgerCorruptionError(
                        "webhook v4 ledger contains invalid durable authority"
                    ) from exc
            self._validate_schema(conn)

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection) -> None:
        """Reject silently shadowed or incompatible authority tables/indexes."""

        expected_operation_columns = (
            "operation_id",
            "profile",
            "route",
            "provider",
            "replay_id",
            "body_sha256",
            "event_type",
            "session_key",
            "state",
            "generation",
            "owner_pid",
            "owner_started_at",
            "owner_instance",
            "event_json",
            "target_json",
            "grant_json",
            "script_started",
            "created_at",
            "updated_at",
            "settled_at",
            "last_error",
        )
        expected_target_columns = (
            "operation_id",
            "target_id",
            "state",
            "attempt_token",
            "content_sha256",
            "delivery_json",
            "delivery_sha256",
            "external_id",
            "owner_pid",
            "owner_started_at",
            "owner_instance",
            "started_at",
            "settled_at",
            "updated_at",
            "last_error",
        )
        operation_info = conn.execute(
            "PRAGMA table_info(webhook_operations)"
        ).fetchall()
        target_info = conn.execute("PRAGMA table_info(webhook_targets)").fetchall()
        tombstone_info = conn.execute(
            "PRAGMA table_info(webhook_delivery_tombstones)"
        ).fetchall()
        usage_info = conn.execute("PRAGMA table_info(webhook_ledger_usage)").fetchall()
        scope_usage_info = conn.execute(
            "PRAGMA table_info(webhook_ledger_scope_usage)"
        ).fetchall()
        auth_binding_info = conn.execute(
            "PRAGMA table_info(webhook_auth_key_bindings)"
        ).fetchall()
        if tuple(row["name"] for row in operation_info) != expected_operation_columns:
            raise WebhookLedgerCorruptionError(
                "webhook operation ledger schema is incompatible"
            )
        if tuple(row["name"] for row in target_info) != expected_target_columns:
            raise WebhookLedgerCorruptionError(
                "webhook target ledger schema is incompatible"
            )
        expected_tombstone_columns = (
            "profile",
            "route",
            "provider",
            "replay_id",
            "body_sha256",
            "operation_id",
            "state",
            "settled_at",
            "expires_at",
        )
        if tuple(row["name"] for row in tombstone_info) != expected_tombstone_columns:
            raise WebhookLedgerCorruptionError(
                "webhook delivery tombstone schema is incompatible"
            )
        expected_usage_columns = (
            "schema_name",
            "reserved_bytes",
            "proof_count",
            "active_record_count",
            "settled_operation_count",
            "indeterminate_operation_count",
            "auth_binding_reserved_bytes",
            "auth_binding_count",
            "max_records",
            "max_storage_bytes",
            "scope_limit_bytes",
            "operation_limits_provisional",
            "auth_binding_limit_bytes",
            "auth_binding_scope_limit_bytes",
        )
        if tuple(row["name"] for row in usage_info) != expected_usage_columns:
            raise WebhookLedgerCorruptionError(
                "webhook ledger usage schema is incompatible"
            )
        if [int(row["pk"]) for row in operation_info] != [1] + [0] * (
            len(operation_info) - 1
        ):
            raise WebhookLedgerCorruptionError(
                "webhook operation primary key is incompatible"
            )
        if [int(row["pk"]) for row in target_info] != [1] + [0] * (
            len(target_info) - 1
        ):
            raise WebhookLedgerCorruptionError(
                "webhook target primary key is incompatible"
            )
        if [int(row["pk"]) for row in tombstone_info] != [1, 2, 3, 4] + [0] * 5:
            raise WebhookLedgerCorruptionError(
                "webhook delivery tombstone primary key is incompatible"
            )
        if [int(row["pk"]) for row in usage_info] != [1] + [0] * 13:
            raise WebhookLedgerCorruptionError(
                "webhook ledger usage primary key is incompatible"
            )
        expected_scope_usage_columns = (
            "profile",
            "route",
            "provider",
            "reserved_bytes",
            "proof_count",
            "active_record_count",
            "settled_operation_count",
            "auth_binding_reserved_bytes",
            "auth_binding_count",
        )
        if tuple(
            row["name"] for row in scope_usage_info
        ) != expected_scope_usage_columns or [
            int(row["pk"]) for row in scope_usage_info
        ] != [1, 2, 3, 0, 0, 0, 0, 0, 0]:
            raise WebhookLedgerCorruptionError(
                "webhook ledger scope usage schema is incompatible"
            )
        expected_auth_binding_columns = (
            "key_fingerprint",
            "profile",
            "route",
            "provider",
            "signature_mode",
            "policy_sha256",
            "bound_at",
        )
        if tuple(
            row["name"] for row in auth_binding_info
        ) != expected_auth_binding_columns or [
            int(row["pk"]) for row in auth_binding_info
        ] != [1, 0, 0, 0, 0, 0, 0]:
            raise WebhookLedgerCorruptionError(
                "webhook authentication key binding schema is incompatible"
            )

        expected_trigger_owners = {
            "trg_webhook_operations_budget_insert": "webhook_operations",
            "trg_webhook_operations_usage_insert": "webhook_operations",
            "trg_webhook_operations_usage_delete": "webhook_operations",
            "trg_webhook_operations_usage_state": "webhook_operations",
            "trg_webhook_tombstones_budget_insert": ("webhook_delivery_tombstones"),
            "trg_webhook_tombstones_usage_insert": ("webhook_delivery_tombstones"),
            "trg_webhook_tombstones_usage_delete": ("webhook_delivery_tombstones"),
            "trg_webhook_auth_bindings_budget_insert": ("webhook_auth_key_bindings"),
            "trg_webhook_auth_bindings_usage_insert": ("webhook_auth_key_bindings"),
            "trg_webhook_auth_bindings_immutable_update": ("webhook_auth_key_bindings"),
            "trg_webhook_auth_bindings_immutable_delete": ("webhook_auth_key_bindings"),
        }
        trigger_names = tuple(expected_trigger_owners)
        trigger_owners = {
            row["name"]: row["tbl_name"]
            for row in conn.execute(
                f"""SELECT name, tbl_name FROM sqlite_master
                    WHERE type='trigger' AND name IN (
                        {",".join("?" for _name in trigger_names)}
                    )""",
                trigger_names,
            )
        }
        if trigger_owners != expected_trigger_owners:
            raise WebhookLedgerCorruptionError(
                "webhook ledger usage triggers are unavailable"
            )

        usage_rows = conn.execute(
            "SELECT * FROM webhook_ledger_usage WHERE schema_name=?",
            (_SCHEMA_NAME,),
        ).fetchall()
        if len(usage_rows) != 1:
            raise WebhookLedgerCorruptionError(
                "webhook ledger usage authority is unavailable"
            )
        operation_state_counts = conn.execute(
            """SELECT COUNT(*) AS operation_count,
                      COALESCE(SUM(
                          CASE WHEN state NOT IN ('settled','indeterminate')
                               THEN 1 ELSE 0 END
                      ), 0) AS active_record_count,
                      COALESCE(SUM(
                          CASE WHEN state='settled' THEN 1 ELSE 0 END
                      ), 0) AS settled_operation_count,
                      COALESCE(SUM(
                          CASE WHEN state='indeterminate' THEN 1 ELSE 0 END
                      ), 0) AS indeterminate_operation_count
                 FROM webhook_operations"""
        ).fetchone()
        if operation_state_counts is None:  # pragma: no cover - aggregate row
            raise WebhookLedgerCorruptionError(
                "webhook operation state counters are unavailable"
            )
        operation_count = int(operation_state_counts["operation_count"])
        tombstone_count = int(
            conn.execute("SELECT COUNT(*) FROM webhook_delivery_tombstones").fetchone()[
                0
            ]
        )
        auth_binding_count = int(
            conn.execute("SELECT COUNT(*) FROM webhook_auth_key_bindings").fetchone()[0]
        )
        for binding in conn.execute("SELECT * FROM webhook_auth_key_bindings"):
            try:
                fingerprint = _normalize_sha256(
                    binding["key_fingerprint"],
                    label="stored authentication key fingerprint",
                )
                policy_sha256 = _normalize_sha256(
                    binding["policy_sha256"],
                    label="stored authentication policy fingerprint",
                )
                owner = tuple(
                    _normalize_nonempty(
                        binding[key],
                        label=f"stored authentication {key}",
                    )
                    for key in (
                        "profile",
                        "route",
                        "provider",
                        "signature_mode",
                    )
                )
                bound_at = float(binding["bound_at"])
            except (TypeError, ValueError, OverflowError, WebhookLedgerError) as exc:
                raise WebhookLedgerCorruptionError(
                    "webhook authentication key binding is invalid"
                ) from exc
            if (
                fingerprint != binding["key_fingerprint"]
                or policy_sha256 != binding["policy_sha256"]
                or owner
                != tuple(
                    binding[key]
                    for key in (
                        "profile",
                        "route",
                        "provider",
                        "signature_mode",
                    )
                )
                or not math.isfinite(bound_at)
            ):
                raise WebhookLedgerCorruptionError(
                    "webhook authentication key binding is invalid"
                )
        usage = usage_rows[0]
        expected_proof_count = operation_count + tombstone_count
        expected_reserved_bytes = (
            operation_count * _OPERATION_STORAGE_RESERVATION_BYTES
            + tombstone_count * _TOMBSTONE_STORAGE_RESERVATION_BYTES
        )
        expected_auth_binding_reserved_bytes = (
            auth_binding_count * _AUTH_BINDING_STORAGE_RESERVATION_BYTES
        )
        if (
            int(usage["proof_count"]) != expected_proof_count
            or int(usage["reserved_bytes"]) != expected_reserved_bytes
            or int(usage["active_record_count"])
            != int(operation_state_counts["active_record_count"])
            or int(usage["settled_operation_count"])
            != int(operation_state_counts["settled_operation_count"])
            or int(usage["indeterminate_operation_count"])
            != int(operation_state_counts["indeterminate_operation_count"])
            or int(usage["auth_binding_reserved_bytes"])
            != expected_auth_binding_reserved_bytes
            or int(usage["auth_binding_count"]) != auth_binding_count
            or int(usage["max_records"]) < 1
            or int(usage["max_records"]) > MAXIMUM_MAX_RECORDS
            or int(usage["max_storage_bytes"]) < MINIMUM_MAX_STORAGE_BYTES
            or int(usage["max_storage_bytes"]) > MAXIMUM_MAX_STORAGE_BYTES
            or int(usage["scope_limit_bytes"])
            != _scope_storage_limit(int(usage["max_storage_bytes"]))
            or int(usage["operation_limits_provisional"]) not in (0, 1)
            or int(usage["auth_binding_limit_bytes"])
            != _AUTH_BINDING_GLOBAL_LIMIT_BYTES
            or int(usage["auth_binding_scope_limit_bytes"])
            != _AUTH_BINDING_SCOPE_LIMIT_BYTES
        ):
            raise WebhookLedgerCorruptionError(
                "webhook ledger usage counter is inconsistent"
            )

        expected_scope_usage = {
            (row["profile"], row["route"], row["provider"]): (
                int(row["reserved_bytes"]),
                int(row["proof_count"]),
                int(row["active_record_count"]),
                int(row["settled_operation_count"]),
                int(row["auth_binding_reserved_bytes"]),
                int(row["auth_binding_count"]),
            )
            for row in conn.execute(
                f"""SELECT profile, route, provider,
                            SUM(reserved_bytes) AS reserved_bytes,
                            SUM(proof_count) AS proof_count,
                            SUM(active_record_count) AS active_record_count,
                            SUM(settled_operation_count)
                                AS settled_operation_count,
                            SUM(auth_binding_reserved_bytes)
                                AS auth_binding_reserved_bytes,
                            SUM(auth_binding_count) AS auth_binding_count
                       FROM (
                           SELECT profile, route, provider,
                                  {_OPERATION_STORAGE_RESERVATION_BYTES}
                                      AS reserved_bytes,
                                  1 AS proof_count,
                                  CASE WHEN state NOT IN (
                                      'settled','indeterminate'
                                  ) THEN 1 ELSE 0 END AS active_record_count,
                                  CASE WHEN state='settled'
                                       THEN 1 ELSE 0 END
                                      AS settled_operation_count,
                                  0 AS auth_binding_reserved_bytes,
                                  0 AS auth_binding_count
                             FROM webhook_operations
                           UNION ALL
                           SELECT profile, route, provider,
                                  {_TOMBSTONE_STORAGE_RESERVATION_BYTES}
                                      AS reserved_bytes,
                                  1 AS proof_count,
                                  0 AS active_record_count,
                                  0 AS settled_operation_count,
                                  0 AS auth_binding_reserved_bytes,
                                  0 AS auth_binding_count
                             FROM webhook_delivery_tombstones
                           UNION ALL
                           SELECT profile, route, provider,
                                  0 AS reserved_bytes,
                                  0 AS proof_count,
                                  0 AS active_record_count,
                                  0 AS settled_operation_count,
                                  {_AUTH_BINDING_STORAGE_RESERVATION_BYTES}
                                      AS auth_binding_reserved_bytes,
                                  1 AS auth_binding_count
                             FROM webhook_auth_key_bindings
                       )
                      GROUP BY profile, route, provider"""
            )
        }
        actual_scope_usage = {
            (row["profile"], row["route"], row["provider"]): (
                int(row["reserved_bytes"]),
                int(row["proof_count"]),
                int(row["active_record_count"]),
                int(row["settled_operation_count"]),
                int(row["auth_binding_reserved_bytes"]),
                int(row["auth_binding_count"]),
            )
            for row in conn.execute("SELECT * FROM webhook_ledger_scope_usage")
        }
        if actual_scope_usage != expected_scope_usage or any(
            reserved_bytes > int(usage["scope_limit_bytes"])
            or auth_binding_reserved_bytes
            > int(usage["auth_binding_scope_limit_bytes"])
            or active_record_count < 0
            or active_record_count > _proof_count
            or settled_operation_count < 0
            or active_record_count + settled_operation_count > _proof_count
            for (
                reserved_bytes,
                _proof_count,
                active_record_count,
                settled_operation_count,
                auth_binding_reserved_bytes,
                _auth_binding_count,
            ) in actual_scope_usage.values()
        ):
            raise WebhookLedgerCorruptionError(
                "webhook ledger scope usage counter is inconsistent"
            )

        bounded_expiry_clause = " OR ".join(
            "substr(replay_id, 1, ?)=?" for _prefix in _BOUNDED_REPLAY_PREFIXES
        )
        bounded_expiry_params: list[Any] = []
        for prefix in _BOUNDED_REPLAY_PREFIXES:
            bounded_expiry_params.extend((len(prefix), prefix))
        invalid_expiry = conn.execute(
            f"""SELECT expires_at FROM webhook_delivery_tombstones
                 WHERE expires_at IS NOT NULL
                   AND (state!='settled' OR NOT ({bounded_expiry_clause}))
                 LIMIT 1""",
            bounded_expiry_params,
        ).fetchone()
        nonfinite_expiry = any(
            not math.isfinite(float(row["expires_at"]))
            for row in conn.execute(
                """SELECT expires_at FROM webhook_delivery_tombstones
                    WHERE expires_at IS NOT NULL"""
            )
        )
        if invalid_expiry is not None or nonfinite_expiry:
            raise WebhookLedgerCorruptionError(
                "webhook delivery tombstone expiry is invalid"
            )

        tombstone_indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(webhook_delivery_tombstones)")
        }
        expiry_index = tombstone_indexes.get("idx_webhook_tombstones_expires_at")
        if (
            expiry_index is None
            or bool(expiry_index["unique"])
            or not bool(expiry_index["partial"])
            or tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA index_info(idx_webhook_tombstones_expires_at)"
                )
            )
            != ("expires_at",)
        ):
            raise WebhookLedgerCorruptionError(
                "webhook tombstone expiry index is unavailable"
            )

        indexes = {
            row["name"]: row
            for row in conn.execute("PRAGMA index_list(webhook_operations)")
        }
        stable = indexes.get("idx_webhook_operations_replay_identity")
        if (
            stable is None
            or not bool(stable["unique"])
            or bool(stable["partial"])
            or tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA index_info(idx_webhook_operations_replay_identity)"
                )
            )
            != ("profile", "route", "provider", "replay_id")
        ):
            raise WebhookLedgerCorruptionError(
                "webhook replay-identity uniqueness is unavailable"
            )
        expected_admission_indexes = {
            "idx_webhook_operations_state_updated": (
                "state",
                "updated_at",
            ),
            "idx_webhook_operations_scope_state_updated": (
                "profile",
                "route",
                "provider",
                "state",
                "updated_at",
            ),
        }
        for index_name, expected_columns in expected_admission_indexes.items():
            index = indexes.get(index_name)
            if (
                index is None
                or bool(index["unique"])
                or bool(index["partial"])
                or tuple(
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM pragma_index_info(?)",
                        (index_name,),
                    )
                )
                != expected_columns
            ):
                raise WebhookLedgerCorruptionError(
                    "webhook bounded admission indexes are unavailable"
                )
        expected_recovery_indexes = {
            "idx_webhook_operations_recovery_order": (
                ("created_at", "operation_id"),
                """CREATE INDEX idx_webhook_operations_recovery_order
                    ON webhook_operations(created_at, operation_id)
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )""",
            ),
            "idx_webhook_operations_owner_recovery_order": (
                ("owner_instance", "created_at", "operation_id"),
                """CREATE INDEX idx_webhook_operations_owner_recovery_order
                    ON webhook_operations(
                        owner_instance, created_at, operation_id
                    )
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )""",
            ),
            "idx_webhook_operations_profile_recovery_order": (
                ("profile", "created_at", "operation_id"),
                """CREATE INDEX idx_webhook_operations_profile_recovery_order
                    ON webhook_operations(
                        profile, created_at, operation_id
                    )
                    WHERE state IN (
                        'preparing','ready','running','delivery_ready','delivering'
                    )""",
            ),
            "idx_webhook_operations_owner_delivery_ready": (
                ("owner_instance", "created_at", "operation_id"),
                """CREATE INDEX idx_webhook_operations_owner_delivery_ready
                    ON webhook_operations(
                        owner_instance, created_at, operation_id
                    )
                    WHERE state='delivery_ready'""",
            ),
            "idx_webhook_operations_owner_profile_delivery_ready": (
                ("owner_instance", "profile", "created_at", "operation_id"),
                """CREATE INDEX idx_webhook_operations_owner_profile_delivery_ready
                    ON webhook_operations(
                        owner_instance, profile, created_at, operation_id
                    )
                    WHERE state='delivery_ready'""",
            ),
            "idx_webhook_operations_owner_current_recovery": (
                ("owner_instance", "created_at", "operation_id"),
                """CREATE INDEX idx_webhook_operations_owner_current_recovery
                    ON webhook_operations(
                        owner_instance, created_at, operation_id
                    )
                    WHERE state='delivery_ready'
                       OR (state='ready' AND generation>=2)""",
            ),
            "idx_webhook_operations_owner_profile_current_recovery": (
                ("owner_instance", "profile", "created_at", "operation_id"),
                """CREATE INDEX
                    idx_webhook_operations_owner_profile_current_recovery
                    ON webhook_operations(
                        owner_instance, profile, created_at, operation_id
                    )
                    WHERE state='delivery_ready'
                       OR (state='ready' AND generation>=2)""",
            ),
        }
        for index_name, (
            expected_columns,
            expected_sql,
        ) in expected_recovery_indexes.items():
            index = indexes.get(index_name)
            stored_sql = conn.execute(
                """SELECT sql FROM sqlite_master
                     WHERE type='index' AND name=?""",
                (index_name,),
            ).fetchone()
            if (
                index is None
                or bool(index["unique"])
                or not bool(index["partial"])
                or tuple(
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM pragma_index_info(?)",
                        (index_name,),
                    )
                )
                != expected_columns
                or stored_sql is None
                or "".join(str(stored_sql["sql"]).lower().split())
                != "".join(expected_sql.lower().split())
            ):
                raise WebhookLedgerCorruptionError(
                    "webhook bounded recovery indexes are unavailable"
                )
        bounded_replay_prefix = _BOUNDED_REPLAY_PREFIXES[0]
        bounded_expiry_index = indexes.get(
            "idx_webhook_operations_bounded_settled_expiry"
        )
        bounded_expiry_sql = conn.execute(
            """SELECT sql FROM sqlite_master
                 WHERE type='index'
                   AND name='idx_webhook_operations_bounded_settled_expiry'"""
        ).fetchone()
        expected_bounded_expiry_sql = f"""CREATE INDEX
            idx_webhook_operations_bounded_settled_expiry
            ON webhook_operations(COALESCE(settled_at, updated_at))
            WHERE state='settled'
              AND substr(replay_id, 1, {len(bounded_replay_prefix)})=
                  '{bounded_replay_prefix}'"""
        if (
            bounded_expiry_index is None
            or bool(bounded_expiry_index["unique"])
            or not bool(bounded_expiry_index["partial"])
            or bounded_expiry_sql is None
            or "".join(str(bounded_expiry_sql["sql"]).lower().split())
            != "".join(expected_bounded_expiry_sql.lower().split())
        ):
            raise WebhookLedgerCorruptionError(
                "webhook bounded-settlement expiry index is unavailable"
            )
        session_unique = any(
            bool(index["unique"])
            and not bool(index["partial"])
            and tuple(
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM pragma_index_info(?)", (index["name"],)
                )
            )
            == ("session_key",)
            for index in indexes.values()
        )
        if not session_unique:
            raise WebhookLedgerCorruptionError(
                "webhook session uniqueness is unavailable"
            )

        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(webhook_targets)"
        ).fetchall()
        if not any(
            row["table"] == "webhook_operations"
            and row["from"] == "operation_id"
            and row["to"] == "operation_id"
            and str(row["on_delete"]).upper() == "CASCADE"
            for row in foreign_keys
        ):
            raise WebhookLedgerCorruptionError(
                "webhook target ownership constraint is unavailable"
            )

        metadata = conn.execute(
            """SELECT schema_version FROM webhook_ledger_meta
               WHERE schema_name=?""",
            (_SCHEMA_NAME,),
        ).fetchone()
        if metadata is None or int(metadata["schema_version"]) != _SCHEMA_VERSION:
            raise WebhookLedgerCorruptionError(
                "webhook operation ledger schema version is unsupported"
            )

    @staticmethod
    def _target_row(
        conn: sqlite3.Connection, operation_id: str
    ) -> Optional[sqlite3.Row]:
        rows = conn.execute(
            """SELECT target_id, state, attempt_token, content_sha256,
                      delivery_json, delivery_sha256, external_id,
                      owner_pid, owner_started_at,
                      owner_instance, started_at, settled_at, updated_at, last_error
               FROM webhook_targets WHERE operation_id=?""",
            (operation_id,),
        ).fetchall()
        if len(rows) > 1:
            raise WebhookLedgerCorruptionError(
                f"operation {operation_id!r} has more than one target"
            )
        return rows[0] if rows else None

    def _authority_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> OperationAuthority:
        try:
            state = OperationState(row["state"])
        except (KeyError, ValueError) as exc:
            raise WebhookLedgerCorruptionError(
                "stored operation state is invalid"
            ) from exc
        target = self._target_row(conn, row["operation_id"])
        try:
            target_state = TargetState(target["state"]) if target is not None else None
        except ValueError as exc:
            raise WebhookLedgerCorruptionError(
                "stored target state is invalid"
            ) from exc
        delivery = (
            _decode_staged_delivery(
                target["delivery_json"],
                target["content_sha256"],
                target["delivery_sha256"],
            )
            if target is not None
            else None
        )
        owner_instance = row["owner_instance"]
        if not isinstance(owner_instance, str) or not owner_instance:
            raise WebhookLedgerCorruptionError(
                "stored operation owner instance is invalid"
            )
        return OperationAuthority(
            operation_id=row["operation_id"],
            generation=int(row["generation"]),
            session_key=row["session_key"],
            profile=row["profile"],
            route=row["route"],
            provider=row["provider"],
            replay_id=row["replay_id"],
            body_sha256=row["body_sha256"],
            event_type=row["event_type"],
            state=state,
            owner_instance=owner_instance,
            target_id=target["target_id"] if target is not None else None,
            target_state=target_state,
            event_snapshot=_decode_json(
                row["event_json"],
                label="event snapshot",
                max_bytes=_MAX_EVENT_JSON_BYTES,
            ),
            target_snapshot=_decode_json(
                row["target_json"],
                label="target snapshot",
                max_bytes=_MAX_AUTHORITY_JSON_BYTES,
            ),
            grant_snapshot=_decode_json(
                row["grant_json"],
                label="grant snapshot",
                max_bytes=_MAX_AUTHORITY_JSON_BYTES,
            ),
            delivery=delivery,
        )

    @staticmethod
    def _operation_row(
        conn: sqlite3.Connection, operation_id: str
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM webhook_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()

    @staticmethod
    def _disposition_for_state(state: OperationState) -> AdmitDisposition:
        if state is OperationState.SETTLED:
            return AdmitDisposition.DUPLICATE
        if state is OperationState.INDETERMINATE:
            return AdmitDisposition.INDETERMINATE
        return AdmitDisposition.ACTIVE

    @staticmethod
    def _tombstone_from_row(row: sqlite3.Row) -> DeliveryTombstone:
        try:
            body_sha256 = _normalize_sha256(
                row["body_sha256"], label="tombstone body_sha256"
            )
            replay_id = _normalize_nonempty(
                row["replay_id"], label="tombstone replay_id"
            )
            operation_id = _normalize_nonempty(
                row["operation_id"], label="tombstone operation_id"
            )
            state = OperationState(row["state"])
            if state not in {
                OperationState.SETTLED,
                OperationState.INDETERMINATE,
            }:
                raise ValueError("nonterminal tombstone state")
            settled_at = float(row["settled_at"])
            if not math.isfinite(settled_at):
                raise ValueError("non-finite settled_at")
            expires_raw = row["expires_at"]
            expires_at = None if expires_raw is None else float(expires_raw)
            if expires_at is not None and (
                not math.isfinite(expires_at)
                or state is not OperationState.SETTLED
                or not replay_id.startswith(_BOUNDED_REPLAY_PREFIXES)
            ):
                raise ValueError("invalid tombstone expiry")
            return DeliveryTombstone(
                profile=_normalize_nonempty(row["profile"], label="tombstone profile"),
                route=_normalize_nonempty(row["route"], label="tombstone route"),
                provider=_normalize_nonempty(
                    row["provider"], label="tombstone provider"
                ),
                replay_id=replay_id,
                body_sha256=body_sha256,
                operation_id=operation_id,
                state=state,
                settled_at=settled_at,
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError, WebhookLedgerError) as exc:
            raise WebhookLedgerCorruptionError(
                "stored delivery tombstone is invalid"
            ) from exc

    @staticmethod
    def _tombstone_row(
        conn: sqlite3.Connection,
        *,
        profile: str,
        route: str,
        provider: str,
        replay_id: str,
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            """SELECT * FROM webhook_delivery_tombstones
               WHERE profile=? AND route=? AND provider=? AND replay_id=?""",
            (profile, route, provider, replay_id),
        ).fetchone()

    @staticmethod
    def _bounded_replay_id(replay_id: str) -> bool:
        return str(replay_id).startswith(_BOUNDED_REPLAY_PREFIXES)

    def _body_replay_expired(
        self,
        *,
        replay_id: str,
        terminal_at: float,
        now: float,
    ) -> bool:
        return self._bounded_replay_id(replay_id) and (
            now - terminal_at >= self.local_bypass_replay_retention_seconds
        )

    def _compact_terminal_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: float,
    ) -> None:
        """Replace one heavy terminal row with its exact replay verdict."""

        try:
            state = OperationState(row["state"])
        except ValueError as exc:
            raise WebhookLedgerCorruptionError(
                "terminal operation has an invalid state"
            ) from exc
        if state is not OperationState.SETTLED:
            raise WebhookLedgerTransitionError(
                "only settled webhook operations can be compacted"
            )
        terminal_at = float(row["settled_at"] or row["updated_at"])
        replay_id = row["replay_id"]
        bounded_settlement_expired = (
            state is OperationState.SETTLED
            and self._body_replay_expired(
                replay_id=replay_id,
                terminal_at=terminal_at,
                now=now,
            )
        )
        existing = self._tombstone_row(
            conn,
            profile=row["profile"],
            route=row["route"],
            provider=row["provider"],
            replay_id=replay_id,
        )
        if existing is not None:
            raise WebhookLedgerCorruptionError(
                "replay identity exists as both an operation and a tombstone"
            )
        cursor = conn.execute(
            """DELETE FROM webhook_operations
               WHERE operation_id=? AND generation=? AND state=?""",
            (row["operation_id"], int(row["generation"]), state.value),
        )
        if cursor.rowcount != 1:
            raise WebhookLedgerTransitionError(
                "terminal operation changed while being compacted"
            )
        if not bounded_settlement_expired:
            expires_at = (
                terminal_at + self.local_bypass_replay_retention_seconds
                if state is OperationState.SETTLED
                and self._bounded_replay_id(replay_id)
                else None
            )
            conn.execute(
                """INSERT INTO webhook_delivery_tombstones (
                       profile, route, provider, replay_id, body_sha256,
                       operation_id, state, settled_at, expires_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["profile"],
                    row["route"],
                    row["provider"],
                    replay_id,
                    row["body_sha256"],
                    row["operation_id"],
                    state.value,
                    terminal_at,
                    expires_at,
                ),
            )

    def _compact_terminal_for_capacity(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
        required: int,
        scope: Optional[tuple[str, str, str]] = None,
    ) -> int:
        if required <= 0:
            return 0
        where = "state='settled'"
        params: list[Any] = []
        if scope is not None:
            where += " AND profile=? AND route=? AND provider=?"
            params.extend(scope)
        index_name = (
            "idx_webhook_operations_state_updated"
            if scope is None
            else "idx_webhook_operations_scope_state_updated"
        )
        rows = conn.execute(
            f"""SELECT * FROM webhook_operations INDEXED BY {index_name}
                WHERE {where}
                ORDER BY updated_at
                LIMIT ?""",
            (*params, required),
        ).fetchall()
        for row in rows:
            self._compact_terminal_row(conn, row, now=now)
        return len(rows)

    @staticmethod
    def _storage_usage_row(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM webhook_ledger_usage WHERE schema_name=?",
            (_SCHEMA_NAME,),
        ).fetchone()
        if row is None:
            raise WebhookLedgerCorruptionError(
                "webhook ledger usage authority is unavailable"
            )
        return row

    @staticmethod
    def _scope_storage_usage(
        conn: sqlite3.Connection,
        scope: tuple[str, str, str],
    ) -> tuple[int, int, int, int]:
        row = conn.execute(
            """SELECT reserved_bytes, proof_count, active_record_count,
                      settled_operation_count
                 FROM webhook_ledger_scope_usage
                WHERE profile=? AND route=? AND provider=?""",
            scope,
        ).fetchone()
        if row is None:
            return 0, 0, 0, 0
        return (
            int(row["reserved_bytes"]),
            int(row["proof_count"]),
            int(row["active_record_count"]),
            int(row["settled_operation_count"]),
        )

    def _reserve_operation_storage(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
        scope: tuple[str, str, str],
    ) -> Optional[AdmitSaturationReason]:
        """Fit one carrier, returning the exact exhausted byte authority."""

        usage = self._storage_usage_row(conn)
        (
            scope_reserved,
            _scope_proofs,
            _scope_active,
            _scope_settled,
        ) = self._scope_storage_usage(conn, scope)
        required_scope_bytes = (
            scope_reserved
            + _OPERATION_STORAGE_RESERVATION_BYTES
            - int(usage["scope_limit_bytes"])
        )
        savings_per_compaction = (
            _OPERATION_STORAGE_RESERVATION_BYTES - _TOMBSTONE_STORAGE_RESERVATION_BYTES
        )
        if required_scope_bytes > 0:
            self._compact_terminal_for_capacity(
                conn,
                now=now,
                required=max(
                    1,
                    math.ceil(required_scope_bytes / savings_per_compaction),
                ),
                scope=scope,
            )

        usage = self._storage_usage_row(conn)
        required_bytes = (
            int(usage["reserved_bytes"])
            + _OPERATION_STORAGE_RESERVATION_BYTES
            - int(usage["max_storage_bytes"])
        )
        if required_bytes > 0:
            self._compact_terminal_for_capacity(
                conn,
                now=now,
                required=max(
                    1,
                    math.ceil(required_bytes / savings_per_compaction),
                ),
            )

        usage = self._storage_usage_row(conn)
        (
            scope_reserved,
            _scope_proofs,
            _scope_active,
            _scope_settled,
        ) = self._scope_storage_usage(conn, scope)
        if int(usage["reserved_bytes"]) + _OPERATION_STORAGE_RESERVATION_BYTES > int(
            usage["max_storage_bytes"]
        ):
            return AdmitSaturationReason.GLOBAL_STORAGE_LIMIT
        if scope_reserved + _OPERATION_STORAGE_RESERVATION_BYTES > int(
            usage["scope_limit_bytes"]
        ):
            return AdmitSaturationReason.SCOPE_STORAGE_LIMIT
        return None

    def _prune_terminal(self, conn: sqlite3.Connection, now: float) -> int:
        """Maintain at most one bounded batch of terminal proof history."""

        expired = conn.execute(
            """DELETE FROM webhook_delivery_tombstones
                WHERE rowid IN (
                    SELECT rowid FROM webhook_delivery_tombstones INDEXED BY
                           idx_webhook_tombstones_expires_at
                     WHERE expires_at IS NOT NULL AND expires_at <= ?
                     ORDER BY expires_at, rowid
                     LIMIT ?
                )""",
            (now, _MAX_PRUNE_BATCH),
        )
        maintained = max(0, int(expired.rowcount))
        remaining = _MAX_PRUNE_BATCH - maintained
        if remaining <= 0:
            return maintained

        retention_cutoff = now - self.terminal_retention_seconds
        bounded_cutoff = now - self.local_bypass_replay_retention_seconds
        rows = conn.execute(
            """SELECT * FROM webhook_operations INDEXED BY
                      idx_webhook_operations_state_updated
                 WHERE state='settled'
                   AND updated_at <= ?
                 ORDER BY updated_at
                 LIMIT ?""",
            (retention_cutoff, remaining),
        ).fetchall()
        for row in rows:
            self._compact_terminal_row(conn, row, now=now)
            maintained += 1
        remaining = _MAX_PRUNE_BATCH - maintained
        if remaining <= 0:
            return maintained

        bounded_replay_prefix = _BOUNDED_REPLAY_PREFIXES[0]
        rows = conn.execute(
            f"""SELECT * FROM webhook_operations INDEXED BY
                      idx_webhook_operations_bounded_settled_expiry
                 WHERE state='settled'
                   AND substr(
                       replay_id, 1, {len(bounded_replay_prefix)}
                   )='{bounded_replay_prefix}'
                   AND COALESCE(settled_at, updated_at) <= ?
                 ORDER BY COALESCE(settled_at, updated_at)
                 LIMIT ?""",
            (bounded_cutoff, remaining),
        ).fetchall()
        for row in rows:
            self._compact_terminal_row(conn, row, now=now)
            maintained += 1
        return maintained

    def admit(self, envelope: Any) -> AdmitResult:
        """Atomically reserve an immutable webhook delivery identity.

        The contract's authenticated replay identity is unique across profile,
        route, and provider.  The same identity with a different authenticated
        body is a conflict. Compact authenticated-ID/timestamp and unknown-
        outcome tombstones remain authoritative indefinitely. Remotely
        authenticated body-only proofs are permanent because their wire
        contracts contain no nonce that can distinguish a replay. Only the
        explicit loopback test bypass uses a bounded replay window.
        """

        try:
            route = envelope.route
            operation_id = _normalize_nonempty(envelope.trace_id, label="operation_id")
            session_key = _normalize_nonempty(envelope.session_key, label="session_key")
            profile = _normalize_nonempty(
                envelope.authority_profile,
                label="authority profile",
            )
            route_name = _normalize_nonempty(route.name, label="route")
            provider = _normalize_nonempty(route.provider, label="provider")
            body_sha256 = _normalize_sha256(envelope.body_sha256, label="body_sha256")
            event_type = _normalize_nonempty(envelope.event_type, label="event_type")
            replay_identity = envelope.replay_identity
            replay_provider = _normalize_nonempty(
                replay_identity.provider, label="replay provider"
            )
            replay_id = _normalize_nonempty(envelope.replay_id, label="replay_id")
            if replay_provider != provider or replay_id != replay_identity.storage_key:
                raise WebhookLedgerError(
                    "envelope replay identity is outside its route authority"
                )
        except AttributeError as exc:
            raise WebhookLedgerError("admit requires a webhook envelope") from exc

        now = time.time()
        owner_pid, owner_started_at = _owner_stamp()
        with self._lock, self._transaction() as conn:
            by_operation = self._operation_row(conn, operation_id)
            if by_operation is not None:
                exact = (
                    by_operation["profile"] == profile
                    and by_operation["route"] == route_name
                    and by_operation["provider"] == provider
                    and by_operation["replay_id"] == replay_id
                    and by_operation["body_sha256"] == body_sha256
                    and by_operation["session_key"] == session_key
                )
                authority = self._authority_from_row(conn, by_operation)
                if not exact:
                    return AdmitResult(AdmitDisposition.CONFLICT, authority)
                return AdmitResult(
                    self._disposition_for_state(authority.state), authority
                )

            existing = conn.execute(
                """SELECT * FROM webhook_operations
                   WHERE profile=? AND route=? AND provider=? AND replay_id=?""",
                (profile, route_name, provider, replay_id),
            ).fetchone()
            tombstone_row = self._tombstone_row(
                conn,
                profile=profile,
                route=route_name,
                provider=provider,
                replay_id=replay_id,
            )
            if existing is not None and tombstone_row is not None:
                raise WebhookLedgerCorruptionError(
                    "replay identity exists as both an operation and a tombstone"
                )
            tombstone = (
                self._tombstone_from_row(tombstone_row)
                if tombstone_row is not None
                else None
            )
            if (
                tombstone is not None
                and tombstone.expires_at is not None
                and tombstone.expires_at <= now
            ):
                conn.execute(
                    """DELETE FROM webhook_delivery_tombstones
                        WHERE profile=? AND route=? AND provider=? AND replay_id=?""",
                    (profile, route_name, provider, replay_id),
                )
                tombstone_row = None
                tombstone = None
            if existing is not None:
                authority = self._authority_from_row(conn, existing)
                if existing["body_sha256"] != body_sha256:
                    return AdmitResult(AdmitDisposition.CONFLICT, authority)
                return AdmitResult(
                    self._disposition_for_state(authority.state), authority
                )
            if tombstone is not None:
                if tombstone.body_sha256 != body_sha256:
                    disposition = AdmitDisposition.CONFLICT
                elif tombstone.state is OperationState.INDETERMINATE:
                    disposition = AdmitDisposition.INDETERMINATE
                else:
                    disposition = AdmitDisposition.DUPLICATE
                return AdmitResult(disposition, tombstone=tombstone)

            # Historical maintenance is bounded and runs only for a genuinely
            # new identity. Exact replay/conflict decisions stay indexed and
            # available even when the ledger is saturated.
            self._prune_terminal(conn, now)
            persisted_usage = self._storage_usage_row(conn)
            max_records = int(persisted_usage["max_records"])
            global_record_count = int(persisted_usage["active_record_count"]) + int(
                persisted_usage["settled_operation_count"]
            )
            if (
                global_record_count >= max_records
                and int(persisted_usage["settled_operation_count"]) > 0
            ):
                self._compact_terminal_for_capacity(
                    conn,
                    now=now,
                    required=min(
                        _MAX_PRUNE_BATCH,
                        global_record_count - max_records + 1,
                    ),
                )
                persisted_usage = self._storage_usage_row(conn)
                global_record_count = int(persisted_usage["active_record_count"]) + int(
                    persisted_usage["settled_operation_count"]
                )
            if global_record_count >= max_records:
                return AdmitResult(
                    AdmitDisposition.SATURATED,
                    saturation=AdmitSaturationReason.GLOBAL_RECORD_LIMIT,
                )

            reserve = (
                0
                if max_records <= 1
                else max(
                    1,
                    min(
                        _MAX_SCOPE_CAPACITY_RESERVE,
                        max_records // 4,
                    ),
                )
            )
            scope_limit = max_records - reserve
            scope = (profile, route_name, provider)
            (
                _scope_bytes,
                _scope_proofs,
                scope_active,
                scope_settled,
            ) = self._scope_storage_usage(conn, scope)
            scope_record_count = scope_active + scope_settled
            if scope_record_count >= scope_limit and scope_settled > 0:
                self._compact_terminal_for_capacity(
                    conn,
                    now=now,
                    required=min(
                        _MAX_PRUNE_BATCH,
                        scope_record_count - scope_limit + 1,
                    ),
                    scope=scope,
                )
                (
                    _scope_bytes,
                    _scope_proofs,
                    scope_active,
                    scope_settled,
                ) = self._scope_storage_usage(conn, scope)
                scope_record_count = scope_active + scope_settled
            if scope_record_count >= scope_limit:
                return AdmitResult(
                    AdmitDisposition.SATURATED,
                    saturation=AdmitSaturationReason.SCOPE_RECORD_LIMIT,
                )

            storage_saturation = self._reserve_operation_storage(
                conn,
                now=now,
                scope=scope,
            )
            if storage_saturation is not None:
                return AdmitResult(
                    AdmitDisposition.SATURATED,
                    saturation=storage_saturation,
                )

            conn.execute(
                """INSERT INTO webhook_operations (
                       operation_id, profile, route, provider, replay_id,
                       body_sha256, event_type, session_key, state, generation,
                       owner_pid, owner_started_at, owner_instance,
                       created_at, updated_at
                   ) VALUES (
                       ?, ?, ?, ?, ?, ?, ?, ?, 'preparing', 1, ?, ?, ?, ?, ?
                   )""",
                (
                    operation_id,
                    profile,
                    route_name,
                    provider,
                    replay_id,
                    body_sha256,
                    event_type,
                    session_key,
                    owner_pid,
                    owner_started_at,
                    self.instance_id,
                    now,
                    now,
                ),
            )
            row = self._operation_row(conn, operation_id)
            if row is None:  # pragma: no cover - SQLite acknowledged the insert
                raise WebhookLedgerError("admitted operation disappeared")
            return AdmitResult(
                AdmitDisposition.ACCEPTED,
                self._authority_from_row(conn, row),
            )

    def prepare(
        self,
        authority: OperationAuthority,
        *,
        event_snapshot: Mapping[str, Any],
        target_snapshot: Mapping[str, Any],
        grant_snapshot: Mapping[str, Any],
    ) -> OperationAuthority:
        """Persist the exact execution carrier and make it replayable."""

        event_json = _canonical_json(
            event_snapshot,
            label="event_snapshot",
            max_bytes=_MAX_EVENT_JSON_BYTES,
        )
        target_json = _canonical_json(
            target_snapshot,
            label="target_snapshot",
            max_bytes=_MAX_AUTHORITY_JSON_BYTES,
        )
        grant_json = _canonical_json(
            grant_snapshot,
            label="grant_snapshot",
            max_bytes=_MAX_AUTHORITY_JSON_BYTES,
        )
        target_id = hashlib.sha256(target_json.encode("utf-8")).hexdigest()[:32]
        now = time.time()
        with self._lock, self._transaction() as conn:
            row = self._operation_row(conn, authority.operation_id)
            if row is None:
                raise WebhookLedgerTransitionError("operation no longer exists")
            if int(row["generation"]) != authority.generation:
                raise WebhookLedgerTransitionError("operation generation is stale")
            if (
                authority.owner_instance != self.instance_id
                or row["owner_instance"] != self.instance_id
            ):
                raise WebhookLedgerTransitionError(
                    "operation belongs to a different adapter instance"
                )
            if row["state"] == OperationState.READY.value:
                target = self._target_row(conn, authority.operation_id)
                if (
                    row["event_json"] == event_json
                    and row["target_json"] == target_json
                    and row["grant_json"] == grant_json
                    and target is not None
                    and target["target_id"] == target_id
                ):
                    return self._authority_from_row(conn, row)
                raise WebhookLedgerTransitionError(
                    "ready operation cannot be rebound to different authority"
                )
            if row["state"] != OperationState.PREPARING.value:
                raise WebhookLedgerTransitionError(
                    f"cannot prepare operation in state {row['state']!r}"
                )
            conn.execute(
                """INSERT INTO webhook_targets (
                       operation_id, target_id, state, updated_at
                   ) VALUES (?, ?, 'pending', ?)""",
                (authority.operation_id, target_id, now),
            )
            cursor = conn.execute(
                """UPDATE webhook_operations
                   SET event_json=?, target_json=?, grant_json=?, state='ready',
                       updated_at=?
                   WHERE operation_id=? AND generation=? AND state='preparing'
                     AND owner_instance=?""",
                (
                    event_json,
                    target_json,
                    grant_json,
                    now,
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                ),
            )
            if cursor.rowcount != 1:
                raise WebhookLedgerTransitionError("prepare lost its operation claim")
            updated = self._operation_row(conn, authority.operation_id)
            if updated is None:  # pragma: no cover - guarded by UPDATE
                raise WebhookLedgerError("prepared operation disappeared")
            return self._authority_from_row(conn, updated)

    def mark_script_started(self, authority: OperationAuthority) -> bool:
        """Fence a route script before its subprocess may produce effects."""

        with self._lock, self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE webhook_operations
                   SET script_started=1, updated_at=?
                   WHERE operation_id=? AND generation=? AND state='preparing'
                     AND script_started=0
                     AND owner_instance=?""",
                (
                    time.time(),
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                ),
            )
            return cursor.rowcount == 1

    def release_pre_effect(self, authority: OperationAuthority) -> bool:
        """Delete a claim only while durable knowledge proves no effect began."""

        with self._lock, self._transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM webhook_operations
                   WHERE operation_id=? AND generation=? AND state='preparing'
                     AND script_started=0 AND owner_instance=?""",
                (authority.operation_id, authority.generation, self.instance_id),
            )
            return cursor.rowcount == 1

    def mark_running(self, authority: OperationAuthority) -> bool:
        """Atomically bind a READY carrier to exactly one processing task."""

        now = time.time()
        owner_pid, owner_started_at = _owner_stamp()
        with self._lock, self._transaction() as conn:
            row = self._operation_row(conn, authority.operation_id)
            if row is None or int(row["generation"]) != authority.generation:
                return False
            if (
                authority.owner_instance != self.instance_id
                or row["owner_instance"] != self.instance_id
            ):
                return False
            cursor = conn.execute(
                """UPDATE webhook_operations
                   SET state='running', owner_pid=?, owner_started_at=?, updated_at=?
                   WHERE operation_id=? AND generation=? AND state='ready'
                     AND owner_instance=?""",
                (
                    owner_pid,
                    owner_started_at,
                    now,
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                ),
            )
            return cursor.rowcount == 1

    def stage_delivery(
        self,
        authority: OperationAuthority,
        *,
        content: str,
        carrier_snapshot: Mapping[str, Any],
    ) -> OperationAuthority:
        """Persist the exact post-agent outbound effect before it is attempted.

        Once this commits, crash recovery resumes only this delivery carrier;
        it never replays the agent turn that produced it.
        """

        if not isinstance(content, str):
            raise WebhookLedgerError("delivery content must be text")
        delivery_json = _canonical_json(
            {"content": content, "carrier": carrier_snapshot},
            label="staged_delivery",
            max_bytes=_MAX_EVENT_JSON_BYTES,
        )
        digest = content_sha256(content)
        delivery_digest = hashlib.sha256(delivery_json.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock, self._transaction() as conn:
            row = self._operation_row(conn, authority.operation_id)
            if row is None or int(row["generation"]) != authority.generation:
                raise WebhookLedgerTransitionError("operation generation is stale")
            if (
                authority.owner_instance != self.instance_id
                or row["owner_instance"] != self.instance_id
            ):
                raise WebhookLedgerTransitionError(
                    "operation belongs to a different adapter instance"
                )
            target = self._target_row(conn, authority.operation_id)
            if target is None:
                raise WebhookLedgerTransitionError("target authority does not exist")
            if row["state"] == OperationState.DELIVERY_READY.value:
                if (
                    target["state"] == TargetState.PENDING.value
                    and target["delivery_json"] == delivery_json
                    and target["content_sha256"] == digest
                    and target["delivery_sha256"] == delivery_digest
                ):
                    return self._authority_from_row(conn, row)
                raise WebhookLedgerTransitionError(
                    "delivery-ready operation cannot be rebound"
                )
            if row["state"] != OperationState.RUNNING.value:
                raise WebhookLedgerTransitionError(
                    f"cannot stage delivery in state {row['state']!r}"
                )
            target_cursor = conn.execute(
                """UPDATE webhook_targets
                   SET delivery_json=?, content_sha256=?, delivery_sha256=?, updated_at=?,
                       last_error=NULL
                   WHERE operation_id=? AND target_id=? AND state='pending'
                     AND delivery_json IS NULL AND content_sha256 IS NULL
                     AND delivery_sha256 IS NULL""",
                (
                    delivery_json,
                    digest,
                    delivery_digest,
                    now,
                    authority.operation_id,
                    target["target_id"],
                ),
            )
            if target_cursor.rowcount != 1:
                raise WebhookLedgerTransitionError(
                    "staged delivery lost its target claim"
                )
            operation_cursor = conn.execute(
                """UPDATE webhook_operations
                   SET state='delivery_ready', updated_at=?, last_error=NULL
                   WHERE operation_id=? AND generation=? AND state='running'
                     AND owner_instance=?""",
                (
                    now,
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                ),
            )
            if operation_cursor.rowcount != 1:
                raise WebhookLedgerTransitionError(
                    "staged delivery lost its operation claim"
                )
            updated = self._operation_row(conn, authority.operation_id)
            if updated is None:  # pragma: no cover - guarded by UPDATE
                raise WebhookLedgerError("staged operation disappeared")
            return self._authority_from_row(conn, updated)

    def lookup_session(self, session_key: str) -> Optional[OperationAuthority]:
        """Resolve delivery and grants from the durable session join."""

        normalized = _normalize_nonempty(session_key, label="session_key")
        with self._lock:
            with self._connection() as conn:
                # Operation and target rows are one authority snapshot.  A
                # deferred read transaction keeps a concurrent settlement
                # from becoming visible between the two SELECTs.
                try:
                    conn.execute("BEGIN")
                    row = conn.execute(
                        "SELECT * FROM webhook_operations WHERE session_key=?",
                        (normalized,),
                    ).fetchone()
                    authority = (
                        self._authority_from_row(conn, row) if row is not None else None
                    )
                    conn.execute("COMMIT")
                    return authority
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    raise

    def _list_current_recovery_page(
        self,
        *,
        limit: int,
        after: Optional[RecoveryCursor],
        delivery_only: bool,
        profiles: Optional[Iterable[str]],
    ) -> RecoveryBatch:
        batch_limit = _normalize_recovery_batch_limit(limit)
        cursor = _normalize_recovery_cursor(after)
        normalized_profiles = _normalize_recovery_profiles(profiles)
        if normalized_profiles == ():
            return RecoveryBatch()
        if delivery_only:
            index_name = "idx_webhook_operations_owner_delivery_ready"
            profile_index_name = "idx_webhook_operations_owner_profile_delivery_ready"
            state_predicate = "o.state='delivery_ready'"
        else:
            index_name = "idx_webhook_operations_owner_current_recovery"
            profile_index_name = "idx_webhook_operations_owner_profile_current_recovery"
            # Generation one READY is ordinary freshly prepared inbound work
            # still owned by its original handler. Only a dead-owner claim
            # increments READY to a rediscoverable recovery generation.
            state_predicate = (
                "(o.state='delivery_ready' OR (o.state='ready' AND o.generation>=2))"
            )
        cursor_predicate = ""
        cursor_params: tuple[Any, ...] = ()
        if cursor is not None:
            cursor_predicate = "AND (o.created_at, o.operation_id) > (?, ?)"
            cursor_params = (cursor.created_at, cursor.operation_id)

        with self._lock:
            with self._connection() as conn:
                try:
                    conn.execute("BEGIN")
                    has_more = False
                    next_cursor: Optional[RecoveryCursor] = None
                    continuation: Optional[RecoveryCursor] = None
                    if normalized_profiles is None:
                        page_query = f"""SELECT o.operation_id, o.created_at
                                              FROM webhook_operations AS o
                                                   INDEXED BY {index_name}
                                              JOIN webhook_targets AS t
                                                ON t.operation_id=o.operation_id
                                             WHERE o.owner_instance=?
                                               AND {state_predicate}
                                               AND t.state='pending'
                                               {cursor_predicate}
                                             ORDER BY o.created_at, o.operation_id
                                             LIMIT ?"""
                        page_rows = conn.execute(
                            page_query,
                            (self.instance_id, *cursor_params, batch_limit),
                        ).fetchall()
                        if len(page_rows) == batch_limit:
                            last = page_rows[-1]
                            continuation = RecoveryCursor(
                                created_at=float(last["created_at"]),
                                operation_id=str(last["operation_id"]),
                            )
                            more_query = f"""SELECT 1
                                                  FROM webhook_operations AS o
                                                       INDEXED BY {index_name}
                                                  JOIN webhook_targets AS t
                                                    ON t.operation_id=o.operation_id
                                                 WHERE o.owner_instance=?
                                                   AND {state_predicate}
                                                   AND t.state='pending'
                                                   AND (o.created_at, o.operation_id) >
                                                       (?, ?)
                                                 ORDER BY o.created_at, o.operation_id
                                                 LIMIT 1"""
                            has_more = (
                                conn.execute(
                                    more_query,
                                    (
                                        self.instance_id,
                                        continuation.created_at,
                                        continuation.operation_id,
                                    ),
                                ).fetchone()
                                is not None
                            )
                    else:
                        candidate_rows: list[sqlite3.Row] = []
                        for profile in normalized_profiles:
                            candidate_rows.extend(
                                conn.execute(
                                    f"""SELECT o.operation_id, o.created_at
                                           FROM webhook_operations AS o
                                                INDEXED BY {profile_index_name}
                                           JOIN webhook_targets AS t
                                             ON t.operation_id=o.operation_id
                                          WHERE o.owner_instance=? AND o.profile=?
                                            AND {state_predicate}
                                            AND t.state='pending'
                                            {cursor_predicate}
                                          ORDER BY o.created_at, o.operation_id
                                          LIMIT ?""",
                                    (
                                        self.instance_id,
                                        profile,
                                        *cursor_params,
                                        batch_limit + 1,
                                    ),
                                ).fetchall()
                            )
                        candidate_rows.sort(
                            key=lambda row: (
                                float(row["created_at"]),
                                str(row["operation_id"]),
                            )
                        )
                        page_rows = candidate_rows[:batch_limit]
                        has_more = len(candidate_rows) > batch_limit
                        if has_more:
                            last = page_rows[-1]
                            continuation = RecoveryCursor(
                                created_at=float(last["created_at"]),
                                operation_id=str(last["operation_id"]),
                            )
                    if has_more:
                        if continuation is None:  # pragma: no cover - invariant
                            raise WebhookLedgerCorruptionError(
                                "current recovery continuation is unavailable"
                            )
                        next_cursor = continuation

                    event_ready: list[OperationAuthority] = []
                    delivery_ready: list[OperationAuthority] = []
                    for page_row in page_rows:
                        operation_id = str(page_row["operation_id"])
                        row = self._operation_row(conn, operation_id)
                        if row is None:  # pragma: no cover - same read transaction
                            raise WebhookLedgerCorruptionError(
                                "current recovery operation disappeared"
                            )
                        authority = self._authority_from_row(conn, row)
                        if authority.state is OperationState.READY:
                            if delivery_only or authority.delivery is not None:
                                raise WebhookLedgerCorruptionError(
                                    "event-ready operation has a staged delivery"
                                )
                            event_ready.append(authority)
                        elif authority.state is OperationState.DELIVERY_READY:
                            if authority.delivery is None:
                                raise WebhookLedgerCorruptionError(
                                    "delivery-ready operation has no staged delivery"
                                )
                            delivery_ready.append(authority)
                        else:  # pragma: no cover - guarded by indexed predicate
                            raise WebhookLedgerCorruptionError(
                                "current recovery operation has an invalid state"
                            )
                    conn.execute("COMMIT")
                    return RecoveryBatch(
                        event_ready=tuple(event_ready),
                        delivery_ready=tuple(delivery_ready),
                        scanned_count=len(page_rows),
                        has_more=has_more,
                        next_cursor=next_cursor,
                    )
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                    raise

    def list_current_recovery_ready(
        self,
        *,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
        after: Optional[RecoveryCursor] = None,
        profiles: Optional[Iterable[str]] = None,
    ) -> RecoveryBatch:
        """List one bounded page of replay-safe work owned by this instance.

        This includes event-ready and delivery-ready claims, allowing a runner
        to rediscover a page claimed immediately before an interrupted task
        scheduling handoff. State transition methods remain the exact-once
        mutation gates when independently triggered recovery passes overlap.
        A supplied profile tuple restricts the read to those exact physical
        authority domains; an empty tuple returns no work.
        """

        return self._list_current_recovery_page(
            limit=limit,
            after=after,
            delivery_only=False,
            profiles=profiles,
        )

    def list_delivery_ready(
        self,
        *,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
        after: Optional[RecoveryCursor] = None,
        profiles: Optional[Iterable[str]] = None,
    ) -> RecoveryBatch:
        """List one bounded page of this instance's retryable deliveries."""

        return self._list_current_recovery_page(
            limit=limit,
            after=after,
            delivery_only=True,
            profiles=profiles,
        )

    def current_delivery_ready(
        self,
        *,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
        after: Optional[RecoveryCursor] = None,
        profiles: Optional[Iterable[str]] = None,
    ) -> tuple[OperationAuthority, ...]:
        """Compatibility projection of :meth:`list_delivery_ready`.

        The result is always bounded. New recovery code should consume the
        batch API so it can honor ``has_more`` and ``next_cursor``.
        """

        return self.list_delivery_ready(
            limit=limit,
            after=after,
            profiles=profiles,
        ).delivery_ready

    def relinquish_recovery_claim(self, authority: OperationAuthority) -> bool:
        """Return an unstarted recovery carrier to dead-owner claimability.

        This is valid only while the carrier is still replay-safe. It is used
        when shutdown cancels a newly scheduled recovery task before that
        coroutine executes even one instruction.
        """

        retired_owner = f"cancelled-recovery:{self.instance_id}"
        with self._lock, self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE webhook_operations
                   SET owner_pid=NULL, owner_started_at=NULL,
                       owner_instance=?, updated_at=?
                   WHERE operation_id=? AND generation=?
                     AND owner_instance=?
                     AND state IN ('ready','delivery_ready')
                     AND NOT EXISTS (
                         SELECT 1 FROM webhook_targets
                         WHERE operation_id=? AND state='attempting'
                     )""",
                (
                    retired_owner,
                    time.time(),
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                    authority.operation_id,
                ),
            )
            return cursor.rowcount == 1

    def begin_target(
        self,
        authority: OperationAuthority,
        *,
        content_sha256: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> TargetAttempt:
        """Claim the already-staged exact delivery immediately before invocation.

        ``content_sha256`` is an optional assertion for compatibility; it never
        supplies delivery authority.  The returned attempt carries the durable
        content and carrier that the caller must invoke.
        """

        expected_hash = (
            _normalize_sha256(content_sha256, label="content_sha256")
            if content_sha256 is not None
            else None
        )
        selected_target = target_id or authority.target_id
        selected_target = _normalize_nonempty(selected_target, label="target_id")
        now = time.time()
        owner_pid, owner_started_at = _owner_stamp()
        with self._lock, self._transaction() as conn:
            operation = self._operation_row(conn, authority.operation_id)
            if (
                operation is None
                or int(operation["generation"]) != authority.generation
            ):
                raise WebhookLedgerTransitionError("operation generation is stale")
            target = conn.execute(
                """SELECT * FROM webhook_targets
                   WHERE operation_id=? AND target_id=?""",
                (authority.operation_id, selected_target),
            ).fetchone()
            if target is None:
                raise WebhookLedgerTransitionError("target authority does not exist")
            target_state = TargetState(target["state"])
            delivery = _decode_staged_delivery(
                target["delivery_json"],
                target["content_sha256"],
                target["delivery_sha256"],
            )
            if expected_hash is not None and (
                delivery is None or delivery.content_sha256 != expected_hash
            ):
                raise WebhookLedgerTransitionError(
                    "target content does not match its durable staged delivery"
                )
            attempt_hash = delivery.content_sha256 if delivery is not None else ""
            if operation["state"] == OperationState.INDETERMINATE.value:
                return TargetAttempt(
                    disposition=TargetAttemptDisposition.INDETERMINATE,
                    operation_id=authority.operation_id,
                    generation=authority.generation,
                    target_id=selected_target,
                    content_sha256=attempt_hash,
                    delivery_sha256=(
                        delivery.delivery_sha256 if delivery is not None else ""
                    ),
                    delivery=delivery,
                    owner_instance=self.instance_id,
                )
            if target_state in {TargetState.CONFIRMED, TargetState.SUPPRESSED}:
                if delivery is None:
                    raise WebhookLedgerCorruptionError(
                        "settled target has no staged delivery"
                    )
                return TargetAttempt(
                    disposition=TargetAttemptDisposition.CACHED,
                    operation_id=authority.operation_id,
                    generation=authority.generation,
                    target_id=selected_target,
                    content_sha256=delivery.content_sha256,
                    delivery_sha256=delivery.delivery_sha256,
                    delivery=delivery,
                    owner_instance=self.instance_id,
                )
            if target_state is TargetState.INDETERMINATE:
                return TargetAttempt(
                    disposition=TargetAttemptDisposition.INDETERMINATE,
                    operation_id=authority.operation_id,
                    generation=authority.generation,
                    target_id=selected_target,
                    content_sha256=attempt_hash,
                    delivery_sha256=(
                        delivery.delivery_sha256 if delivery is not None else ""
                    ),
                    delivery=delivery,
                    owner_instance=self.instance_id,
                )
            if target_state is TargetState.ATTEMPTING:
                if delivery is None:
                    raise WebhookLedgerCorruptionError(
                        "attempting target has no staged delivery"
                    )
                return TargetAttempt(
                    disposition=TargetAttemptDisposition.IN_PROGRESS,
                    operation_id=authority.operation_id,
                    generation=authority.generation,
                    target_id=selected_target,
                    content_sha256=delivery.content_sha256,
                    delivery_sha256=delivery.delivery_sha256,
                    delivery=delivery,
                    owner_instance=self.instance_id,
                )
            if (
                authority.owner_instance != self.instance_id
                or operation["owner_instance"] != self.instance_id
            ):
                raise WebhookLedgerTransitionError(
                    "operation belongs to a different adapter instance"
                )
            if delivery is None:
                raise WebhookLedgerTransitionError(
                    "target delivery has not been durably staged"
                )
            if operation["state"] != OperationState.DELIVERY_READY.value:
                raise WebhookLedgerTransitionError(
                    f"cannot attempt target in operation state {operation['state']!r}"
                )
            attempt_token = uuid.uuid4().hex
            cursor = conn.execute(
                """UPDATE webhook_targets
                   SET state='attempting', attempt_token=?,
                       owner_pid=?, owner_started_at=?, started_at=?, updated_at=?,
                       owner_instance=?, settled_at=NULL, external_id=NULL,
                       last_error=NULL
                   WHERE operation_id=? AND target_id=? AND state='pending'""",
                (
                    attempt_token,
                    owner_pid,
                    owner_started_at,
                    now,
                    now,
                    self.instance_id,
                    authority.operation_id,
                    selected_target,
                ),
            )
            if cursor.rowcount != 1:
                raise WebhookLedgerTransitionError("target attempt lost its claim")
            operation_cursor = conn.execute(
                """UPDATE webhook_operations
                   SET state='delivering', owner_pid=?, owner_started_at=?, updated_at=?
                   WHERE operation_id=? AND generation=? AND state='delivery_ready'
                     AND owner_instance=?""",
                (
                    owner_pid,
                    owner_started_at,
                    now,
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                ),
            )
            if operation_cursor.rowcount != 1:
                raise WebhookLedgerTransitionError(
                    "target attempt lost its delivery-ready operation"
                )
            return TargetAttempt(
                disposition=TargetAttemptDisposition.STARTED,
                operation_id=authority.operation_id,
                generation=authority.generation,
                target_id=selected_target,
                content_sha256=delivery.content_sha256,
                delivery_sha256=delivery.delivery_sha256,
                delivery=delivery,
                attempt_token=attempt_token,
                owner_instance=self.instance_id,
            )

    def settle_target(self, attempt: TargetAttempt, settlement: Settlement) -> bool:
        """Settle only the exact fenced target attempt supplied by the caller."""

        if attempt.disposition is not TargetAttemptDisposition.STARTED:
            return False
        if not attempt.attempt_token:
            raise WebhookLedgerTransitionError("started attempt has no fence token")
        if not isinstance(settlement.kind, SettlementKind):
            raise WebhookLedgerError("settlement kind must be typed")
        now = time.time()
        error = _safe_error(settlement.error)
        external_id = (
            str(settlement.external_id)[:512]
            if settlement.external_id is not None
            else None
        )
        with self._lock, self._transaction() as conn:
            operation = self._operation_row(conn, attempt.operation_id)
            if operation is None or int(operation["generation"]) != attempt.generation:
                return False
            if (
                attempt.owner_instance != self.instance_id
                or operation["owner_instance"] != self.instance_id
            ):
                return False
            guard = (
                attempt.operation_id,
                attempt.target_id,
                attempt.attempt_token,
                attempt.content_sha256,
                attempt.delivery_sha256,
                self.instance_id,
            )
            if settlement.kind is SettlementKind.PRE_EFFECT_FAILED:
                cursor = conn.execute(
                    """UPDATE webhook_targets
                       SET state='pending', attempt_token=NULL,
                           owner_pid=NULL, owner_started_at=NULL, started_at=NULL,
                           owner_instance=NULL, updated_at=?, last_error=?
                       WHERE operation_id=? AND target_id=? AND state='attempting'
                         AND attempt_token=? AND content_sha256=?
                         AND delivery_sha256=?
                         AND owner_instance=?""",
                    (now, error, *guard),
                )
                if cursor.rowcount != 1:
                    return False
                operation_cursor = conn.execute(
                    """UPDATE webhook_operations
                       SET state='delivery_ready', updated_at=?, last_error=?
                       WHERE operation_id=? AND generation=? AND state='delivering'
                         AND owner_instance=?""",
                    (
                        now,
                        error,
                        attempt.operation_id,
                        attempt.generation,
                        self.instance_id,
                    ),
                )
                if operation_cursor.rowcount != 1:
                    raise WebhookLedgerTransitionError(
                        "pre-effect target reset lost its operation claim"
                    )
                return True

            target_state = (
                TargetState.CONFIRMED
                if settlement.kind is SettlementKind.CONFIRMED
                else TargetState.SUPPRESSED
                if settlement.kind is SettlementKind.SUPPRESSED
                else TargetState.INDETERMINATE
            )
            cursor = conn.execute(
                """UPDATE webhook_targets
                   SET state=?, external_id=?, settled_at=?, updated_at=?,
                       last_error=?
                   WHERE operation_id=? AND target_id=? AND state='attempting'
                     AND attempt_token=? AND content_sha256=?
                     AND delivery_sha256=?
                     AND owner_instance=?""",
                (
                    target_state.value,
                    external_id,
                    now,
                    now,
                    error,
                    *guard,
                ),
            )
            if cursor.rowcount != 1:
                return False
            if target_state is TargetState.INDETERMINATE:
                operation_cursor = conn.execute(
                    """UPDATE webhook_operations
                       SET state='indeterminate', updated_at=?, last_error=?
                       WHERE operation_id=? AND generation=? AND state='delivering'
                         AND owner_instance=?""",
                    (
                        now,
                        error,
                        attempt.operation_id,
                        attempt.generation,
                        self.instance_id,
                    ),
                )
            else:
                operation_cursor = conn.execute(
                    """UPDATE webhook_operations
                       SET state='settled', settled_at=?, updated_at=?, last_error=NULL
                       WHERE operation_id=? AND generation=?
                         AND state='delivering' AND owner_instance=?""",
                    (
                        now,
                        now,
                        attempt.operation_id,
                        attempt.generation,
                        self.instance_id,
                    ),
                )
            if operation_cursor.rowcount != 1:
                raise WebhookLedgerTransitionError(
                    "target settlement lost its delivering operation"
                )
            return True

    def settle_no_effect(
        self, authority: OperationAuthority, reason: Optional[str] = None
    ) -> bool:
        """Record a terminal operation for which no external effect was required."""

        now = time.time()
        safe_reason = _safe_error(reason)
        with self._lock, self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE webhook_operations
                   SET state='settled', settled_at=?, updated_at=?, last_error=?
                   WHERE operation_id=? AND generation=?
                     AND state IN ('preparing','ready','running')
                     AND owner_instance=?
                     AND NOT EXISTS (
                         SELECT 1 FROM webhook_targets
                         WHERE operation_id=? AND state='attempting'
                     )""",
                (
                    now,
                    now,
                    safe_reason,
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                    authority.operation_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE webhook_targets
                   SET state='suppressed', settled_at=?, updated_at=?, last_error=?
                   WHERE operation_id=? AND state='pending'""",
                (now, now, safe_reason, authority.operation_id),
            )
            return True

    def mark_indeterminate(self, authority: OperationAuthority, reason: object) -> bool:
        """Preserve an operation whose external postcondition is unknown."""

        now = time.time()
        safe_reason = _safe_error(reason) or "webhook operation outcome is unknown"
        with self._lock, self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE webhook_operations
                   SET state='indeterminate', updated_at=?, last_error=?
                   WHERE operation_id=? AND generation=? AND state!='settled'
                     AND owner_instance=?""",
                (
                    now,
                    safe_reason,
                    authority.operation_id,
                    authority.generation,
                    self.instance_id,
                ),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                """UPDATE webhook_targets
                   SET state='indeterminate', settled_at=?, updated_at=?, last_error=?
                   WHERE operation_id=? AND state='attempting'
                     AND owner_instance=?""",
                (
                    now,
                    now,
                    safe_reason,
                    authority.operation_id,
                    self.instance_id,
                ),
            )
            return True

    def retire_instance(
        self,
        *,
        now: Optional[float] = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
    ) -> RecoveryBatch:
        """Compatibility wrapper that fences one bounded retirement page.

        Callers must continue with :meth:`retire_instance_page` while
        ``has_more`` is true before treating the whole instance as fenced.
        """

        return self.retire_instance_page(now=now, limit=limit)

    def retire_instance_page(
        self,
        *,
        now: Optional[float] = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
    ) -> RecoveryBatch:
        """Fence one bounded page before same-process replacement.

        Replay-safe carriers are relinquished with a non-live owner stamp so a
        replacement instance can claim them through :meth:`recover_dead_owners`.
        Work that may already have produced agent or external effects is made
        indeterminate.  After this commits, this instance's old authorities can
        no longer mutate any relinquished row.
        """

        return self.retire_owner_instance_page(
            self.instance_id,
            now=now,
            limit=limit,
        )

    def retire_owner_instance(
        self,
        owner_instance: str,
        *,
        now: Optional[float] = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
    ) -> RecoveryBatch:
        """Compatibility wrapper that fences one exact bounded owner page.

        Callers must continue with :meth:`retire_owner_instance_page` while
        ``has_more`` is true before clearing the owner's quarantine.
        """

        return self.retire_owner_instance_page(
            owner_instance,
            now=now,
            limit=limit,
        )

    def retire_owner_instance_page(
        self,
        owner_instance: str,
        *,
        now: Optional[float] = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
    ) -> RecoveryBatch:
        """Fence one page of an exact quarantined adapter owner.

        This is the retry form of :meth:`retire_instance`: an in-process
        replacement may supply the prior instance ID after the prior adapter's
        retirement transaction failed or had an unknown result.  Every mutation
        remains guarded by that exact durable owner ID, so live peer instances
        are not selected.  Repeating a committed retirement is a no-op.
        """

        prior_owner = _normalize_nonempty(
            owner_instance,
            label="retired webhook owner instance",
        )
        batch_limit = _normalize_recovery_batch_limit(limit)
        try:
            timestamp = time.time() if now is None else float(now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebhookLedgerError("retirement timestamp must be finite") from exc
        if not math.isfinite(timestamp):
            raise WebhookLedgerError("retirement timestamp must be finite")
        retired_owner = (
            "retired:" + hashlib.sha256(prior_owner.encode("utf-8")).hexdigest()
        )
        released: list[str] = []
        indeterminate: list[str] = []
        with self._lock, self._transaction() as conn:
            page_rows = conn.execute(
                """SELECT operation_id, created_at
                     FROM webhook_operations INDEXED BY
                          idx_webhook_operations_owner_recovery_order
                    WHERE owner_instance=?
                      AND state IN (
                          'preparing','ready','running','delivery_ready','delivering'
                      )
                    ORDER BY created_at, operation_id
                    LIMIT ?""",
                (prior_owner, batch_limit),
            ).fetchall()
            has_more = False
            if len(page_rows) == batch_limit:
                last = page_rows[-1]
                has_more = (
                    conn.execute(
                        """SELECT 1
                             FROM webhook_operations INDEXED BY
                                  idx_webhook_operations_owner_recovery_order
                            WHERE owner_instance=?
                              AND state IN (
                                  'preparing','ready','running',
                                  'delivery_ready','delivering'
                              )
                              AND (created_at, operation_id) > (?, ?)
                            ORDER BY created_at, operation_id
                            LIMIT 1""",
                        (
                            prior_owner,
                            float(last["created_at"]),
                            str(last["operation_id"]),
                        ),
                    ).fetchone()
                    is not None
                )
            for page_row in page_rows:
                row = self._operation_row(conn, str(page_row["operation_id"]))
                if row is None:  # pragma: no cover - same write transaction
                    raise WebhookLedgerCorruptionError(
                        "retired webhook operation disappeared"
                    )
                operation_id = row["operation_id"]
                if row["state"] == OperationState.PREPARING.value and not bool(
                    row["script_started"]
                ):
                    conn.execute(
                        """DELETE FROM webhook_operations
                           WHERE operation_id=? AND generation=?
                             AND owner_instance=? AND state='preparing'
                             AND script_started=0""",
                        (
                            operation_id,
                            int(row["generation"]),
                            prior_owner,
                        ),
                    )
                    released.append(operation_id)
                    continue
                if row["state"] in {
                    OperationState.READY.value,
                    OperationState.DELIVERY_READY.value,
                }:
                    cursor = conn.execute(
                        """UPDATE webhook_operations
                           SET owner_pid=NULL, owner_started_at=NULL,
                               owner_instance=?, updated_at=?
                           WHERE operation_id=? AND generation=?
                             AND owner_instance=? AND state=?""",
                        (
                            retired_owner,
                            timestamp,
                            operation_id,
                            int(row["generation"]),
                            prior_owner,
                            row["state"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise WebhookLedgerTransitionError(
                            "adapter retirement lost a replayable operation"
                        )
                    continue

                reason = (
                    "adapter instance retired after webhook effects may have started"
                )
                cursor = conn.execute(
                    """UPDATE webhook_operations
                       SET state='indeterminate', updated_at=?, last_error=?
                       WHERE operation_id=? AND generation=? AND owner_instance=?
                         AND state IN ('preparing','running','delivering')""",
                    (
                        timestamp,
                        reason,
                        operation_id,
                        int(row["generation"]),
                        prior_owner,
                    ),
                )
                if cursor.rowcount != 1:
                    raise WebhookLedgerTransitionError(
                        "adapter retirement lost an active operation"
                    )
                conn.execute(
                    """UPDATE webhook_targets
                       SET state='indeterminate', settled_at=?, updated_at=?,
                           last_error=?
                       WHERE operation_id=? AND state='attempting'
                         AND owner_instance=?""",
                    (
                        timestamp,
                        timestamp,
                        reason,
                        operation_id,
                        prior_owner,
                    ),
                )
                indeterminate.append(operation_id)
        return RecoveryBatch(
            released=tuple(released),
            indeterminate=tuple(indeterminate),
            scanned_count=len(page_rows),
            has_more=has_more,
        )

    def recover_dead_owners(
        self,
        *,
        now: Optional[float] = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
        after: Optional[RecoveryCursor] = None,
        profiles: Optional[Iterable[str]] = None,
    ) -> RecoveryBatch:
        """Compatibility wrapper for one bounded dead-owner recovery page."""

        return self.recover_dead_owners_page(
            now=now,
            limit=limit,
            after=after,
            profiles=profiles,
        )

    def recover_dead_owners_page(
        self,
        *,
        now: Optional[float] = None,
        limit: int = DEFAULT_RECOVERY_BATCH_SIZE,
        after: Optional[RecoveryCursor] = None,
        profiles: Optional[Iterable[str]] = None,
    ) -> RecoveryBatch:
        """Reconcile one bounded page whose exact process owner disappeared.

        ``ready`` rows replay the event because no agent task began.
        ``delivery_ready`` rows replay only their exact staged outbound carrier.
        ``running`` and ``delivering`` rows are ambiguous and become
        indeterminate.  A pre-script ``preparing`` claim is safe to release;
        once a route script was started it is ambiguous too.

        ``has_more`` means more nonterminal rows remain to *scan*, not that the
        current page necessarily recovered an authority. Live owners are
        intentionally counted in ``scanned_count`` and advanced by the keyset
        cursor so they cannot starve dead rows later in the ordered index.
        When ``profiles`` is supplied, only those exact physical authority
        domains are scanned and the same frozen tuple must accompany every
        continuation page. An empty tuple claims nothing.
        """

        batch_limit = _normalize_recovery_batch_limit(limit)
        cursor = _normalize_recovery_cursor(after)
        normalized_profiles = _normalize_recovery_profiles(profiles)
        try:
            timestamp = time.time() if now is None else float(now)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WebhookLedgerError("recovery timestamp must be finite") from exc
        if not math.isfinite(timestamp):
            raise WebhookLedgerError("recovery timestamp must be finite")
        if normalized_profiles == ():
            return RecoveryBatch()
        owner_pid, owner_started_at = _owner_stamp()
        event_ready: list[OperationAuthority] = []
        delivery_ready: list[OperationAuthority] = []
        released: list[str] = []
        indeterminate: list[str] = []
        with self._lock, self._transaction() as conn:
            has_more = False
            next_cursor: Optional[RecoveryCursor] = None
            continuation: Optional[RecoveryCursor] = None
            cursor_predicate = ""
            cursor_params: tuple[Any, ...] = ()
            if cursor is not None:
                cursor_predicate = "AND (created_at, operation_id) > (?, ?)"
                cursor_params = (cursor.created_at, cursor.operation_id)
            if normalized_profiles is None:
                page_rows = conn.execute(
                    f"""SELECT operation_id, created_at
                           FROM webhook_operations INDEXED BY
                                idx_webhook_operations_recovery_order
                          WHERE state IN (
                              'preparing','ready','running',
                              'delivery_ready','delivering'
                          )
                            {cursor_predicate}
                          ORDER BY created_at, operation_id
                          LIMIT ?""",
                    (*cursor_params, batch_limit),
                ).fetchall()
                if len(page_rows) == batch_limit:
                    last = page_rows[-1]
                    continuation = RecoveryCursor(
                        created_at=float(last["created_at"]),
                        operation_id=str(last["operation_id"]),
                    )
                    has_more = (
                        conn.execute(
                            """SELECT 1
                                 FROM webhook_operations INDEXED BY
                                      idx_webhook_operations_recovery_order
                                WHERE state IN (
                                    'preparing','ready','running',
                                    'delivery_ready','delivering'
                                )
                                  AND (created_at, operation_id) > (?, ?)
                                ORDER BY created_at, operation_id
                                LIMIT 1""",
                            (
                                continuation.created_at,
                                continuation.operation_id,
                            ),
                        ).fetchone()
                        is not None
                    )
            else:
                candidate_rows: list[sqlite3.Row] = []
                for profile in normalized_profiles:
                    candidate_rows.extend(
                        conn.execute(
                            f"""SELECT operation_id, created_at
                                   FROM webhook_operations INDEXED BY
                                        idx_webhook_operations_profile_recovery_order
                                  WHERE profile=? AND state IN (
                                      'preparing','ready','running',
                                      'delivery_ready','delivering'
                                  )
                                    {cursor_predicate}
                                  ORDER BY created_at, operation_id
                                  LIMIT ?""",
                            (
                                profile,
                                *cursor_params,
                                batch_limit + 1,
                            ),
                        ).fetchall()
                    )
                candidate_rows.sort(
                    key=lambda row: (
                        float(row["created_at"]),
                        str(row["operation_id"]),
                    )
                )
                page_rows = candidate_rows[:batch_limit]
                has_more = len(candidate_rows) > batch_limit
                if has_more:
                    last = page_rows[-1]
                    continuation = RecoveryCursor(
                        created_at=float(last["created_at"]),
                        operation_id=str(last["operation_id"]),
                    )
            if has_more:
                if continuation is None:  # pragma: no cover - invariant
                    raise WebhookLedgerCorruptionError(
                        "dead-owner recovery continuation is unavailable"
                    )
                next_cursor = continuation
            for page_row in page_rows:
                row = self._operation_row(conn, str(page_row["operation_id"]))
                if row is None:  # pragma: no cover - same write transaction
                    raise WebhookLedgerCorruptionError("recovery operation disappeared")
                if _owner_alive(row["owner_pid"], row["owner_started_at"]):
                    continue
                operation_id = row["operation_id"]
                target = self._target_row(conn, operation_id)
                target_attempting = (
                    target is not None
                    and target["state"] == TargetState.ATTEMPTING.value
                )
                if row["state"] == OperationState.PREPARING.value:
                    if not bool(row["script_started"]):
                        conn.execute(
                            "DELETE FROM webhook_operations WHERE operation_id=?",
                            (operation_id,),
                        )
                        released.append(operation_id)
                        continue
                elif (
                    row["state"]
                    in {
                        OperationState.READY.value,
                        OperationState.DELIVERY_READY.value,
                    }
                    and not target_attempting
                    and target is not None
                    and target["state"] == TargetState.PENDING.value
                ):
                    cursor = conn.execute(
                        """UPDATE webhook_operations
                           SET generation=generation+1, owner_pid=?,
                               owner_started_at=?, owner_instance=?, updated_at=?
                           WHERE operation_id=? AND generation=? AND state=?""",
                        (
                            owner_pid,
                            owner_started_at,
                            self.instance_id,
                            timestamp,
                            operation_id,
                            int(row["generation"]),
                            row["state"],
                        ),
                    )
                    if cursor.rowcount == 1:
                        claimed = self._operation_row(conn, operation_id)
                        if claimed is None:  # pragma: no cover - guarded by UPDATE
                            raise WebhookLedgerError("recovered operation disappeared")
                        authority = self._authority_from_row(conn, claimed)
                        if authority.state is OperationState.READY:
                            if authority.delivery is not None:
                                raise WebhookLedgerCorruptionError(
                                    "event-ready operation has a staged delivery"
                                )
                            event_ready.append(authority)
                        else:
                            if authority.delivery is None:
                                raise WebhookLedgerCorruptionError(
                                    "delivery-ready operation has no staged delivery"
                                )
                            delivery_ready.append(authority)
                        continue

                reason = "gateway owner exited after webhook effects may have started"
                conn.execute(
                    """UPDATE webhook_operations
                       SET state='indeterminate', updated_at=?, last_error=?
                       WHERE operation_id=? AND state!='settled'""",
                    (timestamp, reason, operation_id),
                )
                conn.execute(
                    """UPDATE webhook_targets
                       SET state='indeterminate', settled_at=?, updated_at=?,
                           last_error=?
                       WHERE operation_id=? AND state='attempting'""",
                    (timestamp, timestamp, reason, operation_id),
                )
                indeterminate.append(operation_id)
        return RecoveryBatch(
            event_ready=tuple(event_ready),
            delivery_ready=tuple(delivery_ready),
            released=tuple(released),
            indeterminate=tuple(indeterminate),
            scanned_count=len(page_rows),
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def prune(self, *, now: Optional[float] = None) -> int:
        """Compact old terminal rows and expire local-bypass proofs."""

        timestamp = time.time() if now is None else float(now)
        with self._lock, self._transaction() as conn:
            return self._prune_terminal(conn, timestamp)

    def count(self) -> int:
        """Return heavy/live operation rows; compact tombstones do not count."""

        with self._lock:
            with self._connection() as conn:
                return int(
                    conn.execute("SELECT COUNT(*) FROM webhook_operations").fetchone()[
                        0
                    ]
                )

    def tombstone_count(self) -> int:
        """Return compact durable replay proofs, including temporary bypasses."""

        with self._lock:
            with self._connection() as conn:
                return int(
                    conn.execute(
                        "SELECT COUNT(*) FROM webhook_delivery_tombstones"
                    ).fetchone()[0]
                )

    def storage_usage(self) -> tuple[int, int, int]:
        """Return reserved bytes, proof count, and the persisted global limit."""

        with self._lock:
            with self._connection() as conn:
                row = self._storage_usage_row(conn)
                return (
                    int(row["reserved_bytes"]),
                    int(row["proof_count"]),
                    int(row["max_storage_bytes"]),
                )

    def bind_authentication_keys(
        self,
        bindings: Iterable[tuple[str, str, str, str, str, str]],
    ) -> None:
        """Permanently bind verifier-key fingerprints to one route authority.

        A key that ever authenticated one profile/route/provider/mode cannot
        later be reassigned to a different authority. This prevents captured
        proofs from gaining a fresh route-scoped replay namespace after a hot
        reload, rename, profile move, provider change, or process restart.
        """

        normalized = [
            _normalize_authentication_binding(binding) for binding in bindings
        ]

        with self._lock, self._transaction() as conn:
            for binding in normalized:
                (
                    fingerprint,
                    profile,
                    route,
                    provider,
                    signature_mode,
                    policy_sha256,
                ) = binding
                existing = conn.execute(
                    """SELECT profile, route, provider, signature_mode,
                              policy_sha256
                         FROM webhook_auth_key_bindings
                        WHERE key_fingerprint=?""",
                    (fingerprint,),
                ).fetchone()
                owner = (
                    profile,
                    route,
                    provider,
                    signature_mode,
                    policy_sha256,
                )
                if existing is not None:
                    persisted_owner = (
                        existing["profile"],
                        existing["route"],
                        existing["provider"],
                        existing["signature_mode"],
                        existing["policy_sha256"],
                    )
                    if persisted_owner != owner:
                        raise WebhookLedgerTransitionError(
                            "webhook authentication key is permanently bound "
                            "to another route policy authority"
                        )
                    continue
                usage = self._storage_usage_row(conn)
                if int(usage["auth_binding_reserved_bytes"]) > (
                    int(usage["auth_binding_limit_bytes"])
                    - _AUTH_BINDING_STORAGE_RESERVATION_BYTES
                ):
                    raise WebhookLedgerCapacityError(
                        "webhook authentication binding capacity exhausted"
                    )
                scope_binding_usage = conn.execute(
                    """SELECT auth_binding_reserved_bytes
                         FROM webhook_ledger_scope_usage
                        WHERE profile=? AND route=? AND provider=?""",
                    (profile, route, provider),
                ).fetchone()
                scope_binding_reserved = (
                    0
                    if scope_binding_usage is None
                    else int(scope_binding_usage["auth_binding_reserved_bytes"])
                )
                if scope_binding_reserved > (
                    int(usage["auth_binding_scope_limit_bytes"])
                    - _AUTH_BINDING_STORAGE_RESERVATION_BYTES
                ):
                    raise WebhookLedgerCapacityError(
                        "webhook authentication binding route-scope capacity exhausted"
                    )
                conn.execute(
                    """INSERT INTO webhook_auth_key_bindings (
                           key_fingerprint, profile, route, provider,
                           signature_mode, policy_sha256, bound_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (*binding, time.time()),
                )

    def authentication_keys_match(
        self,
        bindings: Iterable[tuple[str, str, str, str, str, str]],
    ) -> bool:
        """Prove a bounded route-key authority still exists exactly.

        The check reconnects to the database path and uses one read snapshot,
        so an unlinked/replaced root database cannot be hidden by the adapter's
        cached route bundle. Missing fingerprints or owner/policy mismatches are
        ordinary negative proofs. Storage and schema failures remain errors so
        callers fail closed rather than treating an unavailable authority as a
        legitimate key rejection.
        """

        normalized: list[tuple[str, str, str, str, str, str]] = []
        for binding in bindings:
            if len(normalized) >= _MAX_AUTH_BINDINGS_PER_AUTHORITY_CHECK:
                raise WebhookLedgerError(
                    "authentication authority check exceeds its fingerprint bound"
                )
            normalized.append(_normalize_authentication_binding(binding))

        expected_columns = (
            "key_fingerprint",
            "profile",
            "route",
            "provider",
            "signature_mode",
            "policy_sha256",
            "bound_at",
        )
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN")
            try:
                metadata = conn.execute(
                    """SELECT schema_version FROM webhook_ledger_meta
                        WHERE schema_name=?""",
                    (_SCHEMA_NAME,),
                ).fetchall()
                binding_info = conn.execute(
                    "PRAGMA table_info(webhook_auth_key_bindings)"
                ).fetchall()
                try:
                    schema_version = (
                        int(metadata[0]["schema_version"])
                        if len(metadata) == 1
                        else None
                    )
                except (TypeError, ValueError, OverflowError) as exc:
                    raise WebhookLedgerCorruptionError(
                        "webhook authentication key binding schema is incompatible"
                    ) from exc
                if (
                    len(metadata) != 1
                    or schema_version != _SCHEMA_VERSION
                    or tuple(row["name"] for row in binding_info) != expected_columns
                    or [int(row["pk"]) for row in binding_info] != [1, 0, 0, 0, 0, 0, 0]
                ):
                    raise WebhookLedgerCorruptionError(
                        "webhook authentication key binding schema is incompatible"
                    )
                for (
                    fingerprint,
                    profile,
                    route,
                    provider,
                    signature_mode,
                    policy_sha256,
                ) in normalized:
                    existing = conn.execute(
                        """SELECT profile, route, provider, signature_mode,
                                  policy_sha256
                             FROM webhook_auth_key_bindings
                            WHERE key_fingerprint=?""",
                        (fingerprint,),
                    ).fetchone()
                    if existing is None or (
                        existing["profile"],
                        existing["route"],
                        existing["provider"],
                        existing["signature_mode"],
                        existing["policy_sha256"],
                    ) != (
                        profile,
                        route,
                        provider,
                        signature_mode,
                        policy_sha256,
                    ):
                        return False
                return True
            finally:
                conn.rollback()

    def has_global_admission_capacity(self, *, now: Optional[float] = None) -> bool:
        """Return whether one new heavy carrier fits the global authorities.

        This is a read-only readiness snapshot. Settled heavy rows count at
        their compact replay-proof size because admission may safely compact
        them before reserving another carrier. Scope limits are deliberately
        excluded: one saturated route must not make the shared listener
        unhealthy while the global reserve remains available.
        """

        timestamp = time.time() if now is None else float(now)
        bounded_replay_prefix = _BOUNDED_REPLAY_PREFIXES[0]

        with self._lock, self._connection() as conn:
            # A deferred read transaction gives every capacity component one
            # SQLite snapshot without taking the writer lock used by mutation.
            conn.execute("BEGIN")
            try:
                usage = self._storage_usage_row(conn)
                effective_global_records = (
                    int(usage["active_record_count"])
                    + int(usage["settled_operation_count"])
                    - min(
                        int(usage["settled_operation_count"]),
                        _MAX_PRUNE_BATCH,
                    )
                )
                if effective_global_records >= int(usage["max_records"]):
                    return False

                expired_proofs = len(
                    conn.execute(
                        """SELECT rowid
                             FROM webhook_delivery_tombstones INDEXED BY
                                  idx_webhook_tombstones_expires_at
                            WHERE expires_at IS NOT NULL AND expires_at <= ?
                            ORDER BY expires_at, rowid
                            LIMIT ?""",
                        (timestamp, _MAX_PRUNE_BATCH),
                    ).fetchall()
                )
                expired_bounded_settlements = len(
                    conn.execute(
                        f"""SELECT operation_id
                             FROM webhook_operations INDEXED BY
                                  idx_webhook_operations_bounded_settled_expiry
                            WHERE state='settled'
                              AND substr(
                                  replay_id, 1, {len(bounded_replay_prefix)}
                              )='{bounded_replay_prefix}'
                              AND COALESCE(settled_at, updated_at) <= ?
                            ORDER BY COALESCE(settled_at, updated_at)
                            LIMIT ?""",
                        (
                            timestamp - self.local_bypass_replay_retention_seconds,
                            _MAX_PRUNE_BATCH,
                        ),
                    ).fetchall()
                )
                effective_reserved = (
                    int(usage["reserved_bytes"])
                    - expired_proofs * _TOMBSTONE_STORAGE_RESERVATION_BYTES
                    - int(usage["settled_operation_count"])
                    * (
                        _OPERATION_STORAGE_RESERVATION_BYTES
                        - _TOMBSTONE_STORAGE_RESERVATION_BYTES
                    )
                    - expired_bounded_settlements * _TOMBSTONE_STORAGE_RESERVATION_BYTES
                )
                return effective_reserved + _OPERATION_STORAGE_RESERVATION_BYTES <= int(
                    usage["max_storage_bytes"]
                )
            finally:
                conn.rollback()


def content_sha256(content: str) -> str:
    """Canonical digest helper for target-attempt joins."""

    return hashlib.sha256(str(content).encode("utf-8", "replace")).hexdigest()


__all__ = [
    "AdmitDisposition",
    "AdmitResult",
    "AdmitSaturationReason",
    "DEFAULT_MAX_STORAGE_BYTES",
    "DEFAULT_RECOVERY_BATCH_SIZE",
    "DeliveryTombstone",
    "MAXIMUM_MAX_STORAGE_BYTES",
    "MAXIMUM_MAX_RECORDS",
    "MAXIMUM_RECOVERY_BATCH_SIZE",
    "MAXIMUM_RECOVERY_PROFILES",
    "MINIMUM_MAX_STORAGE_BYTES",
    "OperationAuthority",
    "OperationState",
    "RecoveryBatch",
    "RecoveryCursor",
    "Settlement",
    "SettlementKind",
    "StagedDelivery",
    "TargetAttempt",
    "TargetAttemptDisposition",
    "TargetState",
    "WebhookLedgerCapacityError",
    "WebhookLedgerConfigurationError",
    "WebhookLedgerCorruptionError",
    "WebhookLedgerError",
    "WebhookLedgerTransitionError",
    "WebhookOperationLedger",
    "content_sha256",
]
