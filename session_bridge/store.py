from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import math
import time
from typing import Any

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
    UpsertResult,
    canonical_session_id,
)


_EXTERNAL_PROVIDERS = (Provider.CLAUDE, Provider.CODEX)
_MESSAGE_KEY_QUERY_CHUNK = 400
_CONTINUATION_SNAPSHOT_STATE_PREFIX = "session-bridge:continuation:"
_MIRROR_ATTEMPT_STATE_PREFIX = "session-bridge:attempt:"
_MIRROR_AUTHORITY_STATE_PREFIX = "session-bridge:mirror-authority:"
_MIRROR_RATE_STATE_KEY = "session-bridge:mirror-rate"
_MIRROR_BREAKER_STATE_KEY = "session-bridge:mirror-breaker"
_MIRROR_BREAKER_RESERVATION_PREFIX = "session-bridge:breaker-reservation:"
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


class SessionBridgeStore:
    """Transactional persistence for the cross-harness session bridge."""

    def __init__(
        self,
        db: SessionDB,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self._clock = clock

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

    def get_session_launch_metadata(
        self, session_id: str
    ) -> dict[str, str | None] | None:
        normalized_session_id = _nonempty_text(session_id, "session ID")
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            row = conn.execute(
                "SELECT title, cwd FROM sessions WHERE id = ?",
                (normalized_session_id,),
            ).fetchone()
        if row is None:
            return None
        metadata = {"title": row["title"], "cwd": row["cwd"]}
        if any(
            value is not None and not isinstance(value, str)
            for value in metadata.values()
        ):
            raise ValueError("invalid session launch metadata")
        return metadata

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

                for row in session_rows:
                    session_id = row["id"]
                    if row["provider"] is None:
                        summaries[session_id] = {
                            "bridge_provider": Provider.HERMES.value,
                            "bridge_mirror_state": None,
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
                    }
        return summaries

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
    ) -> list[dict[str, Any]]:
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise ValueError("now must be a finite number")
        from .mirror import claim_due_mirror_jobs

        return claim_due_mirror_jobs(self, limit=limit, policy=policy)

    def claim_due_jobs_with_limits(
        self,
        *,
        now: float,
        limit: int,
        policy: Any,
    ) -> list[dict[str, Any]]:
        claim_time = _finite_number(now, "now")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("claim limit must be a non-negative integer")
        policy_values = _validated_claim_policy(policy)
        if limit == 0:
            return []

        def _write(conn):
            automatic_creation = policy_values["automatic_creation"]
            breaker = _read_breaker_progress(conn)
            if automatic_creation and _healthy_breaker_batch_completed(
                breaker,
                stop_after_attempts=policy_values["stop_after_attempts"],
                stop_error_rate=policy_values["stop_error_rate"],
            ):
                breaker = {"attempts": 0, "errors": 0, "pending": 0}
                _write_breaker_progress(conn, breaker, updated_at=claim_time)
            automatic_allowed = automatic_creation and not _breaker_is_halted(
                breaker,
                stop_after_attempts=policy_values["stop_after_attempts"],
                stop_error_rate=policy_values["stop_error_rate"],
            )

            recent = _read_rate_attempts(conn, now=claim_time)
            capacity = min(
                limit,
                max(0, policy_values["creates_per_minute"] - len(recent)),
            )
            if capacity == 0:
                _write_rate_attempts(conn, recent, updated_at=claim_time)
                return []

            scan_limit = max(capacity * 4, capacity + 32)
            due = conn.execute(
                """SELECT job.* FROM session_mirror_jobs AS job
                   LEFT JOIN session_bridge_state AS authority
                     ON authority.key = ? || job.id
                   WHERE job.state IN (?, ?) AND job.next_attempt_at <= ?
                   ORDER BY
                     CASE WHEN authority.value_json LIKE ? THEN 0 ELSE 1 END,
                     job.next_attempt_at, job.created_at, job.id
                   LIMIT ?""",
                (
                    _MIRROR_AUTHORITY_STATE_PREFIX,
                    MirrorJobState.QUEUED.value,
                    MirrorJobState.RETRY.value,
                    claim_time,
                    '{"authority":"manual",%',
                    scan_limit,
                ),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            automatic_reserved = 0
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
                if claim_authority == "automatic":
                    if not automatic_allowed or automatic_reserved:
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
                if claim_authority == "automatic":
                    automatic_reserved = 1
                    _create_breaker_reservation(
                        conn,
                        claimed_job,
                        updated_at=claim_time,
                    )
                claimed.append(claimed_job)

            if automatic_reserved:
                breaker = {
                    "attempts": breaker["attempts"] + automatic_reserved,
                    "errors": breaker["errors"],
                    "pending": breaker["pending"] + automatic_reserved,
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
    current_fields = {*legacy_fields, "require_unmapped"}
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_fields),
        frozenset(current_fields),
    }:
        raise ValueError("invalid mirror authority metadata")
    require_unmapped = value.get("require_unmapped", False)
    if type(require_unmapped) is not bool:
        raise ValueError("invalid mirror authority metadata")
    value["require_unmapped"] = require_unmapped
    authority = value["authority"]
    generation = value["policy_generation"]
    if authority not in ("automatic", "manual"):
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
