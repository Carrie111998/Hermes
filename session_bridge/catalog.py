"""Authoritative unified catalog over Hermes and external harness sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
from typing import Any

from hermes_state import SessionDB
from tools.session_search_tool import _format_timestamp, _shape_message

from .models import MirrorJobState, OriginKind, Provider, Relation
from .store import SessionBridgeStore


_MAX_LIMIT = 100
_MAX_WINDOW = 200
_HIDDEN_SOURCES = ("subagent", "tool")
_MESSAGE_COLUMNS = (
    "id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp"
)
_PROVIDERS = frozenset({Provider.HERMES.value, Provider.CLAUDE.value, Provider.CODEX.value})
_MIRROR_STATES = frozenset(
    {"catalog_only", "queued", "failed", "mirrored", "continued", "diverged"}
)

_LAST_ACTIVE_SQL = """COALESCE(
    (SELECT MAX(activity.timestamp)
       FROM messages AS activity
      WHERE activity.session_id = s.id
        AND (activity.active = 1 OR activity.compacted = 1)),
    s.started_at
)"""

_MIRROR_STATE_SQL = f"""CASE
    WHEN EXISTS (
        SELECT 1 FROM session_links AS link
         WHERE (link.from_session_id = s.id OR link.to_session_id = s.id)
           AND link.diverged_at IS NOT NULL
    ) THEN 'diverged'
    WHEN EXISTS (
        SELECT 1 FROM session_links AS link
         WHERE (link.from_session_id = s.id OR link.to_session_id = s.id)
           AND link.relation IN ('{Relation.CONTINUES.value}', '{Relation.FORKS.value}')
    ) THEN 'continued'
    WHEN EXISTS (
        SELECT 1 FROM session_links AS link
         WHERE (link.from_session_id = s.id OR link.to_session_id = s.id)
           AND link.relation = '{Relation.MIRRORS.value}'
    ) THEN 'mirrored'
    WHEN EXISTS (
        SELECT 1 FROM session_mirror_jobs AS job
         WHERE job.source_session_id = s.id
           AND job.state = '{MirrorJobState.MANUAL_FAILURE.value}'
    ) THEN 'failed'
    WHEN EXISTS (
        SELECT 1 FROM session_mirror_jobs AS job
         WHERE job.source_session_id = s.id
           AND job.state IN (
               '{MirrorJobState.QUEUED.value}',
               '{MirrorJobState.RUNNING.value}',
               '{MirrorJobState.RETRY.value}'
           )
    ) THEN 'queued'
    WHEN EXISTS (
        SELECT 1 FROM session_mirror_jobs AS job
         WHERE job.source_session_id = s.id
           AND job.state = '{MirrorJobState.SUCCEEDED.value}'
    ) THEN 'mirrored'
    WHEN e.provider IS NOT NULL THEN 'catalog_only'
    ELSE NULL
END"""

_BASE_COLUMNS = f"""
    s.id AS session_id,
    s.source,
    s.model,
    s.title,
    s.started_at,
    s.ended_at,
    s.message_count,
    s.cwd,
    s.git_branch,
    s.git_repo_root AS repo,
    s.parent_session_id,
    s.archived,
    e.provider AS external_provider,
    e.native_id,
    e.native_status,
    e.first_indexed_at,
    e.last_indexed_at,
    e.origin_kind,
    e.origin_bridge_id,
    e.sync_error,
    {_LAST_ACTIVE_SQL} AS last_active,
    {_MIRROR_STATE_SQL} AS mirror_state,
    (SELECT preview.content
       FROM messages AS preview
      WHERE preview.session_id = s.id
        AND preview.role = 'user'
        AND (preview.active = 1 OR preview.compacted = 1)
        AND length(COALESCE(preview.content, '')) > 0
      ORDER BY preview.id
      LIMIT 1) AS preview
"""


class UnifiedCatalog:
    """Query SessionDB with bridge predicates applied before truncation."""

    def __init__(self, db: SessionDB, store: SessionBridgeStore) -> None:
        if not isinstance(db, SessionDB):
            raise TypeError("db must be a SessionDB")
        if not isinstance(store, SessionBridgeStore):
            raise TypeError("store must be a SessionBridgeStore")
        if store.db is not db:
            raise ValueError("catalog and bridge store must share one SessionDB")
        self.db = db
        self.store = store

    def search(
        self,
        *,
        query: str = "",
        session_id: str | None = None,
        around_message_id: int | None = None,
        window: int = 5,
        limit: int = 10,
        provider: str | None = None,
        mirror_state: str | None = None,
        relation: str | None = None,
        cwd: str | None = None,
        repo: str | None = None,
        before: float | None = None,
        after: float | None = None,
    ) -> dict[str, Any]:
        normalized_window = _clamp_int(window, default=5, minimum=1, maximum=_MAX_WINDOW)
        normalized_limit = _clamp_int(limit, default=10, minimum=1, maximum=_MAX_LIMIT)
        normalized_session_id = _optional_text(session_id, "session ID", maximum=512)
        normalized_query = _text(query, "query", maximum=4_000, allow_empty=True)
        filters = _Filters.create(
            provider=provider,
            mirror_state=mirror_state,
            relation=relation,
            cwd=cwd,
            repo=repo,
            before=before,
            after=after,
        )

        if normalized_session_id is not None and around_message_id is not None:
            return self._profile_exact_read(
                "scroll",
                normalized_session_id,
                window=normalized_window,
                around_message_id=around_message_id,
            )
        if normalized_session_id is not None:
            return self._profile_exact_read(
                "read", normalized_session_id, window=normalized_window
            )
        if normalized_query:
            primary = self._discover(
                normalized_query,
                window=normalized_window,
                limit=normalized_limit,
                filters=filters,
            )
        else:
            primary = self._browse(limit=normalized_limit, filters=filters)
        return self._merge_profile_results(
            primary,
            query=normalized_query,
            window=normalized_window,
            limit=normalized_limit,
            filters=filters,
        )

    def get(self, session_id: str, *, window: int = 50) -> dict[str, Any]:
        return self.search(session_id=session_id, window=window)

    def _profile_exact_read(
        self,
        mode: str,
        session_id: str,
        *,
        window: int,
        around_message_id: int | None = None,
    ) -> dict[str, Any]:
        matches: list[tuple[str, dict[str, Any]]] = []
        with self.store._native_hermes_databases() as databases:
            for profile, database, owned in databases:
                if owned and not self.store._profile_catalog_compatible(database):
                    continue
                catalog = self if not owned else UnifiedCatalog(
                    database,
                    SessionBridgeStore(
                        database,
                        hermes_profile_db_paths=lambda: (),
                    ),
                )
                try:
                    result = (
                        catalog._scroll(
                            session_id,
                            around_message_id,
                            window=window,
                        )
                        if mode == "scroll"
                        else catalog._read(session_id, window=window)
                    )
                except KeyError:
                    continue
                if owned:
                    result["session"]["profile"] = profile
                    result["session_meta"]["profile"] = profile
                matches.append((profile, result))
        if not matches:
            raise KeyError(session_id)
        if len(matches) != 1:
            raise ValueError("duplicate native Hermes session identity across profiles")
        return matches[0][1]

    def _merge_profile_results(
        self,
        primary: dict[str, Any],
        *,
        query: str,
        window: int,
        limit: int,
        filters: _Filters,
    ) -> dict[str, Any]:
        results = list(primary["results"])
        with self.store._native_hermes_databases() as databases:
            for profile, database, owned in databases:
                if not owned:
                    continue
                if not self.store._profile_catalog_compatible(database):
                    continue
                catalog = UnifiedCatalog(
                    database,
                    SessionBridgeStore(
                        database,
                        hermes_profile_db_paths=lambda: (),
                    ),
                )
                page = (
                    catalog._discover(
                        query,
                        window=window,
                        limit=limit,
                        filters=filters,
                    )
                    if query
                    else catalog._browse(limit=limit, filters=filters)
                )
                for result in page["results"]:
                    result["profile"] = profile
                    results.append(result)
        identities = [result["session_id"] for result in results]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate native Hermes session identity across profiles")
        results.sort(
            key=lambda result: (
                -float(result.get("last_active") or 0.0),
                result["session_id"],
            )
        )
        primary["results"] = results[:limit]
        primary["count"] = len(primary["results"])
        if "sessions_searched" in primary:
            primary["sessions_searched"] = len(primary["results"])
        return primary

    def resolve_continuation(
        self,
        *,
        session_id: str | None,
        bridge_id: str | None,
        target_provider: str | None,
    ) -> dict[str, str]:
        normalized_session_id = _optional_text(session_id, "session ID", maximum=512)
        normalized_bridge_id = _optional_text(bridge_id, "bridge ID", maximum=512)
        if normalized_session_id is None and normalized_bridge_id is None:
            raise ValueError("session_id or bridge_id is required")
        normalized_target = (
            _external_provider(target_provider) if target_provider is not None else None
        )

        where: list[str] = [
            "link.relation IN (?, ?)",
            "source_session.id IS NOT NULL",
            "target.provider IS NOT NULL",
        ]
        params: list[Any] = [Relation.MIRRORS.value, Relation.CONTINUES.value]
        if normalized_bridge_id is not None:
            where.append("link.bridge_id = ?")
            params.append(normalized_bridge_id)
        if normalized_session_id is not None:
            where.append("(link.from_session_id = ? OR link.to_session_id = ?)")
            params.extend([normalized_session_id, normalized_session_id])
        if normalized_target is not None:
            where.append("target.provider = ?")
            params.append(normalized_target.value)

        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT link.bridge_id, link.from_session_id, link.to_session_id,
                           target.provider AS target_provider
                      FROM session_links AS link
                      JOIN sessions AS source_session
                        ON source_session.id = link.from_session_id
                      LEFT JOIN external_sessions AS source
                        ON source.session_id = link.from_session_id
                      JOIN external_sessions AS target
                        ON target.session_id = link.to_session_id
                     WHERE {' AND '.join(where)}
                     ORDER BY CASE link.relation
                                  WHEN '{Relation.CONTINUES.value}' THEN 0 ELSE 1 END,
                              link.created_at DESC, link.id""",
                params,
            ).fetchall()
        identities = {
            (
                row["bridge_id"],
                row["from_session_id"],
                row["to_session_id"],
                row["target_provider"],
            )
            for row in rows
        }
        if not identities:
            raise KeyError(normalized_bridge_id or normalized_session_id)
        if len(identities) != 1:
            raise ValueError("continuation identity is ambiguous; pass bridge_id")
        resolved_bridge, source_id, target_id, resolved_provider = identities.pop()
        if normalized_session_id is not None and normalized_session_id not in {
            source_id,
            target_id,
        }:
            raise ValueError("session is not a member of the requested bridge")
        return {
            "bridge_id": resolved_bridge,
            "source_session_id": source_id,
            "target_session_id": target_id,
            "target_provider": resolved_provider,
        }

    def mirror_preview(
        self,
        session_id: str,
        target_provider: str,
    ) -> dict[str, Any]:
        normalized_session_id = _text(
            session_id, "session ID", maximum=512, allow_empty=False
        )
        target = _external_provider(target_provider)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            source = conn.execute(
                "SELECT * FROM external_sessions WHERE session_id = ?",
                (normalized_session_id,),
            ).fetchone()
            if source is None:
                raise KeyError(normalized_session_id)
            source_provider = Provider(source["provider"])
            if source_provider is target:
                raise ValueError("mirror target must be the inverse provider")
            if {source_provider, target} != {Provider.CLAUDE, Provider.CODEX}:
                raise ValueError("mirror target must be the inverse provider")
            mapping = conn.execute(
                """SELECT 1
                     FROM session_links AS link
                     JOIN external_sessions AS target
                       ON target.session_id = CASE
                           WHEN link.from_session_id = ? THEN link.to_session_id
                           ELSE link.from_session_id
                       END
                    WHERE (link.from_session_id = ? OR link.to_session_id = ?)
                      AND target.provider = ?
                    LIMIT 1""",
                (
                    normalized_session_id,
                    normalized_session_id,
                    normalized_session_id,
                    target.value,
                ),
            ).fetchone()
            job = conn.execute(
                """SELECT state FROM session_mirror_jobs
                    WHERE source_session_id = ? AND target_provider = ?
                    ORDER BY created_at DESC, id DESC LIMIT 1""",
                (normalized_session_id, target.value),
            ).fetchone()

        if source["origin_kind"] != OriginKind.NATIVE.value:
            return _mirror_plan(
                normalized_session_id, target, False, "bridge_origin"
            )
        if mapping is not None:
            return _mirror_plan(
                normalized_session_id, target, False, "already_mapped"
            )
        if job is not None:
            state = str(job["state"])
            reason = "failed" if state == MirrorJobState.MANUAL_FAILURE.value else "already_queued"
            return _mirror_plan(normalized_session_id, target, False, reason)
        return _mirror_plan(normalized_session_id, target, True, "eligible")

    def status(self) -> dict[str, Any]:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                """SELECT COALESCE(e.provider, 'hermes') AS provider,
                          COUNT(*) AS session_count,
                          SUM(CASE WHEN e.sync_error IS NOT NULL THEN 1 ELSE 0 END)
                              AS degraded_count
                     FROM sessions AS s
                     LEFT JOIN external_sessions AS e ON e.session_id = s.id
                    GROUP BY COALESCE(e.provider, 'hermes')
                    ORDER BY provider"""
            ).fetchall()
        providers = {
            row["provider"]: {
                "sessions": int(row["session_count"]),
                "degraded": int(row["degraded_count"] or 0),
            }
            for row in rows
        }
        return {"providers": providers, "total_sessions": sum(v["sessions"] for v in providers.values())}

    def _browse(self, *, limit: int, filters: _Filters) -> dict[str, Any]:
        where, params = filters.sql()
        where.extend("s.source != ?" for _ in _HIDDEN_SOURCES)
        params.extend(_HIDDEN_SOURCES)
        rows = self._query_rows(
            where,
            params,
            order_by="last_active DESC, s.id",
            limit=limit,
        )
        results = self._enrich(rows)
        return {
            "success": True,
            "mode": "browse",
            "results": results,
            "count": len(results),
            "limit": limit,
        }

    def _discover(
        self,
        query: str,
        *,
        window: int,
        limit: int,
        filters: _Filters,
    ) -> dict[str, Any]:
        sanitized_query = self.db._sanitize_fts5_query(query)
        if not sanitized_query:
            return {
                "success": True,
                "mode": "discover",
                "query": query,
                "results": [],
                "count": 0,
                "limit": limit,
            }
        where, params = filters.sql()
        where.extend(
            [
                "messages_fts MATCH ?",
                "(m.active = 1 OR m.compacted = 1)",
                "m.role IN ('user', 'assistant')",
            ]
        )
        params.append(sanitized_query)
        where.extend("s.source != ?" for _ in _HIDDEN_SOURCES)
        params.extend(_HIDDEN_SOURCES)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT {_BASE_COLUMNS},
                           MIN(m.id) AS match_message_id,
                           MIN(messages_fts.rank) AS match_rank
                      FROM messages_fts
                      JOIN messages AS m ON m.id = messages_fts.rowid
                      JOIN sessions AS s ON s.id = m.session_id
                      LEFT JOIN external_sessions AS e ON e.session_id = s.id
                     WHERE {' AND '.join(where)}
                     GROUP BY s.id
                     ORDER BY match_rank,
                              CASE WHEN s.source = 'cron' THEN 1 ELSE 0 END,
                              last_active DESC, s.id
                     LIMIT ?""",
                [*params, limit],
            ).fetchall()
        results = self._enrich(rows)
        for result in results:
            match_id = int(result.pop("_match_message_id"))
            view = self._active_anchored_view(
                result["session_id"], match_id, window=window, bookend=3
            )
            anchor = next(
                (message for message in view.get("window", []) if message["id"] == match_id),
                None,
            )
            result.update(
                {
                    "matched_role": anchor.get("role") if anchor else None,
                    "match_message_id": match_id,
                    "snippet": _snippet(anchor.get("content") if anchor else ""),
                    "bookend_start": [
                        _shape_message(message)
                        for message in view.get("bookend_start", [])
                    ],
                    "messages": [
                        _shape_message(message, anchor_id=match_id)
                        for message in view.get("window", [])
                    ],
                    "bookend_end": [
                        _shape_message(message)
                        for message in view.get("bookend_end", [])
                    ],
                    "messages_before": view.get("messages_before", 0),
                    "messages_after": view.get("messages_after", 0),
                }
            )
        return {
            "success": True,
            "mode": "discover",
            "query": query,
            "results": results,
            "count": len(results),
            "sessions_searched": len(results),
            "limit": limit,
        }

    def _read(self, session_id: str, *, window: int) -> dict[str, Any]:
        row = self._session_row(session_id)
        if row is None:
            raise KeyError(session_id)
        total, messages = self._bounded_read(session_id, window=window)
        truncated = total > window
        session = self._enrich([row])[0]
        session_meta = _legacy_session_meta(session)
        return {
            "success": True,
            "mode": "read",
            "session_id": session_id,
            "session": session,
            "session_meta": session_meta,
            "message_count": total,
            "truncated": truncated,
            "window": window,
            "messages": [_shape_message(message) for message in messages],
        }

    def _scroll(
        self,
        session_id: str,
        around_message_id: int,
        *,
        window: int,
    ) -> dict[str, Any]:
        if not isinstance(around_message_id, int) or isinstance(around_message_id, bool):
            raise ValueError("around_message_id must be an integer")
        row = self._session_row(session_id)
        if row is None:
            raise KeyError(session_id)
        view = self._bounded_window(
            session_id,
            around_message_id,
            window=window,
        )
        if not view["window"]:
            raise KeyError(around_message_id)
        session = self._enrich([row])[0]
        return {
            "success": True,
            "mode": "scroll",
            "session_id": session_id,
            "around_message_id": around_message_id,
            "session": session,
            "session_meta": _legacy_session_meta(session),
            "window": window,
            "messages": [
                _shape_message(message, anchor_id=around_message_id)
                for message in view["window"]
            ],
            "messages_before": view["messages_before"],
            "messages_after": view["messages_after"],
        }

    def _query_rows(
        self,
        where: Sequence[str],
        params: Sequence[Any],
        *,
        order_by: str,
        limit: int,
    ) -> list[Mapping[str, Any]]:
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT {_BASE_COLUMNS}
                      FROM sessions AS s
                      LEFT JOIN external_sessions AS e ON e.session_id = s.id
                      {where_sql}
                     ORDER BY {order_by}
                     LIMIT ?""",
                [*params, limit],
            ).fetchall()
        return rows

    def _session_row(self, session_id: str) -> Mapping[str, Any] | None:
        rows = self._query_rows(["s.id = ?"], [session_id], order_by="s.id", limit=1)
        return rows[0] if rows else None

    def _bounded_read(
        self,
        session_id: str,
        *,
        window: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            total = int(
                conn.execute(
                    """SELECT COUNT(*) FROM messages
                        WHERE session_id = ? AND (active = 1 OR compacted = 1)""",
                    (session_id,),
                ).fetchone()[0]
            )
            if total <= window:
                rows = conn.execute(
                    f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                        WHERE session_id = ? AND (active = 1 OR compacted = 1)
                        ORDER BY id LIMIT ?""",
                    (session_id, window),
                ).fetchall()
            else:
                head = (window + 1) // 2
                tail = window - head
                head_rows = conn.execute(
                    f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                        WHERE session_id = ? AND (active = 1 OR compacted = 1)
                        ORDER BY id LIMIT ?""",
                    (session_id, head),
                ).fetchall()
                tail_rows = (
                    conn.execute(
                        f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                            WHERE session_id = ? AND (active = 1 OR compacted = 1)
                            ORDER BY id DESC LIMIT ?""",
                        (session_id, tail),
                    ).fetchall()
                    if tail
                    else []
                )
                rows = [*head_rows, *reversed(tail_rows)]
        return total, self._decode_message_rows(rows)

    def _bounded_window(
        self,
        session_id: str,
        around_message_id: int,
        *,
        window: int,
    ) -> dict[str, Any]:
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            anchor = conn.execute(
                f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                    WHERE session_id = ? AND id = ?
                      AND (active = 1 OR compacted = 1)""",
                (session_id, around_message_id),
            ).fetchone()
            if anchor is None:
                return {"window": [], "messages_before": 0, "messages_after": 0}
            before_rows = conn.execute(
                f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                    WHERE session_id = ? AND id < ?
                      AND (active = 1 OR compacted = 1)
                    ORDER BY id DESC LIMIT ?""",
                (session_id, around_message_id, window),
            ).fetchall()
            after_rows = conn.execute(
                f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                    WHERE session_id = ? AND id > ?
                      AND (active = 1 OR compacted = 1)
                    ORDER BY id LIMIT ?""",
                (session_id, around_message_id, window),
            ).fetchall()
        rows = [*reversed(before_rows), anchor, *after_rows]
        return {
            "window": self._decode_message_rows(rows),
            "messages_before": len(before_rows),
            "messages_after": len(after_rows),
        }

    def _decode_message_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in rows:
            message = dict(row)
            message["content"] = self.db._decode_content(message.get("content"))
            if message.get("tool_calls"):
                try:
                    message["tool_calls"] = json.loads(message["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    message["tool_calls"] = []
            result.append(message)
        return result

    def _active_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        *,
        window: int,
        bookend: int,
    ) -> dict[str, Any]:
        primitive = self._bounded_window(
            session_id,
            around_message_id,
            window=window,
        )
        raw_window = primitive["window"]
        if not raw_window:
            return {
                "window": [],
                "messages_before": 0,
                "messages_after": 0,
                "bookend_start": [],
                "bookend_end": [],
            }
        filtered_window = [
            message
            for message in raw_window
            if message["id"] == around_message_id
            or message.get("role") in {"user", "assistant"}
        ]
        window_min_id = raw_window[0]["id"]
        window_max_id = raw_window[-1]["id"]
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            start_rows = conn.execute(
                f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                    WHERE session_id = ? AND id < ?
                      AND (active = 1 OR compacted = 1)
                      AND role IN ('user', 'assistant')
                      AND length(COALESCE(content, '')) > 0
                    ORDER BY id LIMIT ?""",
                (session_id, window_min_id, bookend),
            ).fetchall()
            end_rows = conn.execute(
                f"""SELECT {_MESSAGE_COLUMNS} FROM messages
                    WHERE session_id = ? AND id > ?
                      AND (active = 1 OR compacted = 1)
                      AND role IN ('user', 'assistant')
                      AND length(COALESCE(content, '')) > 0
                    ORDER BY id DESC LIMIT ?""",
                (session_id, window_max_id, bookend),
            ).fetchall()
        return {
            "window": filtered_window,
            "messages_before": primitive["messages_before"],
            "messages_after": primitive["messages_after"],
            "bookend_start": self._decode_message_rows(start_rows),
            "bookend_end": self._decode_message_rows(list(reversed(end_rows))),
        }

    def _enrich(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        session_ids = [str(row["session_id"]) for row in rows]
        links = self._links(session_ids)
        results: list[dict[str, Any]] = []
        for row in rows:
            session_id = str(row["session_id"])
            provider = row["external_provider"] or Provider.HERMES.value
            origin_kind = row["origin_kind"] or OriginKind.NATIVE.value
            session_links = links.get(session_id, [])
            result = {
                "session_id": session_id,
                "canonical_id": session_id,
                "native_id": row["native_id"] or session_id,
                "provider": provider,
                "source": row["source"],
                "title": row["title"],
                "model": row["model"],
                "started_at": row["started_at"],
                "last_active": row["last_active"],
                "when": _format_timestamp(row["started_at"]),
                "message_count": int(row["message_count"] or 0),
                "preview": _snippet(row["preview"]),
                "cwd": row["cwd"],
                "repo": row["repo"],
                "git_branch": row["git_branch"],
                "origin_kind": origin_kind,
                "origin_bridge_id": row["origin_bridge_id"],
                "mirror_state": row["mirror_state"],
                "links": session_links,
                "diverged": any(link["diverged_at"] is not None for link in session_links),
                "sync_health": (
                    "local"
                    if provider == Provider.HERMES.value
                    else "degraded"
                    if row["sync_error"] is not None
                    else "healthy"
                ),
                "native_status": row["native_status"],
                "archived": bool(row["archived"]),
            }
            if "match_message_id" in row.keys():
                result["_match_message_id"] = row["match_message_id"]
            results.append(result)
        return results

    def _links(self, session_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        placeholders = ",".join("?" for _ in session_ids)
        with self.db._lock:
            conn = self.db._conn
            assert conn is not None
            rows = conn.execute(
                f"""SELECT id, from_session_id, to_session_id, relation, bridge_id,
                           created_at, hydrated_at, diverged_at
                      FROM session_links
                     WHERE from_session_id IN ({placeholders})
                        OR to_session_id IN ({placeholders})
                     ORDER BY created_at, id""",
                [*session_ids, *session_ids],
            ).fetchall()
        known = set(session_ids)
        links: dict[str, list[dict[str, Any]]] = {session_id: [] for session_id in session_ids}
        for row in rows:
            summary = dict(row)
            if row["from_session_id"] in known:
                links[row["from_session_id"]].append(summary)
            if row["to_session_id"] in known and row["to_session_id"] != row["from_session_id"]:
                links[row["to_session_id"]].append(summary)
        return links


class _Filters:
    def __init__(
        self,
        *,
        provider: str | None,
        mirror_state: str | None,
        relation: str | None,
        cwd: str | None,
        repo: str | None,
        before: float | None,
        after: float | None,
    ) -> None:
        self.provider = provider
        self.mirror_state = mirror_state
        self.relation = relation
        self.cwd = cwd
        self.repo = repo
        self.before = before
        self.after = after

    @classmethod
    def create(
        cls,
        *,
        provider: str | None,
        mirror_state: str | None,
        relation: str | None,
        cwd: str | None,
        repo: str | None,
        before: float | None,
        after: float | None,
    ) -> _Filters:
        normalized_provider = _optional_choice(provider, "provider", _PROVIDERS)
        normalized_state = _optional_choice(
            mirror_state, "mirror state", _MIRROR_STATES
        )
        normalized_relation = None
        if relation is not None:
            try:
                normalized_relation = Relation(_text(relation, "relation", maximum=32)).value
            except ValueError as exc:
                raise ValueError(f"unknown relation: {relation!r}") from exc
        normalized_cwd = _optional_text(cwd, "cwd", maximum=32_768)
        normalized_repo = _optional_text(repo, "repo", maximum=32_768)
        normalized_before = _optional_finite(before, "before")
        normalized_after = _optional_finite(after, "after")
        if (
            normalized_before is not None
            and normalized_after is not None
            and normalized_after >= normalized_before
        ):
            raise ValueError("after must be earlier than before")
        return cls(
            provider=normalized_provider,
            mirror_state=normalized_state,
            relation=normalized_relation,
            cwd=normalized_cwd,
            repo=normalized_repo,
            before=normalized_before,
            after=normalized_after,
        )

    def sql(self) -> tuple[list[str], list[Any]]:
        where: list[str] = []
        params: list[Any] = []
        if self.provider is not None:
            where.append("COALESCE(e.provider, 'hermes') = ?")
            params.append(self.provider)
        if self.mirror_state is not None:
            where.append(f"({_MIRROR_STATE_SQL}) = ?")
            params.append(self.mirror_state)
        if self.relation is not None:
            where.append(
                """EXISTS (
                    SELECT 1 FROM session_links AS relation_link
                     WHERE (relation_link.from_session_id = s.id
                            OR relation_link.to_session_id = s.id)
                       AND relation_link.relation = ?
                )"""
            )
            params.append(self.relation)
        if self.cwd is not None:
            where.append("LOWER(COALESCE(s.cwd, '')) = LOWER(?)")
            params.append(self.cwd)
        if self.repo is not None:
            where.append("LOWER(COALESCE(s.git_repo_root, '')) = LOWER(?)")
            params.append(self.repo)
        if self.before is not None:
            where.append(f"({_LAST_ACTIVE_SQL}) < ?")
            params.append(self.before)
        if self.after is not None:
            where.append(f"({_LAST_ACTIVE_SQL}) > ?")
            params.append(self.after)
        return where, params


def _legacy_session_meta(session: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "when": session["when"],
        "source": session["source"],
        "model": session["model"],
        "title": session["title"],
    }


def _mirror_plan(
    session_id: str,
    target: Provider,
    would_enqueue: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "target_provider": target.value,
        "would_enqueue": would_enqueue,
        "reason": reason,
    }


def _external_provider(value: Provider | str) -> Provider:
    try:
        provider = Provider(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown target provider: {value!r}") from exc
    if provider not in (Provider.CLAUDE, Provider.CODEX):
        raise ValueError("target provider must be claude or codex")
    return provider


def _optional_choice(
    value: str | None,
    label: str,
    choices: frozenset[str],
) -> str | None:
    normalized = _optional_text(value, label, maximum=64)
    if normalized is None:
        return None
    normalized = normalized.lower()
    if normalized not in choices:
        raise ValueError(f"unknown {label}: {value!r}")
    return normalized


def _optional_text(value: str | None, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = _text(value, label, maximum=maximum, allow_empty=True)
    return normalized or None


def _text(
    value: object,
    label: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{label} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{label} is too long")
    return normalized


def _clamp_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        value = default
    return max(minimum, min(int(value), maximum))


def _optional_finite(value: object, label: str) -> float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _snippet(value: object, maximum: int = 240) -> str:
    if value is None:
        return ""
    compact = " ".join(str(value).split())
    return compact if len(compact) <= maximum else f"{compact[: maximum - 1]}…"


__all__ = ["UnifiedCatalog"]
