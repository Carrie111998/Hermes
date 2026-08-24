"""Append-audited hidden research labels, never customer serialization data."""
from __future__ import annotations

from typing import Literal

from ..db import new_id, now
from .models import LabelAssignment


class LabelRepository:
    def __init__(self, db) -> None:
        self.db = db

    @staticmethod
    def _model(row) -> LabelAssignment:
        return LabelAssignment(**dict(row))

    def assign(
        self,
        company_id: str,
        result_id: str,
        label_id: str,
        value: str,
        scope: str,
        source: Literal["system", "admin", "outcome_analysis"],
        actor_id: str,
        reason: str,
        profile_version_id: str,
    ) -> LabelAssignment:
        result = self.db.one(
            "SELECT id FROM research_results WHERE id=? AND company_id=?",
            (result_id, company_id),
        )
        if result is None:
            raise ValueError("research result is outside the tenant")
        stamp = now()
        self.db.execute(
            "UPDATE research_label_assignments SET effective_until=? "
            "WHERE company_id=? AND result_id=? AND label_id=? AND effective_until IS NULL",
            (stamp, company_id, result_id, label_id),
        )
        assignment_id = new_id("label")
        self.db.execute(
            "INSERT INTO research_label_assignments("
            "id,company_id,result_id,label_id,value,scope,source,actor_id,reason,"
            "profile_version_id,effective_from,effective_until) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                assignment_id, company_id, result_id, label_id, value, scope,
                source, actor_id, reason, profile_version_id, stamp,
            ),
        )
        return self._model(self.db.one(
            "SELECT * FROM research_label_assignments WHERE id=?", (assignment_id,),
        ))

    def history(self, company_id: str, result_id: str) -> list[LabelAssignment]:
        return [self._model(row) for row in self.db.all(
            "SELECT * FROM research_label_assignments "
            "WHERE company_id=? AND result_id=? ORDER BY effective_from,id",
            (company_id, result_id),
        )]
