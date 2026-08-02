"""SQLite persistence for the Amorphous Applications PoC.

Tables:
  layouts        versioned dashboard specs per user (append-only; active = latest applied)
  events         raw interaction telemetry
  proposals      curator-proposed mutation sets awaiting approval
  workflows      repeatable agent workflows surfaced as dashboard shortcuts
  workflow_runs  execution history + results
  feedback       user feedback on proposals / evolution
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS layouts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'seed',      -- seed | user | curator | rebuild
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_layouts_user ON layouts(user_id, version);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,                        -- view|click|hover|focus_dwell|hide|move|resize|workflow_run|chat|proposal_action
    component_id TEXT,
    payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events(user_id, ts);

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | rejected | superseded
    summary TEXT NOT NULL,
    rationale TEXT,
    mutations_json TEXT NOT NULL,
    engine TEXT NOT NULL DEFAULT 'heuristic',  -- heuristic | llm | rebuild
    resolved_at REAL,
    feedback TEXT
);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    prompt_template TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'user',   -- user | curator | seed
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,                      -- ok | error
    prompt TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_wf ON workflow_runs(workflow_id, ts);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    ts REAL NOT NULL,
    proposal_id TEXT,
    sentiment TEXT,                            -- up | down | neutral
    text TEXT
);
"""


def _uid() -> str:
    return uuid.uuid4().hex[:12]


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- layouts ----------
    def get_active_layout(self, user_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM layouts WHERE user_id=? ORDER BY version DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        spec = json.loads(row["spec_json"])
        spec["_meta"] = {
            "version": row["version"],
            "source": row["source"],
            "created_at": row["created_at"],
        }
        return spec

    def save_layout(self, user_id: str, spec: dict, source: str = "user") -> int:
        spec = {k: v for k, v in spec.items() if k != "_meta"}
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM layouts WHERE user_id=?",
                (user_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            self._conn.execute(
                "INSERT INTO layouts (id,user_id,version,spec_json,source,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (_uid(), user_id, version, json.dumps(spec), source, time.time()),
            )
            self._conn.commit()
        return version

    def layout_history(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version,source,created_at FROM layouts WHERE user_id=?"
                " ORDER BY version DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_users(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT user_id FROM layouts"
            ).fetchall()
        return [r["user_id"] for r in rows]

    # ---------- telemetry ----------
    def record_event(self, user_id: str, etype: str, component_id: str | None,
                     payload: Any = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (id,user_id,ts,type,component_id,payload_json)"
                " VALUES (?,?,?,?,?,?)",
                (_uid(), user_id, time.time(), etype, component_id,
                 json.dumps(payload) if payload is not None else None),
            )
            self._conn.commit()

    def events_since(self, user_id: str, since_ts: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE user_id=? AND ts>=? ORDER BY ts",
                (user_id, since_ts),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json")) if d.get("payload_json") else None
            out.append(d)
        return out

    def usage_stats(self, user_id: str, since_ts: float) -> dict:
        """Aggregate per-component usage for the curator."""
        events = self.events_since(user_id, since_ts)
        comp: dict[str, dict] = {}
        chat_prompts: list[str] = []
        for ev in events:
            cid = ev.get("component_id")
            if cid:
                c = comp.setdefault(cid, {"clicks": 0, "dwell_s": 0.0, "views": 0,
                                          "workflow_runs": 0, "hidden": 0, "moved": 0})
                t = ev["type"]
                if t == "click":
                    c["clicks"] += 1
                elif t == "focus_dwell":
                    c["dwell_s"] += float((ev.get("payload") or {}).get("seconds", 0))
                elif t == "view":
                    c["views"] += 1
                elif t == "workflow_run":
                    c["workflow_runs"] += 1
                elif t == "hide":
                    c["hidden"] += 1
                elif t in ("move", "resize"):
                    c["moved"] += 1
            if ev["type"] == "chat":
                text = (ev.get("payload") or {}).get("text", "")
                if text:
                    chat_prompts.append(text)
        return {"components": comp, "chat_prompts": chat_prompts,
                "event_count": len(events)}

    # ---------- proposals ----------
    def create_proposal(self, user_id: str, summary: str, mutations: list[dict],
                        rationale: str = "", engine: str = "heuristic") -> Optional[dict]:
        pid = _uid()
        with self._lock:
            self._conn.execute(
                "INSERT INTO proposals (id,user_id,created_at,status,summary,rationale,"
                "mutations_json,engine) VALUES (?,?,?,?,?,?,?,?)",
                (pid, user_id, time.time(), "pending", summary, rationale,
                 json.dumps(mutations), engine),
            )
            self._conn.commit()
        return self.get_proposal(pid)

    def get_proposal(self, pid: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["mutations"] = json.loads(d.pop("mutations_json"))
        return d

    def list_proposals(self, user_id: str, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM proposals WHERE user_id=?"
        args: list = [user_id]
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT 50"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["mutations"] = json.loads(d.pop("mutations_json"))
            out.append(d)
        return out

    def resolve_proposal(self, pid: str, status: str, feedback: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE proposals SET status=?, resolved_at=?, feedback=? WHERE id=?",
                (status, time.time(), feedback, pid),
            )
            self._conn.commit()

    def rejected_mutations(self, user_id: str, within_s: float = 7 * 86400) -> list[dict]:
        """Mutations from recently-rejected proposals, each tagged with the
        rejection feedback. The curator uses this as negative guidance."""
        cutoff = time.time() - within_s
        with self._lock:
            rows = self._conn.execute(
                "SELECT mutations_json, feedback, resolved_at FROM proposals"
                " WHERE user_id=? AND status='rejected' AND resolved_at>=?"
                " ORDER BY resolved_at DESC LIMIT 20",
                (user_id, cutoff),
            ).fetchall()
        out = []
        for r in rows:
            for m in json.loads(r["mutations_json"]):
                out.append({"mutation": m, "feedback": r["feedback"] or "",
                            "when": r["resolved_at"]})
        return out

    # ---------- workflows ----------
    def create_workflow(self, user_id: str, name: str, prompt_template: str,
                        description: str = "", created_by: str = "user",
                        wf_id: str | None = None) -> dict:
        wf_id = wf_id or _uid()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO workflows (id,user_id,name,description,"
                "prompt_template,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                (wf_id, user_id, name, description, prompt_template, created_by,
                 time.time()),
            )
            self._conn.commit()
        return {"id": wf_id, "name": name, "description": description,
                "prompt_template": prompt_template, "created_by": created_by}

    def get_workflow(self, wf_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM workflows WHERE id=?", (wf_id,)).fetchone()
        return dict(row) if row else None

    def list_workflows(self, user_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflows WHERE user_id=? ORDER BY created_at", (user_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def record_workflow_run(self, wf_id: str, user_id: str, status: str,
                            prompt: str, result: str) -> dict:
        rid = _uid()
        with self._lock:
            self._conn.execute(
                "INSERT INTO workflow_runs (id,workflow_id,user_id,ts,status,prompt,result)"
                " VALUES (?,?,?,?,?,?,?)",
                (rid, wf_id, user_id, time.time(), status, prompt, result),
            )
            self._conn.commit()
        return {"id": rid, "workflow_id": wf_id, "status": status, "result": result}

    def workflow_runs(self, wf_id: str, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY ts DESC LIMIT ?",
                (wf_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- feedback ----------
    def add_feedback(self, user_id: str, proposal_id: str | None, sentiment: str,
                     text: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO feedback (id,user_id,ts,proposal_id,sentiment,text)"
                " VALUES (?,?,?,?,?,?)",
                (_uid(), user_id, time.time(), proposal_id, sentiment, text),
            )
            self._conn.commit()

    def recent_feedback(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM feedback WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
