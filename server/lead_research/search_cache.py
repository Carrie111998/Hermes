"""Durable negative-search cache with explicit public/private scope."""
from __future__ import annotations

from typing import Literal

from ..db import new_id, now
from .models import ResearchQuery, SearchAttempt, SearchScope


def query_scope(query: ResearchQuery) -> SearchScope:
    shareable = not (
        query.customer_terms or query.hidden_label_ids or query.licensed_source_ids
    )
    return SearchScope(
        company_id=None if shareable else query.company_id,
        shareable=shareable,
    )


class SearchAttemptRepository:
    def __init__(self, db) -> None:
        self.db = db

    @staticmethod
    def _where(scope: SearchScope) -> tuple[str, tuple]:
        if scope.shareable:
            return "shareable=1 AND company_id IS NULL", ()
        return "shareable=0 AND company_id=?", (scope.company_id,)

    @staticmethod
    def _model(row) -> SearchAttempt:
        return SearchAttempt(
            id=row["id"],
            scope=SearchScope(
                company_id=row["company_id"], shareable=bool(row["shareable"]),
            ),
            organization_id=row["organization_id"],
            field=row["field"],
            query_hash=row["query_hash"],
            source_id=row["source_id"],
            status=row["status"],
            reason=row["reason"],
            request_count=row["request_count"],
            attempted_at=row["attempted_at"],
            retry_after=row["retry_after"],
        )

    def lookup(
        self, scope: SearchScope, query_hash: str, at: float,
    ) -> SearchAttempt | None:
        where, scope_params = self._where(scope)
        row = self.db.one(
            f"SELECT * FROM research_search_attempts WHERE {where} "
            "AND query_hash=? AND retry_after>? ORDER BY attempted_at DESC LIMIT 1",
            (*scope_params, query_hash, at),
        )
        return self._model(row) if row else None

    def _record(
        self,
        scope: SearchScope,
        query_hash: str,
        status: Literal["empty", "failed", "succeeded"],
        reason: str | None,
        retry_after: float,
        *,
        organization_id: str = "",
        field: str = "",
        source_id: str = "",
        request_count: int = 1,
        attempted_at: float | None = None,
    ) -> SearchAttempt:
        attempted_at = now() if attempted_at is None else attempted_at
        where, scope_params = self._where(scope)
        existing = self.db.one(
            f"SELECT id FROM research_search_attempts WHERE {where} AND query_hash=?",
            (*scope_params, query_hash),
        )
        attempt_id = existing["id"] if existing else new_id("search")
        if existing:
            self.db.execute(
                "UPDATE research_search_attempts SET organization_id=?,field=?,source_id=?,"
                "status=?,reason=?,request_count=?,attempted_at=?,retry_after=?,updated_at=? "
                "WHERE id=?",
                (
                    organization_id, field, source_id, status, reason, request_count,
                    attempted_at, retry_after, now(), attempt_id,
                ),
            )
        else:
            self.db.execute(
                "INSERT INTO research_search_attempts("
                "id,company_id,shareable,organization_id,field,query_hash,source_id,status,"
                "reason,request_count,attempted_at,retry_after,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, scope.company_id, int(scope.shareable), organization_id,
                    field, query_hash, source_id, status, reason, request_count,
                    attempted_at, retry_after, now(), now(),
                ),
            )
        row = self.db.one("SELECT * FROM research_search_attempts WHERE id=?", (attempt_id,))
        return self._model(row)

    def record_empty(
        self, scope: SearchScope, query_hash: str, retry_after: float, **details,
    ) -> SearchAttempt:
        return self._record(scope, query_hash, "empty", None, retry_after, **details)

    def record_failure(
        self,
        scope: SearchScope,
        query_hash: str,
        reason: str,
        retry_after: float,
        **details,
    ) -> SearchAttempt:
        return self._record(scope, query_hash, "failed", reason, retry_after, **details)

    def record_succeeded(
        self, scope: SearchScope, query_hash: str, retry_after: float, **details,
    ) -> SearchAttempt:
        return self._record(scope, query_hash, "succeeded", None, retry_after, **details)
