"""Staged, role-separated self-improvement for workspace knowledge."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

_ALLOWED_DESTINATIONS = frozenset({"memory", "notion", "project_doc", "skill", "user_memory"})
_ALLOWED_RISKS = frozenset({"low", "medium", "high"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_INSTANCE_ID = uuid.uuid4().hex
_ATTEMPT_LEASE_SECONDS = 60.0
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d .()-]{7,}\d)(?!\w)")
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"),
)


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _attempt_is_stale(payload: dict[str, Any], now: float) -> bool:
    owner_pid = int(payload.get("owner_pid") or 0)
    owner_instance_id = str(payload.get("owner_instance_id") or "")
    lease_expires_at = float(payload.get("lease_expires_at") or 0)
    if owner_pid == os.getpid() and owner_instance_id == _PROCESS_INSTANCE_ID:
        return lease_expires_at < now
    if owner_pid and not _process_is_alive(owner_pid):
        return True
    return lease_expires_at < now


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sanitize_text(value: str) -> tuple[str, int]:
    text = value
    redactions = 0
    for pattern in _SECRET_PATTERNS:
        text, count = pattern.subn("[REDACTED]", text)
        redactions += count
    text, count = _EMAIL_RE.subn("[REDACTED_EMAIL]", text)
    redactions += count
    text, count = _PHONE_RE.subn("[REDACTED_PHONE]", text)
    redactions += count
    return text.strip(), redactions


def _sanitize_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        result = []
        total = 0
        for item in value:
            sanitized, count = _sanitize_value(item)
            result.append(sanitized)
            total += count
        return result, total
    if isinstance(value, dict):
        result = {}
        total = 0
        for key, item in value.items():
            sanitized, count = _sanitize_value(item)
            result[str(key)] = sanitized
            total += count
        return result, total
    return value, 0


def _validate_metrics(metrics: dict[str, Any]) -> dict[str, float | int]:
    required = {"cases", "cost", "latency_ms", "safety_failures", "successes"}
    if set(metrics) != required:
        raise ValueError("learning metrics are malformed")
    for key in ("cases", "safety_failures", "successes"):
        value = metrics[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("learning integer metrics are invalid")
    for key in ("cost", "latency_ms"):
        value = metrics[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("learning numeric metrics are invalid")
    normalized: dict[str, float | int] = {
        "cases": int(metrics["cases"]),
        "cost": float(metrics["cost"]),
        "latency_ms": float(metrics["latency_ms"]),
        "safety_failures": int(metrics["safety_failures"]),
        "successes": int(metrics["successes"]),
    }
    if (
        not math.isfinite(float(normalized["cost"]))
        or not math.isfinite(float(normalized["latency_ms"]))
        or normalized["cases"] <= 0
        or normalized["successes"] < 0
        or normalized["successes"] > normalized["cases"]
        or normalized["safety_failures"] < 0
        or normalized["latency_ms"] < 0
        or normalized["cost"] < 0
    ):
        raise ValueError("learning metrics are invalid")
    return normalized


def _evaluation_passes(
    baseline: dict[str, float | int],
    candidate: dict[str, float | int],
) -> tuple[bool, list[str]]:
    failures = []
    baseline_rate = float(baseline["successes"]) / float(baseline["cases"])
    candidate_rate = float(candidate["successes"]) / float(candidate["cases"])
    if candidate_rate < baseline_rate:
        failures.append("success regression")
    if int(candidate["safety_failures"]) > 0:
        failures.append("safety failure")
    if float(candidate["latency_ms"]) > max(float(baseline["latency_ms"]) * 1.25, 1):
        failures.append("latency regression")
    if float(candidate["cost"]) > max(float(baseline["cost"]) * 1.25, 0.01):
        failures.append("cost regression")
    return not failures, failures


class LearningDestinationAdapter(Protocol):
    def resource_key(self, candidate: dict[str, Any]) -> str: ...

    def current_digest(self, candidate: dict[str, Any]) -> str: ...

    def canary(self, candidate: dict[str, Any]) -> dict[str, Any]: ...

    def snapshot(self, candidate: dict[str, Any]) -> str: ...

    def apply(self, candidate: dict[str, Any]) -> dict[str, Any]: ...

    def restore(
        self,
        candidate: dict[str, Any],
        snapshot_id: str,
        *,
        attempt_id: str,
    ) -> dict[str, Any]: ...


class LearningStore:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        if os.name != "nt":
            self.path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspace_learning_signals (
                signal_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                content TEXT NOT NULL,
                kind TEXT NOT NULL,
                project_id TEXT,
                provenance_json TEXT NOT NULL,
                redactions INTEGER NOT NULL,
                reusable INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_learning_candidates (
                candidate_id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                proposer_id TEXT NOT NULL,
                evaluator_id TEXT,
                evaluation_json TEXT,
                approved_by TEXT,
                approval_json TEXT,
                canary_json TEXT,
                promoter_id TEXT,
                application_json TEXT,
                quarantine_reason TEXT,
                expires_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS workspace_learning_active_digest_idx
                ON workspace_learning_candidates(destination, content_digest)
                WHERE status IN ('staged','approval_pending','approved','canary_passed',
                                 'applying','apply_uncertain');
            CREATE TABLE IF NOT EXISTS workspace_learning_candidate_signals (
                candidate_id TEXT NOT NULL REFERENCES workspace_learning_candidates(candidate_id),
                signal_id TEXT NOT NULL REFERENCES workspace_learning_signals(signal_id),
                PRIMARY KEY(candidate_id, signal_id)
            );
            CREATE TABLE IF NOT EXISTS workspace_learning_resources (
                resource_key TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES workspace_learning_candidates(candidate_id),
                state TEXT NOT NULL,
                post_digest TEXT,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_learning_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL REFERENCES workspace_learning_candidates(candidate_id),
                actor_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self._connection.execute("DROP INDEX IF EXISTS workspace_learning_active_digest_idx")
        self._connection.execute(
            """
            CREATE UNIQUE INDEX workspace_learning_active_digest_idx
            ON workspace_learning_candidates(destination, content_digest)
            WHERE status IN (
                'staged','approval_pending','approved','canary_passed',
                'applying','apply_uncertain'
            )
            """
        )
        self._connection.commit()
        self.recover_stale_applications(now=time.time())

    def close(self) -> None:
        self._connection.close()

    def recover_stale_applications(
        self,
        *,
        now: float | None = None,
        grace_seconds: float = 300,
    ) -> int:
        del grace_seconds  # retained for API compatibility; attempts carry their own lease.
        timestamp = time.time() if now is None else now
        recovered = 0
        with self._lock:
            rows = self._connection.execute(
                "SELECT candidate_id,status,application_json FROM workspace_learning_candidates "
                "WHERE status IN ('applying','rolling_back')"
            ).fetchall()
            for row in rows:
                original_application_json = str(row["application_json"] or "")
                application = json.loads(original_application_json or "{}")
                attempt = (
                    dict(application.get("rollback") or {})
                    if row["status"] == "rolling_back"
                    else application
                )
                if not _attempt_is_stale(attempt, timestamp):
                    continue
                candidate_id = str(row["candidate_id"])
                status = "rollback_uncertain" if row["status"] == "rolling_back" else "apply_uncertain"
                cursor = self._connection.execute(
                    "UPDATE workspace_learning_candidates SET status=?,updated_at=? "
                    "WHERE candidate_id=? AND status=? AND application_json=?",
                    (status, timestamp, candidate_id, row["status"], original_application_json),
                )
                if cursor.rowcount != 1:
                    continue
                resource_key = str(application.get("resource_key") or "")
                if resource_key:
                    self._connection.execute(
                        "UPDATE workspace_learning_resources SET state=?,updated_at=? "
                        "WHERE resource_key=? AND candidate_id=?",
                        (status, timestamp, resource_key, candidate_id),
                    )
                recovered += 1
                self._event(
                    candidate_id,
                    "system",
                    f"candidate.{status}",
                    {"reason": "destination operation owner stopped or lease expired"},
                    timestamp,
                )
            self._connection.commit()
        return recovered

    def _event(
        self,
        candidate_id: str,
        actor_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: float,
    ) -> None:
        self._connection.execute(
            "INSERT INTO workspace_learning_events(candidate_id,actor_id,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (candidate_id, actor_id, event_type, _canonical(payload), now),
        )

    def record_signal(
        self,
        *,
        actor_id: str,
        content: str,
        kind: str,
        project_id: str | None,
        provenance: list[dict[str, str]],
        reusable: bool,
        now: float | None = None,
    ) -> dict[str, Any]:
        actor_id = actor_id.strip()
        kind = kind.strip()
        if not actor_id or not kind or not content.strip():
            raise ValueError("learning signal identity, kind, and content are required")
        if not provenance or any(
            not isinstance(item, dict)
            or not str(item.get("source") or "").strip()
            or not str(item.get("ref") or "").strip()
            for item in provenance
        ):
            raise ValueError("learning signal provenance is required")
        sanitized, redactions = _sanitize_text(content)
        clean_provenance, provenance_redactions = _sanitize_value(provenance)
        timestamp = time.time() if now is None else now
        signal_id = f"signal_{uuid.uuid4().hex}"
        fingerprint = _digest(
            {"content": " ".join(sanitized.lower().split()), "kind": kind, "project_id": project_id}
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO workspace_learning_signals(
                    signal_id,actor_id,content,kind,project_id,provenance_json,
                    redactions,reusable,fingerprint,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    signal_id,
                    actor_id,
                    sanitized,
                    kind,
                    project_id,
                    _canonical(clean_provenance),
                    redactions + provenance_redactions,
                    int(reusable),
                    fingerprint,
                    timestamp,
                ),
            )
            self._connection.commit()
        return {
            "actor_id": actor_id,
            "content": sanitized,
            "created_at": timestamp,
            "fingerprint": fingerprint,
            "kind": kind,
            "project_id": project_id,
            "provenance": clean_provenance,
            "redactions": redactions + provenance_redactions,
            "reusable": bool(reusable),
            "signal_id": signal_id,
        }

    def matching_signal_ids(self, fingerprint: str, *, limit: int = 100) -> list[str]:
        if not fingerprint or limit < 1 or limit > 1000:
            raise ValueError("learning signal fingerprint or limit is invalid")
        with self._lock:
            rows = self._connection.execute(
                "SELECT signal_id FROM workspace_learning_signals "
                "WHERE fingerprint=? AND reusable=1 ORDER BY created_at ASC LIMIT ?",
                (fingerprint, limit),
            ).fetchall()
        return [str(row["signal_id"]) for row in rows]

    def _signals(self, signal_ids: list[str]) -> list[sqlite3.Row]:
        unique = list(dict.fromkeys(signal_ids))
        if not unique:
            raise ValueError("candidate evidence is required")
        placeholders = ",".join("?" for _ in unique)
        rows = self._connection.execute(
            f"SELECT * FROM workspace_learning_signals WHERE signal_id IN ({placeholders})",
            unique,
        ).fetchall()
        if len(rows) != len(unique):
            raise ValueError("candidate evidence is unknown")
        return rows

    def propose_candidate(
        self,
        *,
        destination: str,
        proposer_id: str,
        proposal: dict[str, Any],
        risk: str,
        signal_ids: list[str],
        now: float | None = None,
        ttl_seconds: float = 30 * 24 * 60 * 60,
    ) -> dict[str, Any]:
        if destination not in _ALLOWED_DESTINATIONS or risk not in _ALLOWED_RISKS:
            raise ValueError("learning candidate destination or risk is invalid")
        if not proposer_id.strip() or not isinstance(proposal, dict) or not proposal:
            raise ValueError("learning candidate proposer and proposal are required")
        timestamp = time.time() if now is None else now
        if ttl_seconds <= 0 or ttl_seconds > 30 * 24 * 60 * 60:
            raise ValueError("learning candidate expiry is invalid")
        with self._lock:
            rows = self._signals(signal_ids)
            if any(str(row["kind"]) == "task_progress" for row in rows):
                raise ValueError("task progress cannot be promoted to durable learning")
            explicit = any(str(row["kind"]) == "explicit_correction" for row in rows)
            evidence_refs = {
                (item["source"], item["ref"])
                for row in rows
                for item in json.loads(str(row["provenance_json"]))
            }
            if not explicit and len(evidence_refs) < 2:
                raise ValueError("reusable learning requires two independent signals")
            if any(not bool(row["reusable"]) for row in rows) and not explicit:
                raise ValueError("non-reusable signal cannot become a candidate")

            sanitized_proposal, _redactions = _sanitize_value(proposal)
            content_digest = _digest(sanitized_proposal)
            existing = self._connection.execute(
                "SELECT candidate_id,status FROM workspace_learning_candidates "
                "WHERE destination=? AND content_digest=? AND status IN "
                "('staged','approval_pending','approved','canary_passed',"
                "'applying','apply_uncertain')",
                (destination, content_digest),
            ).fetchone()
            if existing:
                if existing["status"] == "staged":
                    for row in rows:
                        self._connection.execute(
                            "INSERT OR IGNORE INTO workspace_learning_candidate_signals(candidate_id,signal_id) "
                            "VALUES(?,?)",
                            (existing["candidate_id"], row["signal_id"]),
                        )
                    self._connection.commit()
                return self.get_candidate(str(existing["candidate_id"]))

            candidate_id = f"candidate_{uuid.uuid4().hex}"
            expires_at = timestamp + ttl_seconds
            try:
                self._connection.execute(
                    """
                    INSERT INTO workspace_learning_candidates(
                        candidate_id,destination,proposal_json,content_digest,risk,status,
                        proposer_id,expires_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,'staged',?,?,?,?)
                    """,
                    (
                        candidate_id,
                        destination,
                        _canonical(sanitized_proposal),
                        content_digest,
                        risk,
                        proposer_id.strip(),
                        expires_at,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                self._connection.rollback()
                existing = self._connection.execute(
                    "SELECT candidate_id,status FROM workspace_learning_candidates "
                    "WHERE destination=? AND content_digest=? AND status IN "
                    "('staged','approval_pending','approved','canary_passed',"
                    "'applying','apply_uncertain')",
                    (destination, content_digest),
                ).fetchone()
                if existing is None:
                    raise
                if existing["status"] == "staged":
                    for row in rows:
                        self._connection.execute(
                            "INSERT OR IGNORE INTO workspace_learning_candidate_signals(candidate_id,signal_id) "
                            "VALUES(?,?)",
                            (existing["candidate_id"], row["signal_id"]),
                        )
                    self._connection.commit()
                return self.get_candidate(str(existing["candidate_id"]))
            for row in rows:
                self._connection.execute(
                    "INSERT INTO workspace_learning_candidate_signals(candidate_id,signal_id) VALUES(?,?)",
                    (candidate_id, row["signal_id"]),
                )
            self._event(
                candidate_id,
                proposer_id.strip(),
                "candidate.staged",
                {"content_digest": content_digest, "destination": destination},
                timestamp,
            )
            self._connection.commit()
        return self.get_candidate(candidate_id)

    def _row(self, candidate_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM workspace_learning_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("learning candidate is unknown")
        return row

    def _require_transition(self, cursor: sqlite3.Cursor, message: str) -> None:
        if cursor.rowcount == 1:
            return
        self._connection.rollback()
        raise ValueError(message)

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._row(candidate_id)
            signals = self._connection.execute(
                """
                SELECT s.provenance_json FROM workspace_learning_signals s
                JOIN workspace_learning_candidate_signals cs ON cs.signal_id=s.signal_id
                WHERE cs.candidate_id=? ORDER BY s.created_at
                """,
                (candidate_id,),
            ).fetchall()
            events = self._connection.execute(
                "SELECT actor_id,event_type,payload_json,created_at FROM workspace_learning_events "
                "WHERE candidate_id=? ORDER BY event_id",
                (candidate_id,),
            ).fetchall()
        provenance = []
        for signal in signals:
            provenance.extend(json.loads(str(signal["provenance_json"])))
        return {
            "application": json.loads(row["application_json"]) if row["application_json"] else None,
            "approval": json.loads(row["approval_json"]) if row["approval_json"] else None,
            "candidate_id": row["candidate_id"],
            "canary": json.loads(row["canary_json"]) if row["canary_json"] else None,
            "content_digest": row["content_digest"],
            "created_at": row["created_at"],
            "destination": row["destination"],
            "evaluation": json.loads(row["evaluation_json"]) if row["evaluation_json"] else None,
            "evaluator_id": row["evaluator_id"],
            "events": [
                {
                    "actor_id": event["actor_id"],
                    "created_at": event["created_at"],
                    "event_type": event["event_type"],
                    "payload": json.loads(str(event["payload_json"])),
                }
                for event in events
            ],
            "expires_at": row["expires_at"],
            "promoter_id": row["promoter_id"],
            "proposal": json.loads(str(row["proposal_json"])),
            "proposer_id": row["proposer_id"],
            "provenance": provenance,
            "quarantine_reason": row["quarantine_reason"],
            "risk": row["risk"],
            "status": row["status"],
            "updated_at": row["updated_at"],
        }

    def list_candidates(self, *, include_terminal: bool = True) -> list[dict[str, Any]]:
        query = "SELECT candidate_id FROM workspace_learning_candidates"
        params: tuple[Any, ...] = ()
        if not include_terminal:
            query += " WHERE status NOT IN ('quarantined','rejected','expired')"
        query += " ORDER BY created_at DESC"
        rows = self._connection.execute(query, params).fetchall()
        return [self.get_candidate(str(row["candidate_id"])) for row in rows]

    def expire_stale(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        with self._lock:
            rows = self._connection.execute(
                "SELECT candidate_id FROM workspace_learning_candidates "
                "WHERE expires_at<? AND status IN "
                "('staged','approval_pending','approved','canary_passed')",
                (timestamp,),
            ).fetchall()
            for row in rows:
                candidate_id = str(row["candidate_id"])
                cursor = self._connection.execute(
                    "UPDATE workspace_learning_candidates SET status='expired',updated_at=? "
                    "WHERE candidate_id=? AND status IN "
                    "('staged','approval_pending','approved','canary_passed')",
                    (timestamp, candidate_id),
                )
                if cursor.rowcount != 1:
                    continue
                self._event(candidate_id, "system", "candidate.expired", {}, timestamp)
            self._connection.commit()
        return len(rows)

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        with self._lock:
            row = self._row(candidate_id)
            if row["status"] not in {"staged", "approval_pending", "approved", "canary_passed"}:
                raise ValueError("learning candidate cannot be rejected in its current state")
            cursor = self._connection.execute(
                "UPDATE workspace_learning_candidates SET status='rejected',quarantine_reason=?,updated_at=? "
                "WHERE candidate_id=? AND status IN "
                "('staged','approval_pending','approved','canary_passed')",
                (reason, timestamp, candidate_id),
            )
            self._require_transition(cursor, "learning candidate changed during rejection")
            self._event(
                candidate_id,
                actor_id,
                "candidate.rejected",
                {"reason": reason},
                timestamp,
            )
            self._connection.commit()
        return self.get_candidate(candidate_id)

    def evaluate_candidate(
        self,
        candidate_id: str,
        *,
        evaluator_id: str,
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        held_out_digest: str,
        policy_digest: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        if not _DIGEST_RE.fullmatch(held_out_digest) or not _DIGEST_RE.fullmatch(policy_digest):
            raise ValueError("held-out and evaluator policy digests are required")
        baseline_metrics = _validate_metrics(baseline)
        candidate_metrics = _validate_metrics(candidate)
        with self._lock:
            row = self._row(candidate_id)
            if row["status"] != "staged":
                raise ValueError("learning candidate is not staged")
            if row["proposer_id"] == evaluator_id:
                raise ValueError("proposer cannot evaluate its own candidate")
            if timestamp > float(row["expires_at"]):
                self._connection.execute(
                    "UPDATE workspace_learning_candidates SET status='expired',updated_at=? WHERE candidate_id=?",
                    (timestamp, candidate_id),
                )
                self._connection.commit()
                raise ValueError("learning candidate has expired")
            passed, failures = _evaluation_passes(baseline_metrics, candidate_metrics)
            evaluation = {
                "baseline": baseline_metrics,
                "candidate": candidate_metrics,
                "failures": failures,
                "held_out_digest": held_out_digest,
                "passed": passed,
                "policy_digest": policy_digest,
            }
            status = "approval_pending" if passed else "quarantined"
            reason = None if passed else "; ".join(failures)
            cursor = self._connection.execute(
                """
                UPDATE workspace_learning_candidates
                SET evaluator_id=?,evaluation_json=?,status=?,quarantine_reason=?,updated_at=?
                WHERE candidate_id=? AND status='staged'
                """,
                (evaluator_id, _canonical(evaluation), status, reason, timestamp, candidate_id),
            )
            self._require_transition(cursor, "learning candidate changed during evaluation")
            self._event(candidate_id, evaluator_id, "candidate.evaluated", evaluation, timestamp)
            self._connection.commit()
        return self.get_candidate(candidate_id)

    def approve_candidate(
        self,
        candidate_id: str,
        *,
        approver_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        with self._lock:
            row = self._row(candidate_id)
            if row["status"] != "approval_pending":
                raise ValueError("learning candidate is not awaiting approval")
            if approver_id in {row["proposer_id"], row["evaluator_id"]}:
                raise ValueError("evaluator or proposer cannot approve the candidate")
            if timestamp > float(row["expires_at"]):
                raise ValueError("learning candidate has expired")
            approval = {
                "approved_at": timestamp,
                "approved_by": approver_id,
                "content_digest": row["content_digest"],
                "expires_at": row["expires_at"],
            }
            cursor = self._connection.execute(
                "UPDATE workspace_learning_candidates SET approved_by=?,approval_json=?,status='approved',updated_at=? "
                "WHERE candidate_id=? AND status='approval_pending'",
                (approver_id, _canonical(approval), timestamp, candidate_id),
            )
            self._require_transition(cursor, "learning candidate changed during approval")
            self._event(candidate_id, approver_id, "candidate.approved", approval, timestamp)
            self._connection.commit()
        return self.get_candidate(candidate_id)

    def record_canary(
        self,
        candidate_id: str,
        *,
        promoter_id: str,
        report: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._row(candidate_id)
            if row["status"] != "approved":
                raise ValueError("learning candidate is not approved for canary")
            if promoter_id in {row["proposer_id"], row["evaluator_id"]}:
                raise ValueError("proposer or evaluator cannot promote the candidate")
            passed = report.get("passed") is True
            status = "canary_passed" if passed else "quarantined"
            reason = None if passed else "canary failed"
            cursor = self._connection.execute(
                """
                UPDATE workspace_learning_candidates
                SET canary_json=?,promoter_id=?,status=?,quarantine_reason=?,updated_at=?
                WHERE candidate_id=? AND status='approved'
                """,
                (_canonical(report), promoter_id, status, reason, now, candidate_id),
            )
            self._require_transition(cursor, "learning candidate changed during canary")
            self._event(candidate_id, promoter_id, "candidate.canary", report, now)
            self._connection.commit()
        return self.get_candidate(candidate_id)

    def begin_application(
        self,
        candidate_id: str,
        *,
        promoter_id: str,
        application: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            row = self._row(candidate_id)
            if row["status"] != "canary_passed" or row["promoter_id"] != promoter_id:
                raise ValueError("learning candidate has not passed this promoter's canary")
            if float(row["expires_at"]) < now:
                cursor = self._connection.execute(
                    "UPDATE workspace_learning_candidates SET status='expired',updated_at=? "
                    "WHERE candidate_id=? AND status='canary_passed' AND expires_at<?",
                    (now, candidate_id, now),
                )
                self._require_transition(cursor, "learning candidate changed during expiry")
                self._event(candidate_id, promoter_id, "candidate.expired", {}, now)
                self._connection.commit()
                raise ValueError("learning candidate approval expired")
            attempt = dict(application)
            attempt_id = str(attempt.get("attempt_id") or "")
            resource_key = str(attempt.get("resource_key") or "")
            pre_digest = str(attempt.get("pre_digest") or "")
            if not re.fullmatch(r"attempt_[0-9a-f]{32}", attempt_id):
                raise ValueError("learning application attempt ID is invalid")
            if not resource_key or len(resource_key) > 256 or not _DIGEST_RE.fullmatch(pre_digest):
                raise ValueError("learning application resource identity is invalid")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                resource = self._connection.execute(
                    "SELECT candidate_id,state,post_digest FROM workspace_learning_resources "
                    "WHERE resource_key=?",
                    (resource_key,),
                ).fetchone()
                if resource and resource["state"] in {
                    "applying",
                    "apply_uncertain",
                    "rolling_back",
                    "rollback_uncertain",
                }:
                    raise ValueError("learning destination already has an active operation")
                if resource and resource["post_digest"] and str(resource["post_digest"]) != pre_digest:
                    raise ValueError("learning destination revision changed")
                attempt["predecessor_candidate_id"] = (
                    str(resource["candidate_id"])
                    if resource and resource["state"] == "applied"
                    else None
                )
                attempt["predecessor_post_digest"] = (
                    str(resource["post_digest"])
                    if resource and resource["state"] == "applied"
                    else None
                )
                serialized = _canonical(attempt)
                cursor = self._connection.execute(
                    "UPDATE workspace_learning_candidates SET application_json=?,status='applying',updated_at=? "
                    "WHERE candidate_id=? AND status='canary_passed' AND promoter_id=? AND expires_at>=?",
                    (serialized, now, candidate_id, promoter_id, now),
                )
                if cursor.rowcount != 1:
                    raise ValueError("learning candidate changed before apply")
                self._connection.execute(
                    """
                    INSERT INTO workspace_learning_resources(
                        resource_key,candidate_id,state,post_digest,updated_at
                    ) VALUES(?,?,'applying',?,?)
                    ON CONFLICT(resource_key) DO UPDATE SET
                        candidate_id=excluded.candidate_id,
                        state='applying',
                        post_digest=excluded.post_digest,
                        updated_at=excluded.updated_at
                    """,
                    (resource_key, candidate_id, pre_digest, now),
                )
                self._event(candidate_id, promoter_id, "candidate.apply_started", attempt, now)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_candidate(candidate_id)

    def heartbeat_operation(
        self,
        candidate_id: str,
        *,
        attempt_id: str,
        operation: str,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else now
        if operation not in {"apply", "rollback"}:
            raise ValueError("learning operation heartbeat type is invalid")
        expected_status = "applying" if operation == "apply" else "rolling_back"
        with self._lock:
            row = self._connection.execute(
                "SELECT application_json FROM workspace_learning_candidates "
                "WHERE candidate_id=? AND status=?",
                (candidate_id, expected_status),
            ).fetchone()
            if row is None:
                return False
            application = json.loads(str(row["application_json"] or "{}"))
            attempt = application if operation == "apply" else dict(application.get("rollback") or {})
            if attempt.get("attempt_id") != attempt_id:
                return False
            attempt["heartbeat_at"] = timestamp
            attempt["lease_expires_at"] = timestamp + _ATTEMPT_LEASE_SECONDS
            if operation == "rollback":
                application["rollback"] = attempt
            else:
                application = attempt
            cursor = self._connection.execute(
                "UPDATE workspace_learning_candidates SET application_json=?,updated_at=? "
                "WHERE candidate_id=? AND status=? AND application_json=?",
                (
                    _canonical(application),
                    timestamp,
                    candidate_id,
                    expected_status,
                    str(row["application_json"]),
                ),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                return False
            self._connection.commit()
            return True

    def record_application(
        self,
        candidate_id: str,
        *,
        promoter_id: str,
        application: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            final_application = dict(application)
            attempt_id = str(final_application.get("attempt_id") or "")
            resource_key = str(final_application.get("resource_key") or "")
            post_digest = str(final_application.get("post_digest") or "")
            if not re.fullmatch(r"attempt_[0-9a-f]{32}", attempt_id):
                raise ValueError("learning application attempt ID is invalid")
            if not resource_key or not _DIGEST_RE.fullmatch(post_digest):
                raise ValueError("learning application result revision is invalid")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(candidate_id)
                stored = json.loads(str(row["application_json"] or "{}"))
                if (
                    row["status"] != "applying"
                    or row["promoter_id"] != promoter_id
                    or stored.get("attempt_id") != attempt_id
                ):
                    raise ValueError("learning candidate application is not in progress")
                resource = self._connection.execute(
                    "SELECT candidate_id,state FROM workspace_learning_resources WHERE resource_key=?",
                    (resource_key,),
                ).fetchone()
                if (
                    resource is None
                    or resource["candidate_id"] != candidate_id
                    or resource["state"] != "applying"
                ):
                    raise ValueError("learning destination ownership changed during apply")
                cursor = self._connection.execute(
                    "UPDATE workspace_learning_candidates SET application_json=?,status='applied',updated_at=? "
                    "WHERE candidate_id=? AND status='applying' AND promoter_id=?",
                    (_canonical(final_application), now, candidate_id, promoter_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("learning candidate changed during apply")
                resource_cursor = self._connection.execute(
                    "UPDATE workspace_learning_resources SET state='applied',post_digest=?,updated_at=? "
                    "WHERE resource_key=? AND candidate_id=? AND state='applying'",
                    (post_digest, now, resource_key, candidate_id),
                )
                if resource_cursor.rowcount != 1:
                    raise ValueError("learning destination changed during apply completion")
                self._event(candidate_id, promoter_id, "candidate.applied", final_application, now)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_candidate(candidate_id)

    def record_application_failure(
        self,
        candidate_id: str,
        *,
        promoter_id: str,
        error: str,
        application: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            uncertain = dict(application)
            uncertain["apply_error"] = error
            attempt_id = str(uncertain.get("attempt_id") or "")
            resource_key = str(uncertain.get("resource_key") or "")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(candidate_id)
                stored = json.loads(str(row["application_json"] or "{}"))
                if (
                    row["status"] != "applying"
                    or row["promoter_id"] != promoter_id
                    or stored.get("attempt_id") != attempt_id
                ):
                    raise ValueError("learning candidate application is not in progress")
                cursor = self._connection.execute(
                    """
                    UPDATE workspace_learning_candidates
                    SET application_json=?,status='apply_uncertain',quarantine_reason=?,updated_at=?
                    WHERE candidate_id=? AND status='applying' AND promoter_id=?
                    """,
                    (_canonical(uncertain), error, now, candidate_id, promoter_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("learning candidate changed during failed apply")
                resource_cursor = self._connection.execute(
                    "UPDATE workspace_learning_resources SET state='apply_uncertain',updated_at=? "
                    "WHERE resource_key=? AND candidate_id=? AND state='applying'",
                    (now, resource_key, candidate_id),
                )
                if resource_cursor.rowcount != 1:
                    raise ValueError("learning destination changed during failed apply")
                self._event(
                    candidate_id,
                    promoter_id,
                    "candidate.apply_uncertain",
                    {"error": error},
                    now,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_candidate(candidate_id)

    def begin_rollback(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        rollback: dict[str, Any],
        current_digest: str,
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(candidate_id)
                if row["status"] not in {"applied", "apply_uncertain", "rollback_uncertain"}:
                    raise ValueError("learning candidate is not rollback-actionable")
                application = json.loads(str(row["application_json"] or "{}"))
                resource_key = str(application.get("resource_key") or "")
                post_digest = str(application.get("post_digest") or "")
                rollback_payload = dict(rollback)
                if row["status"] == "rollback_uncertain":
                    previous = dict(application.get("rollback") or {})
                    rollback_payload = {
                        **previous,
                        "heartbeat_at": rollback.get("heartbeat_at"),
                        "last_retry_at": now,
                        "lease_expires_at": rollback.get("lease_expires_at"),
                        "owner_instance_id": rollback.get("owner_instance_id"),
                        "owner_pid": rollback.get("owner_pid"),
                        "reason": rollback.get("reason") or previous.get("reason"),
                    }
                attempt_id = str(rollback_payload.get("attempt_id") or "")
                if not re.fullmatch(r"attempt_[0-9a-f]{32}", attempt_id):
                    raise ValueError("learning rollback attempt ID is invalid")
                resource = self._connection.execute(
                    "SELECT candidate_id,state,post_digest FROM workspace_learning_resources "
                    "WHERE resource_key=?",
                    (resource_key,),
                ).fetchone()
                if resource is None or resource["candidate_id"] != candidate_id:
                    raise ValueError("a newer learning candidate owns this destination")
                if row["status"] == "applied" and (
                    str(resource["post_digest"] or "") != post_digest or current_digest != post_digest
                ):
                    raise ValueError("learning destination revision changed before rollback")
                updated = dict(application)
                updated["rollback"] = rollback_payload
                cursor = self._connection.execute(
                    "UPDATE workspace_learning_candidates SET application_json=?,status='rolling_back',updated_at=? "
                    "WHERE candidate_id=? AND status IN ('applied','apply_uncertain','rollback_uncertain')",
                    (_canonical(updated), now, candidate_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("learning candidate changed before rollback")
                resource_cursor = self._connection.execute(
                    "UPDATE workspace_learning_resources SET state='rolling_back',updated_at=? "
                    "WHERE resource_key=? AND candidate_id=? AND state IN "
                    "('applied','apply_uncertain','rollback_uncertain')",
                    (now, resource_key, candidate_id),
                )
                if resource_cursor.rowcount != 1:
                    raise ValueError("learning destination changed before rollback")
                self._event(
                    candidate_id,
                    actor_id,
                    "candidate.rollback_started",
                    rollback_payload,
                    now,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_candidate(candidate_id)

    def record_rollback_failure(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        attempt_id: str,
        error: str,
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(candidate_id)
                application = json.loads(str(row["application_json"] or "{}"))
                rollback = dict(application.get("rollback") or {})
                if row["status"] != "rolling_back" or rollback.get("attempt_id") != attempt_id:
                    raise ValueError("learning rollback is not in progress")
                rollback["error"] = error
                application["rollback"] = rollback
                resource_key = str(application.get("resource_key") or "")
                self._connection.execute(
                    "UPDATE workspace_learning_candidates SET application_json=?,status='rollback_uncertain',"
                    "quarantine_reason=?,updated_at=? WHERE candidate_id=? AND status='rolling_back'",
                    (_canonical(application), error, now, candidate_id),
                )
                self._connection.execute(
                    "UPDATE workspace_learning_resources SET state='rollback_uncertain',updated_at=? "
                    "WHERE resource_key=? AND candidate_id=? AND state='rolling_back'",
                    (now, resource_key, candidate_id),
                )
                self._event(
                    candidate_id,
                    actor_id,
                    "candidate.rollback_uncertain",
                    {"error": error},
                    now,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_candidate(candidate_id)

    def record_rollback(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        reason: str,
        application: dict[str, Any],
        attempt_id: str,
        now: float,
    ) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(candidate_id)
                stored = json.loads(str(row["application_json"] or "{}"))
                rollback = dict(stored.get("rollback") or {})
                if row["status"] != "rolling_back" or rollback.get("attempt_id") != attempt_id:
                    raise ValueError("learning rollback is not in progress")
                resource_key = str(stored.get("resource_key") or "")
                predecessor_id = stored.get("predecessor_candidate_id")
                predecessor_digest = stored.get("predecessor_post_digest")
                cursor = self._connection.execute(
                    """
                    UPDATE workspace_learning_candidates
                    SET application_json=?,status='quarantined',quarantine_reason=?,updated_at=?
                    WHERE candidate_id=? AND status='rolling_back'
                    """,
                    (_canonical(application), reason, now, candidate_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("learning candidate changed during rollback")
                if predecessor_id and predecessor_digest:
                    resource_cursor = self._connection.execute(
                        "UPDATE workspace_learning_resources SET candidate_id=?,state='applied',"
                        "post_digest=?,updated_at=? WHERE resource_key=? AND candidate_id=? "
                        "AND state='rolling_back'",
                        (
                            predecessor_id,
                            predecessor_digest,
                            now,
                            resource_key,
                            candidate_id,
                        ),
                    )
                else:
                    resource_cursor = self._connection.execute(
                        "DELETE FROM workspace_learning_resources WHERE resource_key=? "
                        "AND candidate_id=? AND state='rolling_back'",
                        (resource_key, candidate_id),
                    )
                if resource_cursor.rowcount != 1:
                    raise ValueError("learning destination changed during rollback completion")
                self._event(
                    candidate_id,
                    actor_id,
                    "candidate.rolled_back",
                    {"reason": reason, "attempt_id": attempt_id},
                    now,
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return self.get_candidate(candidate_id)


class LearningController:
    def __init__(
        self,
        store: LearningStore,
        adapters: dict[str, LearningDestinationAdapter],
    ):
        self.store = store
        self.adapters = adapters

    @contextmanager
    def _heartbeat(
        self,
        candidate_id: str,
        *,
        attempt_id: str,
        operation: str,
    ):
        stopped = threading.Event()
        lost = threading.Event()

        def beat() -> None:
            while not stopped.wait(_ATTEMPT_LEASE_SECONDS / 3):
                try:
                    if not self.store.heartbeat_operation(
                        candidate_id,
                        attempt_id=attempt_id,
                        operation=operation,
                    ):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        thread = threading.Thread(target=beat, name=f"learning-{operation}-heartbeat", daemon=True)
        thread.start()
        try:
            yield lost
        finally:
            stopped.set()
            thread.join(timeout=2)

    def _adapter(self, candidate: dict[str, Any]) -> LearningDestinationAdapter:
        adapter = self.adapters.get(str(candidate["destination"]))
        if adapter is None:
            raise ValueError(
                "destination requires the normal project or external workflow; auto-apply is forbidden"
            )
        return adapter

    @staticmethod
    def _verify_candidate(candidate: dict[str, Any]) -> None:
        if _digest(candidate["proposal"]) != candidate["content_digest"]:
            raise ValueError("learning candidate content digest changed")
        approval = candidate.get("approval") or {}
        if approval.get("content_digest") != candidate["content_digest"]:
            raise ValueError("learning candidate approval is stale")

    def run_canary(
        self,
        candidate_id: str,
        *,
        promoter_id: str,
        metrics: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        candidate = self.store.get_candidate(candidate_id)
        if candidate["status"] != "approved":
            raise ValueError("learning candidate is not approved for canary")
        if timestamp > float(candidate["expires_at"]):
            raise ValueError("learning candidate approval has expired")
        self._verify_candidate(candidate)
        normalized = _validate_metrics(metrics)
        adapter_report = self._adapter(candidate).canary(candidate)
        passed = (
            adapter_report.get("passed") is True
            and int(normalized["safety_failures"]) == 0
            and int(normalized["successes"]) == int(normalized["cases"])
        )
        report = {
            "adapter": adapter_report,
            "metrics": normalized,
            "passed": passed,
            "run_at": timestamp,
        }
        return self.store.record_canary(
            candidate_id,
            promoter_id=promoter_id,
            report=report,
            now=timestamp,
        )

    def apply_candidate(
        self,
        candidate_id: str,
        *,
        promoter_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        clock = time.time if now is None else lambda: now
        candidate = self.store.get_candidate(candidate_id)
        if candidate["status"] != "canary_passed":
            raise ValueError("learning candidate must pass canary before apply")
        self._verify_candidate(candidate)
        adapter = self._adapter(candidate)
        if clock() > float(candidate["expires_at"]):
            self.store.expire_stale(now=clock())
            raise ValueError("learning candidate approval is expired")
        resource_key = adapter.resource_key(candidate)
        pre_digest = adapter.current_digest(candidate)
        snapshot_id = adapter.snapshot(candidate)
        if adapter.current_digest(candidate) != pre_digest:
            raise ValueError("learning destination changed while creating backup")
        timestamp = clock()
        if timestamp > float(candidate["expires_at"]):
            self.store.expire_stale(now=timestamp)
            raise ValueError("learning candidate approval expired during preparation")
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        application: dict[str, Any] = {
            "applied_at": None,
            "attempt_id": attempt_id,
            "backup_id": snapshot_id,
            "heartbeat_at": timestamp,
            "lease_expires_at": timestamp + _ATTEMPT_LEASE_SECONDS,
            "owner_instance_id": _PROCESS_INSTANCE_ID,
            "owner_pid": os.getpid(),
            "post_digest": None,
            "pre_digest": pre_digest,
            "resource_key": resource_key,
            "result": None,
            "rollback": None,
            "rolled_back_at": None,
            "started_at": timestamp,
            "version_id": f"learning_{uuid.uuid4().hex}",
        }
        begun = self.store.begin_application(
            candidate_id,
            promoter_id=promoter_id,
            application=application,
            now=timestamp,
        )
        application = dict(begun["application"])
        try:
            with self._heartbeat(
                candidate_id,
                attempt_id=attempt_id,
                operation="apply",
            ) as heartbeat_lost:
                result = adapter.apply(candidate)
                if result.get("applied") is not True:
                    raise ValueError(str(result.get("error") or "learning apply failed"))
                if heartbeat_lost.is_set():
                    raise ValueError("learning application ownership was lost")
        except Exception as exc:
            self.store.record_application_failure(
                candidate_id,
                promoter_id=promoter_id,
                error=str(exc) or type(exc).__name__,
                application=application,
                now=time.time() if now is None else timestamp,
            )
            raise
        application["applied_at"] = time.time() if now is None else timestamp
        application["post_digest"] = adapter.current_digest(candidate)
        application["result"] = result
        return self.store.record_application(
            candidate_id,
            promoter_id=promoter_id,
            application=application,
            now=time.time() if now is None else timestamp,
        )

    def rollback_candidate(
        self,
        candidate_id: str,
        *,
        actor_id: str,
        reason: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        candidate = self.store.get_candidate(candidate_id)
        if candidate["status"] not in {"applied", "apply_uncertain", "rollback_uncertain"} or not candidate.get(
            "application"
        ):
            raise ValueError("learning candidate is not rollback-actionable")
        adapter = self._adapter(candidate)
        application = dict(candidate["application"])
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        rollback = {
            "attempt_id": attempt_id,
            "heartbeat_at": timestamp,
            "lease_expires_at": timestamp + _ATTEMPT_LEASE_SECONDS,
            "owner_instance_id": _PROCESS_INSTANCE_ID,
            "owner_pid": os.getpid(),
            "reason": reason,
            "started_at": timestamp,
        }
        begun = self.store.begin_rollback(
            candidate_id,
            actor_id=actor_id,
            rollback=rollback,
            current_digest=adapter.current_digest(candidate),
            now=timestamp,
        )
        application = dict(begun["application"])
        rollback = dict(application.get("rollback") or {})
        attempt_id = str(rollback.get("attempt_id") or "")
        try:
            with self._heartbeat(
                candidate_id,
                attempt_id=attempt_id,
                operation="rollback",
            ) as heartbeat_lost:
                result = adapter.restore(
                    candidate,
                    str(application["backup_id"]),
                    attempt_id=attempt_id,
                )
                if result.get("restored") is not True:
                    raise ValueError(str(result.get("error") or "learning rollback failed"))
                restored_digest = adapter.current_digest(candidate)
                if restored_digest != str(application["pre_digest"]):
                    raise ValueError("learning rollback restored an unexpected destination revision")
                if heartbeat_lost.is_set():
                    raise ValueError("learning rollback ownership was lost")
        except Exception as exc:
            self.store.record_rollback_failure(
                candidate_id,
                actor_id=actor_id,
                attempt_id=attempt_id,
                error=str(exc) or type(exc).__name__,
                now=time.time() if now is None else timestamp,
            )
            raise
        rollback = dict(application.get("rollback") or {})
        rollback["completed_at"] = time.time() if now is None else timestamp
        rollback["result"] = result
        application["rollback"] = rollback
        application["rolled_back_at"] = rollback["completed_at"]
        application["rollback_reason"] = reason
        return self.store.record_rollback(
            candidate_id,
            actor_id=actor_id,
            reason=reason,
            application=application,
            attempt_id=attempt_id,
            now=time.time() if now is None else timestamp,
        )


class ProfileLearningAdapter:
    """Apply approved memory/skill candidates through the existing tool lifecycles."""

    def __init__(self, profile_home: str | Path):
        self.profile_home = Path(profile_home).expanduser().resolve()
        self.backup_root = self.profile_home / "learning" / "backups"

    def _require_profile(self) -> None:
        from hermes_constants import get_hermes_home

        if get_hermes_home().expanduser().resolve() != self.profile_home:
            raise ValueError("learning adapter profile does not match active profile")

    @staticmethod
    def _path_digest(path: Path) -> str:
        digest = hashlib.sha256()
        if not path.exists():
            digest.update(b"missing")
            return digest.hexdigest()
        if path.is_symlink():
            raise ValueError("learning destination cannot be a symlink")
        if path.is_file():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
            return digest.hexdigest()
        digest.update(b"directory\0")
        for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
            relative = child.relative_to(path).as_posix()
            if child.is_symlink():
                raise ValueError("learning destination cannot contain symlinks")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            if child.is_file():
                digest.update(child.read_bytes())
        return digest.hexdigest()

    def resource_key(self, candidate: dict[str, Any]) -> str:
        destination = str(candidate["destination"])
        if destination in {"memory", "user_memory"}:
            return f"memory:{destination}"
        if destination == "skill":
            name = str(candidate["proposal"].get("name") or "").strip()
            if not name:
                raise ValueError("learning skill name is required")
            return f"skill:{name}"
        raise ValueError("profile adapter destination is unsupported")

    def current_digest(self, candidate: dict[str, Any]) -> str:
        destination = str(candidate["destination"])
        if destination in {"memory", "user_memory"}:
            target = "USER.md" if destination == "user_memory" else "MEMORY.md"
            return self._path_digest(self.profile_home / "memories" / target)
        if destination == "skill":
            from tools.skill_manager_tool import _find_skill

            found = _find_skill(str(candidate["proposal"].get("name") or ""))
            if not found:
                return self._path_digest(self.profile_home / "skills" / "__missing__")
            return self._path_digest(Path(found["path"]))
        raise ValueError("profile adapter destination is unsupported")

    def canary(self, candidate: dict[str, Any]) -> dict[str, Any]:
        proposal = candidate["proposal"]
        destination = candidate["destination"]
        if destination in {"memory", "user_memory"}:
            from tools.memory_tool import _scan_memory_content, load_on_disk_store

            content = str(proposal.get("content") or "")
            threat = _scan_memory_content(content) if content else None
            if threat:
                return {"checks": ["memory-threat-scan"], "error": threat, "passed": False}
            store = load_on_disk_store()
            target = "user" if destination == "user_memory" else "memory"
            entries = list(store._entries_for(target))
            action = str(proposal.get("action") or "")
            if action == "add":
                entries.append(content)
            elif action == "replace":
                old = str(proposal.get("old_text") or "")
                matches = [entry for entry in entries if old in entry]
                if len(matches) != 1:
                    return {"checks": ["memory-target"], "passed": False}
                entries[entries.index(matches[0])] = content
            elif action == "remove":
                old = str(proposal.get("old_text") or "")
                matches = [entry for entry in entries if old in entry]
                if len(matches) != 1:
                    return {"checks": ["memory-target"], "passed": False}
                entries.remove(matches[0])
            else:
                return {"checks": ["memory-action"], "passed": False}
            char_count = len("\n§\n".join(dict.fromkeys(entries)))
            return {
                "checks": ["memory-threat-scan", "memory-budget"],
                "passed": char_count <= store._char_limit(target),
            }

        if destination == "skill":
            from tools.skill_manager_tool import (
                _validate_content_size,
                _validate_frontmatter,
                _validate_name,
            )

            action = str(proposal.get("action") or "")
            name = str(proposal.get("name") or "")
            content = str(proposal.get("content") or "")
            errors = [_validate_name(name)]
            if action in {"create", "edit"}:
                errors.extend(
                    [
                        _validate_frontmatter(content, new_skill=action == "create"),
                        _validate_content_size(content),
                    ]
                )
            return {
                "checks": ["skill-name", "skill-frontmatter", "skill-size"],
                "errors": [error for error in errors if error],
                "passed": not any(errors),
            }
        return {"checks": [], "passed": False}

    def snapshot(self, candidate: dict[str, Any]) -> str:
        self._require_profile()
        backup_id = f"backup_{secrets.token_hex(16)}"
        directory = self.backup_root / backup_id
        directory.mkdir(parents=True, mode=0o700)
        proposal = candidate["proposal"]
        destination = candidate["destination"]
        manifest: dict[str, Any] = {"destination": destination, "existed": False}
        if destination in {"memory", "user_memory"}:
            target = "USER.md" if destination == "user_memory" else "MEMORY.md"
            source = self.profile_home / "memories" / target
            manifest["target"] = target
            manifest["existed"] = source.is_file()
            if source.is_file():
                shutil.copy2(source, directory / "original")
        elif destination == "skill":
            from tools.skill_manager_tool import _find_skill

            name = str(proposal.get("name") or "")
            manifest["name"] = name
            found = _find_skill(name)
            manifest["existed"] = bool(found)
            if found:
                shutil.copytree(found["path"], directory / "original")
        else:
            raise ValueError("profile adapter destination is unsupported")
        (directory / "manifest.json").write_text(_canonical(manifest), encoding="utf-8")
        os.chmod(directory / "manifest.json", 0o600)
        return backup_id

    def apply(self, candidate: dict[str, Any]) -> dict[str, Any]:
        self._require_profile()
        proposal = dict(candidate["proposal"])
        destination = candidate["destination"]
        if destination in {"memory", "user_memory"}:
            from tools.memory_tool import apply_memory_pending, load_on_disk_store

            proposal["target"] = "user" if destination == "user_memory" else "memory"
            result = apply_memory_pending(proposal, load_on_disk_store())
            return {"applied": result.get("success") is True, "tool_result": result}
        if destination == "skill":
            from tools.skill_manager_tool import apply_skill_pending

            raw = apply_skill_pending(proposal)
            result = json.loads(raw)
            return {"applied": result.get("success") is True, "tool_result": result}
        raise ValueError("profile adapter destination is unsupported")

    def restore(
        self,
        candidate: dict[str, Any],
        snapshot_id: str,
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        del candidate, attempt_id
        self._require_profile()
        if not re.fullmatch(r"backup_[0-9a-f]{32}", snapshot_id):
            raise ValueError("learning backup ID is invalid")
        directory = (self.backup_root / snapshot_id).resolve()
        try:
            directory.relative_to(self.backup_root.resolve())
        except ValueError as exc:
            raise ValueError("learning backup escapes backup root") from exc
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        destination = manifest["destination"]
        if destination in {"memory", "user_memory"}:
            target = self.profile_home / "memories" / str(manifest["target"])
            if manifest["existed"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(directory / "original", target)
            else:
                target.unlink(missing_ok=True)
            return {"restored": True}
        if destination == "skill":
            from tools.skill_manager_tool import _find_skill, _skills_dir, _validate_delete_target

            name = str(manifest["name"])
            found = _find_skill(name)
            if found:
                error = _validate_delete_target(found["path"])
                if error:
                    raise ValueError(error)
                shutil.rmtree(found["path"])
            if manifest["existed"]:
                target = _skills_dir() / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(directory / "original", target)
            return {"restored": True}
        raise ValueError("learning backup destination is unsupported")
