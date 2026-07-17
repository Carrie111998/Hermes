from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, tzinfo
from decimal import Decimal
import errno
import hashlib
import hmac
import json
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from hermes_state import SessionDB

from .models import (
    ContextPack,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarJobState,
    UpsertResult,
    canonical_session_id,
)

if TYPE_CHECKING:
    from .claude_visibility import (
        ClaudeVisibilityCandidate,
        ClaudeVisibilityIdentity,
    )
    from .sidebar import SidebarCandidate
    from .worktree import WorktreeSnapshot


_EXTERNAL_PROVIDERS = (Provider.CLAUDE, Provider.CODEX)
_MESSAGE_KEY_QUERY_CHUNK = 400
_CONTINUATION_SNAPSHOT_STATE_PREFIX = "session-bridge:continuation:"
_MIRROR_ATTEMPT_STATE_PREFIX = "session-bridge:attempt:"
_MIRROR_AUTHORITY_STATE_PREFIX = "session-bridge:mirror-authority:"
_MIRROR_RATE_STATE_KEY = "session-bridge:mirror-rate"
_MIRROR_BREAKER_STATE_KEY = "session-bridge:mirror-breaker"
_MIRROR_BREAKER_RESERVATION_PREFIX = "session-bridge:breaker-reservation:"
_SIDEBAR_DELIVERY_STATE_PREFIX = "session-bridge:sidebar-delivery:"
_SIDEBAR_BROKER_HEARTBEAT_STATE_KEY = "session-bridge:sidebar:broker-heartbeat"
_PROFILE_SHADOW_SOURCE = "session_bridge_profile"
_WORKTREE_SNAPSHOT_STATE_PREFIX = "session-bridge:worktree:"
_WORKTREE_SNAPSHOT_FIELDS = frozenset({
    "version",
    "source_session_id",
    "cwd",
    "git_root",
    "branch",
    "head",
    "worktree_id",
})
_NATIVE_SESSION_SNAPSHOT_FIELDS = (
    "id",
    "source",
    "model",
    "title",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "cwd",
    "git_branch",
    "git_repo_root",
    "rewind_count",
    "archived",
)
_NATIVE_MESSAGE_SNAPSHOT_FIELDS = (
    "id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "finish_reason",
    "reasoning",
    "reasoning_details",
    "codex_reasoning_items",
    "reasoning_content",
    "codex_message_items",
    "active",
    "compacted",
)
_SIDEBAR_DELIVERY_STATE_FIELDS = frozenset({
    "version",
    "source_session_id",
    "provider",
    "bridge_id",
    "title",
    "cwd",
    "git_root",
    "git_branch",
    "git_head",
    "worktree_id",
    "eligible_at",
})
_STRUCTURED_CONTENT_HEX_PREFIX = "006A736F6E3A"
_PYTHON_STRIP_CHARACTERS = (
    "\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)
_CONTINUATION_SNAPSHOT_FIELDS = frozenset({
    "version",
    "pack_id",
    "source_session_id",
    "source_cursor",
    "source_hash",
    "target_session_id",
    "target_cursor",
    "target_hash",
})
SIDEBAR_RETRYABLE_ERRORS = frozenset({
    "codex_tool_unavailable",
    "desktop_offline",
    "bridge_temporarily_unavailable",
    "sqlite_busy",
    "rename_failed",
    "project_lookup_failed",
    "native_task_not_indexed",
    "broker_time_budget",
})
SIDEBAR_FATAL_ERRORS = frozenset({
    "marker_conflict",
    "source_identity_mismatch",
    "codex_thread_conflict",
    "provider_mismatch",
    "source_cwd_missing",
    "permission_preflight_failed",
    "retry_budget_exhausted",
})
SIDEBAR_EXCLUSION_REASONS = frozenset({"source_cwd_missing"})
PUBLIC_SIDEBAR_STATE = {
    SidebarJobState.PENDING.value: "pending",
    SidebarJobState.LEASED.value: "pending",
    SidebarJobState.RETRY.value: "retrying",
    SidebarJobState.VISIBLE.value: "visible",
    SidebarJobState.FAILED.value: "failed",
}
_PUBLIC_CODEX_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,511}")
_SIDEBAR_RETRY_DELAYS_SECONDS = (60.0, 120.0, 240.0, 480.0, 900.0)
_SIDEBAR_LEASE_SECONDS = 300
# Four maximum-size broker batches bound malformed-row cleanup per write lock.
_SIDEBAR_CLAIM_SCAN_LIMIT = 40
# Delivery latency summarizes only the newest durable visible jobs, keeping status
# reads bounded while preserving a useful recent operational window.
_SIDEBAR_LATENCY_SAMPLE_LIMIT = 512


NativeProjectionCursor = tuple[float, str]
SidebarCandidateCursor = tuple[float, str]


def _claude_error_detail(value: object) -> str:
    from .context_pack import _redact

    if not isinstance(value, str):
        return "Claude visibility operation failed"
    compact = " ".join(_redact(value).split())
    return (compact or "Claude visibility operation failed")[:512]


def _canonical_snapshot_value(value: object) -> list[Any]:
    """Encode persisted SQLite/JSON values without lossy type coercion."""

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if math.isnan(value):
            encoded_float = "nan"
        elif math.isinf(value):
            encoded_float = "infinity" if value > 0 else "-infinity"
        else:
            encoded_float = value.hex()
        return ["float", encoded_float]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, Mapping):
        items = [
            (_canonical_snapshot_value(key), _canonical_snapshot_value(item))
            for key, item in value.items()
        ]
        items.sort(
            key=lambda pair: json.dumps(
                pair[0], separators=(",", ":"), ensure_ascii=False
            )
        )
        return ["mapping", items]
    if isinstance(value, Sequence):
        return ["sequence", [_canonical_snapshot_value(item) for item in value]]
    raise TypeError(f"unsupported native snapshot value: {type(value).__name__}")


def _native_session_snapshot_identity(
    session_row: Mapping[str, Any],
    message_rows: Sequence[Mapping[str, Any]],
    *,
    decode_content: Callable[[Any], Any],
) -> dict[str, str]:
    session_payload = {
        key: session_row[key] for key in _NATIVE_SESSION_SNAPSHOT_FIELDS
    }
    messages_payload: list[dict[str, Any]] = []
    for row in message_rows:
        message = {key: row[key] for key in _NATIVE_MESSAGE_SNAPSHOT_FIELDS}
        message["content"] = decode_content(message.get("content"))
        messages_payload.append(message)
    canonical = _canonical_snapshot_value(
        {"session": session_payload, "messages": messages_payload}
    )
    encoded = json.dumps(
        canonical,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    source_hash = hashlib.sha256(encoded).hexdigest()
    last_message_id = messages_payload[-1]["id"] if messages_payload else 0
    return {
        "cursor": (
            f"hermes:{len(messages_payload)}:{last_message_id}:{source_hash[:16]}"
        ),
        "source_hash": source_hash,
    }


class _MirrorWorkerFileLock:
    """Crash-releasing lock handle for mirror worker critical sections."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream: BinaryIO | None = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _mirror_worker_lock_contended(exc: OSError) -> bool:
    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
        return True
    return sys.platform == "win32" and getattr(exc, "winerror", None) in {
        32,
        33,
        36,
    }


class NativeProjectionPage(list[SessionProjection]):
    """A bounded newest-first page of minimal mirror-eligibility evidence."""

    def __init__(
        self,
        projections: Sequence[SessionProjection] = (),
        *,
        has_more: bool = False,
        next_cursor: NativeProjectionCursor | None = None,
    ) -> None:
        super().__init__(projections)
        self.has_more = has_more
        self.next_cursor = next_cursor


@dataclass(frozen=True)
class SidebarSource:
    source_session_id: str
    projection: SessionProjection
    git_root: str | None
    git_head: str | None
    worktree_id: str | None
    automation_only: bool
    subagent_only: bool


class SidebarSourcePage(list[SidebarSource]):
    """A bounded newest-first page of sidebar-classification inputs."""

    def __init__(
        self,
        sources: Sequence[SidebarSource] = (),
        *,
        has_more: bool = False,
        next_cursor: SidebarCandidateCursor | None = None,
    ) -> None:
        super().__init__(sources)
        self.has_more = has_more
        self.next_cursor = next_cursor


class SessionBridgeStore:
    """Transactional persistence for the cross-harness session bridge."""

    def __init__(
        self,
        db: SessionDB,
        *,
        clock: Callable[[], float] = time.time,
        sidebar_token_factory: Callable[[], str] | None = None,
        sidebar_jitter: Callable[[float], float] | None = None,
        local_timezone: tzinfo | None = None,
        claude_lease_factory: Callable[[], str] | None = None,
        hermes_profile_db_paths: Callable[
            [], Sequence[tuple[str, Path]]
        ] | None = None,
    ) -> None:
        self.db = db
        self._clock = clock
        self._sidebar_token_factory = sidebar_token_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._sidebar_jitter = sidebar_jitter or (
            lambda bound: random.uniform(0.0, bound)
        )
        if local_timezone is not None and not isinstance(local_timezone, tzinfo):
            raise TypeError("local_timezone must be a tzinfo or None")
        self._local_timezone = local_timezone
        self._claude_lease_factory = claude_lease_factory or (
            lambda: secrets.token_urlsafe(32)
        )
        self._hermes_profile_db_paths = (
            hermes_profile_db_paths or self._discover_hermes_profile_db_paths
        )

    def enqueue_claude_visibility_job(
        self,
        candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
    ) -> dict[str, Any]:
        from .claude_visibility import (
            ClaudeVisibilityCandidate,
            ClaudeVisibilityIdentity,
            validate_claude_visibility_identity_binding,
        )

        if not isinstance(candidate, ClaudeVisibilityCandidate):
            raise TypeError("candidate must be a ClaudeVisibilityCandidate")
        if not isinstance(identity, ClaudeVisibilityIdentity):
            raise TypeError("identity must be a ClaudeVisibilityIdentity")
        validate_claude_visibility_identity_binding(candidate, identity)
        now = _finite_number(self._clock(), "clock")

        def _write(conn):
            collisions = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE source_session_id = ? OR bridge_id = ?
                      OR idempotency_key = ? OR reserved_claude_uuid = ?
                   ORDER BY id LIMIT 5""",
                (
                    candidate.source_session_id,
                    identity.bridge_id,
                    identity.idempotency_key,
                    identity.claude_uuid,
                ),
            ).fetchall()
            if collisions:
                if len(collisions) == 1:
                    existing = dict(collisions[0])
                    immutable = {
                        "id": identity.job_id,
                        "source_session_id": candidate.source_session_id,
                        "bridge_id": identity.bridge_id,
                        "idempotency_key": identity.idempotency_key,
                        "reserved_claude_uuid": identity.claude_uuid,
                        "native_name": candidate.native_name,
                        "source_provider": candidate.source_provider.value,
                        "source_cwd": candidate.source_cwd,
                        "git_root": candidate.git_root,
                        "git_branch": candidate.git_branch,
                        "git_head": candidate.git_head,
                        "worktree_id": candidate.worktree_id,
                        "signed_marker": identity.signed_marker,
                        "eligible_at": candidate.eligible_at,
                    }
                    if all(existing[key] == value for key, value in immutable.items()):
                        return existing
                raise ValueError("Claude visibility identity collision")
            try:
                conn.execute(
                    """INSERT INTO session_claude_visibility_jobs (
                       id, source_session_id, bridge_id, idempotency_key,
                       reserved_claude_uuid, native_name, source_provider,
                       source_cwd, git_root, git_branch, git_head, worktree_id,
                       signed_marker, state, attempts, next_attempt_at,
                       eligible_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                 'claude_pending', 0, ?, ?, ?, ?)""",
                    (
                        identity.job_id,
                        candidate.source_session_id,
                        identity.bridge_id,
                        identity.idempotency_key,
                        identity.claude_uuid,
                        candidate.native_name,
                        candidate.source_provider.value,
                        candidate.source_cwd,
                        candidate.git_root,
                        candidate.git_branch,
                        candidate.git_head,
                        candidate.worktree_id,
                        identity.signed_marker,
                        candidate.eligible_at,
                        candidate.eligible_at,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("Claude visibility identity collision") from exc
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (identity.job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def inspect_due_claude_visibility_reconciliation(
        self, now: float
    ) -> ClaudeVisibilityClaim:
        """Return immutable exact-ID lookup work without leasing or reserving cost."""

        from .claude_visibility import ClaudeVisibilityClaim

        inspection_time = _finite_number(now, "now")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            due = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE (
                       state = 'claude_retry'
                       AND error_code <> 'exact_id_absent_reconciled'
                       AND next_attempt_at <= ?
                   ) OR (
                       state = 'claude_leased' AND lease_expires_at <= ?
                   )
                   ORDER BY next_attempt_at, eligible_at, id LIMIT 1""",
                (inspection_time, inspection_time),
            ).fetchone()
        if due is None:
            return ClaudeVisibilityClaim(status="no_due_job")
        prior_error = (
            "lease_expired"
            if due["state"] == "claude_leased"
            else due["error_code"]
        )
        return ClaudeVisibilityClaim(
            status="reconciliation_required",
            job_id=due["id"],
            source_session_id=due["source_session_id"],
            source_provider=Provider(due["source_provider"]),
            reserved_claude_uuid=due["reserved_claude_uuid"],
            native_name=due["native_name"],
            source_cwd=due["source_cwd"],
            git_root=due["git_root"],
            git_branch=due["git_branch"],
            git_head=due["git_head"],
            worktree_id=due["worktree_id"],
            signed_marker=due["signed_marker"],
            attempt_ordinal=int(due["attempts"]),
            prior_error_code=prior_error,
            requires_exact_id_reconciliation=True,
        )

    def claim_claude_visibility_reconciliation(
        self,
        now: float,
        lease_seconds: float,
    ) -> ClaudeVisibilityClaim:
        """Lease exact-ID reconciliation without reserving launch budget."""

        from .claude_visibility import ClaudeVisibilityClaim

        claim_time = _finite_number(now, "now")
        lease_duration = _finite_number(lease_seconds, "lease_seconds")
        if lease_duration <= 0:
            raise ValueError("lease_seconds must be positive")

        def _write(conn):
            conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_retry', next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = 'lease_expired',
                       error_detail = 'active lease expired before completion',
                       updated_at = ?
                   WHERE state = 'claude_leased' AND lease_expires_at <= ?""",
                (claim_time, claim_time, claim_time),
            )
            due = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE state = 'claude_retry'
                     AND error_code <> 'exact_id_absent_reconciled'
                     AND next_attempt_at <= ?
                   ORDER BY next_attempt_at, eligible_at, id LIMIT 1""",
                (claim_time,),
            ).fetchone()
            if due is None:
                return ClaudeVisibilityClaim(status="no_due_job")
            return self._lease_claude_visibility_reconciliation(
                conn,
                due,
                claim_time=claim_time,
                lease_duration=lease_duration,
            )

        return self.db._execute_write(_write)

    def record_claude_visibility_exact_id_absent(
        self,
        job_id: str,
        lease_digest: str,
        evidence_digest: str,
    ) -> dict[str, Any]:
        """Persist exact reserved-UUID absence under a reconciliation lease."""

        normalized_job_id = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude visibility lease digest"
        )
        evidence = _exact_nonempty_text(
            evidence_digest, "Claude reconciliation evidence digest"
        )
        if re.fullmatch(r"[0-9a-f]{64}", evidence) is None:
            raise ValueError(
                "Claude reconciliation evidence digest must be lowercase SHA-256"
            )
        reconciled_at = _finite_number(self._clock(), "clock")

        def _write(conn):
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_retry', next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = 'exact_id_absent_reconciled',
                       error_detail = ?, updated_at = ?
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?
                     AND error_code = 'exact_id_reconciliation_in_progress'""",
                (
                    reconciled_at,
                    f"absence-evidence:{evidence}",
                    reconciled_at,
                    normalized_job_id,
                    normalized_lease,
                    reconciled_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude reconciliation lease required")
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def claim_claude_visibility_job(
        self,
        now: float,
        lease_seconds: float,
        daily_limit: int,
        cost_limit: object,
        reserved_cost: object,
    ) -> ClaudeVisibilityClaim:
        from .claude_visibility import ClaudeVisibilityClaim, decimal_cost

        claim_time = _finite_number(now, "now")
        lease_duration = _finite_number(lease_seconds, "lease_seconds")
        if lease_duration <= 0:
            raise ValueError("lease_seconds must be positive")
        if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or daily_limit < 1:
            raise ValueError("daily_limit must be a positive integer")
        if daily_limit > 25:
            raise ValueError("daily_limit cannot exceed 25")
        maximum_cost = decimal_cost(cost_limit, "cost_limit")
        attempt_cost = decimal_cost(reserved_cost, "reserved_cost")
        if attempt_cost <= 0:
            raise ValueError("reserved_cost must be positive")
        local_day = self._claude_visibility_local_day(claim_time)

        def _write(conn):
            conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_retry', next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = 'lease_expired',
                       error_detail = 'active lease expired before completion',
                       updated_at = ?
                   WHERE state = 'claude_leased' AND lease_expires_at <= ?""",
                (claim_time, claim_time, claim_time),
            )
            due = conn.execute(
                """SELECT * FROM session_claude_visibility_jobs
                   WHERE state IN ('claude_pending', 'claude_retry')
                     AND next_attempt_at <= ?
                   ORDER BY next_attempt_at, eligible_at, id LIMIT 1""",
                (claim_time,),
            ).fetchone()
            if due is None:
                return ClaudeVisibilityClaim(status="no_due_job")
            launch_permitted = (
                due["state"] == "claude_pending" and int(due["attempts"]) == 0
            ) or (
                due["state"] == "claude_retry"
                and due["error_code"] == "exact_id_absent_reconciled"
            )
            if not launch_permitted:
                return self._lease_claude_visibility_reconciliation(
                    conn,
                    due,
                    claim_time=claim_time,
                    lease_duration=lease_duration,
                )
            usage = conn.execute(
                """SELECT reserved_estimated_cost_usd
                   FROM session_claude_registration_usage
                   WHERE local_day = ?""",
                (local_day,),
            ).fetchall()
            if len(usage) >= daily_limit:
                return ClaudeVisibilityClaim(status="daily_limit")
            spent = sum(
                (Decimal(row["reserved_estimated_cost_usd"]) for row in usage),
                Decimal("0"),
            )
            if spent + attempt_cost > maximum_cost:
                return ClaudeVisibilityClaim(status="cost_limit")

            lease_digest = hashlib.sha256(
                self._claude_lease_factory().encode("utf-8")
            ).hexdigest()
            if conn.execute(
                """SELECT 1 FROM session_claude_visibility_jobs
                   WHERE lease_digest = ? LIMIT 1""",
                (lease_digest,),
            ).fetchone() is not None:
                raise ValueError("Claude visibility lease factory returned a duplicate")
            attempt = int(due["attempts"]) + 1
            prior_error_code = due["error_code"]
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_leased', attempts = ?, lease_digest = ?,
                       lease_expires_at = ?, error_code = NULL,
                       error_detail = NULL, updated_at = ?
                   WHERE id = ? AND state = ? AND attempts = ?""",
                (
                    attempt,
                    lease_digest,
                    claim_time + lease_duration,
                    claim_time,
                    due["id"],
                    due["state"],
                    due["attempts"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale Claude visibility claim")
            conn.execute(
                """INSERT INTO session_claude_registration_usage (
                   local_day, job_id, attempt_ordinal,
                   reserved_estimated_cost_usd, reserved_at
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    local_day,
                    due["id"],
                    attempt,
                    str(attempt_cost),
                    claim_time,
                ),
            )
            return ClaudeVisibilityClaim(
                status="claimed",
                job_id=due["id"],
                source_session_id=due["source_session_id"],
                source_provider=Provider(due["source_provider"]),
                reserved_claude_uuid=due["reserved_claude_uuid"],
                native_name=due["native_name"],
                source_cwd=due["source_cwd"],
                git_root=due["git_root"],
                git_branch=due["git_branch"],
                git_head=due["git_head"],
                worktree_id=due["worktree_id"],
                signed_marker=due["signed_marker"],
                lease_digest=lease_digest,
                attempt_ordinal=attempt,
                prior_error_code=prior_error_code,
                requires_exact_id_reconciliation=False,
                registration_reserved=True,
                launch_permitted=True,
            )

        return self.db._execute_write(_write)

    def _lease_claude_visibility_reconciliation(
        self,
        conn: Any,
        due: Any,
        *,
        claim_time: float,
        lease_duration: float,
    ) -> ClaudeVisibilityClaim:
        from .claude_visibility import ClaudeVisibilityClaim

        prior_error_code = due["error_code"]
        lease_digest = hashlib.sha256(
            self._claude_lease_factory().encode("utf-8")
        ).hexdigest()
        if conn.execute(
            """SELECT 1 FROM session_claude_visibility_jobs
               WHERE lease_digest = ? LIMIT 1""",
            (lease_digest,),
        ).fetchone() is not None:
            raise ValueError("Claude visibility lease factory returned a duplicate")
        cursor = conn.execute(
            """UPDATE session_claude_visibility_jobs
               SET state = 'claude_leased', lease_digest = ?,
                   lease_expires_at = ?,
                   error_code = 'exact_id_reconciliation_in_progress',
                   error_detail = NULL, updated_at = ?
               WHERE id = ? AND state = ? AND attempts = ?""",
            (
                lease_digest,
                claim_time + lease_duration,
                claim_time,
                due["id"],
                due["state"],
                due["attempts"],
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("stale Claude visibility reconciliation claim")
        return ClaudeVisibilityClaim(
            status="claimed",
            job_id=due["id"],
            source_session_id=due["source_session_id"],
            source_provider=Provider(due["source_provider"]),
            reserved_claude_uuid=due["reserved_claude_uuid"],
            native_name=due["native_name"],
            source_cwd=due["source_cwd"],
            git_root=due["git_root"],
            git_branch=due["git_branch"],
            git_head=due["git_head"],
            worktree_id=due["worktree_id"],
            signed_marker=due["signed_marker"],
            lease_digest=lease_digest,
            attempt_ordinal=int(due["attempts"]),
            prior_error_code=prior_error_code,
            requires_exact_id_reconciliation=True,
            registration_reserved=False,
            launch_permitted=False,
        )

    def retry_claude_visibility_job(
        self,
        job_id: str,
        lease_digest: str,
        error_code: str,
        next_attempt_at: float,
        detail: str,
    ) -> dict[str, Any]:
        from .claude_visibility import normalized_claude_visibility_error

        next_at = _finite_number(next_attempt_at, "next_attempt_at")
        normalized_code, retryable = normalized_claude_visibility_error(error_code)
        return self._finish_claude_visibility_lease(
            job_id=job_id,
            lease_digest=lease_digest,
            state="claude_retry" if retryable else "claude_failed",
            error_code=normalized_code,
            error_detail=_claude_error_detail(detail),
            next_attempt_at=next_at,
        )

    def fail_claude_visibility_job(
        self,
        job_id: str,
        lease_digest: str,
        error_code: str,
        detail: str,
    ) -> dict[str, Any]:
        from .claude_visibility import normalized_claude_visibility_error

        normalized_code, _retryable = normalized_claude_visibility_error(error_code)
        return self._finish_claude_visibility_lease(
            job_id=job_id,
            lease_digest=lease_digest,
            state="claude_failed",
            error_code=normalized_code,
            error_detail=_claude_error_detail(detail),
            next_attempt_at=None,
        )

    def commit_claude_visibility_job(
        self,
        job_id: str,
        lease_digest: str,
        transcript_digest: str,
        visible_at: float,
    ) -> dict[str, Any]:
        normalized_job_id = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude visibility lease digest"
        )
        completion = _exact_nonempty_text(transcript_digest, "transcript digest")
        timestamp = _finite_number(visible_at, "visible_at")
        operation_time = _finite_number(self._clock(), "clock")

        def _write(conn):
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = 'claude_visible', lease_digest = NULL,
                       lease_expires_at = NULL, completion_digest = ?,
                       error_code = NULL, error_detail = NULL,
                       visible_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?""",
                (
                    completion,
                    timestamp,
                    operation_time,
                    normalized_job_id,
                    normalized_lease,
                    operation_time,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude visibility lease required")
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def _finish_claude_visibility_lease(
        self,
        *,
        job_id: str,
        lease_digest: str,
        state: str,
        error_code: str,
        error_detail: str,
        next_attempt_at: float | None,
    ) -> dict[str, Any]:
        normalized_job_id = _exact_nonempty_text(job_id, "Claude visibility job ID")
        normalized_lease = _exact_nonempty_text(
            lease_digest, "Claude visibility lease digest"
        )
        updated_at = _finite_number(self._clock(), "clock")

        def _write(conn):
            cursor = conn.execute(
                """UPDATE session_claude_visibility_jobs
                   SET state = ?, next_attempt_at = COALESCE(?, next_attempt_at),
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = ?, error_detail = ?, updated_at = ?
                   WHERE id = ? AND state = 'claude_leased'
                     AND lease_digest = ? AND lease_expires_at > ?""",
                (
                    state,
                    next_attempt_at,
                    error_code,
                    error_detail,
                    updated_at,
                    normalized_job_id,
                    normalized_lease,
                    updated_at,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("exact active Claude visibility lease required")
            return dict(
                conn.execute(
                    "SELECT * FROM session_claude_visibility_jobs WHERE id = ?",
                    (normalized_job_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def claude_visibility_status(self, now: float) -> dict[str, Any]:
        status_time = _finite_number(now, "now")
        local_day = self._claude_visibility_local_day(status_time)
        states = (
            "claude_pending",
            "claude_leased",
            "claude_retry",
            "claude_visible",
            "claude_failed",
        )
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            count_rows = conn.execute(
                """SELECT state, COUNT(*) AS count
                   FROM session_claude_visibility_jobs GROUP BY state"""
            ).fetchall()
            code_rows = conn.execute(
                """SELECT state, error_code, COUNT(*) AS count
                   FROM session_claude_visibility_jobs
                   WHERE error_code IS NOT NULL
                   GROUP BY state, error_code"""
            ).fetchall()
            usage_rows = conn.execute(
                """SELECT reserved_estimated_cost_usd
                   FROM session_claude_registration_usage
                   WHERE local_day = ?""",
                (local_day,),
            ).fetchall()
        counts = {state: 0 for state in states}
        for row in count_rows:
            if row["state"] in counts:
                counts[row["state"]] = int(row["count"])
        retry_codes: dict[str, int] = {}
        failed_codes: dict[str, int] = {}
        for row in code_rows:
            target = retry_codes if row["state"] == "claude_retry" else failed_codes
            if row["state"] in ("claude_retry", "claude_failed"):
                target[row["error_code"]] = int(row["count"])
        total_cost = sum(
            (Decimal(row["reserved_estimated_cost_usd"]) for row in usage_rows),
            Decimal("0"),
        )
        return {
            "counts": counts,
            "retry_codes": retry_codes,
            "failed_codes": failed_codes,
            "usage": {
                "local_day": local_day,
                "attempts": len(usage_rows),
                "reserved_cost_usd": str(total_cost),
            },
        }

    def _claude_visibility_local_day(self, timestamp: float) -> str:
        if self._local_timezone is None:
            local = datetime.fromtimestamp(timestamp).astimezone()
        else:
            local = datetime.fromtimestamp(timestamp, tz=self._local_timezone)
        return local.date().isoformat()

    def _discover_hermes_profile_db_paths(self) -> tuple[tuple[str, Path], ...]:
        profiles_root = self.db.db_path.parent / "profiles"
        if not profiles_root.is_dir():
            return ()
        return tuple(
            (entry.name, entry / "state.db")
            for entry in sorted(profiles_root.iterdir(), key=lambda path: path.name)
            if entry.is_dir() and (entry / "state.db").is_file()
        )

    @contextmanager
    def _native_hermes_databases(self):
        databases: list[tuple[str, SessionDB, bool]] = [
            ("default", self.db, False)
        ]
        seen = {str(self.db.db_path.resolve()).casefold()}
        try:
            for profile, raw_path in self._hermes_profile_db_paths():
                if not isinstance(profile, str) or not profile.strip():
                    raise ValueError("Hermes profile name must be nonempty")
                path = Path(raw_path)
                key = str(path.resolve()).casefold()
                if key in seen or not path.is_file():
                    continue
                seen.add(key)
                database = SessionDB(path, read_only=True)
                self._install_profile_read_compatibility(database)
                databases.append((profile.strip(), database, True))
            yield databases
        finally:
            for _profile, database, owned in databases:
                if owned:
                    database.close()

    @staticmethod
    def _install_profile_read_compatibility(database: SessionDB) -> None:
        """Supply empty TEMP bridge tables for pre-bridge profile databases."""

        definitions = {
            "external_sessions": """(
                session_id TEXT, provider TEXT, native_id TEXT,
                native_status TEXT, first_indexed_at REAL, last_indexed_at REAL,
                origin_kind TEXT, origin_bridge_id TEXT, sync_error TEXT
            )""",
            "session_links": """(
                id TEXT, from_session_id TEXT, to_session_id TEXT,
                relation TEXT, bridge_id TEXT, created_at REAL,
                hydrated_at REAL, diverged_at REAL
            )""",
            "session_mirror_jobs": """(
                source_session_id TEXT, state TEXT
            )""",
            "session_sidebar_jobs": """(
                source_session_id TEXT, state TEXT, lease_expires_at REAL,
                codex_thread_id TEXT, error_code TEXT
            )""",
        }
        with database._lock:
            conn = database._conn
            assert conn is not None
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            for name, definition in definitions.items():
                if name not in existing:
                    conn.execute(f"CREATE TEMP TABLE {name} {definition}")

    @staticmethod
    def _database_columns(database: SessionDB, table: str) -> set[str]:
        with database._lock:
            conn = database._conn
            assert conn is not None
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _profile_catalog_compatible(cls, database: SessionDB) -> bool:
        session_columns = cls._database_columns(database, "sessions")
        message_columns = cls._database_columns(database, "messages")
        return {
            "id", "source", "model", "title", "started_at", "ended_at",
            "message_count", "cwd", "git_branch", "git_repo_root",
            "parent_session_id", "archived",
        }.issubset(session_columns) and {
            "id", "session_id", "role", "content", "tool_call_id",
            "tool_calls", "tool_name", "timestamp", "active", "compacted",
        }.issubset(message_columns)

    def try_acquire_mirror_worker_lock(self) -> _MirrorWorkerFileLock | None:
        """Try to serialize mirror processing and reconciliation across processes."""

        lock_path = self.db.db_path.with_name(
            f"{self.db.db_path.name}.session-bridge-worker.lock"
        )
        stream = lock_path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(
                    stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except OSError as exc:
            stream.close()
            if _mirror_worker_lock_contended(exc):
                return None
            raise
        return _MirrorWorkerFileLock(stream)

    def upsert_projection(
        self,
        projection: SessionProjection,
        *,
        rebuild: bool = False,
    ) -> UpsertResult:
        provider = _external_provider(projection.provider)
        session_id = canonical_session_id(provider, projection.native_id)
        native_id = projection.native_id.strip()
        git_branch = (
            projection.git_branch.strip() if projection.git_branch is not None else None
        )
        git_branch = git_branch or None
        now = float(self._clock())
        activity_state_key = _external_activity_state_key(session_id)
        last_active = float(projection.last_active)
        activity_value_json = json.dumps(
            {"last_active": last_active},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        projected_messages = _snapshot_projected_messages(projection)
        projected_keys = [
            (message.native_event_id, message.ordinal)
            for message, _ in projected_messages
        ]
        if len(set(projected_keys)) != len(projected_keys):
            raise ValueError("projection contains duplicate native message identities")

        def _write(conn):
            session_row = conn.execute(
                "SELECT source FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            external_row = conn.execute(
                """SELECT provider, native_id, origin_kind, origin_bridge_id
                   FROM external_sessions WHERE session_id = ?""",
                (session_id,),
            ).fetchone()

            if session_row is not None:
                matching_identity = (
                    external_row is not None
                    and external_row["provider"] == provider.value
                    and external_row["native_id"] == native_id
                    and session_row["source"] == provider.value
                )
                if not matching_identity:
                    raise ValueError(
                        f"session ID collision for imported session {session_id!r}"
                    )
            elif external_row is not None:
                raise ValueError(
                    f"session ID collision for imported session {session_id!r}"
                )

            activity_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (activity_state_key,),
            ).fetchone()
            persisted_last_active = (
                _decode_external_activity(activity_row["value_json"])
                if activity_row is not None
                else None
            )
            if (
                not rebuild
                and external_row is not None
                and persisted_last_active is not None
                and last_active < persisted_last_active
            ):
                raise ValueError(f"stale projection for session {session_id!r}")

            if rebuild:
                pending = projected_messages
                has_new_human_user = False
            else:
                existing_keys = _existing_message_keys(conn, session_id, projected_keys)
                pending = [
                    (message, row)
                    for message, row in projected_messages
                    if (message.native_event_id, message.ordinal) not in existing_keys
                ]
                has_new_human_user = any(
                    message.role == "user"
                    and isinstance(message.content, str)
                    and bool(message.content.strip())
                    for message, _ in pending
                )

            origin_kind, origin_bridge_id = _resolve_projection_provenance(
                external_row,
                projection.origin_kind,
                projection.origin_bridge_id,
                has_new_human_user=has_new_human_user,
            )

            first_seen = external_row is None
            self.db._upsert_session_row(
                conn,
                session_id,
                provider.value,
                cwd=projection.cwd,
                started_at=float(projection.started_at),
            )
            conn.execute(
                """UPDATE sessions
                   SET source = ?,
                       title = CASE
                           WHEN ? IS NULL THEN title
                           WHEN NOT EXISTS (
                               SELECT 1 FROM sessions AS other
                               WHERE other.title = ? AND other.id != ?
                           ) THEN ?
                           ELSE title
                       END,
                       cwd = COALESCE(?, cwd),
                       git_branch = COALESCE(?, git_branch),
                       started_at = MIN(started_at, ?)
                   WHERE id = ?""",
                (
                    provider.value,
                    projection.title,
                    projection.title,
                    session_id,
                    projection.title,
                    projection.cwd,
                    git_branch,
                    float(projection.started_at),
                    session_id,
                ),
            )

            conn.execute(
                """INSERT INTO external_sessions (
                   session_id, provider, native_id, native_path, native_status,
                   last_native_cursor, last_native_hash, first_indexed_at,
                   last_indexed_at, parser_version, origin_kind, origin_bridge_id,
                   sync_error
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(session_id) DO UPDATE SET
                       native_path = excluded.native_path,
                       native_status = excluded.native_status,
                       last_native_cursor = excluded.last_native_cursor,
                       last_native_hash = excluded.last_native_hash,
                       last_indexed_at = excluded.last_indexed_at,
                       parser_version = excluded.parser_version,
                       origin_kind = excluded.origin_kind,
                       origin_bridge_id = excluded.origin_bridge_id,
                       sync_error = NULL""",
                (
                    session_id,
                    provider.value,
                    native_id,
                    projection.native_path,
                    projection.native_status,
                    projection.native_cursor,
                    projection.native_hash,
                    now,
                    now,
                    projection.parser_version,
                    origin_kind,
                    origin_bridge_id,
                ),
            )

            if rebuild:
                conn.execute(
                    """DELETE FROM messages
                       WHERE id IN (
                           SELECT message_id FROM external_message_map
                           WHERE session_id = ?
                       )""",
                    (session_id,),
                )

            inserted_ids, _ = self.db._insert_message_rows_with_ids(
                conn, session_id, [row for _, row in pending]
            )
            for (message, _), message_id in zip(pending, inserted_ids, strict=True):
                conn.execute(
                    """INSERT INTO external_message_map (
                       session_id, native_event_id, ordinal, message_id
                       ) VALUES (?, ?, ?, ?)""",
                    (
                        session_id,
                        message.native_event_id,
                        message.ordinal,
                        message_id,
                    ),
                )

            message_count, tool_call_count = _active_message_counters(conn, session_id)
            conn.execute(
                """UPDATE sessions
                   SET message_count = ?, tool_call_count = ?
                   WHERE id = ?""",
                (message_count, tool_call_count, session_id),
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (activity_state_key, activity_value_json, now),
            )
            return UpsertResult(
                session_id=session_id,
                inserted_messages=len(inserted_ids),
                rebuilt=rebuild,
                first_seen=first_seen,
            )

        return self.db._execute_write(_write)

    def get_external_session(self, session_id: str) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT * FROM external_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_native_projections(
        self,
        after: float,
        limit: int,
        *,
        cursor: NativeProjectionCursor | None = None,
    ) -> NativeProjectionPage:
        cutoff = _finite_number(after, "after")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("native projection limit must be between 1 and 1000")
        normalized_cursor = _validated_native_projection_cursor(cursor)
        cursor_clause = ""
        query_params: dict[str, Any] = {
            "activity_prefix": "session-bridge:external-activity:",
            "profile_shadow_source": _PROFILE_SHADOW_SOURCE,
            "origin_kind": OriginKind.NATIVE.value,
            "after": cutoff,
            "queued": MirrorJobState.QUEUED.value,
            "running": MirrorJobState.RUNNING.value,
            "retry": MirrorJobState.RETRY.value,
            "structured_prefix": _STRUCTURED_CONTENT_HEX_PREFIX,
            "trim_chars": _PYTHON_STRIP_CHARACTERS,
            "query_limit": limit + 1,
        }
        if normalized_cursor is not None:
            cursor_clause = """WHERE (
                candidate.last_active < :cursor_activity
                OR (
                    candidate.last_active = :cursor_activity
                    AND candidate.id > :cursor_session_id
                )
            )"""
            query_params["cursor_activity"] = normalized_cursor[0]
            query_params["cursor_session_id"] = normalized_cursor[1]

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            session_rows = conn.execute(
                f"""WITH candidate_evidence AS (
                       SELECT s.id, s.title, s.cwd, s.started_at, s.git_branch,
                              e.provider, e.native_id, e.native_path,
                              e.native_status, e.last_native_cursor,
                              e.last_native_hash, e.parser_version,
                              e.origin_kind, e.origin_bridge_id,
                              activity.value_json AS activity_value_json,
                              CAST(json_extract(
                                  activity.value_json, '$.last_active'
                              ) AS REAL) AS last_active,
                              (
                                  SELECT map.message_id
                                  FROM external_message_map AS map
                                  JOIN messages AS message
                                    ON message.id = map.message_id
                                  WHERE map.session_id = e.session_id
                                    AND message.active = 1
                                    AND message.role = 'user'
                                    AND typeof(message.content) = 'text'
                                    AND length(trim(
                                        message.content, :trim_chars
                                    )) > 0
                                    AND substr(
                                        hex(message.content), 1, 12
                                    ) != :structured_prefix
                                  ORDER BY message.timestamp, map.message_id
                                  LIMIT 1
                              ) AS evidence_message_id
                       FROM external_sessions AS e
                       JOIN sessions AS s ON s.id = e.session_id
                       JOIN session_bridge_state AS activity
                         ON activity.key = :activity_prefix || e.session_id
                       WHERE e.origin_kind = :origin_kind
                         AND e.origin_bridge_id IS NULL
                         AND CAST(json_extract(
                             activity.value_json, '$.last_active'
                         ) AS REAL) >= :after
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_links AS link
                             JOIN external_sessions AS target
                               ON target.session_id = link.to_session_id
                             WHERE link.from_session_id = e.session_id
                               AND target.provider = CASE e.provider
                                   WHEN 'claude' THEN 'codex'
                                   ELSE 'claude'
                               END
                         )
                         AND NOT EXISTS (
                             SELECT 1
                             FROM session_mirror_jobs AS job
                             WHERE job.source_session_id = e.session_id
                               AND job.state IN (
                                   :queued, :running, :retry
                               )
                         )
                   ), eligible_candidates AS (
                       SELECT *
                       FROM candidate_evidence
                       WHERE evidence_message_id IS NOT NULL
                   )
                   SELECT candidate.*,
                          map.native_event_id AS evidence_native_event_id,
                          map.ordinal AS evidence_ordinal,
                          message.role AS evidence_role,
                          substr(trim(
                              message.content, :trim_chars
                          ), 1, 1) AS evidence_content,
                          message.timestamp AS evidence_timestamp
                   FROM eligible_candidates AS candidate
                   JOIN external_message_map AS map
                     ON map.session_id = candidate.id
                    AND map.message_id = candidate.evidence_message_id
                   JOIN messages AS message
                     ON message.id = candidate.evidence_message_id
                   {cursor_clause}
                   ORDER BY candidate.last_active DESC, candidate.id
                   LIMIT :query_limit""",
                query_params,
            ).fetchall()
            if not session_rows:
                return NativeProjectionPage()

        has_more = len(session_rows) > limit
        page_rows = session_rows[:limit]
        projections: list[SessionProjection] = []
        for row in page_rows:
            last_active = _decode_external_activity(row["activity_value_json"])
            if last_active < cutoff or last_active != float(row["last_active"]):
                raise ValueError("native projection activity ordering is invalid")
            evidence_content = row["evidence_content"]
            if not isinstance(evidence_content, str) or not evidence_content.strip():
                raise ValueError("native projection evidence is invalid")
            projections.append(
                SessionProjection(
                    provider=_external_provider(row["provider"]),
                    native_id=row["native_id"],
                    title=row["title"],
                    cwd=row["cwd"],
                    started_at=float(row["started_at"]),
                    last_active=last_active,
                    messages=(
                        ProjectedMessage(
                            native_event_id=row["evidence_native_event_id"],
                            ordinal=row["evidence_ordinal"],
                            role=row["evidence_role"],
                            content=evidence_content,
                            timestamp=float(row["evidence_timestamp"]),
                        ),
                    ),
                    native_path=row["native_path"],
                    native_status=row["native_status"],
                    native_cursor=row["last_native_cursor"],
                    native_hash=row["last_native_hash"],
                    parser_version=row["parser_version"],
                    origin_kind=OriginKind(row["origin_kind"]),
                    origin_bridge_id=row["origin_bridge_id"],
                    git_branch=row["git_branch"],
                )
            )
        next_cursor = (
            (projections[-1].last_active, page_rows[-1]["id"])
            if has_more and projections
            else None
        )
        return NativeProjectionPage(
            projections,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def list_existing_target_mappings(
        self,
        source_ids: Sequence[str],
    ) -> frozenset[tuple[str, Provider]]:
        normalized_source_ids = _bounded_exact_ids(
            source_ids,
            label="source_ids",
            maximum=1000,
        )
        if not normalized_source_ids:
            return frozenset()

        mappings: set[tuple[str, Provider]] = set()
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            for start in range(0, len(normalized_source_ids), 400):
                batch = normalized_source_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT link.from_session_id, target.provider
                         FROM session_links AS link
                         JOIN external_sessions AS target
                           ON target.session_id = link.to_session_id
                         WHERE link.from_session_id IN ({placeholders})""",
                    batch,
                ).fetchall()
                mappings.update(
                    (row["from_session_id"], _external_provider(row["provider"]))
                    for row in rows
                )
        return frozenset(mappings)

    def record_sidebar_exclusion(
        self,
        source_session_id: str,
        provider: Provider,
        reason_code: str,
        now: float,
    ) -> dict[str, Any]:
        from .sidebar import sidebar_idempotency_key

        sidebar_idempotency_key(source_session_id)
        if not isinstance(provider, Provider) or provider not in (
            Provider.CLAUDE,
            Provider.HERMES,
        ):
            raise ValueError("sidebar exclusion provider must be Claude or Hermes")
        expected_provider = (
            Provider.CLAUDE
            if source_session_id.startswith(f"{Provider.CLAUDE.value}:")
            else Provider.HERMES
        )
        if provider is not expected_provider:
            raise ValueError("sidebar exclusion provider does not match source")
        if (
            not isinstance(reason_code, str)
            or reason_code not in SIDEBAR_EXCLUSION_REASONS
        ):
            raise ValueError("sidebar exclusion reason is not in the fixed allowlist")
        excluded_at = _finite_number(now, "now")
        identity_digest = _sidebar_exclusion_identity_digest(
            source_session_id,
            provider,
            reason_code,
        )

        def _write(conn):
            insert = conn.execute(
                """INSERT OR IGNORE INTO session_sidebar_exclusions (
                   source_session_id, provider, reason_code,
                   source_identity_digest, excluded_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source_session_id,
                    provider.value,
                    reason_code,
                    identity_digest,
                    excluded_at,
                    excluded_at,
                ),
            )
            row = conn.execute(
                """SELECT source_session_id, provider, reason_code,
                          source_identity_digest, excluded_at, updated_at
                     FROM session_sidebar_exclusions
                    WHERE source_session_id = ?""",
                (source_session_id,),
            ).fetchone()
            if row is None or (
                row["source_session_id"] != source_session_id
                or row["provider"] != provider.value
                or row["reason_code"] != reason_code
                or row["source_identity_digest"] != identity_digest
            ):
                raise ValueError("conflicting sidebar exclusion")
            return {
                "source_session_id": row["source_session_id"],
                "reason_code": row["reason_code"],
                "created": insert.rowcount == 1,
            }

        return self.db._execute_write(_write)

    def sidebar_exclusion_counts(self) -> dict[str, Any]:
        by_reason = {reason: 0 for reason in SIDEBAR_EXCLUSION_REASONS}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            total = conn.execute(
                "SELECT COUNT(*) AS count FROM session_sidebar_exclusions"
            ).fetchone()["count"]
            rows = conn.execute(
                """SELECT reason_code, COUNT(*) AS count
                     FROM session_sidebar_exclusions
                    GROUP BY reason_code"""
            ).fetchall()
        for row in rows:
            if row["reason_code"] in by_reason:
                by_reason[row["reason_code"]] = row["count"]
        return {"total": total, "by_reason": by_reason}

    def list_sidebar_candidates(
        self,
        after: float,
        limit: int,
        *,
        cursor: SidebarCandidateCursor | None = None,
    ) -> SidebarSourcePage:
        cutoff = _finite_number(after, "sidebar candidate cutoff")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("sidebar candidate limit must be between 1 and 1000")
        normalized_cursor = _validated_sidebar_candidate_cursor(cursor)
        cursor_clause = ""
        params: dict[str, Any] = {
            "after": cutoff,
            "claude": Provider.CLAUDE.value,
            "native": OriginKind.NATIVE.value,
            "activity_prefix": "session-bridge:external-activity:",
            "profile_shadow_source": _PROFILE_SHADOW_SOURCE,
            "query_limit": limit + 1,
        }
        if normalized_cursor is not None:
            cursor_clause = """AND (
                candidate.last_active < :cursor_activity
                OR (
                    candidate.last_active = :cursor_activity
                    AND candidate.session_id > :cursor_session_id
                )
            )"""
            params["cursor_activity"] = normalized_cursor[0]
            params["cursor_session_id"] = normalized_cursor[1]

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""WITH source_metadata AS (
                       SELECT s.id AS session_id, s.source, s.model_config,
                              s.title, s.cwd, s.started_at, s.git_branch,
                              s.git_repo_root,
                              e.provider AS external_provider,
                              e.native_id AS external_native_id,
                              e.native_path, e.native_status,
                              e.last_native_cursor, e.last_native_hash,
                              e.parser_version, e.origin_kind,
                              e.origin_bridge_id,
                              CASE
                                  WHEN e.provider = :claude THEN CAST(json_extract(
                                      activity.value_json, '$.last_active'
                                  ) AS REAL)
                                  ELSE COALESCE(
                                      (SELECT MAX(message.timestamp)
                                         FROM messages AS message
                                        WHERE message.session_id = s.id
                                          AND (
                                              message.active = 1
                                              OR message.compacted = 1
                                          )),
                                      s.started_at
                                  )
                              END AS last_active,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END
                                  AS automation_only,
                              CASE
                                  WHEN s.source IN ('subagent', 'tool') THEN 1
                                  WHEN json_extract(
                                      COALESCE(s.model_config, '{{}}'),
                                      '$._delegate_from'
                                  ) IS NOT NULL THEN 1
                                  ELSE 0
                              END AS subagent_only
                         FROM sessions AS s
                         LEFT JOIN external_sessions AS e
                           ON e.session_id = s.id
                         LEFT JOIN session_bridge_state AS activity
                           ON activity.key = :activity_prefix || s.id
                        WHERE (
                            (
                                e.provider = :claude
                                AND e.origin_kind = :native
                                AND e.origin_bridge_id IS NULL
                            )
                            OR (
                                e.session_id IS NULL
                                AND s.id NOT LIKE 'claude:%'
                                AND s.id NOT LIKE 'codex:%'
                                AND NOT EXISTS (
                                    SELECT 1
                                      FROM session_links AS incoming_link
                                     WHERE incoming_link.to_session_id = s.id
                                )
                            )
                        )
                          AND s.source != :profile_shadow_source
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_sidebar_jobs AS sidebar_job
                               WHERE sidebar_job.source_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1
                                FROM session_sidebar_exclusions AS exclusion
                               WHERE exclusion.source_session_id = s.id
                          )
                   ), candidate AS (
                       SELECT * FROM source_metadata
                        WHERE last_active IS NOT NULL
                          AND last_active >= :after
                   )
                   SELECT * FROM candidate
                    WHERE 1 = 1
                      {cursor_clause}
                    ORDER BY candidate.last_active DESC, candidate.session_id
                    LIMIT :query_limit""",
                params,
            ).fetchall()
            page_rows = rows[:limit]
            messages_by_session: dict[str, list[ProjectedMessage]] = {
                row["session_id"]: [] for row in page_rows
            }
            session_ids = list(messages_by_session)
            for start in range(0, len(session_ids), _MESSAGE_KEY_QUERY_CHUNK):
                batch = session_ids[start : start + _MESSAGE_KEY_QUERY_CHUNK]
                placeholders = ",".join("?" for _ in batch)
                message_rows = conn.execute(
                    f"""SELECT message.id, message.session_id, message.role,
                               message.content, message.timestamp,
                               map.native_event_id, map.ordinal
                          FROM messages AS message
                          LEFT JOIN external_message_map AS map
                            ON map.message_id = message.id
                         WHERE message.session_id IN ({placeholders})
                           AND message.role = 'user'
                           AND (message.active = 1 OR message.compacted = 1)
                         ORDER BY message.session_id,
                                  message.timestamp, message.id""",
                    batch,
                ).fetchall()
                for message_row in message_rows:
                    decoded_content = self.db._decode_content(message_row["content"])
                    content = (
                        decoded_content if isinstance(decoded_content, str) else None
                    )
                    message_id = int(message_row["id"])
                    messages_by_session[message_row["session_id"]].append(
                        ProjectedMessage(
                            native_event_id=(
                                message_row["native_event_id"]
                                or f"hermes-message:{message_id}"
                            ),
                            ordinal=(
                                int(message_row["ordinal"])
                                if message_row["ordinal"] is not None
                                else message_id
                            ),
                            role=message_row["role"],
                            content=content,
                            timestamp=float(message_row["timestamp"]),
                        )
                    )

        sources: list[SidebarSource] = []
        for row in page_rows:
            source_session_id = row["session_id"]
            provider = (
                Provider.CLAUDE
                if row["external_provider"] == Provider.CLAUDE.value
                else Provider.HERMES
            )
            native_id = (
                row["external_native_id"]
                if provider is Provider.CLAUDE
                else source_session_id
            )
            last_active = _finite_number(
                row["last_active"], "sidebar candidate activity"
            )
            projection = SessionProjection(
                provider=provider,
                native_id=native_id,
                title=row["title"],
                cwd=row["cwd"],
                started_at=float(row["started_at"]),
                last_active=last_active,
                messages=tuple(messages_by_session[source_session_id]),
                native_path=row["native_path"],
                native_status=row["native_status"] or "active",
                native_cursor=row["last_native_cursor"],
                native_hash=row["last_native_hash"],
                parser_version=int(row["parser_version"] or 1),
                origin_kind=OriginKind.NATIVE,
                origin_bridge_id=None,
                git_branch=row["git_branch"],
            )
            sources.append(
                SidebarSource(
                    source_session_id=source_session_id,
                    projection=projection,
                    git_root=row["git_repo_root"],
                    git_head=None,
                    worktree_id=None,
                    automation_only=bool(row["automation_only"]),
                    subagent_only=bool(row["subagent_only"]),
                )
            )

        profile_has_more = False
        with self._native_hermes_databases() as databases:
            for profile, profile_db, owned in databases:
                if not owned:
                    continue
                profile_sources, more = self._list_profile_sidebar_sources(
                    profile_db,
                    profile=profile,
                    after=cutoff,
                    limit=limit,
                    cursor=normalized_cursor,
                )
                sources.extend(profile_sources)
                profile_has_more = profile_has_more or more

        identities = [source.source_session_id for source in sources]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate native Hermes session identity across profiles")
        sources.sort(
            key=lambda source: (
                -source.projection.last_active,
                source.source_session_id,
            )
        )
        combined_has_more = len(sources) > limit
        sources = sources[:limit]
        has_more = len(rows) > limit or profile_has_more or combined_has_more
        next_cursor = (
            (sources[-1].projection.last_active, sources[-1].source_session_id)
            if has_more and sources
            else None
        )
        return SidebarSourcePage(
            sources,
            has_more=has_more,
            next_cursor=next_cursor,
        )

    def _list_profile_sidebar_sources(
        self,
        profile_db: SessionDB,
        *,
        profile: str,
        after: float,
        limit: int,
        cursor: SidebarCandidateCursor | None,
    ) -> tuple[list[SidebarSource], bool]:
        if not self._profile_catalog_compatible(profile_db):
            return [], False
        cursor_clause = ""
        params: dict[str, Any] = {"after": after, "query_limit": limit + 1}
        if cursor is not None:
            cursor_clause = """AND (
                candidate.last_active < :cursor_activity
                OR (
                    candidate.last_active = :cursor_activity
                    AND candidate.session_id > :cursor_session_id
                )
            )"""
            params.update(
                cursor_activity=cursor[0], cursor_session_id=cursor[1]
            )
        with self.db._lock:
            root_conn = self.db._conn
            assert root_conn is not None
            blocked = {
                row[0]
                for row in root_conn.execute(
                    """SELECT source_session_id FROM session_sidebar_jobs
                       UNION SELECT source_session_id FROM session_sidebar_exclusions"""
                ).fetchall()
            }
        with profile_db._lock:
            conn = profile_db._conn
            assert conn is not None
            rows = conn.execute(
                f"""WITH candidate AS (
                       SELECT s.id AS session_id, s.source, s.model_config,
                              s.title, s.cwd, s.started_at, s.git_branch,
                              s.git_repo_root,
                              COALESCE(
                                  (SELECT MAX(message.timestamp)
                                     FROM messages AS message
                                    WHERE message.session_id = s.id
                                      AND (message.active = 1 OR message.compacted = 1)),
                                  s.started_at
                              ) AS last_active,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END
                                  AS automation_only,
                              CASE
                                  WHEN s.source IN ('subagent', 'tool') THEN 1
                                  WHEN json_extract(COALESCE(s.model_config, '{{}}'),
                                                    '$._delegate_from') IS NOT NULL THEN 1
                                  ELSE 0
                              END AS subagent_only
                         FROM sessions AS s
                         LEFT JOIN external_sessions AS e ON e.session_id = s.id
                        WHERE e.session_id IS NULL
                          AND s.id NOT LIKE 'claude:%'
                          AND s.id NOT LIKE 'codex:%'
                          AND NOT EXISTS (
                              SELECT 1 FROM session_links AS incoming_link
                               WHERE incoming_link.to_session_id = s.id
                          )
                   )
                   SELECT * FROM candidate
                    WHERE last_active IS NOT NULL AND last_active >= :after
                      {cursor_clause}
                    ORDER BY last_active DESC, session_id
                    LIMIT :query_limit""",
                params,
            ).fetchall()
            rows = [row for row in rows if row["session_id"] not in blocked]
            page_rows = rows[:limit]
            messages: dict[str, list[ProjectedMessage]] = {
                row["session_id"]: [] for row in page_rows
            }
            for session_id in messages:
                message_rows = conn.execute(
                    """SELECT id, role, content, timestamp FROM messages
                        WHERE session_id = ? AND role = 'user'
                          AND (active = 1 OR compacted = 1)
                        ORDER BY timestamp, id""",
                    (session_id,),
                ).fetchall()
                for message in message_rows:
                    message_id = int(message["id"])
                    decoded = profile_db._decode_content(message["content"])
                    messages[session_id].append(
                        ProjectedMessage(
                            native_event_id=f"hermes-message:{message_id}",
                            ordinal=message_id,
                            role=message["role"],
                            content=decoded if isinstance(decoded, str) else None,
                            timestamp=float(message["timestamp"]),
                        )
                    )
        sources = [
            SidebarSource(
                source_session_id=row["session_id"],
                projection=SessionProjection(
                    provider=Provider.HERMES,
                    native_id=row["session_id"],
                    title=row["title"],
                    cwd=row["cwd"],
                    started_at=float(row["started_at"]),
                    last_active=float(row["last_active"]),
                    messages=tuple(messages[row["session_id"]]),
                    native_status="active",
                    origin_kind=OriginKind.NATIVE,
                    git_branch=row["git_branch"],
                ),
                git_root=row["git_repo_root"],
                git_head=None,
                worktree_id=None,
                automation_only=bool(row["automation_only"]),
                subagent_only=bool(row["subagent_only"]),
            )
            for row in page_rows
        ]
        return sources, len(rows) > limit

    def get_session_launch_metadata(
        self, session_id: str
    ) -> dict[str, str | None] | None:
        normalized_session_id = _nonempty_text(session_id, "session ID")
        with self._native_hermes_databases() as databases:
            for profile, database, owned in databases:
                if owned and not self._profile_catalog_compatible(database):
                    continue
                with database._lock:
                    conn = database._conn
                    assert conn is not None
                    row = conn.execute(
                        "SELECT source, title, cwd FROM sessions WHERE id = ?",
                        (normalized_session_id,),
                    ).fetchone()
                if row is None:
                    continue
                if row["source"] == _PROFILE_SHADOW_SOURCE:
                    continue
                metadata = {"title": row["title"], "cwd": row["cwd"]}
                if owned:
                    metadata["profile"] = profile
                if any(
                    value is not None and not isinstance(value, str)
                    for value in metadata.values()
                ):
                    raise ValueError("invalid session launch metadata")
                return metadata
        return None

    def get_native_session_snapshot(
        self, session_id: str
    ) -> dict[str, str] | None:
        """Return a stable snapshot identity for a native Hermes session.

        External harness sessions already carry provider cursors and hashes in
        ``external_sessions``. Native Hermes rows do not, so continuation uses
        this transactionally read digest instead of pretending they are an
        external provider session.
        """

        normalized_session_id = _nonempty_text(session_id, "session ID")
        with self._native_hermes_databases() as databases:
            matches: list[tuple[str, SessionDB, Mapping[str, Any], list[Mapping[str, Any]]]] = []
            for profile, database, _owned in databases:
                if database is not self.db and not self._profile_catalog_compatible(database):
                    continue
                with database._lock:
                    conn = database._conn
                    assert conn is not None
                    session_row = conn.execute(
                """SELECT s.id, s.source, s.model, s.title, s.started_at,
                          s.ended_at, s.end_reason, s.message_count,
                          s.tool_call_count, s.cwd, s.git_branch,
                          s.git_repo_root, s.rewind_count, s.archived,
                          e.session_id AS external_session_id
                     FROM sessions AS s
                     LEFT JOIN external_sessions AS e ON e.session_id = s.id
                    WHERE s.id = ?""",
                (normalized_session_id,),
                    ).fetchone()
                    if session_row is None:
                        continue
                    if session_row["source"] == _PROFILE_SHADOW_SOURCE:
                        continue
                    message_rows = conn.execute(
                """SELECT id, role, content, tool_call_id, tool_calls,
                          tool_name, timestamp, finish_reason, reasoning,
                          reasoning_details, codex_reasoning_items,
                          reasoning_content, codex_message_items, active,
                          compacted
                     FROM messages
                    WHERE session_id = ?
                    ORDER BY id""",
                (normalized_session_id,),
                    ).fetchall()
                matches.append((profile, database, session_row, message_rows))
            if not matches:
                raise KeyError(normalized_session_id)
            if len(matches) != 1:
                raise ValueError("duplicate native Hermes session identity across profiles")
            profile, database, session_row, message_rows = matches[0]
            if session_row["external_session_id"] is not None:
                return None

            identity = _native_session_snapshot_identity(
                dict(session_row),
                [dict(row) for row in message_rows],
                decode_content=database._decode_content,
            )
            return {
                "session_id": normalized_session_id,
                "provider": Provider.HERMES.value,
                "profile": profile,
                **identity,
            }

    def find_external_session_by_origin_bridge(
        self,
        bridge_id: str,
        provider: Provider,
    ) -> dict[str, Any] | None:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        normalized_provider = _external_provider(provider)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT * FROM external_sessions
                   WHERE origin_bridge_id = ? AND provider = ?
                     AND origin_kind IN (?, ?)
                   ORDER BY session_id
                   LIMIT 2""",
                (
                    normalized_bridge_id,
                    normalized_provider.value,
                    OriginKind.BRIDGE_PLACEHOLDER.value,
                    OriginKind.BRIDGE_CONTINUATION.value,
                ),
            ).fetchall()
        if len(rows) > 1:
            raise ValueError(
                "duplicate bridge provenance for provider and origin bridge ID"
            )
        return dict(rows[0]) if rows else None

    def get_bridge_summaries(
        self, session_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(session_ids))
        if not unique_ids:
            return {}

        summaries: dict[str, dict[str, Any]] = {}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            for start in range(0, len(unique_ids), 400):
                batch = unique_ids[start : start + 400]
                placeholders = ",".join("?" for _ in batch)
                session_rows = conn.execute(
                    f"""SELECT s.id, e.provider, e.native_id, e.origin_kind,
                               e.sync_error
                        FROM sessions AS s
                        LEFT JOIN external_sessions AS e ON e.session_id = s.id
                        WHERE s.id IN ({placeholders})""",
                    batch,
                ).fetchall()
                job_rows = conn.execute(
                    f"""SELECT source_session_id, state
                        FROM session_mirror_jobs
                        WHERE source_session_id IN ({placeholders})""",
                    batch,
                ).fetchall()
                link_rows = conn.execute(
                    f"""SELECT * FROM session_links
                        WHERE from_session_id IN ({placeholders})
                           OR to_session_id IN ({placeholders})""",
                    [*batch, *batch],
                ).fetchall()
                sidebar_rows = conn.execute(
                    f"""SELECT source_session_id, state, lease_expires_at,
                               codex_thread_id, error_code
                        FROM session_sidebar_jobs
                        WHERE source_session_id IN ({placeholders})""",
                    batch,
                ).fetchall()

                jobs_by_session: dict[str, list[str]] = {}
                for row in job_rows:
                    jobs_by_session.setdefault(row["source_session_id"], []).append(
                        row["state"]
                    )
                links_by_session: dict[str, list[dict[str, Any]]] = {}
                for row in link_rows:
                    link = dict(row)
                    links_by_session.setdefault(row["from_session_id"], []).append(link)
                    if row["to_session_id"] != row["from_session_id"]:
                        links_by_session.setdefault(row["to_session_id"], []).append(
                            link
                        )
                sidebar_by_session: dict[str, dict[str, Any]] = {}
                sidebar_now: float | None = None
                for row in sidebar_rows:
                    session_id = row["source_session_id"]
                    if session_id in sidebar_by_session:
                        raise ValueError("duplicate sidebar summary source identity")
                    public_state = PUBLIC_SIDEBAR_STATE.get(row["state"])
                    if public_state is None:
                        raise ValueError("invalid sidebar summary state")
                    error_code = row["error_code"]
                    thread_id = None
                    stale = False
                    if row["state"] == SidebarJobState.LEASED.value:
                        lease_expires_at = row["lease_expires_at"]
                        if (
                            not isinstance(lease_expires_at, (int, float))
                            or isinstance(lease_expires_at, bool)
                            or not math.isfinite(float(lease_expires_at))
                        ):
                            public_state = "failed"
                            error_code = "catalog_metadata_invalid"
                            stale = True
                        else:
                            if sidebar_now is None:
                                sidebar_now = _finite_number(
                                    self._clock(), "store clock"
                                )
                            stale = float(lease_expires_at) <= sidebar_now
                            if error_code is not None:
                                public_state = "failed"
                                error_code = "catalog_metadata_invalid"
                    elif row["state"] == SidebarJobState.VISIBLE.value:
                        visible_thread_id = _public_codex_thread_id(
                            row["codex_thread_id"]
                        )
                        if visible_thread_id is None or error_code is not None:
                            public_state = "failed"
                            error_code = "catalog_metadata_invalid"
                        else:
                            thread_id = visible_thread_id
                    sidebar_by_session[session_id] = {
                        "bridge_sidebar_state": public_state,
                        "bridge_sidebar_codex_thread_id": thread_id,
                        "bridge_sidebar_error": error_code,
                        "bridge_sidebar_stale": stale,
                    }

                for row in session_rows:
                    session_id = row["id"]
                    if row["provider"] is None:
                        summaries[session_id] = {
                            "bridge_provider": Provider.HERMES.value,
                            "bridge_mirror_state": None,
                            **sidebar_by_session.get(session_id, {}),
                        }
                        continue
                    links = links_by_session.get(session_id, [])
                    summaries[session_id] = {
                        "bridge_provider": row["provider"],
                        "bridge_native_id": row["native_id"],
                        "bridge_origin_kind": row["origin_kind"],
                        "bridge_mirror_state": _mirror_state(
                            jobs_by_session.get(session_id, []), links
                        ),
                        "bridge_sync_error": row["sync_error"],
                        "bridge_links": [dict(link) for link in links],
                        **sidebar_by_session.get(session_id, {}),
                    }
        return summaries

    def enqueue_sidebar_job(
        self,
        candidate: SidebarCandidate,
        *,
        worktree_snapshot: WorktreeSnapshot | None = None,
    ) -> dict[str, Any]:
        from .sidebar import (
            SidebarCandidate,
            sidebar_bridge_id,
            sidebar_idempotency_key,
        )

        if not isinstance(candidate, SidebarCandidate):
            raise ValueError("sidebar candidate is malformed")
        state_key = _sidebar_delivery_state_key(candidate.source_session_id)
        state_value_json = _encode_sidebar_delivery_candidate(candidate)
        worktree_key: str | None = None
        worktree_value_json: str | None = None
        if worktree_snapshot is not None:
            worktree_key = _worktree_snapshot_state_key(candidate.source_session_id)
            worktree_value_json = _encode_worktree_snapshot(
                candidate.source_session_id,
                candidate,
                worktree_snapshot,
            )
        idempotency_key = sidebar_idempotency_key(candidate.source_session_id)
        expected_provider = (
            Provider.CLAUDE
            if candidate.source_session_id.startswith("claude:")
            else Provider.HERMES
        )
        if candidate.provider is not expected_provider:
            raise ValueError("sidebar candidate provider does not match source")
        expected_bridge_id = sidebar_bridge_id(candidate.source_session_id)
        if candidate.bridge_id != expected_bridge_id:
            raise ValueError("sidebar candidate bridge ID does not match source")
        eligible_at = _finite_number(candidate.eligible_at, "sidebar eligible_at")
        now = _finite_number(self._clock(), "store clock")
        job_id = f"sidebar-job:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"
        launch_metadata = self.get_session_launch_metadata(candidate.source_session_id)
        profile = (
            launch_metadata.get("profile")
            if isinstance(launch_metadata, Mapping)
            else None
        )

        def _write(conn):
            source_row = conn.execute(
                "SELECT source, model_config FROM sessions WHERE id = ?",
                (candidate.source_session_id,),
            ).fetchone()
            if source_row is None:
                if not isinstance(profile, str) or not profile.strip():
                    raise KeyError(candidate.source_session_id)
                model_config = json.dumps(
                    {"_session_bridge_profile": profile.strip()},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    """INSERT INTO sessions (
                           id, source, model_config, started_at, title, cwd
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        candidate.source_session_id,
                        _PROFILE_SHADOW_SOURCE,
                        model_config,
                        eligible_at,
                        launch_metadata.get("title") if launch_metadata else None,
                        candidate.cwd,
                    ),
                )
            elif source_row["source"] == _PROFILE_SHADOW_SOURCE:
                try:
                    shadow_config = json.loads(source_row["model_config"] or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid profile shadow identity") from exc
                if shadow_config.get("_session_bridge_profile") != profile:
                    raise ValueError("conflicting profile shadow identity")
            insert = conn.execute(
                """INSERT OR IGNORE INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, eligible_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (
                    job_id,
                    idempotency_key,
                    candidate.source_session_id,
                    expected_bridge_id,
                    SidebarJobState.PENDING.value,
                    eligible_at,
                    eligible_at,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM session_sidebar_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None or (
                row["id"] != job_id
                or row["source_session_id"] != candidate.source_session_id
                or row["bridge_id"] != expected_bridge_id
            ):
                raise ValueError("conflicting sidebar job identity")
            if worktree_key is not None and worktree_value_json is not None:
                worktree_row = conn.execute(
                    "SELECT value_json FROM session_bridge_state WHERE key = ?",
                    (worktree_key,),
                ).fetchone()
                if worktree_row is None:
                    conn.execute(
                        """INSERT INTO session_bridge_state (key, value_json, updated_at)
                           VALUES (?, ?, ?)""",
                        (worktree_key, worktree_value_json, now),
                    )
                elif worktree_row["value_json"] != worktree_value_json:
                    raise ValueError("conflicting worktree snapshot identity")
            state_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if state_row is None:
                conn.execute(
                    """INSERT INTO session_bridge_state (key, value_json, updated_at)
                       VALUES (?, ?, ?)""",
                    (state_key, state_value_json, now),
                )
            elif state_row["value_json"] != state_value_json:
                raise ValueError("conflicting sidebar delivery candidate")
            return {**dict(row), "created": insert.rowcount == 1}

        return self.db._execute_write(_write)

    def get_worktree_snapshot(self, source_session_id: str) -> WorktreeSnapshot | None:
        state_key = _worktree_snapshot_state_key(source_session_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
        if row is None:
            return None
        return _decode_worktree_snapshot(row["value_json"], source_session_id)

    def claim_sidebar_jobs(
        self,
        *,
        now: float,
        limit: int,
        lease_seconds: int = _SIDEBAR_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        claim_time = _finite_number(now, "now")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 10
        ):
            raise ValueError("sidebar claim limit must be between 1 and 10")
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds != _SIDEBAR_LEASE_SECONDS
        ):
            raise ValueError("sidebar lease duration must be exactly 300 seconds")

        def _write(conn):
            due = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE state IN (?, ?) AND next_attempt_at <= ?
                   ORDER BY CASE WHEN state = ? THEN 0 ELSE 1 END,
                            eligible_at, id
                   LIMIT ?""",
                (
                    SidebarJobState.PENDING.value,
                    SidebarJobState.RETRY.value,
                    claim_time,
                    SidebarJobState.RETRY.value,
                    _SIDEBAR_CLAIM_SCAN_LIMIT,
                ),
            ).fetchall()
            conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, error_code = NULL, updated_at = ?
                   WHERE state = ? AND lease_expires_at <= ?""",
                (
                    SidebarJobState.RETRY.value,
                    claim_time,
                    claim_time,
                    SidebarJobState.LEASED.value,
                    claim_time,
                ),
            )
            claimed: list[dict[str, Any]] = []
            for raw_row in due:
                if len(claimed) >= limit:
                    break
                row = dict(raw_row)
                try:
                    provider = _validated_sidebar_job_provider(row)
                except ValueError:
                    conn.execute(
                        """UPDATE session_sidebar_jobs
                           SET state = ?, error_code = ?, updated_at = ?
                           WHERE id = ? AND state = ?""",
                        (
                            SidebarJobState.FAILED.value,
                            "provider_mismatch",
                            claim_time,
                            row["id"],
                            row["state"],
                        ),
                    )
                    continue
                lease_token, lease_digest = self._new_sidebar_lease(conn)
                lease_expires_at = claim_time + lease_seconds
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, lease_digest = ?, lease_expires_at = ?,
                           error_code = NULL, updated_at = ?
                       WHERE id = ? AND state = ? AND attempts = ?""",
                    (
                        SidebarJobState.LEASED.value,
                        lease_digest,
                        lease_expires_at,
                        claim_time,
                        row["id"],
                        row["state"],
                        row["attempts"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale sidebar job claim")
                claimed_row = dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                )
                claimed_row["provider"] = provider.value
                claimed_row["lease_token"] = lease_token
                claimed.append(claimed_row)
            return claimed

        return self.db._execute_write(_write)

    def bind_sidebar_thread(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        now: float,
    ) -> dict[str, Any]:
        """Durably reserve one native task identity before rename or commit."""

        token_digest = _sidebar_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        bind_time = _finite_number(now, "now")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= bind_time:
                _recover_one_expired_sidebar_lease(conn, job, now=bind_time)
                return dict(job), True
            existing = job["codex_thread_id"]
            if existing is not None:
                if existing == thread_id:
                    return dict(job), False
                raise ValueError("conflicting Codex thread identity")
            conflict = conn.execute(
                """SELECT id FROM session_sidebar_jobs
                   WHERE codex_thread_id = ? AND id != ?""",
                (thread_id, job["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError("conflicting Codex thread identity")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET codex_thread_id = ?, updated_at = ?
                   WHERE id = ? AND state = ? AND codex_thread_id IS NULL""",
                (
                    thread_id,
                    bind_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar thread binding")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def commit_sidebar_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        now: float,
    ) -> dict[str, Any]:
        token_digest = _sidebar_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        commit_time = _finite_number(now, "now")

        def _write(conn):
            job, matched_completion = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=True,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if matched_completion:
                if (
                    job["state"] == SidebarJobState.VISIBLE.value
                    and job["codex_thread_id"] == thread_id
                ):
                    return dict(job), False
                raise ValueError("conflicting sidebar completion replay")
            if job["state"] != SidebarJobState.LEASED.value:
                raise ValueError("sidebar job is not leased")
            if float(job["lease_expires_at"]) <= commit_time:
                _recover_one_expired_sidebar_lease(conn, job, now=commit_time)
                return dict(job), True
            conflict = conn.execute(
                """SELECT id FROM session_sidebar_jobs
                   WHERE codex_thread_id = ? AND id != ?""",
                (thread_id, job["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError("conflicting Codex thread identity")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, completion_digest = lease_digest,
                       lease_digest = NULL, lease_expires_at = NULL,
                       codex_thread_id = ?, error_code = NULL,
                       visible_at = ?, updated_at = ?
                   WHERE id = ? AND state = ?""",
                (
                    SidebarJobState.VISIBLE.value,
                    thread_id,
                    commit_time,
                    commit_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job commit")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def commit_sidebar_job_with_lineage(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        source_session_id: str,
        bridge_id: str,
        now: float,
    ) -> dict[str, Any]:
        """Atomically bind verified lineage and commit one sidebar lease."""

        token_digest = _sidebar_lease_digest(lease_token)
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")
        source_id = _exact_nonempty_text(
            source_session_id, "sidebar source session ID"
        )
        normalized_bridge_id = _exact_nonempty_text(bridge_id, "sidebar bridge ID")
        commit_time = _finite_number(now, "now")

        def _write(conn):
            job, matched_completion = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=True,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if (
                job["source_session_id"] != source_id
                or job["bridge_id"] != normalized_bridge_id
            ):
                raise ValueError("source_identity_mismatch")
            if matched_completion:
                if (
                    job["state"] != SidebarJobState.VISIBLE.value
                    or job["codex_thread_id"] != thread_id
                ):
                    raise ValueError("conflicting sidebar completion replay")
                _ensure_sidebar_lineage_row(
                    conn,
                    source_session_id=source_id,
                    bridge_id=normalized_bridge_id,
                    codex_thread_id=thread_id,
                    created_at=commit_time,
                )
                return dict(job), False
            if job["state"] != SidebarJobState.LEASED.value:
                raise ValueError("sidebar job is not leased")
            if float(job["lease_expires_at"]) <= commit_time:
                _recover_one_expired_sidebar_lease(conn, job, now=commit_time)
                return dict(job), True
            conflict = conn.execute(
                """SELECT id FROM session_sidebar_jobs
                   WHERE codex_thread_id = ? AND id != ?""",
                (thread_id, job["id"]),
            ).fetchone()
            if conflict is not None:
                raise ValueError("conflicting Codex thread identity")
            _ensure_sidebar_lineage_row(
                conn,
                source_session_id=source_id,
                bridge_id=normalized_bridge_id,
                codex_thread_id=thread_id,
                created_at=commit_time,
            )
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, completion_digest = lease_digest,
                       lease_digest = NULL, lease_expires_at = NULL,
                       codex_thread_id = ?, error_code = NULL,
                       visible_at = ?, updated_at = ?
                   WHERE id = ? AND state = ?""",
                (
                    SidebarJobState.VISIBLE.value,
                    thread_id,
                    commit_time,
                    commit_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job commit")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def lookup_sidebar_job_by_lease(self, lease_token: str) -> dict[str, Any]:
        token_digest = _sidebar_lease_digest(lease_token)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            job, matched_completion = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=True,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            state = job["state"]
            if matched_completion:
                if state != SidebarJobState.VISIBLE.value:
                    raise ValueError("invalid sidebar completion state")
            elif state != SidebarJobState.LEASED.value:
                raise ValueError("sidebar job is not leased")
            return {
                "source_session_id": job["source_session_id"],
                "bridge_id": job["bridge_id"],
                "state": state,
                "codex_thread_id": job["codex_thread_id"],
            }

    def fail_sidebar_job(
        self,
        *,
        lease_token: str,
        error_code: str,
        now: float,
    ) -> dict[str, Any]:
        if (
            type(error_code) is not str
            or error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
        ):
            raise ValueError("sidebar error code is not in the fixed allowlist")
        token_digest = _sidebar_lease_digest(lease_token)
        failure_time = _finite_number(now, "now")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= failure_time:
                _recover_one_expired_sidebar_lease(conn, job, now=failure_time)
                return dict(job), True
            if error_code == "broker_time_budget":
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                           lease_expires_at = NULL, error_code = ?, updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (
                        SidebarJobState.PENDING.value,
                        failure_time,
                        error_code,
                        failure_time,
                        job["id"],
                        SidebarJobState.LEASED.value,
                    ),
                )
            elif error_code in SIDEBAR_FATAL_ERRORS:
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, lease_digest = NULL,
                           lease_expires_at = NULL, error_code = ?, updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (
                        SidebarJobState.FAILED.value,
                        error_code,
                        failure_time,
                        job["id"],
                        SidebarJobState.LEASED.value,
                    ),
                )
            else:
                attempts = int(job["attempts"]) + 1
                base_delay = _SIDEBAR_RETRY_DELAYS_SECONDS[attempts - 1]
                jitter_bound = min(30.0, base_delay * 0.1)
                jitter = _finite_number(
                    self._sidebar_jitter(jitter_bound),
                    "sidebar retry jitter",
                )
                if not 0.0 <= jitter <= jitter_bound:
                    raise ValueError("sidebar retry jitter is outside its bound")
                next_attempt_at = failure_time + base_delay + jitter
                state = (
                    SidebarJobState.FAILED
                    if attempts >= len(_SIDEBAR_RETRY_DELAYS_SECONDS)
                    else SidebarJobState.RETRY
                )
                cursor = conn.execute(
                    """UPDATE session_sidebar_jobs
                       SET state = ?, attempts = ?, next_attempt_at = ?,
                           lease_digest = NULL, lease_expires_at = NULL,
                           error_code = ?, updated_at = ?
                       WHERE id = ? AND state = ?""",
                    (
                        state.value,
                        attempts,
                        next_attempt_at,
                        error_code,
                        failure_time,
                        job["id"],
                        SidebarJobState.LEASED.value,
                    ),
                )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job failure")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def release_sidebar_job(
        self,
        *,
        lease_token: str,
        now: float,
    ) -> dict[str, Any]:
        token_digest = _sidebar_lease_digest(lease_token)
        release_time = _finite_number(now, "now")

        def _write(conn):
            job, _ = _find_sidebar_job_by_digest(
                conn,
                token_digest,
                allow_completion=False,
            )
            if job is None:
                raise ValueError("invalid sidebar lease token")
            if float(job["lease_expires_at"]) <= release_time:
                _recover_one_expired_sidebar_lease(conn, job, now=release_time)
                return dict(job), True
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, next_attempt_at = ?, lease_digest = NULL,
                       lease_expires_at = NULL, error_code = NULL, updated_at = ?
                   WHERE id = ? AND state = ?""",
                (
                    SidebarJobState.PENDING.value,
                    release_time,
                    release_time,
                    job["id"],
                    SidebarJobState.LEASED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("stale sidebar job release")
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                ),
                False,
            )

        result, expired = self.db._execute_write(_write)
        if expired:
            raise ValueError("sidebar lease has expired")
        return result

    def retry_failed_sidebar_job(
        self,
        *,
        source_session_id: str,
        expected_error_code: str,
        now: float,
    ) -> dict[str, Any]:
        from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

        source_id = _exact_nonempty_text(
            source_session_id,
            "sidebar source session ID",
        )
        error_code = _exact_nonempty_text(
            expected_error_code,
            "expected sidebar failure",
        )
        if error_code not in SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS:
            raise ValueError("expected sidebar failure is not in the fixed allowlist")
        retry_time = _finite_number(now, "now")
        idempotency_key = sidebar_idempotency_key(source_id)
        bridge_id = sidebar_bridge_id(source_id)
        job_id = f"sidebar-job:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"

        def _write(conn):
            jobs = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE source_session_id = ? ORDER BY id LIMIT 2""",
                (source_id,),
            ).fetchall()
            job = jobs[0] if len(jobs) == 1 else None
            if (
                job is None
                or job["id"] != job_id
                or job["idempotency_key"] != idempotency_key
                or job["bridge_id"] != bridge_id
                or job["state"] != SidebarJobState.FAILED.value
                or job["error_code"] != error_code
                or job["codex_thread_id"] is not None
                or job["completion_digest"] is not None
                or job["visible_at"] is not None
            ):
                raise ValueError("expected sidebar failure does not match")
            cursor = conn.execute(
                """UPDATE session_sidebar_jobs
                   SET state = ?, attempts = 0, next_attempt_at = ?,
                       lease_digest = NULL, lease_expires_at = NULL,
                       error_code = NULL, updated_at = ?
                   WHERE id = ? AND idempotency_key = ? AND bridge_id = ?
                     AND source_session_id = ? AND state = ? AND error_code = ?
                     AND codex_thread_id IS NULL AND completion_digest IS NULL
                     AND visible_at IS NULL""",
                (
                    SidebarJobState.PENDING.value,
                    retry_time,
                    retry_time,
                    job["id"],
                    idempotency_key,
                    bridge_id,
                    source_id,
                    SidebarJobState.FAILED.value,
                    error_code,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("expected sidebar failure does not match")
            return dict(
                conn.execute(
                    "SELECT * FROM session_sidebar_jobs WHERE id = ?",
                    (job["id"],),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def sidebar_job_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in SidebarJobState}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT state, COUNT(*) AS job_count
                   FROM session_sidebar_jobs GROUP BY state"""
            ).fetchall()
        for row in rows:
            counts[row["state"]] = int(row["job_count"])
        return counts

    def record_sidebar_broker_heartbeat(self, *, now: float) -> None:
        heartbeat = _finite_number(now, "sidebar broker heartbeat")
        updated_at = _finite_number(self._clock(), "current time")

        def _write(conn) -> None:
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (_SIDEBAR_BROKER_HEARTBEAT_STATE_KEY,),
            ).fetchone()
            persisted = heartbeat
            if row is not None:
                try:
                    existing = json.loads(row["value_json"])
                except (TypeError, ValueError):
                    existing = None
                if isinstance(existing, dict) and "at" in existing:
                    try:
                        existing_at = _finite_number(
                            existing["at"], "persisted sidebar broker heartbeat"
                        )
                    except (TypeError, ValueError):
                        pass
                    else:
                        persisted = max(existing_at, heartbeat)

            value_json = json.dumps(
                {"at": persisted},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (_SIDEBAR_BROKER_HEARTBEAT_STATE_KEY, value_json, updated_at),
            )

        self.db._execute_write(_write)

    def sidebar_delivery_status(self, *, now: float | None = None) -> dict[str, Any]:
        status_time = _finite_number(self._clock() if now is None else now, "now")
        counts = self.sidebar_job_counts()
        counts["sidebar_excluded"] = self.sidebar_exclusion_counts()["total"]
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            provider_rows = conn.execute(
                """SELECT CASE
                              WHEN e.provider = ? THEN ?
                              WHEN e.session_id IS NULL THEN ?
                              ELSE 'invalid'
                          END AS provider,
                          COUNT(*) AS job_count
                     FROM session_sidebar_jobs AS job
                     JOIN sessions AS s ON s.id = job.source_session_id
                     LEFT JOIN external_sessions AS e ON e.session_id = s.id
                    GROUP BY provider""",
                (
                    Provider.CLAUDE.value,
                    Provider.CLAUDE.value,
                    Provider.HERMES.value,
                ),
            ).fetchall()
            oldest = conn.execute(
                """SELECT MIN(
                              CASE WHEN state = ? THEN updated_at ELSE eligible_at END
                          ) AS actionable_at
                     FROM session_sidebar_jobs
                    WHERE state IN (?, ?, ?)""",
                (
                    SidebarJobState.LEASED.value,
                    SidebarJobState.PENDING.value,
                    SidebarJobState.RETRY.value,
                    SidebarJobState.LEASED.value,
                ),
            ).fetchone()
            last_visible = conn.execute(
                """SELECT codex_thread_id
                     FROM session_sidebar_jobs
                    WHERE state = ?
                    ORDER BY visible_at DESC, id DESC LIMIT 1""",
                (SidebarJobState.VISIBLE.value,),
            ).fetchone()
            error_rows = conn.execute(
                """SELECT error_code
                     FROM session_sidebar_jobs
                    WHERE error_code IS NOT NULL
                    ORDER BY updated_at DESC, id DESC LIMIT 10"""
            ).fetchall()
            latency_rows = conn.execute(
                """SELECT visible_at - eligible_at AS latency
                     FROM session_sidebar_jobs
                    WHERE state = ? AND visible_at IS NOT NULL
                    ORDER BY visible_at DESC, id DESC
                    LIMIT ?""",
                (SidebarJobState.VISIBLE.value, _SIDEBAR_LATENCY_SAMPLE_LIMIT),
            ).fetchall()

        eligible_by_provider = {
            Provider.CLAUDE.value: 0,
            Provider.HERMES.value: 0,
        }
        for row in provider_rows:
            if row["provider"] in eligible_by_provider:
                eligible_by_provider[row["provider"]] = int(row["job_count"])
        oldest_at = oldest["actionable_at"] if oldest is not None else None
        oldest_age = (
            max(0.0, status_time - float(oldest_at))
            if oldest_at is not None
            else None
        )
        heartbeat = self.get_state("session-bridge:sidebar:broker-heartbeat")
        heartbeat_at = heartbeat.get("at") if isinstance(heartbeat, Mapping) else None
        if not isinstance(heartbeat_at, (int, float)) or isinstance(heartbeat_at, bool):
            heartbeat_at = None
        allowed_codes = SIDEBAR_RETRYABLE_ERRORS | SIDEBAR_FATAL_ERRORS
        recent_codes: list[str] = []
        for row in error_rows:
            code = row["error_code"]
            if code in allowed_codes and code not in recent_codes:
                recent_codes.append(code)
        latencies = sorted(
            max(0.0, float(row["latency"])) for row in latency_rows
        )
        return {
            "eligible_by_provider": eligible_by_provider,
            "counts": counts,
            "oldest_pending_age_seconds": oldest_age,
            "last_heartbeat_at": float(heartbeat_at) if heartbeat_at is not None else None,
            "last_visible_task_id": (
                last_visible["codex_thread_id"] if last_visible is not None else None
            ),
            "recent_error_codes": recent_codes,
            "delivery_latency_seconds": {
                "p50": _nearest_rank_percentile(latencies, 0.50),
                "p95": _nearest_rank_percentile(latencies, 0.95),
                "p99": _nearest_rank_percentile(latencies, 0.99),
            },
        }

    def get_sidebar_job_for_source(
        self,
        source_session_id: str,
    ) -> dict[str, Any] | None:
        from .sidebar import sidebar_idempotency_key

        sidebar_idempotency_key(source_session_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT * FROM session_sidebar_jobs
                   WHERE source_session_id = ?""",
                (source_session_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def get_sidebar_candidate_for_delivery(
        self,
        source_session_id: str,
    ) -> SidebarCandidate:
        """Read immutable, bounded delivery metadata for an already queued job."""

        source_id = _exact_nonempty_text(
            source_session_id, "sidebar source session ID"
        )
        state_key = _sidebar_delivery_state_key(source_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT job.source_session_id, job.idempotency_key,
                          job.bridge_id, job.eligible_at, state.value_json
                     FROM session_sidebar_jobs AS job
                     LEFT JOIN session_bridge_state AS state ON state.key = ?
                    WHERE job.source_session_id = ?""",
                (state_key, source_id),
            ).fetchone()
        if row is None:
            raise KeyError(source_id)
        if row["value_json"] is None:
            raise ValueError("missing sidebar delivery candidate")
        candidate = _decode_sidebar_delivery_candidate(row["value_json"])
        expected_provider = _validated_sidebar_job_provider(dict(row))
        if (
            candidate.source_session_id != row["source_session_id"]
            or candidate.bridge_id != row["bridge_id"]
            or candidate.provider is not expected_provider
            or candidate.eligible_at != float(row["eligible_at"])
        ):
            raise ValueError("invalid sidebar delivery candidate identity")
        return candidate

    def ensure_sidebar_lineage(
        self,
        *,
        source_session_id: str,
        bridge_id: str,
        codex_thread_id: str,
    ) -> dict[str, Any]:
        """Idempotently bind one verified native Codex task to its source."""

        source_id = _exact_nonempty_text(
            source_session_id, "sidebar source session ID"
        )
        normalized_bridge_id = _exact_nonempty_text(bridge_id, "sidebar bridge ID")
        thread_id = _exact_nonempty_text(codex_thread_id, "Codex thread ID")

        def _write(conn):
            return _ensure_sidebar_lineage_row(
                conn,
                source_session_id=source_id,
                bridge_id=normalized_bridge_id,
                codex_thread_id=thread_id,
                created_at=float(self._clock()),
            )

        return self.db._execute_write(_write)

    def _new_sidebar_lease(self, conn: Any) -> tuple[str, str]:
        token = self._sidebar_token_factory()
        digest = _sidebar_lease_digest(token)
        existing, _ = _find_sidebar_job_by_digest(
            conn,
            digest,
            allow_completion=True,
        )
        if existing is not None:
            raise ValueError("sidebar lease token factory returned a duplicate")
        return token, digest

    def enqueue_mirror_job(
        self,
        source_session_id: str,
        target_provider: Provider,
        *,
        policy_generation: int,
    ) -> dict[str, Any]:
        provider = _external_provider(target_provider)
        if (
            not isinstance(policy_generation, int)
            or isinstance(policy_generation, bool)
            or policy_generation < 0
        ):
            raise ValueError("policy generation must be a non-negative integer")
        idempotency_key = _stable_id(
            "mirror-job",
            source_session_id,
            provider.value,
            str(policy_generation),
        )
        job_id = f"job:{idempotency_key}"
        now = float(self._clock())

        def _write(conn):
            conn.execute(
                """INSERT OR IGNORE INTO session_mirror_jobs (
                   id, idempotency_key, source_session_id, target_provider,
                   state, attempts, next_attempt_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    job_id,
                    idempotency_key,
                    source_session_id,
                    provider.value,
                    MirrorJobState.QUEUED.value,
                    now,
                    now,
                    now,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM session_mirror_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def list_mirror_jobs(
        self,
        states: Sequence[MirrorJobState | str],
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("mirror job list limit must be between 1 and 1000")
        if isinstance(states, (str, bytes)) or not isinstance(states, Sequence):
            raise TypeError("mirror job states must be a sequence")
        normalized_states: list[MirrorJobState] = []
        for state in states:
            try:
                normalized = MirrorJobState(state)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"unknown mirror job state: {state!r}") from exc
            if normalized not in normalized_states:
                normalized_states.append(normalized)
        if not normalized_states:
            return []

        placeholders = ",".join("?" for _ in normalized_states)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT * FROM session_mirror_jobs
                    WHERE state IN ({placeholders})
                    ORDER BY created_at, id
                    LIMIT ?""",
                [*(state.value for state in normalized_states), limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def mirror_job_counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in MirrorJobState}
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT state, COUNT(*) AS job_count
                   FROM session_mirror_jobs
                   GROUP BY state"""
            ).fetchall()
        for row in rows:
            counts[row["state"]] = int(row["job_count"])
        return counts

    def claim_due_jobs(
        self,
        *,
        now: float,
        limit: int,
        policy: Any,
        job_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise ValueError("now must be a finite number")
        from .mirror import claim_due_mirror_jobs

        return claim_due_mirror_jobs(
            self,
            limit=limit,
            policy=policy,
            job_ids=job_ids,
        )

    def claim_due_jobs_with_limits(
        self,
        *,
        now: float,
        limit: int,
        policy: Any,
        job_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        claim_time = _finite_number(now, "now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("claim limit must be a non-negative integer")
        normalized_job_ids = (
            None
            if job_ids is None
            else _bounded_exact_ids(job_ids, label="job_ids", maximum=1000)
        )
        policy_values = _validated_claim_policy(policy)
        if limit == 0 or normalized_job_ids == ():
            return []

        def _write(conn):
            automatic_creation = policy_values["automatic_creation"]
            breaker = _read_breaker_progress(conn)
            if _healthy_breaker_batch_completed(
                breaker,
                stop_after_attempts=policy_values["stop_after_attempts"],
                stop_error_rate=policy_values["stop_error_rate"],
            ):
                breaker = {"attempts": 0, "errors": 0, "pending": 0}
                _write_breaker_progress(conn, breaker, updated_at=claim_time)
            limited_allowed = not _breaker_is_halted(
                breaker,
                stop_after_attempts=policy_values["stop_after_attempts"],
                stop_error_rate=policy_values["stop_error_rate"],
            )
            automatic_allowed = automatic_creation and limited_allowed

            recent = _read_rate_attempts(conn, now=claim_time)
            capacity = min(
                limit,
                max(0, policy_values["creates_per_minute"] - len(recent)),
            )
            if capacity == 0:
                _write_rate_attempts(conn, recent, updated_at=claim_time)
                return []

            scan_limit = max(capacity * 4, capacity + 32)
            scope_clause = ""
            scope_params: list[Any] = []
            if normalized_job_ids is not None:
                placeholders = ",".join("?" for _ in normalized_job_ids)
                scope_clause = f" AND job.id IN ({placeholders})"
                scope_params.extend(normalized_job_ids)
            due = conn.execute(
                f"""SELECT job.* FROM session_mirror_jobs AS job
                   LEFT JOIN session_bridge_state AS authority
                     ON authority.key = ? || job.id
                   WHERE job.state IN (?, ?) AND job.next_attempt_at <= ?
                     {scope_clause}
                   ORDER BY
                     CASE WHEN authority.value_json LIKE ? THEN 0 ELSE 1 END,
                     job.next_attempt_at, job.created_at, job.id
                   LIMIT ?""",
                (
                    _MIRROR_AUTHORITY_STATE_PREFIX,
                    MirrorJobState.QUEUED.value,
                    MirrorJobState.RETRY.value,
                    claim_time,
                    *scope_params,
                    '{"authority":"manual",%',
                    scan_limit,
                ),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            limited_reserved = 0
            for job in due:
                if len(claimed) >= capacity:
                    break
                try:
                    authority = _read_claim_authority(conn, job)
                except KeyError:
                    _terminalize_unclaimable_job(
                        conn,
                        job,
                        now=claim_time,
                        code="authority_missing",
                        detail="mirror authority metadata is missing",
                    )
                    continue
                except ValueError:
                    _terminalize_unclaimable_job(
                        conn,
                        job,
                        now=claim_time,
                        code="authority_invalid",
                        detail="mirror authority metadata is invalid",
                    )
                    continue
                claim_authority = authority["authority"]
                rollout_limited = authority["rollout_limited"]
                is_limited = claim_authority == "automatic" or rollout_limited
                if claim_authority == "automatic":
                    if not automatic_allowed:
                        continue
                if is_limited:
                    if not limited_allowed or limited_reserved:
                        continue
                if claim_authority == "automatic" or authority["require_unmapped"]:
                    if _automatic_claim_denial(conn, job) is not None:
                        code = (
                            "automatic_authority_revoked"
                            if claim_authority == "automatic"
                            else "manual_authority_revoked"
                        )
                        detail = (
                            "automatic mirror authority is no longer valid"
                            if claim_authority == "automatic"
                            else "safe manual mirror authority is no longer valid"
                        )
                        _terminalize_unclaimable_job(
                            conn,
                            job,
                            now=claim_time,
                            code=code,
                            detail=detail,
                        )
                        continue
                cursor = conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, attempts = attempts + 1, updated_at = ?
                       WHERE id = ? AND state = ? AND attempts = ?
                         AND idempotency_key = ?""",
                    (
                        MirrorJobState.RUNNING.value,
                        claim_time,
                        job["id"],
                        job["state"],
                        job["attempts"],
                        job["idempotency_key"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("stale mirror job claim")
                claimed_job = dict(
                    conn.execute(
                        "SELECT * FROM session_mirror_jobs WHERE id = ?",
                        (job["id"],),
                    ).fetchone()
                )
                claimed_job["claim_authority"] = claim_authority
                claimed_job["rollout_limited"] = rollout_limited
                if is_limited:
                    limited_reserved = 1
                    _create_breaker_reservation(
                        conn,
                        claimed_job,
                        updated_at=claim_time,
                    )
                claimed.append(claimed_job)

            if limited_reserved:
                breaker = {
                    "attempts": breaker["attempts"] + limited_reserved,
                    "errors": breaker["errors"],
                    "pending": breaker["pending"] + limited_reserved,
                }
                _write_breaker_progress(conn, breaker, updated_at=claim_time)

            _write_rate_attempts(
                conn,
                [*recent, *([claim_time] * len(claimed))],
                updated_at=claim_time,
            )
            return claimed

        return self.db._execute_write(_write)

    def get_mirror_breaker_progress(self) -> dict[str, int]:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            progress = _read_breaker_progress(conn)
            return {
                "attempts": progress["attempts"],
                "errors": progress["errors"],
            }

    def accumulate_mirror_breaker_progress(
        self,
        *,
        attempts: int,
        errors: int,
        reset: bool = False,
    ) -> dict[str, int]:
        _nonnegative_integer(attempts, "breaker attempts")
        _nonnegative_integer(errors, "breaker errors")
        if errors > attempts:
            raise ValueError("breaker errors cannot exceed attempts")
        if type(reset) is not bool:
            raise ValueError("breaker reset must be a boolean")
        now = _finite_number(self._clock(), "store clock")

        def _write(conn):
            current = _read_breaker_progress(conn)
            if reset and current["pending"]:
                raise ValueError("cannot reset mirror breaker with pending attempts")
            base = (
                {"attempts": 0, "errors": 0, "pending": 0}
                if reset
                else current
            )
            updated = {
                "attempts": base["attempts"] + attempts,
                "errors": base["errors"] + errors,
                "pending": base["pending"],
            }
            if updated["errors"] > updated["attempts"]:
                raise ValueError("breaker errors cannot exceed attempts")
            _write_breaker_progress(conn, updated, updated_at=now)
            return {
                "attempts": updated["attempts"],
                "errors": updated["errors"],
            }

        return self.db._execute_write(_write)

    def complete_job(
        self,
        job_id: str,
        *,
        target_native_id: str,
        target_session_id: str,
        bridge_id: str,
    ) -> None:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        now = float(self._clock())

        def _write(conn):
            job = conn.execute(
                "SELECT * FROM session_mirror_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            expected_target_id = canonical_session_id(
                Provider(job["target_provider"]), target_native_id
            )
            normalized_target_native_id = target_native_id.strip()
            if target_session_id != expected_target_id:
                raise ValueError(
                    "target session ID does not match the mirror job identity"
                )

            target = conn.execute(
                """SELECT s.source, e.provider, e.native_id,
                          e.origin_kind, e.origin_bridge_id
                   FROM sessions AS s
                   JOIN external_sessions AS e ON e.session_id = s.id
                   WHERE s.id = ?""",
                (target_session_id,),
            ).fetchone()
            if target is None or (
                target["source"] != job["target_provider"]
                or target["provider"] != job["target_provider"]
                or target["native_id"] != normalized_target_native_id
            ):
                raise ValueError(
                    "mirror completion requires a matching cataloged target identity"
                )
            if (
                target["origin_kind"]
                not in (
                    OriginKind.BRIDGE_PLACEHOLDER.value,
                    OriginKind.BRIDGE_CONTINUATION.value,
                )
                or target["origin_bridge_id"] != normalized_bridge_id
            ):
                raise ValueError(
                    "mirror completion requires authenticated exact bridge provenance"
                )

            if job["state"] == MirrorJobState.SUCCEEDED.value:
                exact_link = conn.execute(
                    """SELECT 1 FROM session_links
                       WHERE bridge_id = ? AND from_session_id = ?
                         AND to_session_id = ? AND relation = ?""",
                    (
                        normalized_bridge_id,
                        job["source_session_id"],
                        target_session_id,
                        Relation.MIRRORS.value,
                    ),
                ).fetchone()
                if (
                    job["target_native_id"] == normalized_target_native_id
                    and exact_link is not None
                ):
                    return
                raise ValueError("conflicting completion replay for succeeded job")
            if job["state"] != MirrorJobState.RUNNING.value:
                raise ValueError("mirror job must be running before completion")

            conn.execute(
                """UPDATE session_mirror_jobs
                   SET state = ?, target_native_id = ?, error_code = NULL,
                       error_detail = NULL, updated_at = ?
                   WHERE id = ?""",
                (
                    MirrorJobState.SUCCEEDED.value,
                    normalized_target_native_id,
                    now,
                    job_id,
                ),
            )
            self._create_link_row(
                conn,
                SessionLink(
                    id=f"link:{_stable_id('mirror-link', normalized_bridge_id, job['source_session_id'], target_session_id)}",
                    from_session_id=job["source_session_id"],
                    to_session_id=target_session_id,
                    relation=Relation.MIRRORS,
                    bridge_id=normalized_bridge_id,
                    source_cursor=None,
                    source_hash=None,
                    created_at=now,
                ),
            )
            _settle_breaker_reservation(
                conn,
                job,
                error=False,
                updated_at=now,
            )

        self.db._execute_write(_write)

    def retry_job(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
        next_attempt_at: float,
    ) -> None:
        self._set_job_failure(
            job_id,
            state=MirrorJobState.RETRY,
            code=code,
            detail=detail,
            next_attempt_at=float(next_attempt_at),
        )

    def fail_job_manually(
        self,
        job_id: str,
        *,
        code: str,
        detail: str,
    ) -> None:
        self._set_job_failure(
            job_id,
            state=MirrorJobState.MANUAL_FAILURE,
            code=code,
            detail=detail,
            next_attempt_at=None,
        )

    def _set_job_failure(
        self,
        job_id: str,
        *,
        state: MirrorJobState,
        code: str,
        detail: str,
        next_attempt_at: float | None,
    ) -> None:
        now = float(self._clock())

        def _write(conn):
            job = conn.execute(
                "SELECT * FROM session_mirror_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)

            current_state = MirrorJobState(job["state"])
            exact_replay = (
                current_state is state
                and job["error_code"] == code
                and job["error_detail"] == detail
                and (
                    state is MirrorJobState.MANUAL_FAILURE
                    or job["next_attempt_at"] == next_attempt_at
                )
            )
            if exact_replay:
                if state is MirrorJobState.RETRY:
                    conn.execute(
                        "DELETE FROM session_bridge_state WHERE key = ?",
                        (f"{_MIRROR_ATTEMPT_STATE_PREFIX}{job_id}",),
                    )
                return

            if current_state in (
                MirrorJobState.SUCCEEDED,
                MirrorJobState.MANUAL_FAILURE,
            ):
                raise ValueError("terminal mirror job cannot be overwritten")
            if state is MirrorJobState.RETRY:
                if current_state is not MirrorJobState.RUNNING:
                    raise ValueError("mirror job must be running before retry")
            elif current_state not in (
                MirrorJobState.RUNNING,
                MirrorJobState.RETRY,
            ):
                raise ValueError(
                    "mirror job must be running or retrying before manual failure"
                )

            if next_attempt_at is None:
                conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, error_code = ?, error_detail = ?, updated_at = ?
                       WHERE id = ?""",
                    (state.value, code, detail, now, job_id),
                )
            else:
                conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, error_code = ?, error_detail = ?,
                           next_attempt_at = ?, updated_at = ?
                       WHERE id = ?""",
                    (state.value, code, detail, next_attempt_at, now, job_id),
                )
            conn.execute(
                "DELETE FROM session_bridge_state WHERE key = ?",
                (f"{_MIRROR_ATTEMPT_STATE_PREFIX}{job_id}",),
            )
            _settle_breaker_reservation(
                conn,
                job,
                error=True,
                updated_at=now,
            )

        self.db._execute_write(_write)

    def create_link(self, link: SessionLink) -> dict[str, Any]:
        return self.db._execute_write(lambda conn: self._create_link_row(conn, link))

    def transition_link_to_continues(
        self,
        bridge_id: str,
        *,
        pack_id: str,
        target_cursor: str,
        target_hash: str,
    ) -> dict[str, Any]:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        normalized_pack_id = _nonempty_text(pack_id, "context pack ID")
        normalized_target_cursor = _nonempty_text(target_cursor, "target cursor")
        normalized_target_hash = _nonempty_text(target_hash, "target hash")
        snapshot_key = _continuation_snapshot_state_key(normalized_bridge_id)
        now = float(self._clock())

        def _write(conn):
            pack = conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE id = ? AND bridge_id = ?""",
                (normalized_pack_id, normalized_bridge_id),
            ).fetchone()
            if pack is None:
                raise KeyError(normalized_pack_id)
            if pack["target_session_id"] is None:
                raise ValueError("context pack target identity is missing")
            expected_snapshot = {
                "version": 1,
                "pack_id": normalized_pack_id,
                "source_session_id": pack["source_session_id"],
                "source_cursor": pack["source_cursor"],
                "source_hash": pack["source_hash"],
                "target_session_id": pack["target_session_id"],
                "target_cursor": normalized_target_cursor,
                "target_hash": normalized_target_hash,
            }
            snapshot_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (snapshot_key,),
            ).fetchone()
            persisted_snapshot = (
                _decode_continuation_snapshot(snapshot_row["value_json"])
                if snapshot_row is not None
                else None
            )
            if persisted_snapshot is not None and (
                persisted_snapshot["target_cursor"] != normalized_target_cursor
                or persisted_snapshot["target_hash"] != normalized_target_hash
            ):
                raise ValueError("conflicting continuation target baseline")
            if (
                persisted_snapshot is not None
                and persisted_snapshot != expected_snapshot
            ):
                raise ValueError("conflicting continuation snapshot identity")

            links = conn.execute(
                """SELECT * FROM session_links
                   WHERE bridge_id = ? AND from_session_id = ?
                     AND to_session_id = ? AND relation IN (?, ?)
                   ORDER BY relation, id""",
                (
                    normalized_bridge_id,
                    pack["source_session_id"],
                    pack["target_session_id"],
                    Relation.MIRRORS.value,
                    Relation.CONTINUES.value,
                ),
            ).fetchall()
            mirror = next(
                (row for row in links if row["relation"] == Relation.MIRRORS.value),
                None,
            )
            continued = next(
                (row for row in links if row["relation"] == Relation.CONTINUES.value),
                None,
            )

            if continued is not None:
                if mirror is not None:
                    raise ValueError("conflicting continues link already exists")
                if (
                    continued["source_cursor"] == pack["source_cursor"]
                    and continued["source_hash"] == pack["source_hash"]
                ):
                    if (
                        pack["immutable_at"] is None
                        or continued["hydrated_at"] is None
                        or persisted_snapshot is None
                    ):
                        raise ValueError("incomplete continues link transition")
                    return dict(continued)
                raise ValueError("conflicting continues link source snapshot")
            if mirror is None:
                raise ValueError("context pack identity has no matching mirror link")
            if (
                mirror["source_cursor"] is not None
                or mirror["source_hash"] is not None
                or mirror["hydrated_at"] is not None
            ):
                raise ValueError("mirror link has conflicting source snapshot identity")
            if persisted_snapshot is not None:
                raise ValueError("continuation snapshot exists before link transition")

            target = conn.execute(
                """SELECT last_native_cursor, last_native_hash
                   FROM external_sessions WHERE session_id = ?""",
                (pack["target_session_id"],),
            ).fetchone()
            if target is None or (
                target["last_native_cursor"] != normalized_target_cursor
                or target["last_native_hash"] != normalized_target_hash
            ):
                raise ValueError(
                    "target baseline does not match cataloged target snapshot"
                )

            conn.execute(
                """UPDATE session_context_packs
                   SET immutable_at = COALESCE(immutable_at, ?)
                   WHERE id = ? AND bridge_id = ?""",
                (now, normalized_pack_id, normalized_bridge_id),
            )
            cursor = conn.execute(
                """UPDATE session_links
                   SET relation = ?, source_cursor = ?, source_hash = ?,
                       hydrated_at = COALESCE(hydrated_at, ?)
                   WHERE id = ? AND relation = ?""",
                (
                    Relation.CONTINUES.value,
                    pack["source_cursor"],
                    pack["source_hash"],
                    now,
                    mirror["id"],
                    Relation.MIRRORS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("mirror link changed during transition")
            snapshot_json = json.dumps(
                expected_snapshot,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)""",
                (snapshot_key, snapshot_json, now),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM session_links WHERE id = ?", (mirror["id"],)
                ).fetchone()
            )

        return self.db._execute_write(_write)

    @staticmethod
    def _create_link_row(conn, link: SessionLink) -> dict[str, Any]:
        conn.execute(
            """INSERT OR IGNORE INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id,
               source_cursor, source_hash, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                link.id,
                link.from_session_id,
                link.to_session_id,
                link.relation.value,
                link.bridge_id,
                link.source_cursor,
                link.source_hash,
                link.created_at,
            ),
        )
        row = conn.execute(
            """SELECT * FROM session_links
               WHERE bridge_id = ? AND from_session_id = ?
                 AND to_session_id = ? AND relation = ?""",
            (
                link.bridge_id,
                link.from_session_id,
                link.to_session_id,
                link.relation.value,
            ),
        ).fetchone()
        if row is None:
            raise ValueError(f"link ID collision for {link.id!r}")
        return dict(row)

    def mark_hydrated(
        self,
        bridge_id: str,
        *,
        source_cursor: str,
        source_hash: str,
        pack_id: str,
    ) -> None:
        now = float(self._clock())

        def _write(conn):
            pack = conn.execute(
                """SELECT id, source_session_id, target_session_id
                   FROM session_context_packs
                   WHERE id = ? AND bridge_id = ? AND source_cursor = ?
                     AND source_hash = ?""",
                (pack_id, bridge_id, source_cursor, source_hash),
            ).fetchone()
            if pack is None:
                raise KeyError(pack_id)
            link = conn.execute(
                """SELECT id FROM session_links
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND from_session_id = ? AND to_session_id = ?
                   LIMIT 1""",
                (
                    bridge_id,
                    source_cursor,
                    source_hash,
                    pack["source_session_id"],
                    pack["target_session_id"],
                ),
            ).fetchone()
            if link is None:
                raise ValueError("context pack has no matching link to hydrate")
            conn.execute(
                """UPDATE session_context_packs
                   SET immutable_at = COALESCE(immutable_at, ?)
                   WHERE id = ?""",
                (now, pack_id),
            )
            conn.execute(
                """UPDATE session_links
                   SET hydrated_at = COALESCE(hydrated_at, ?)
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND from_session_id = ? AND to_session_id = ?""",
                (
                    now,
                    bridge_id,
                    source_cursor,
                    source_hash,
                    pack["source_session_id"],
                    pack["target_session_id"],
                ),
            )

        self.db._execute_write(_write)

    def mark_diverged(self, bridge_id: str, *, at: float) -> None:
        def _write(conn):
            conn.execute(
                """UPDATE session_links
                   SET diverged_at = COALESCE(diverged_at, ?)
                   WHERE bridge_id = ?""",
                (float(at), bridge_id),
            )

        self.db._execute_write(_write)

    def put_context_pack(self, pack: ContextPack) -> dict[str, Any]:
        def _write(conn):
            row = conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE bridge_id = ? AND source_cursor = ? AND source_hash = ?
                     AND budget_chars = ?""",
                (
                    pack.bridge_id,
                    pack.source_cursor,
                    pack.source_hash,
                    pack.budget_chars,
                ),
            ).fetchone()
            if row is not None:
                if row["source_session_id"] != pack.source_session_id:
                    raise ValueError("context pack source identity mismatch")
                if (
                    row["target_session_id"] is not None
                    and pack.target_session_id is not None
                    and row["target_session_id"] != pack.target_session_id
                ):
                    raise ValueError("context pack target identity mismatch")
                if row["immutable_at"] is None:
                    target_session_id = (
                        pack.target_session_id
                        if pack.target_session_id is not None
                        else row["target_session_id"]
                    )
                    conn.execute(
                        """UPDATE session_context_packs
                           SET target_session_id = ?, payload = ?, created_at = ?
                           WHERE id = ?""",
                        (
                            target_session_id,
                            pack.payload,
                            pack.created_at,
                            row["id"],
                        ),
                    )
                    row = conn.execute(
                        "SELECT * FROM session_context_packs WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                return dict(row)
            conn.execute(
                """INSERT INTO session_context_packs (
                   id, bridge_id, source_session_id, target_session_id,
                   source_cursor, source_hash, budget_chars, payload, created_at,
                   immutable_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pack.id,
                    pack.bridge_id,
                    pack.source_session_id,
                    pack.target_session_id,
                    pack.source_cursor,
                    pack.source_hash,
                    pack.budget_chars,
                    pack.payload,
                    pack.created_at,
                    pack.immutable_at,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM session_context_packs WHERE id = ?", (pack.id,)
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def get_context_pack(
        self, bridge_id: str, *, budget_chars: int
    ) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                """SELECT * FROM session_context_packs
                   WHERE bridge_id = ? AND budget_chars = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (bridge_id, budget_chars),
            ).fetchone()
        return dict(row) if row else None

    def set_state(self, key: str, value: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping):
            raise TypeError("bridge state must be a mapping")
        value_json = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        snapshot = json.loads(value_json)
        if not isinstance(snapshot, dict):
            raise TypeError("bridge state must encode as a JSON object")
        now = float(self._clock())

        def _write(conn):
            conn.execute(
                """INSERT INTO session_bridge_state (key, value_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json = excluded.value_json,
                       updated_at = excluded.updated_at""",
                (key, value_json, now),
            )

        self.db._execute_write(_write)

    def get_state(self, key: str) -> dict[str, Any] | None:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["value_json"])
        if not isinstance(value, dict):
            raise ValueError(f"bridge state {key!r} is not a JSON object")
        return value

    def get_continuation_snapshot(
        self, bridge_id: str
    ) -> dict[str, Any] | None:
        normalized_bridge_id = _nonempty_text(bridge_id, "bridge ID")
        state_key = _continuation_snapshot_state_key(normalized_bridge_id)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            state_row = conn.execute(
                "SELECT value_json FROM session_bridge_state WHERE key = ?",
                (state_key,),
            ).fetchone()
            if state_row is None:
                return None
            snapshot = _decode_continuation_snapshot(state_row["value_json"])
            _validate_continuation_snapshot_identity(
                conn, normalized_bridge_id, snapshot
            )
        return snapshot

    def list_continuation_snapshots(
        self,
        *,
        limit: int = 1000,
        after_bridge_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 1000
        ):
            raise ValueError(
                "continuation snapshot list limit must be between 1 and 1000"
            )
        if after_bridge_id is None:
            lower_key = _CONTINUATION_SNAPSHOT_STATE_PREFIX
            comparison = ">="
        else:
            normalized_after = _nonempty_text(after_bridge_id, "after bridge ID")
            if normalized_after != after_bridge_id:
                raise ValueError("after bridge ID must be canonical")
            lower_key = _continuation_snapshot_state_key(normalized_after)
            comparison = ">"
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT key, value_json FROM session_bridge_state
                   WHERE key {comparison} ? AND key < ?
                   ORDER BY key LIMIT ?""",
                (
                    lower_key,
                    f"{_CONTINUATION_SNAPSHOT_STATE_PREFIX}\uffff",
                    limit,
                ),
            ).fetchall()
            snapshots: list[dict[str, Any]] = []
            for row in rows:
                raw_bridge_id = row["key"][
                    len(_CONTINUATION_SNAPSHOT_STATE_PREFIX) :
                ]
                bridge_id = _nonempty_text(
                    raw_bridge_id,
                    "continuation snapshot bridge ID",
                )
                if raw_bridge_id != bridge_id:
                    raise ValueError(
                        "continuation snapshot state key has noncanonical bridge ID"
                    )
                snapshot = _decode_continuation_snapshot(row["value_json"])
                _validate_continuation_snapshot_identity(conn, bridge_id, snapshot)
                snapshots.append({"bridge_id": bridge_id, **snapshot})
        return snapshots


def _finite_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _exact_nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be an exact nonempty string")
    return value


def _sidebar_lease_digest(lease_token: object) -> str:
    token = _exact_nonempty_text(lease_token, "sidebar lease token")
    return hashlib.sha256(token.encode()).hexdigest()


def _sidebar_exclusion_identity_digest(
    source_session_id: str,
    provider: Provider,
    reason_code: str,
) -> str:
    identity = "\0".join((source_session_id, provider.value, reason_code))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _ensure_sidebar_lineage_row(
    conn: Any,
    *,
    source_session_id: str,
    bridge_id: str,
    codex_thread_id: str,
    created_at: float,
) -> dict[str, Any]:
    target = conn.execute(
        """SELECT e.session_id, e.origin_kind, e.origin_bridge_id
             FROM external_sessions AS e
            WHERE e.provider = ? AND e.native_id = ?""",
        (Provider.CODEX.value, codex_thread_id),
    ).fetchone()
    if target is None:
        raise ValueError("native_task_not_indexed")
    if (
        target["origin_kind"] != OriginKind.BRIDGE_PLACEHOLDER.value
        or target["origin_bridge_id"] != bridge_id
    ):
        raise ValueError("source_identity_mismatch")
    conflicting = conn.execute(
        """SELECT 1 FROM session_links
            WHERE bridge_id = ? AND (
                from_session_id != ? OR to_session_id != ? OR relation != ?
            ) LIMIT 1""",
        (
            bridge_id,
            source_session_id,
            target["session_id"],
            Relation.MIRRORS.value,
        ),
    ).fetchone()
    if conflicting is not None:
        raise ValueError("source_identity_mismatch")
    link_digest = hashlib.sha256(
        f"{bridge_id}\0{source_session_id}\0{target['session_id']}".encode()
    ).hexdigest()
    link_id = f"sidebar-link:{link_digest}"
    conn.execute(
        """INSERT OR IGNORE INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id,
               source_cursor, source_hash, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            link_id,
            source_session_id,
            target["session_id"],
            Relation.MIRRORS.value,
            bridge_id,
            None,
            None,
            created_at,
        ),
    )
    row = conn.execute(
        """SELECT * FROM session_links
           WHERE bridge_id = ? AND from_session_id = ?
             AND to_session_id = ? AND relation = ?""",
        (
            bridge_id,
            source_session_id,
            target["session_id"],
            Relation.MIRRORS.value,
        ),
    ).fetchone()
    if row is None or row["id"] != link_id:
        raise ValueError(f"link ID collision for {link_id!r}")
    return dict(row)


def _find_sidebar_job_by_digest(
    conn: Any,
    token_digest: str,
    *,
    allow_completion: bool,
) -> tuple[Mapping[str, Any] | None, bool]:
    matches: list[tuple[Mapping[str, Any], bool]] = []
    lease_rows = conn.execute(
        """SELECT * FROM session_sidebar_jobs
           WHERE lease_digest = ? LIMIT 2""",
        (token_digest,),
    ).fetchall()
    for row in lease_rows:
        lease_digest = row["lease_digest"]
        if isinstance(lease_digest, str) and hmac.compare_digest(
            lease_digest,
            token_digest,
        ):
            matches.append((row, False))
    if allow_completion:
        completion_rows = conn.execute(
            """SELECT * FROM session_sidebar_jobs
               WHERE completion_digest = ? LIMIT 2""",
            (token_digest,),
        ).fetchall()
        for row in completion_rows:
            completion_digest = row["completion_digest"]
            if isinstance(completion_digest, str) and hmac.compare_digest(
                completion_digest,
                token_digest,
            ):
                matches.append((row, True))
    if len(matches) > 1:
        raise ValueError("ambiguous sidebar lease token")
    return matches[0] if matches else (None, False)


def _recover_one_expired_sidebar_lease(
    conn: Any,
    job: Mapping[str, Any],
    *,
    now: float,
) -> None:
    cursor = conn.execute(
        """UPDATE session_sidebar_jobs
           SET state = ?, next_attempt_at = ?, lease_digest = NULL,
               lease_expires_at = NULL, error_code = NULL, updated_at = ?
           WHERE id = ? AND state = ?""",
        (
            SidebarJobState.RETRY.value,
            now,
            now,
            job["id"],
            SidebarJobState.LEASED.value,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale expired sidebar lease")


def _validated_sidebar_job_provider(job: Mapping[str, Any]) -> Provider:
    from .sidebar import sidebar_bridge_id, sidebar_idempotency_key

    source_session_id = job.get("source_session_id")
    if not isinstance(source_session_id, str):
        raise ValueError("invalid sidebar source identity")
    idempotency_key = sidebar_idempotency_key(source_session_id)
    bridge_id = sidebar_bridge_id(source_session_id)
    if (
        job.get("idempotency_key") != idempotency_key
        or job.get("bridge_id") != bridge_id
    ):
        raise ValueError("invalid sidebar job identity")
    if source_session_id.startswith("claude:"):
        return Provider.CLAUDE
    if source_session_id.startswith("codex:"):
        raise ValueError("Codex cannot be a sidebar source")
    return Provider.HERMES


def _sidebar_delivery_state_key(source_session_id: str) -> str:
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(source_session_id)
    digest = hashlib.sha256(source_session_id.encode()).hexdigest()
    return f"{_SIDEBAR_DELIVERY_STATE_PREFIX}{digest}"


def _worktree_snapshot_state_key(source_session_id: str) -> str:
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(source_session_id)
    digest = hashlib.sha256(source_session_id.encode("utf-8")).hexdigest()
    return f"{_WORKTREE_SNAPSHOT_STATE_PREFIX}{digest}"


def _encode_worktree_snapshot(
    source_session_id: str,
    candidate: SidebarCandidate,
    snapshot: WorktreeSnapshot,
) -> str:
    from .context_pack import _redact
    from .worktree import WorktreeSnapshot

    if not isinstance(snapshot, WorktreeSnapshot):
        raise ValueError("invalid worktree snapshot")
    if (
        candidate.cwd != snapshot.cwd
        or candidate.git_root != snapshot.git_root
        or candidate.git_branch != snapshot.branch
        or candidate.git_head != snapshot.head
        or candidate.worktree_id != snapshot.worktree_id
    ):
        raise ValueError("sidebar candidate worktree snapshot mismatch")
    required_values = (
        source_session_id,
        snapshot.cwd,
        snapshot.worktree_id,
    )
    if any(
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
        or _redact(value) != value
        for value in required_values
    ):
        raise ValueError("invalid worktree snapshot")
    if snapshot.git_root is None:
        if snapshot.branch is not None or snapshot.head is not None:
            raise ValueError("invalid worktree snapshot")
    elif snapshot.branch is None:
        raise ValueError("invalid worktree snapshot")
    optional_values = (snapshot.git_root, snapshot.branch, snapshot.head)
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or not value
            or any(character in value for character in "\x00\r\n")
            or _redact(value) != value
        )
        for value in optional_values
    ):
        raise ValueError("invalid worktree snapshot")
    payload = {
        "version": 1,
        "source_session_id": source_session_id,
        "cwd": snapshot.cwd,
        "git_root": snapshot.git_root,
        "branch": snapshot.branch,
        "head": snapshot.head,
        "worktree_id": snapshot.worktree_id,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_worktree_snapshot(
    value_json: object,
    expected_source_session_id: str,
) -> WorktreeSnapshot:
    from .context_pack import _redact
    from .worktree import WorktreeSnapshot

    if not isinstance(value_json, (str, bytes, bytearray)):
        raise ValueError("invalid worktree snapshot")
    try:
        payload = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid worktree snapshot") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _WORKTREE_SNAPSHOT_FIELDS
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
        or payload.get("source_session_id") != expected_source_session_id
    ):
        raise ValueError("invalid worktree snapshot")
    required_values = tuple(payload.get(field) for field in (
        "source_session_id",
        "cwd",
        "worktree_id",
    ))
    if any(
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\x00\r\n")
        or _redact(value) != value
        for value in required_values
    ):
        raise ValueError("invalid worktree snapshot")
    optional_values = tuple(payload.get(field) for field in (
        "git_root",
        "branch",
        "head",
    ))
    if any(
        value is not None
        and (
            not isinstance(value, str)
            or not value
            or any(character in value for character in "\x00\r\n")
            or _redact(value) != value
        )
        for value in optional_values
    ):
        raise ValueError("invalid worktree snapshot")
    git_root, branch, head = optional_values
    if git_root is None:
        if branch is not None or head is not None:
            raise ValueError("invalid worktree snapshot")
    elif branch is None:
        raise ValueError("invalid worktree snapshot")
    return WorktreeSnapshot(
        cwd=payload["cwd"],
        git_root=payload["git_root"],
        branch=payload["branch"],
        head=payload["head"],
        worktree_id=payload["worktree_id"],
    )


def _encode_sidebar_delivery_candidate(candidate: SidebarCandidate) -> str:
    from .context_pack import _redact
    from .sidebar import _validate_candidate

    _validate_candidate(candidate)
    title = _exact_nonempty_text(candidate.title, "sidebar candidate title")
    _exact_nonempty_text(candidate.cwd, "sidebar candidate cwd")
    for value, label in (
        (candidate.git_root, "sidebar candidate git root"),
        (candidate.git_branch, "sidebar candidate git branch"),
        (candidate.git_head, "sidebar candidate git HEAD"),
        (candidate.worktree_id, "sidebar candidate worktree ID"),
    ):
        if value is not None:
            _exact_nonempty_text(value, label)
    if any(character in title for character in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError("sidebar candidate title must be a single line")
    if _redact(candidate.source_session_id) != candidate.source_session_id:
        raise ValueError("sidebar source identity cannot be persisted safely")
    if _redact(candidate.bridge_id) != candidate.bridge_id:
        raise ValueError("sidebar bridge identity cannot be persisted safely")

    def _safe(value: str | None) -> str | None:
        return None if value is None else _redact(value)

    payload = {
        "version": 1,
        "source_session_id": candidate.source_session_id,
        "provider": candidate.provider.value,
        "bridge_id": candidate.bridge_id,
        "title": _safe(title),
        "cwd": _safe(candidate.cwd),
        "git_root": _safe(candidate.git_root),
        "git_branch": _safe(candidate.git_branch),
        "git_head": _safe(candidate.git_head),
        "worktree_id": _safe(candidate.worktree_id),
        "eligible_at": _finite_number(candidate.eligible_at, "sidebar eligible_at"),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode_sidebar_delivery_candidate(value_json: object) -> SidebarCandidate:
    from .context_pack import _redact
    from .sidebar import SidebarCandidate, _validate_candidate

    if not isinstance(value_json, (str, bytes, bytearray)):
        raise ValueError("invalid sidebar delivery candidate")
    try:
        payload = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid sidebar delivery candidate") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != _SIDEBAR_DELIVERY_STATE_FIELDS
        or payload.get("version") != 1
        or isinstance(payload.get("version"), bool)
    ):
        raise ValueError("invalid sidebar delivery candidate")
    provider_value = payload.get("provider")
    try:
        provider = Provider(provider_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid sidebar delivery candidate") from exc
    required = (
        payload.get("source_session_id"),
        payload.get("bridge_id"),
        payload.get("title"),
        payload.get("cwd"),
    )
    optional = (
        payload.get("git_root"),
        payload.get("git_branch"),
        payload.get("git_head"),
        payload.get("worktree_id"),
    )
    if any(
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _redact(value) != value
        for value in required
    ) or any(
        value is not None
        and (
            not isinstance(value, str)
            or not value.strip()
            or _redact(value) != value
        )
        for value in optional
    ):
        raise ValueError("invalid sidebar delivery candidate")
    title = payload["title"]
    if any(
        character in title
        for character in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    ):
        raise ValueError("invalid sidebar delivery candidate")
    try:
        candidate = SidebarCandidate(
            source_session_id=payload["source_session_id"],
            provider=provider,
            bridge_id=payload["bridge_id"],
            title=payload["title"],
            cwd=payload["cwd"],
            git_root=payload["git_root"],
            git_branch=payload["git_branch"],
            git_head=payload["git_head"],
            worktree_id=payload["worktree_id"],
            eligible_at=_finite_number(
                payload.get("eligible_at"), "sidebar eligible_at"
            ),
        )
        _validate_candidate(candidate)
        _exact_nonempty_text(candidate.title, "sidebar candidate title")
    except ValueError as exc:
        raise ValueError("invalid sidebar delivery candidate") from exc
    return candidate


def _nonnegative_integer(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _validated_claim_policy(policy: object) -> dict[str, Any]:
    automatic_creation = getattr(policy, "automatic_creation", None)
    creates_per_minute = getattr(policy, "creates_per_minute", None)
    stop_after_attempts = getattr(policy, "stop_after_attempts", None)
    stop_error_rate = getattr(policy, "stop_error_rate", None)
    if type(automatic_creation) is not bool:
        raise ValueError("policy automatic_creation must be a boolean")
    if (
        not isinstance(creates_per_minute, int)
        or isinstance(creates_per_minute, bool)
        or creates_per_minute <= 0
    ):
        raise ValueError("policy creates_per_minute must be a positive integer")
    if (
        not isinstance(stop_after_attempts, int)
        or isinstance(stop_after_attempts, bool)
        or stop_after_attempts <= 0
    ):
        raise ValueError("policy stop_after_attempts must be a positive integer")
    error_rate = _finite_number(stop_error_rate, "policy stop_error_rate")
    if not 0.0 <= error_rate <= 1.0:
        raise ValueError("policy stop_error_rate must be between zero and one")
    return {
        "automatic_creation": automatic_creation,
        "creates_per_minute": creates_per_minute,
        "stop_after_attempts": stop_after_attempts,
        "stop_error_rate": error_rate,
    }


def _read_rate_attempts(conn: Any, *, now: float) -> list[float]:
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (_MIRROR_RATE_STATE_KEY,),
    ).fetchone()
    if row is None:
        return []
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror rate state") from exc
    if not isinstance(value, dict) or set(value) != {"version", "attempted_at"}:
        raise ValueError("invalid mirror rate state")
    if value["version"] != 1 or isinstance(value["version"], bool):
        raise ValueError("invalid mirror rate state")
    attempted_at = value["attempted_at"]
    if not isinstance(attempted_at, list):
        raise ValueError("invalid mirror rate state")
    recent: list[float] = []
    for raw_timestamp in attempted_at:
        timestamp = _finite_number(raw_timestamp, "mirror rate timestamp")
        if timestamp > now:
            raise ValueError("mirror rate timestamp cannot be in the future")
        if timestamp > now - 60.0:
            recent.append(timestamp)
    return recent


def _write_rate_attempts(conn: Any, attempted_at: Sequence[float], *, updated_at: float) -> None:
    value_json = json.dumps(
        {"version": 1, "attempted_at": list(attempted_at)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    conn.execute(
        """INSERT INTO session_bridge_state (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                          updated_at = excluded.updated_at""",
        (_MIRROR_RATE_STATE_KEY, value_json, updated_at),
    )


def _read_breaker_progress(conn: Any) -> dict[str, int]:
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (_MIRROR_BREAKER_STATE_KEY,),
    ).fetchone()
    if row is None:
        return {"attempts": 0, "errors": 0, "pending": 0}
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror breaker progress") from exc
    if not isinstance(value, dict) or set(value) not in (
        {"version", "attempts", "errors"},
        {"version", "attempts", "errors", "pending"},
    ):
        raise ValueError("invalid mirror breaker progress")
    if value["version"] != 1 or isinstance(value["version"], bool):
        raise ValueError("invalid mirror breaker progress")
    attempts = value["attempts"]
    errors = value["errors"]
    pending = value.get("pending", 0)
    _nonnegative_integer(attempts, "mirror breaker progress attempts")
    _nonnegative_integer(errors, "mirror breaker progress errors")
    _nonnegative_integer(pending, "mirror breaker progress pending")
    if errors > attempts or pending > attempts:
        raise ValueError("invalid mirror breaker progress")
    return {"attempts": attempts, "errors": errors, "pending": pending}


def _write_breaker_progress(
    conn: Any, progress: Mapping[str, int], *, updated_at: float
) -> None:
    value_json = json.dumps(
        {
            "version": 1,
            "attempts": progress["attempts"],
            "errors": progress["errors"],
            "pending": progress["pending"],
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    conn.execute(
        """INSERT INTO session_bridge_state (key, value_json, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json,
                                          updated_at = excluded.updated_at""",
        (_MIRROR_BREAKER_STATE_KEY, value_json, updated_at),
    )


def _healthy_breaker_batch_completed(
    progress: Mapping[str, int], *, stop_after_attempts: int, stop_error_rate: float
) -> bool:
    attempts = progress["attempts"]
    return (
        attempts >= stop_after_attempts
        and progress["pending"] == 0
        and (
            progress["errors"] == 0
            or progress["errors"] / attempts < stop_error_rate
        )
    )


def _breaker_is_halted(
    progress: Mapping[str, int], *, stop_after_attempts: int, stop_error_rate: float
) -> bool:
    attempts = progress["attempts"]
    errors = progress["errors"]
    return progress["pending"] > 0 or attempts >= stop_after_attempts or (
        attempts > 0 and errors > 0 and errors / attempts >= stop_error_rate
    )


def _create_breaker_reservation(
    conn: Any,
    job: Mapping[str, Any],
    *,
    updated_at: float,
) -> None:
    value_json = json.dumps(
        {"version": 1, "attempts": job["attempts"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        conn.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES (?, ?, ?)""",
            (
                f"{_MIRROR_BREAKER_RESERVATION_PREFIX}{job['id']}",
                value_json,
                updated_at,
            ),
        )
    except Exception as exc:
        raise ValueError("mirror breaker reservation already exists") from exc


def _settle_breaker_reservation(
    conn: Any,
    job: Mapping[str, Any],
    *,
    error: bool,
    updated_at: float,
) -> None:
    key = f"{_MIRROR_BREAKER_RESERVATION_PREFIX}{job['id']}"
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return
    try:
        reservation = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror breaker reservation") from exc
    if (
        not isinstance(reservation, dict)
        or set(reservation) != {"version", "attempts"}
        or reservation.get("version") != 1
        or reservation.get("attempts") != job["attempts"]
    ):
        raise ValueError("invalid mirror breaker reservation")
    progress = _read_breaker_progress(conn)
    if progress["pending"] <= 0:
        raise ValueError("mirror breaker reservation is not pending")
    updated = {
        "attempts": progress["attempts"],
        "errors": progress["errors"] + int(error),
        "pending": progress["pending"] - 1,
    }
    _write_breaker_progress(conn, updated, updated_at=updated_at)
    conn.execute("DELETE FROM session_bridge_state WHERE key = ?", (key,))


def _read_claim_authority(conn: Any, job: Mapping[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        (f"{_MIRROR_AUTHORITY_STATE_PREFIX}{job['id']}",),
    ).fetchone()
    if row is None:
        raise KeyError(job["id"])
    try:
        value = json.loads(row["value_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid mirror authority metadata") from exc
    legacy_fields = {
        "authority",
        "idempotency_key",
        "policy_generation",
        "source_session_id",
        "target_provider",
    }
    safe_manual_fields = {*legacy_fields, "require_unmapped"}
    current_fields = {*safe_manual_fields, "rollout_limited"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_fields),
        frozenset(safe_manual_fields),
        frozenset(current_fields),
    }:
        raise ValueError("invalid mirror authority metadata")
    require_unmapped = value.get("require_unmapped", False)
    rollout_limited = value.get("rollout_limited", False)
    if (
        type(require_unmapped) is not bool
        or type(rollout_limited) is not bool
        or (rollout_limited and not require_unmapped)
    ):
        raise ValueError("invalid mirror authority metadata")
    value["require_unmapped"] = require_unmapped
    value["rollout_limited"] = rollout_limited
    authority = value["authority"]
    generation = value["policy_generation"]
    if authority not in ("automatic", "manual") or (
        rollout_limited and authority != "manual"
    ):
        raise ValueError("invalid mirror authority metadata")
    _nonnegative_integer(generation, "mirror authority policy generation")
    provider = _external_provider(value["target_provider"])
    source_session_id = value["source_session_id"]
    expected_key = _stable_id(
        "mirror-job", source_session_id, provider.value, str(generation)
    )
    if (
        value["idempotency_key"] != expected_key
        or value["idempotency_key"] != job["idempotency_key"]
        or source_session_id != job["source_session_id"]
        or provider.value != job["target_provider"]
        or job["id"] != f"job:{expected_key}"
    ):
        raise ValueError("invalid mirror authority metadata")
    return value


def _terminalize_unclaimable_job(
    conn: Any,
    job: Mapping[str, Any],
    *,
    now: float,
    code: str,
    detail: str,
) -> None:
    cursor = conn.execute(
        """UPDATE session_mirror_jobs
           SET state = ?, error_code = ?, error_detail = ?, updated_at = ?
           WHERE id = ? AND state = ? AND attempts = ? AND idempotency_key = ?""",
        (
            MirrorJobState.MANUAL_FAILURE.value,
            code,
            detail,
            now,
            job["id"],
            job["state"],
            job["attempts"],
            job["idempotency_key"],
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("stale mirror job authority transition")


def _automatic_claim_denial(conn: Any, job: Mapping[str, Any]) -> str | None:
    source_session_id = job["source_session_id"]
    try:
        source_provider = _provider_from_canonical_session_id(source_session_id)
        target_provider = _external_provider(job["target_provider"])
    except (TypeError, ValueError):
        return "automatic mirror authority is invalid"
    source = conn.execute(
        """SELECT s.source, e.provider, e.native_id, e.origin_kind,
                  e.origin_bridge_id
           FROM sessions AS s
           JOIN external_sessions AS e ON e.session_id = s.id
           WHERE s.id = ?""",
        (source_session_id,),
    ).fetchone()
    expected_native_id = source_session_id.split(":", 1)[1]
    if source is None or (
        source["source"] != source_provider.value
        or source["provider"] != source_provider.value
        or source["native_id"] != expected_native_id
    ):
        return "automatic mirror source identity is not durable"
    if (
        source["origin_kind"] != OriginKind.NATIVE.value
        or source["origin_bridge_id"] is not None
    ):
        return "automatic mirror source origin is not native"
    mapped = conn.execute(
        """SELECT 1 FROM session_links AS link
           JOIN external_sessions AS target ON target.session_id = link.to_session_id
           WHERE link.from_session_id = ? AND target.provider = ? LIMIT 1""",
        (source_session_id, target_provider.value),
    ).fetchone()
    return "automatic mirror source is already mapped" if mapped is not None else None


def _model_config_has_delegate(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return False
    return isinstance(decoded, Mapping) and decoded.get("_delegate_from") is not None


def _nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    rank = max(1, math.ceil(percentile * len(values)))
    return float(values[rank - 1])


def _provider_from_canonical_session_id(session_id: object) -> Provider:
    if not isinstance(session_id, str) or session_id != session_id.strip():
        raise ValueError("invalid external session ID")
    prefix, separator, native_id = session_id.partition(":")
    if not separator or not native_id or native_id != native_id.strip():
        raise ValueError("invalid external session ID")
    provider = _external_provider(prefix)
    if canonical_session_id(provider, native_id) != session_id:
        raise ValueError("invalid external session ID")
    return provider


def _external_provider(provider: Provider | str) -> Provider:
    normalized = Provider(provider)
    if normalized not in _EXTERNAL_PROVIDERS:
        raise ValueError("bridge provider must be Claude or Codex")
    return normalized


def _validated_native_projection_cursor(
    cursor: NativeProjectionCursor | None,
) -> NativeProjectionCursor | None:
    if cursor is None:
        return None
    if not isinstance(cursor, tuple) or len(cursor) != 2:
        raise ValueError("native projection cursor must be an exact pair")
    activity = _finite_number(cursor[0], "native projection cursor activity")
    session_id = cursor[1]
    _provider_from_canonical_session_id(session_id)
    return activity, session_id


def _validated_sidebar_candidate_cursor(
    cursor: SidebarCandidateCursor | None,
) -> SidebarCandidateCursor | None:
    if cursor is None:
        return None
    if not isinstance(cursor, tuple) or len(cursor) != 2:
        raise ValueError("sidebar candidate cursor must be an exact pair")
    activity = _finite_number(cursor[0], "sidebar candidate cursor activity")
    session_id = _exact_nonempty_text(
        cursor[1], "sidebar candidate cursor session ID"
    )
    from .sidebar import sidebar_idempotency_key

    sidebar_idempotency_key(session_id)
    return activity, session_id


def _bounded_exact_ids(
    values: Sequence[str],
    *,
    label: str,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence")
    if len(values) > maximum:
        raise ValueError(f"{label} must contain at most {maximum} IDs")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{label} must contain exact nonempty IDs")
        normalized.append(value)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must not contain duplicates")
    return tuple(normalized)


def _public_codex_thread_id(value: object) -> str | None:
    if type(value) is not str or _PUBLIC_CODEX_THREAD_ID.fullmatch(value) is None:
        return None
    return value


def redact_codex_thread_id(value: object) -> str | None:
    """Return a fixed opaque tag only for a structurally safe native task ID."""

    thread_id = _public_codex_thread_id(value)
    if thread_id is None:
        return None
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:16]
    return f"task:{digest}"


def _nonempty_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value.strip()


def _continuation_snapshot_state_key(bridge_id: str) -> str:
    return f"{_CONTINUATION_SNAPSHOT_STATE_PREFIX}{bridge_id}"


def _decode_continuation_snapshot(value_json: str) -> dict[str, Any]:
    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid continuation snapshot encoding") from exc
    if not isinstance(value, dict) or set(value) != _CONTINUATION_SNAPSHOT_FIELDS:
        raise ValueError("invalid continuation snapshot schema")
    if (
        not isinstance(value["version"], int)
        or isinstance(value["version"], bool)
        or value["version"] != 1
    ):
        raise ValueError("invalid continuation snapshot version")
    for field in _CONTINUATION_SNAPSHOT_FIELDS - {"version"}:
        field_value = value[field]
        if (
            not isinstance(field_value, str)
            or not field_value.strip()
            or field_value != field_value.strip()
        ):
            raise ValueError(f"invalid continuation snapshot {field}")
    return value


def _validate_continuation_snapshot_identity(
    conn: Any,
    bridge_id: str,
    snapshot: Mapping[str, Any],
) -> None:
    pack = conn.execute(
        """SELECT 1 FROM session_context_packs
           WHERE id = ? AND bridge_id = ? AND source_session_id = ?
             AND target_session_id = ? AND source_cursor = ?
             AND source_hash = ? AND immutable_at IS NOT NULL""",
        (
            snapshot["pack_id"],
            bridge_id,
            snapshot["source_session_id"],
            snapshot["target_session_id"],
            snapshot["source_cursor"],
            snapshot["source_hash"],
        ),
    ).fetchone()
    link = conn.execute(
        """SELECT 1 FROM session_links
           WHERE bridge_id = ? AND from_session_id = ?
             AND to_session_id = ? AND relation = ?
             AND source_cursor = ? AND source_hash = ?
             AND hydrated_at IS NOT NULL""",
        (
            bridge_id,
            snapshot["source_session_id"],
            snapshot["target_session_id"],
            Relation.CONTINUES.value,
            snapshot["source_cursor"],
            snapshot["source_hash"],
        ),
    ).fetchone()
    if pack is None or link is None:
        raise ValueError("continuation snapshot durable identity mismatch")


def _existing_message_keys(
    conn: Any,
    session_id: str,
    projected_keys: list[tuple[str, int]],
) -> set[tuple[str, int]]:
    existing: set[tuple[str, int]] = set()
    for start in range(0, len(projected_keys), _MESSAGE_KEY_QUERY_CHUNK):
        chunk = projected_keys[start : start + _MESSAGE_KEY_QUERY_CHUNK]
        placeholders = ",".join("(?, ?)" for _ in chunk)
        params: list[Any] = [session_id]
        for native_event_id, ordinal in chunk:
            params.extend((native_event_id, ordinal))
        rows = conn.execute(
            f"""SELECT native_event_id, ordinal
                FROM external_message_map
                WHERE session_id = ?
                  AND (native_event_id, ordinal) IN ({placeholders})""",
            params,
        ).fetchall()
        existing.update((row["native_event_id"], row["ordinal"]) for row in rows)
    return existing


def _external_activity_state_key(session_id: str) -> str:
    return f"session-bridge:external-activity:{session_id}"


def _decode_external_activity(value_json: str) -> float:
    try:
        value = json.loads(value_json)
        last_active = value["last_active"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("invalid external session activity watermark") from exc
    if not isinstance(last_active, (int, float)) or isinstance(last_active, bool):
        raise ValueError("invalid external session activity watermark")
    return float(last_active)


def _resolve_projection_provenance(
    existing: Mapping[str, Any] | None,
    incoming_kind: OriginKind,
    incoming_bridge_id: str | None,
    *,
    has_new_human_user: bool,
) -> tuple[str, str | None]:
    if incoming_kind is not OriginKind.NATIVE and not incoming_bridge_id:
        raise ValueError("non-native projection provenance requires a bridge ID")

    if existing is None or existing["origin_kind"] == OriginKind.NATIVE.value:
        return (
            incoming_kind.value,
            None if incoming_kind is OriginKind.NATIVE else incoming_bridge_id,
        )

    existing_kind = OriginKind(existing["origin_kind"])
    existing_bridge_id = existing["origin_bridge_id"]
    if (
        incoming_kind is not OriginKind.NATIVE
        and incoming_bridge_id != existing_bridge_id
    ):
        raise ValueError("projection provenance conflicts with persisted origin")

    if existing_kind is OriginKind.BRIDGE_CONTINUATION:
        return existing_kind.value, existing_bridge_id
    if incoming_kind is OriginKind.BRIDGE_CONTINUATION or (
        incoming_kind is OriginKind.NATIVE and has_new_human_user
    ):
        return OriginKind.BRIDGE_CONTINUATION.value, existing_bridge_id
    return existing_kind.value, existing_bridge_id


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _snapshot_projected_messages(
    projection: SessionProjection,
) -> list[tuple[ProjectedMessage, dict[str, Any]]]:
    snapshot: list[tuple[ProjectedMessage, dict[str, Any]]] = []
    for message in projection.messages:
        tool_calls = message.tool_calls
        if tool_calls is not None:
            tool_calls = json.loads(
                json.dumps(
                    tool_calls,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        snapshot.append((
            message,
            {
                "role": message.role,
                "content": message.content,
                "timestamp": message.timestamp,
                "tool_name": message.tool_name,
                "tool_calls": tool_calls,
                "tool_call_id": message.tool_call_id,
                "reasoning": message.reasoning,
            },
        ))
    return snapshot


def _active_message_counters(conn, session_id: str) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT tool_calls FROM messages WHERE session_id = ? AND active = 1",
        (session_id,),
    ).fetchall()
    tool_calls = 0
    for row in rows:
        if row["tool_calls"] is None:
            continue
        value = json.loads(row["tool_calls"])
        tool_calls += len(value) if isinstance(value, list) else 1
    return len(rows), tool_calls


def _mirror_state(job_states: Sequence[str], links: Sequence[dict[str, Any]]) -> str:
    if any(link["diverged_at"] is not None for link in links):
        return "diverged"
    if any(
        link["relation"] in (Relation.CONTINUES.value, Relation.FORKS.value)
        for link in links
    ):
        return "continued"
    if any(link["relation"] == Relation.MIRRORS.value for link in links):
        return "mirrored"
    if MirrorJobState.MANUAL_FAILURE.value in job_states:
        return "failed"
    if any(
        state
        in (
            MirrorJobState.QUEUED.value,
            MirrorJobState.RUNNING.value,
            MirrorJobState.RETRY.value,
        )
        for state in job_states
    ):
        return "queued"
    if MirrorJobState.SUCCEEDED.value in job_states:
        return "mirrored"
    return "catalog_only"


__all__ = ["SessionBridgeStore"]
