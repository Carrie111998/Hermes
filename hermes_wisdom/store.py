"""Profile-scoped crash-safe SQLite state for Collective Wisdom."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import get_hermes_home


SCHEMA_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def filesystem_identity(path: Path) -> str | None:
    stat = path.stat()
    return f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else None


class WisdomStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or (get_hermes_home() / "wisdom")
        self.path = self.root / "wisdom.db"
        self._lock = threading.RLock()
        self._prepare()

    def _prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_skill (
                  id TEXT PRIMARY KEY,
                  canonical_path TEXT NOT NULL,
                  fs_identity TEXT,
                  current_hash TEXT,
                  source_kind TEXT NOT NULL,
                  deleted_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS local_skill_fs_identity
                  ON local_skill(fs_identity) WHERE fs_identity IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS local_skill_active_path
                  ON local_skill(canonical_path) WHERE deleted_at IS NULL;
                CREATE TABLE IF NOT EXISTS snapshot (
                  skill_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  captured_at TEXT NOT NULL,
                  tree_json TEXT NOT NULL DEFAULT '{}',
                  PRIMARY KEY(skill_id, content_hash),
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS candidate (
                  skill_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  qualification TEXT NOT NULL,
                  state TEXT NOT NULL,
                  suggested_at TEXT,
                  dismissed_at TEXT,
                  PRIMARY KEY(skill_id, content_hash, qualification),
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS local_draft (
                  id TEXT PRIMARY KEY,
                  skill_id TEXT NOT NULL,
                  source_hash TEXT NOT NULL,
                  overlay_path TEXT NOT NULL,
                  draft_commit TEXT,
                  server_revision TEXT,
                  state TEXT NOT NULL,
                  description TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  description_hash TEXT NOT NULL,
                  manifest_hash TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS review_receipt (
                  id TEXT PRIMARY KEY,
                  draft_id TEXT NOT NULL UNIQUE,
                  server_revision TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  description_hash TEXT NOT NULL,
                  manifest_hash TEXT NOT NULL,
                  reviewed_at TEXT NOT NULL,
                  consumed_at TEXT,
                  FOREIGN KEY(draft_id) REFERENCES local_draft(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS installation_identity (
                  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                  installation_id TEXT NOT NULL,
                  verified_org_id TEXT,
                  verified_at TEXT,
                  disclosure_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_install (
                  skill_id TEXT PRIMARY KEY,
                  org_id TEXT NOT NULL,
                  slug TEXT NOT NULL,
                  version INTEGER NOT NULL CHECK(version > 0),
                  content_hash TEXT NOT NULL,
                  baseline_json TEXT NOT NULL,
                  target_path TEXT NOT NULL UNIQUE,
                  update_mode TEXT NOT NULL,
                  state TEXT NOT NULL,
                  installed_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_journal (
                  id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  entity_id TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(kind, entity_id, state)
                );
                CREATE TABLE IF NOT EXISTS usage_day (
                  skill_id TEXT NOT NULL,
                  day_utc TEXT NOT NULL,
                  use_count INTEGER NOT NULL CHECK(use_count >= 0),
                  PRIMARY KEY(skill_id, day_utc),
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS refinement (
                  skill_id TEXT NOT NULL,
                  from_hash TEXT NOT NULL,
                  to_hash TEXT NOT NULL,
                  classification TEXT NOT NULL,
                  structural_json TEXT NOT NULL,
                  recorded_at TEXT NOT NULL,
                  PRIMARY KEY(skill_id, to_hash),
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS stability_job (
                  skill_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  due_at TEXT NOT NULL,
                  state TEXT NOT NULL,
                  evaluated_at TEXT,
                  PRIMARY KEY(skill_id, content_hash),
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS local_event (
                  id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  session_id TEXT,
                  task_id TEXT,
                  skill_id TEXT NOT NULL,
                  content_hash TEXT NOT NULL,
                  qualification TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(kind, skill_id, content_hash, qualification),
                  FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(snapshot)").fetchall()
            }
            if "tree_json" not in columns:
                db.execute(
                    "ALTER TABLE snapshot ADD COLUMN tree_json TEXT NOT NULL DEFAULT '{}'"
                )
            candidate_sql = str(
                db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='candidate'"
                ).fetchone()[0]
            ).replace("\n", " ")
            if (
                "PRIMARY KEY(skill_id, content_hash, qualification)"
                not in candidate_sql
            ):
                db.executescript(
                    """
                    ALTER TABLE candidate RENAME TO candidate_v1;
                    CREATE TABLE candidate (
                      skill_id TEXT NOT NULL,
                      content_hash TEXT NOT NULL,
                      qualification TEXT NOT NULL,
                      state TEXT NOT NULL,
                      suggested_at TEXT,
                      dismissed_at TEXT,
                      PRIMARY KEY(skill_id, content_hash, qualification),
                      FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                    );
                    INSERT INTO candidate
                      SELECT skill_id,content_hash,qualification,state,suggested_at,dismissed_at
                      FROM candidate_v1;
                    DROP TABLE candidate_v1;
                    """
                )
            event_columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(local_event)").fetchall()
            }
            if "qualification" not in event_columns:
                db.executescript(
                    """
                    ALTER TABLE local_event RENAME TO local_event_v2;
                    CREATE TABLE local_event (
                      id TEXT PRIMARY KEY,
                      kind TEXT NOT NULL,
                      session_id TEXT,
                      task_id TEXT,
                      skill_id TEXT NOT NULL,
                      content_hash TEXT NOT NULL,
                      qualification TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      state TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      UNIQUE(kind, skill_id, content_hash, qualification),
                      FOREIGN KEY(skill_id) REFERENCES local_skill(id) ON DELETE CASCADE
                    );
                    INSERT INTO local_event
                      SELECT id,kind,session_id,task_id,skill_id,content_hash,
                        COALESCE(json_extract(payload_json,'$.qualification'),'legacy'),
                        payload_json,state,created_at
                      FROM local_event_v2;
                    DROP TABLE local_event_v2;
                    """
                )
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if db.in_transaction:
                    db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                db.close()

    def register_skill(
        self,
        path: Path,
        *,
        content_hash: str | None,
        source_kind: str,
        tree: dict[str, str] | None = None,
    ) -> str:
        resolved = path.resolve()
        fs_identity = filesystem_identity(resolved)
        now = utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM local_skill WHERE canonical_path=? AND deleted_at IS NULL",
                (str(resolved),),
            ).fetchone()
            if row is None and fs_identity:
                matches = db.execute(
                    "SELECT id FROM local_skill WHERE fs_identity=? AND deleted_at IS NULL",
                    (fs_identity,),
                ).fetchall()
                row = matches[0] if len(matches) == 1 else None
            if row is None and content_hash:
                matches = db.execute(
                    "SELECT id FROM local_skill WHERE current_hash=? AND canonical_path<>? AND deleted_at IS NOT NULL",
                    (content_hash, str(resolved)),
                ).fetchall()
                row = matches[0] if len(matches) == 1 else None
            skill_id = str(row["id"]) if row else str(uuid.uuid4())
            if row:
                db.execute(
                    "UPDATE local_skill SET canonical_path=?,fs_identity=?,current_hash=?,"
                    "source_kind=?,deleted_at=NULL,updated_at=? WHERE id=?",
                    (
                        str(resolved),
                        fs_identity,
                        content_hash,
                        source_kind,
                        now,
                        skill_id,
                    ),
                )
            else:
                db.execute(
                    "INSERT INTO local_skill VALUES(?,?,?,?,?,?,?,?)",
                    (
                        skill_id,
                        str(resolved),
                        fs_identity,
                        content_hash,
                        source_kind,
                        None,
                        now,
                        now,
                    ),
                )
            if content_hash:
                db.execute(
                    "INSERT OR IGNORE INTO snapshot(skill_id,content_hash,captured_at,tree_json) "
                    "VALUES(?,?,?,?)",
                    (
                        skill_id,
                        content_hash,
                        now,
                        json.dumps(tree or {}, sort_keys=True),
                    ),
                )
        return skill_id

    def latest_snapshot(self, skill_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM snapshot WHERE skill_id=? ORDER BY captured_at DESC LIMIT 1",
                (skill_id,),
            ).fetchone()
            if not row:
                return None
            value = dict(row)
            value["tree"] = json.loads(value.pop("tree_json"))
            return value

    def record_usage_day(
        self, skill_id: str, day_utc: str, *, retain_after: str
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO usage_day VALUES(?,?,1) ON CONFLICT(skill_id,day_utc) "
                "DO UPDATE SET use_count=MIN(use_count+1,2147483647)",
                (skill_id, day_utc),
            )
            db.execute(
                "DELETE FROM usage_day WHERE skill_id=? AND day_utc<?",
                (skill_id, retain_after),
            )

    def usage_days(self, skill_id: str, *, since: str) -> list[str]:
        with self.transaction() as db:
            return [
                str(row[0])
                for row in db.execute(
                    "SELECT day_utc FROM usage_day WHERE skill_id=? AND day_utc>=? ORDER BY day_utc",
                    (skill_id, since),
                ).fetchall()
            ]

    def record_refinement(
        self,
        skill_id: str,
        *,
        from_hash: str,
        to_hash: str,
        classification: str,
        structural: dict[str, Any],
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO refinement VALUES(?,?,?,?,?,?)",
                (
                    skill_id,
                    from_hash,
                    to_hash,
                    classification,
                    json.dumps(structural, sort_keys=True),
                    utc_now(),
                ),
            )

    def meaningful_refinement_count(self, skill_id: str, *, since: str) -> int:
        with self.transaction() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM refinement WHERE skill_id=? AND classification='meaningful' "
                "AND recorded_at>=?",
                (skill_id, since),
            ).fetchone()
            return int(row[0]) if row else 0

    def schedule_stability(self, skill_id: str, content_hash: str, due_at: str) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO stability_job VALUES(?,?,?,'pending',NULL) "
                "ON CONFLICT(skill_id,content_hash) DO UPDATE SET due_at=excluded.due_at,state='pending',evaluated_at=NULL",
                (skill_id, content_hash, due_at),
            )

    def due_stability_jobs(self, now: str) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM stability_job WHERE state='pending' AND due_at<=? ORDER BY due_at",
                    (now,),
                ).fetchall()
            ]

    def finish_stability_job(self, skill_id: str, content_hash: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE stability_job SET state='done',evaluated_at=? WHERE skill_id=? AND content_hash=?",
                (utc_now(), skill_id, content_hash),
            )

    def emit_local_event(
        self,
        *,
        kind: str,
        skill_id: str,
        content_hash: str,
        payload: dict[str, Any],
        session_id: str | None,
        task_id: str | None,
        qualification: str,
    ) -> str | None:
        event_id = str(uuid.uuid4())
        with self.transaction() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO candidate VALUES(?,?,?,'suggested',?,NULL)",
                (skill_id, content_hash, qualification, utc_now()),
            )
            if cursor.rowcount == 0:
                row = db.execute(
                    "SELECT state FROM candidate WHERE skill_id=? AND content_hash=? AND qualification=?",
                    (skill_id, content_hash, qualification),
                ).fetchone()
                if row and row["state"] == "dismissed":
                    return None
            cursor = db.execute(
                "INSERT OR IGNORE INTO local_event VALUES(?,?,?,?,?,?,?,?,'unread',?)",
                (
                    event_id,
                    kind,
                    session_id,
                    task_id,
                    skill_id,
                    content_hash,
                    qualification,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                ),
            )
            return event_id if cursor.rowcount else None

    def local_events(
        self, *, kind: str | None = None, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM local_event WHERE state!='dismissed'"
        params: list[str] = []
        if kind:
            query += " AND kind=?"
            params.append(kind)
        if session_id:
            query += " AND session_id=?"
            params.append(session_id)
        query += " ORDER BY created_at DESC"
        with self.transaction() as db:
            rows = [dict(row) for row in db.execute(query, params).fetchall()]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def dismiss_candidate(self, skill_id: str, content_hash: str) -> None:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                "UPDATE candidate SET state='dismissed',dismissed_at=? WHERE skill_id=? AND content_hash=?",
                (now, skill_id, content_hash),
            )
            db.execute(
                "UPDATE local_event SET state='dismissed' WHERE skill_id=? AND content_hash=?",
                (skill_id, content_hash),
            )

    def mark_missing_skills(self, seen_paths: set[str]) -> None:
        """Tombstone disappeared paths so delete/recreate gets a new identity."""
        now = utc_now()
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,canonical_path FROM local_skill WHERE deleted_at IS NULL"
            ).fetchall()
            for row in rows:
                if str(row["canonical_path"]) not in seen_paths:
                    db.execute(
                        "UPDATE local_skill SET deleted_at=?,updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )

    def installation_identity(self) -> str:
        with self.transaction() as db:
            row = db.execute(
                "SELECT installation_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            if row:
                return str(row["installation_id"])
            installation_id = "hwi_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO installation_identity VALUES(1,?,?,?,?)",
                (installation_id, None, None, utc_now()),
            )
            return installation_id

    def verify_installation_identity(self, org_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE installation_identity SET verified_org_id=?,verified_at=? WHERE singleton=1",
                (org_id, utc_now()),
            )

    def active_org_id(self) -> str | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT verified_org_id FROM installation_identity WHERE singleton=1"
            ).fetchone()
            return str(row[0]) if row and row[0] else None

    def record_draft(self, values: dict[str, str]) -> None:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO local_draft(
                   id,skill_id,source_hash,overlay_path,draft_commit,server_revision,state,
                   description,content_hash,description_hash,manifest_hash,created_at,updated_at
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET draft_commit=excluded.draft_commit,
                   server_revision=excluded.server_revision,state=excluded.state,
                   description=excluded.description,content_hash=excluded.content_hash,
                   description_hash=excluded.description_hash,manifest_hash=excluded.manifest_hash,
                   updated_at=excluded.updated_at""",
                (
                    values["id"],
                    values["skill_id"],
                    values["source_hash"],
                    values["overlay_path"],
                    values.get("draft_commit"),
                    values.get("server_revision"),
                    values["state"],
                    values["description"],
                    values["content_hash"],
                    values["description_hash"],
                    values["manifest_hash"],
                    now,
                    now,
                ),
            )

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_draft WHERE id=?", (draft_id,)
            ).fetchone()
            return dict(row) if row else None

    def prepared_draft(self, skill_id: str, source_hash: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM local_draft WHERE skill_id=? AND source_hash=? "
                "AND state='prepared' ORDER BY updated_at DESC LIMIT 1",
                (skill_id, source_hash),
            ).fetchone()
            return dict(row) if row else None

    def set_draft_state(self, draft_id: str, state: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE local_draft SET state=?,updated_at=? WHERE id=?",
                (state, utc_now(), draft_id),
            )

    def save_receipt(
        self,
        *,
        draft_id: str,
        server_revision: str,
        content_hash: str,
        description_hash: str,
        manifest_hash: str,
    ) -> str:
        receipt_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute("DELETE FROM review_receipt WHERE draft_id=?", (draft_id,))
            db.execute(
                "INSERT INTO review_receipt VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    receipt_id,
                    draft_id,
                    server_revision,
                    content_hash,
                    description_hash,
                    manifest_hash,
                    utc_now(),
                ),
            )
        return receipt_id

    def receipt(self, draft_id: str) -> dict[str, Any] | None:
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM review_receipt WHERE draft_id=? AND consumed_at IS NULL",
                (draft_id,),
            ).fetchone()
            return dict(row) if row else None

    def consume_receipt(self, draft_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE review_receipt SET consumed_at=? WHERE draft_id=?",
                (utc_now(), draft_id),
            )

    def journal(
        self, kind: str, entity_id: str, phase: str, payload: dict[str, Any]
    ) -> str:
        operation_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute(
                "INSERT INTO operation_journal VALUES(?,?,?,?,?,'pending',?,?)",
                (
                    operation_id,
                    kind,
                    entity_id,
                    phase,
                    json.dumps(payload, sort_keys=True),
                    utc_now(),
                    utc_now(),
                ),
            )
        return operation_id

    def advance(self, operation_id: str, phase: str, *, done: bool = False) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE operation_journal SET phase=?,state=?,updated_at=? WHERE id=?",
                (phase, "done" if done else "pending", utc_now(), operation_id),
            )

    def pending_operations(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM operation_journal WHERE state='pending' ORDER BY created_at"
                ).fetchall()
            ]

    def record_install(self, values: dict[str, Any]) -> None:
        now = utc_now()
        with self.transaction() as db:
            db.execute(
                """INSERT INTO managed_install VALUES(?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(skill_id) DO UPDATE SET org_id=excluded.org_id,
                   slug=excluded.slug,version=excluded.version,content_hash=excluded.content_hash,
                   baseline_json=excluded.baseline_json,target_path=excluded.target_path,
                   update_mode=excluded.update_mode,state=excluded.state,updated_at=excluded.updated_at""",
                (
                    values["skill_id"],
                    values["org_id"],
                    values["slug"],
                    int(values["version"]),
                    values["content_hash"],
                    json.dumps(values["baseline"], sort_keys=True),
                    values["target_path"],
                    values["update_mode"],
                    values.get("state", "active"),
                    now,
                    now,
                ),
            )

    def installations(self) -> list[dict[str, Any]]:
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM managed_install ORDER BY slug"
                ).fetchall()
            ]
