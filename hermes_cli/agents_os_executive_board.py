"""Local-first Doni/Kodi Executive Board domain model.

No model, network, credential or foreign-memory access lives here. The module stores
reviewed board records and enforces the owner-decision and shared-memory boundaries.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SECRET = re.compile(r"(?i)(api[_ -]?key|password|token|secret)\s*[:=]\s*\S{8,}")
_RAW = re.compile(
    r"(?i)(private\s+memory\s+(dump|export)|privatn\w*\s+memorij\w*\s+(dump|izvoz)|"
    r"raw\s+(chat|conversation|session|transcript|memory)(\s+(dump|transcript|export))?|"
    r"session\s+dump|sirovi\s+(prijepis|razgovor|session))"
)
_SCORE_KEYS = ("value", "feasibility", "risk", "cost", "time", "revenue")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _safe_text(value: str, field: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    if _SECRET.search(value) or _RAW.search(value):
        raise ValueError(f"{field} contains blocked private/secret-like content")
    return value


def _scorecard(value: dict[str, Any]) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(_SCORE_KEYS):
        raise ValueError(f"scorecard must contain exactly: {', '.join(_SCORE_KEYS)}")
    result = {key: int(value[key]) for key in _SCORE_KEYS}
    if any(number < 0 or number > 10 for number in result.values()):
        raise ValueError("scorecard values must be 0..10")
    return result


def ensure_executive_board_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS board_meetings (
          meeting_id TEXT PRIMARY KEY, objective TEXT NOT NULL, project_id TEXT,
          risk_class TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS board_proposals (
          proposal_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, agent_id TEXT NOT NULL,
          content TEXT NOT NULL, scorecard TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(meeting_id, agent_id), FOREIGN KEY(meeting_id) REFERENCES board_meetings(meeting_id)
        );
        CREATE TABLE IF NOT EXISTS board_challenges (
          challenge_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, challenger_id TEXT NOT NULL,
          target_agent_id TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(meeting_id, challenger_id, target_agent_id), FOREIGN KEY(meeting_id) REFERENCES board_meetings(meeting_id)
        );
        CREATE TABLE IF NOT EXISTS board_recommendations (
          recommendation_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL UNIQUE,
          recommendation TEXT NOT NULL, goal_prompt TEXT NOT NULL, consensus TEXT NOT NULL,
          dissent TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(meeting_id) REFERENCES board_meetings(meeting_id)
        );
        CREATE TABLE IF NOT EXISTS board_decisions (
          decision_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL UNIQUE, decision TEXT NOT NULL,
          decided_by TEXT NOT NULL, reason TEXT NOT NULL, decided_at TEXT NOT NULL,
          FOREIGN KEY(meeting_id) REFERENCES board_meetings(meeting_id)
        );
        CREATE TABLE IF NOT EXISTS board_memory_candidates (
          candidate_id TEXT PRIMARY KEY, capsule_id TEXT NOT NULL UNIQUE, capsule_sha256 TEXT NOT NULL,
          classification TEXT NOT NULL, summary TEXT NOT NULL, provenance TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, reviewed_at TEXT, approved_by TEXT, review_reason TEXT
        );
        CREATE TABLE IF NOT EXISTS board_shared_memory (
          memory_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE, capsule_id TEXT NOT NULL,
          classification TEXT NOT NULL, summary TEXT NOT NULL, provenance TEXT NOT NULL,
          approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
          FOREIGN KEY(candidate_id) REFERENCES board_memory_candidates(candidate_id)
        );
        CREATE TABLE IF NOT EXISTS company_projects (
          project_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
          next_action TEXT NOT NULL, revenue_potential INTEGER NOT NULL, strategic_value INTEGER NOT NULL,
          risk INTEGER NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS company_ideas (
          idea_id TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
          value INTEGER NOT NULL, feasibility INTEGER NOT NULL, risk INTEGER NOT NULL,
          cost INTEGER NOT NULL, time INTEGER NOT NULL, revenue INTEGER NOT NULL,
          opportunity_score INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


class ExecutiveBoardService:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        ensure_executive_board_schema(conn)

    def agent_roster(self) -> list[dict[str, Any]]:
        return [
            {"id": "doni", "name": "Doni", "status": "available", "role": "strategy-operations", "activation_enabled": True,
             "memory_boundary": "private-profile-plus-reviewed-shared", "auth_boundary": "profile-local-only"},
            {"id": "kodi", "name": "Kodi", "status": "available", "role": "technical-execution-review", "activation_enabled": True,
             "memory_boundary": "private-runtime-plus-reviewed-shared", "auth_boundary": "runtime-local-only"},
            {"id": "openclaw", "name": "ERO / OpenClaw", "status": "disabled", "role": "future-adapter", "activation_enabled": False,
             "memory_boundary": "no-private-memory-access", "auth_boundary": "no-credentials-configured"},
            {"id": "claude", "name": "Claude", "status": "disabled", "role": "future-adapter", "activation_enabled": False,
             "memory_boundary": "no-private-memory-access", "auth_boundary": "no-credentials-configured"},
        ]

    def create_meeting(self, objective: str, *, project_id: str | None = None, risk_class: str = "safe-local") -> str:
        objective = _safe_text(objective, "objective")[:4000]
        if risk_class not in {"safe-local", "approval-gated"}:
            raise ValueError("invalid risk_class")
        meeting_id, now = _id("board"), utc_now()
        self.conn.execute(
            "INSERT INTO board_meetings VALUES(?,?,?,?,?,?,?)",
            (meeting_id, objective, project_id, risk_class, "collecting-proposals", now, now),
        )
        self.conn.commit()
        return meeting_id

    def submit_proposal(self, meeting_id: str, agent_id: str, content: str, scorecard: dict[str, Any]) -> dict[str, Any]:
        if agent_id not in {"doni", "kodi"}:
            raise ValueError("proposal agent must be doni or kodi")
        self._meeting(meeting_id)
        proposal_id, now = _id("proposal"), utc_now()
        scores = _scorecard(scorecard)
        self.conn.execute(
            "INSERT INTO board_proposals VALUES(?,?,?,?,?,?)",
            (proposal_id, meeting_id, agent_id, _safe_text(content, "proposal")[:12000], _json(scores), now),
        )
        self.conn.execute("UPDATE board_meetings SET status='challenging', updated_at=? WHERE meeting_id=?", (now, meeting_id))
        self.conn.commit()
        return {"proposal_id": proposal_id, "meeting_id": meeting_id, "agent_id": agent_id, "scorecard": scores}

    def submit_challenge(self, meeting_id: str, challenger_id: str, target_agent_id: str, content: str) -> dict[str, Any]:
        if {challenger_id, target_agent_id} != {"doni", "kodi"}:
            raise ValueError("challenge must be mutual Doni↔Kodi")
        proposal_agents = {r[0] for r in self.conn.execute("SELECT agent_id FROM board_proposals WHERE meeting_id=?", (meeting_id,))}
        if proposal_agents != {"doni", "kodi"}:
            raise ValueError("both proposals are required before challenge")
        challenge_id, now = _id("challenge"), utc_now()
        self.conn.execute(
            "INSERT INTO board_challenges VALUES(?,?,?,?,?,?)",
            (challenge_id, meeting_id, challenger_id, target_agent_id, _safe_text(content, "challenge")[:8000], now),
        )
        self.conn.commit()
        return {"challenge_id": challenge_id, "meeting_id": meeting_id, "challenger_id": challenger_id, "target_agent_id": target_agent_id}

    def finalize_recommendation(self, meeting_id: str, recommendation: str, goal_prompt: str, *, consensus: str = "consensus", dissent: str = "") -> dict[str, Any]:
        if consensus not in {"consensus", "dissent"}:
            raise ValueError("consensus must be consensus or dissent")
        proposals = [dict(r) for r in self.conn.execute("SELECT * FROM board_proposals WHERE meeting_id=?", (meeting_id,))]
        proposal_agents = {p["agent_id"] for p in proposals}
        challenges = [dict(r) for r in self.conn.execute("SELECT * FROM board_challenges WHERE meeting_id=?", (meeting_id,))]
        directions = {(c["challenger_id"], c["target_agent_id"]) for c in challenges}
        if proposal_agents != {"doni", "kodi"} or directions != {("doni", "kodi"), ("kodi", "doni")}:
            raise ValueError("two proposals and mutual challenges are required")
        if consensus == "dissent" and not str(dissent).strip():
            raise ValueError("dissent text is required")
        recommendation_id, now = _id("recommendation"), utc_now()
        self.conn.execute(
            "INSERT INTO board_recommendations VALUES(?,?,?,?,?,?,?,?)",
            (recommendation_id, meeting_id, _safe_text(recommendation, "recommendation")[:16000],
             _safe_text(goal_prompt, "goal_prompt")[:16000], consensus, str(dissent).strip()[:8000], "needs-owner-decision", now),
        )
        self.conn.execute("UPDATE board_meetings SET status='needs-owner-decision', updated_at=? WHERE meeting_id=?", (now, meeting_id))
        self.conn.commit()
        return {"recommendation_id": recommendation_id, "meeting_id": meeting_id, "status": "needs-owner-decision",
                "proposal_agents": sorted(proposal_agents), "challenge_count": len(challenges), "consensus": consensus}

    def record_owner_decision(self, meeting_id: str, decision: str, *, decided_by: str, reason: str) -> dict[str, Any]:
        if str(decided_by).lower() != "goran":
            raise ValueError("only Goran can record the final decision")
        if decision not in {"approved", "rejected", "deferred"}:
            raise ValueError("invalid decision")
        if not self.conn.execute("SELECT 1 FROM board_recommendations WHERE meeting_id=?", (meeting_id,)).fetchone():
            raise ValueError("joint recommendation is required")
        decision_id, now = _id("decision"), utc_now()
        self.conn.execute("INSERT INTO board_decisions VALUES(?,?,?,?,?,?)", (decision_id, meeting_id, decision, "goran", _safe_text(reason, "reason")[:8000], now))
        self.conn.execute("UPDATE board_meetings SET status=?, updated_at=? WHERE meeting_id=?", (decision, now, meeting_id))
        self.conn.commit()
        return {"decision_id": decision_id, "meeting_id": meeting_id, "decision": decision, "decided_by": "goran"}

    def _meeting(self, meeting_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM board_meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
        if not row:
            raise ValueError("meeting not found")
        return dict(row)

    def meeting_snapshot(self, meeting_id: str) -> dict[str, Any]:
        meeting = self._meeting(meeting_id)
        proposals = []
        for row in self.conn.execute("SELECT * FROM board_proposals WHERE meeting_id=? ORDER BY created_at", (meeting_id,)):
            item = dict(row); item["scorecard"] = json.loads(item["scorecard"]); proposals.append(item)
        challenges = [dict(r) for r in self.conn.execute("SELECT * FROM board_challenges WHERE meeting_id=? ORDER BY created_at", (meeting_id,))]
        recommendation = _row(self.conn.execute("SELECT * FROM board_recommendations WHERE meeting_id=?", (meeting_id,)).fetchone())
        decision = _row(self.conn.execute("SELECT * FROM board_decisions WHERE meeting_id=?", (meeting_id,)).fetchone())
        return {"meeting": meeting, "proposals": proposals, "challenges": challenges, "recommendation": recommendation, "decision": decision}

    def stage_memory_candidate(self, capsule_id: str, capsule_sha256: str, classification: str, summary: str, provenance: list[dict[str, Any]]) -> dict[str, Any]:
        if classification not in {"P0", "P1"}:
            raise ValueError("only P0/P1 shared-memory candidates are allowed")
        if not _SHA256.fullmatch(str(capsule_sha256)):
            raise ValueError("invalid capsule SHA-256")
        if not isinstance(provenance, list) or not provenance:
            raise ValueError("provenance is required")
        for item in provenance:
            if not isinstance(item, dict) or not item.get("source_type") or not item.get("source_ref"):
                raise ValueError("invalid provenance item")
            _safe_text(_json(item), "provenance")
            if item.get("sha256") and not _SHA256.fullmatch(str(item["sha256"])):
                raise ValueError("invalid provenance SHA-256")
        candidate_id, now = _id("memory-candidate"), utc_now()
        self.conn.execute(
            "INSERT INTO board_memory_candidates(candidate_id,capsule_id,capsule_sha256,classification,summary,provenance,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (candidate_id, _safe_text(capsule_id, "capsule_id")[:200], capsule_sha256, classification,
             _safe_text(summary, "summary")[:8000], _json(provenance), "pending-review", now),
        )
        self.conn.commit()
        return {"candidate_id": candidate_id, "capsule_id": capsule_id, "status": "pending-review"}

    def promote_memory_candidate(self, candidate_id: str, *, approved_by: str, reason: str) -> dict[str, Any]:
        if str(approved_by).lower() != "goran":
            raise ValueError("only Goran can approve shared-memory promotion")
        row = self.conn.execute("SELECT * FROM board_memory_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        if not row or row["status"] != "pending-review":
            raise ValueError("pending candidate not found")
        now, memory_id = utc_now(), _id("shared-memory")
        self.conn.execute(
            "UPDATE board_memory_candidates SET status='approved',reviewed_at=?,approved_by='goran',review_reason=? WHERE candidate_id=?",
            (now, _safe_text(reason, "reason")[:4000], candidate_id),
        )
        self.conn.execute(
            "INSERT INTO board_shared_memory VALUES(?,?,?,?,?,?,?,?)",
            (memory_id, candidate_id, row["capsule_id"], row["classification"], row["summary"], row["provenance"], "goran", now),
        )
        self.conn.commit()
        return {"memory_id": memory_id, "candidate_id": candidate_id, "status": "approved", "approved_by": "goran"}

    def search_shared_memory(self, query: str) -> list[dict[str, Any]]:
        query = _safe_text(query, "query")[:500]
        rows = self.conn.execute(
            "SELECT * FROM board_shared_memory WHERE lower(summary) LIKE ? ORDER BY approved_at DESC LIMIT 50",
            (f"%{query.lower()}%",),
        ).fetchall()
        hits = []
        for row in rows:
            item = dict(row); item["provenance"] = json.loads(item["provenance"]); hits.append(item)
        return hits

    def upsert_project(self, project_id: str, name: str, *, status: str, owner: str, next_action: str, revenue_potential: int, strategic_value: int, risk: int) -> dict[str, Any]:
        values = [int(revenue_potential), int(strategic_value), int(risk)]
        if any(v < 0 or v > 10 for v in values):
            raise ValueError("project scores must be 0..10")
        now = utc_now()
        self.conn.execute(
            "INSERT INTO company_projects VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET name=excluded.name,status=excluded.status,owner=excluded.owner,next_action=excluded.next_action,revenue_potential=excluded.revenue_potential,strategic_value=excluded.strategic_value,risk=excluded.risk,updated_at=excluded.updated_at",
            (_safe_text(project_id, "project_id"), _safe_text(name, "name"), _safe_text(status, "status"), _safe_text(owner, "owner"), _safe_text(next_action, "next_action"), *values, now),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM company_projects WHERE project_id=?", (project_id,)).fetchone())

    def add_idea(self, idea_id: str, title: str, *, source: str, status: str, value: int, feasibility: int, risk: int, cost: int, time: int, revenue: int) -> dict[str, Any]:
        scores = _scorecard({"value": value, "feasibility": feasibility, "risk": risk, "cost": cost, "time": time, "revenue": revenue})
        opportunity = round(scores["value"] * 2 + scores["feasibility"] * 2 + scores["revenue"] * 2.5 + (10 - scores["risk"]) + (10 - scores["cost"]) + (10 - scores["time"]))
        now = utc_now()
        self.conn.execute(
            "INSERT INTO company_ideas VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_safe_text(idea_id, "idea_id"), _safe_text(title, "title"), _safe_text(source, "source"), _safe_text(status, "status"),
             scores["value"], scores["feasibility"], scores["risk"], scores["cost"], scores["time"], scores["revenue"], opportunity, now, now),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM company_ideas WHERE idea_id=?", (idea_id,)).fetchone())

    def company_snapshot(self) -> dict[str, Any]:
        projects = [dict(r) for r in self.conn.execute("SELECT * FROM company_projects ORDER BY strategic_value DESC, revenue_potential DESC")]
        ideas = [{**dict(r), "id": r["idea_id"]} for r in self.conn.execute("SELECT * FROM company_ideas ORDER BY opportunity_score DESC")]
        meetings = [dict(r) for r in self.conn.execute("SELECT * FROM board_meetings ORDER BY updated_at DESC")]
        decisions = [dict(r) for r in self.conn.execute("SELECT * FROM board_decisions ORDER BY decided_at DESC")]
        pending_memory = self.conn.execute("SELECT COUNT(*) FROM board_memory_candidates WHERE status='pending-review'").fetchone()[0]
        approved_memory = [dict(r) for r in self.conn.execute(
            "SELECT memory_id,candidate_id,capsule_id,classification,summary,provenance,approved_by,approved_at "
            "FROM board_shared_memory ORDER BY approved_at DESC LIMIT 50"
        )]
        for item in approved_memory:
            item["provenance"] = json.loads(item["provenance"])
        return {
            "company_overview": {"project_count": len(projects), "active_projects": [p for p in projects if p["status"] == "active"], "projects": projects},
            "idea_pipeline": {"count": len(ideas), "items": ideas},
            "money_opportunity": ideas,
            "execution_room": {"meetings": meetings, "active_count": sum(m["status"] not in {"approved", "rejected"} for m in meetings)},
            "decision_desk": {"pending": [m for m in meetings if m["status"] == "needs-owner-decision"], "decisions": decisions},
            "shared_knowledge": {"pending_review_count": pending_memory, "approved_count": len(approved_memory), "items": approved_memory, "identity_merge": False},
            "agent_roster": self.agent_roster(),
        }
