from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
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

            origin_kind, origin_bridge_id = _resolve_projection_provenance(
                external_row,
                projection.origin_kind,
                projection.origin_bridge_id,
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
                       started_at = MIN(started_at, ?)
                   WHERE id = ?""",
                (
                    provider.value,
                    projection.title,
                    projection.title,
                    session_id,
                    projection.title,
                    projection.cwd,
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

            existing_keys = {
                (row["native_event_id"], row["ordinal"])
                for row in conn.execute(
                    """SELECT native_event_id, ordinal
                       FROM external_message_map WHERE session_id = ?""",
                    (session_id,),
                ).fetchall()
            }
            pending = [
                (message, row)
                for message, row in projected_messages
                if (message.native_event_id, message.ordinal) not in existing_keys
            ]
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

    def claim_due_jobs(self, *, now: float, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        claimed_at = float(now)

        def _write(conn):
            rows = conn.execute(
                """SELECT id FROM session_mirror_jobs
                   WHERE state IN (?, ?) AND next_attempt_at <= ?
                   ORDER BY next_attempt_at, created_at, id
                   LIMIT ?""",
                (
                    MirrorJobState.QUEUED.value,
                    MirrorJobState.RETRY.value,
                    claimed_at,
                    limit,
                ),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for row in rows:
                conn.execute(
                    """UPDATE session_mirror_jobs
                       SET state = ?, attempts = attempts + 1, updated_at = ?
                       WHERE id = ?""",
                    (MirrorJobState.RUNNING.value, claimed_at, row["id"]),
                )
                claimed.append(
                    dict(
                        conn.execute(
                            "SELECT * FROM session_mirror_jobs WHERE id = ?",
                            (row["id"],),
                        ).fetchone()
                    )
                )
            return claimed

        return self.db._execute_write(_write)

    def complete_job(
        self,
        job_id: str,
        *,
        target_native_id: str,
        target_session_id: str,
        bridge_id: str,
    ) -> None:
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
                """SELECT s.source, e.provider, e.native_id
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

            if job["state"] == MirrorJobState.SUCCEEDED.value:
                exact_link = conn.execute(
                    """SELECT 1 FROM session_links
                       WHERE bridge_id = ? AND from_session_id = ?
                         AND to_session_id = ? AND relation = ?""",
                    (
                        bridge_id,
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
                    id=f"link:{_stable_id('mirror-link', bridge_id, job['source_session_id'], target_session_id)}",
                    from_session_id=job["source_session_id"],
                    to_session_id=target_session_id,
                    relation=Relation.MIRRORS,
                    bridge_id=bridge_id,
                    source_cursor=None,
                    source_hash=None,
                    created_at=now,
                ),
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

        self.db._execute_write(_write)

    def create_link(self, link: SessionLink) -> dict[str, Any]:
        return self.db._execute_write(lambda conn: self._create_link_row(conn, link))

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


def _external_provider(provider: Provider | str) -> Provider:
    normalized = Provider(provider)
    if normalized not in _EXTERNAL_PROVIDERS:
        raise ValueError("bridge provider must be Claude or Codex")
    return normalized


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
) -> tuple[str, str | None]:
    if incoming_kind is not OriginKind.NATIVE and not incoming_bridge_id:
        raise ValueError("non-native projection provenance requires a bridge ID")

    if existing is None or existing["origin_kind"] == OriginKind.NATIVE.value:
        return (
            incoming_kind.value,
            None if incoming_kind is OriginKind.NATIVE else incoming_bridge_id,
        )

    if incoming_kind is OriginKind.NATIVE:
        return existing["origin_kind"], existing["origin_bridge_id"]
    if (
        existing["origin_kind"] == incoming_kind.value
        and existing["origin_bridge_id"] == incoming_bridge_id
    ):
        return existing["origin_kind"], existing["origin_bridge_id"]
    raise ValueError("projection provenance conflicts with persisted origin")


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
