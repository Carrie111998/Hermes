"""Bounded read/control service for Kanban security surfaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from hermes_cli.kanban_store.events import read_events
from hermes_cli.kanban_store.publication import approve_intent
from hermes_cli.kanban_store.types import ContractError


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    roles: frozenset[str]

    def require(self, role: str) -> None:
        if role not in self.roles:
            raise PermissionError(f"role required: {role}")


class KanbanSecurityService:
    def __init__(self, *, conn) -> None:
        self.conn = conn

    def run_summary(self, actor: Actor, *, task_id: str, run_id: int) -> dict[str, object]:
        actor.require("kanban.read")
        row = self.conn.execute(
            """
            SELECT r.id, r.task_id, r.profile, r.status, r.started_at, r.ended_at,
                   r.outcome, r.summary, r.claim_generation, r.finalized_at,
                   t.status AS task_status, t.publication_state
              FROM task_runs r JOIN tasks t ON t.id=r.task_id
             WHERE r.id=? AND r.task_id=?
            """,
            (run_id, task_id),
        ).fetchone()
        if not row:
            raise KeyError((task_id, run_id))
        return dict(row)

    def event_page(
        self,
        actor: Actor,
        *,
        task_id: str,
        cursor: str | None,
        limit: int = 100,
    ) -> dict[str, object]:
        actor.require("kanban.read")
        if limit < 1 or limit > 500:
            raise ContractError("event page limit must be 1..500")
        return read_events(self.conn, task_id=task_id, cursor=cursor, limit=limit)

    def publication_queue(self, actor: Actor, *, limit: int = 100) -> list[dict[str, object]]:
        actor.require("kanban.publication.read")
        if limit < 1 or limit > 500:
            raise ContractError("queue limit must be 1..500")
        rows = self.conn.execute(
            """
            SELECT intent_id, task_id, run_id, claim_generation, kind, required,
                   state, publisher_principal, adapter_version, target_json,
                   marker, wire_sha256, created_at
              FROM publication_intents
             WHERE state IN ('sealed','approved','dispatch_claimed','dispatch_started',
                             'reconcile_required','conflict')
             ORDER BY created_at, intent_id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        values: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["target"] = json.loads(item.pop("target_json"))
            values.append(item)
        return values

    def approve(
        self,
        actor: Actor,
        *,
        intent_id: str,
        wire_sha256: str,
        decision: str,
        reason: str | None,
    ) -> str:
        actor.require("kanban.publication.approve")
        return approve_intent(
            self.conn,
            intent_id=intent_id,
            wire_sha256=wire_sha256,
            actor=actor.actor_id,
            decision=decision,
            reason=reason,
        )

    def evidence_vector(self, actor: Actor, *, task_id: str, run_id: int) -> dict[str, object]:
        actor.require("kanban.read")
        rows = self.conn.execute(
            """
            SELECT observation_id, source, observed_at, fresh_until, detail_json
              FROM run_observations WHERE task_id=? AND run_id=?
             ORDER BY observed_at, observation_id
            """,
            (task_id, run_id),
        ).fetchall()
        return {
            "task_id": task_id,
            "run_id": run_id,
            "observations": [
                {
                    "observation_id": row[0],
                    "source": row[1],
                    "observed_at": row[2],
                    "fresh_until": row[3],
                    "detail": json.loads(row[4]),
                }
                for row in rows
            ],
        }
