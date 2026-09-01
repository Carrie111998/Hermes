from __future__ import annotations

from pathlib import Path
import json
import os
import sqlite3
import stat
from typing import Any

from agent.memory_policy import validate_approval
from agent.memory_records import Approval, Candidate, CanonicalRecord, Lifecycle, LifecycleStatus, MemoryClass, Provenance, ReviewPacket, Scope, TransitionReceipt


class VersionConflict(RuntimeError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value


def _integer(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _limit(value: object) -> int:
    result = _integer(value, "limit", 0)
    if result > 500:
        raise ValueError("limit must be <= 500")
    return result


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _optional_integer(value: object, name: str, minimum: int) -> int | None:
    return None if value is None else _integer(value, name, minimum)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _scope(profile: str, namespace: str, project: str) -> Scope:
    return Scope(profile, namespace, project)


def _reject_redirected_components(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("root must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part in ("", ".", ".."):
            raise ValueError("root contains an invalid component")
        current = current / part
        if not os.path.lexists(current):
            continue
        value = current.lstat()
        if stat.S_ISLNK(value.st_mode):
            raise ValueError("symlinked path component is forbidden")
        if not stat.S_ISDIR(value.st_mode):
            raise ValueError("path component must be a directory")


def _validate_owned_directory(path: Path, name: str) -> None:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode):
        raise ValueError(f"symlinked {name} is forbidden")
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError(f"{name} must be a directory")
    if value.st_uid != os.geteuid():
        raise ValueError(f"{name} must be owned by the current user")


def _validate_database(path: Path) -> None:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode):
        raise ValueError("database symlink is forbidden")
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("database must be a regular file")
    if value.st_uid != os.geteuid():
        raise ValueError("database must be owned by the current user")
    if value.st_nlink != 1:
        raise ValueError("database hard links are forbidden")


class MemoryStore:
    def __init__(self, root: Path | str, profile_id: str) -> None:
        self.profile_id = _text(profile_id, "profile_id")
        self.root = Path(root)
        _reject_redirected_components(self.root)
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        _validate_owned_directory(self.root, "root")
        self.state_dir = self.root / "state"
        _reject_redirected_components(self.state_dir)
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        _validate_owned_directory(self.state_dir, "state directory")
        self.path = self.state_dir / "context-memory.sqlite3"
        self._closed = False
        if os.path.lexists(self.path):
            _validate_database(self.path)
        else:
            flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            self._closed = True
            raise ValueError("database is not valid SQLite") from exc

    def close(self) -> None:
        self._closed = True

    def __del__(self) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("store is closed")
        _reject_redirected_components(self.state_dir)
        _validate_owned_directory(self.state_dir, "state directory")
        _validate_database(self.path)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS candidates (
          profile_id TEXT NOT NULL, namespace TEXT NOT NULL, project_id TEXT NOT NULL,
          candidate_id TEXT NOT NULL, status TEXT NOT NULL, memory_class TEXT NOT NULL,
          source_type TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
          PRIMARY KEY(profile_id, namespace, project_id, candidate_id));
        CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(profile_id,status,candidate_id);
        CREATE INDEX IF NOT EXISTS idx_candidates_class ON candidates(profile_id,memory_class,candidate_id);
        CREATE INDEX IF NOT EXISTS idx_candidates_namespace ON candidates(profile_id,namespace,candidate_id);
        CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidates(profile_id,project_id,candidate_id);
        CREATE INDEX IF NOT EXISTS idx_candidates_scope ON candidates(profile_id,namespace,project_id,candidate_id);
        CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(profile_id,source_id,candidate_id);
        CREATE TABLE IF NOT EXISTS candidate_events (
          event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          profile_id TEXT NOT NULL, namespace TEXT NOT NULL, project_id TEXT NOT NULL,
          candidate_id TEXT NOT NULL, event TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_candidate_events_lookup ON candidate_events(profile_id,namespace,project_id,candidate_id,event_sequence);
        CREATE TABLE IF NOT EXISTS review_packets (
          profile_id TEXT NOT NULL, namespace TEXT NOT NULL, project_id TEXT NOT NULL,
          packet_id TEXT NOT NULL, base_version INTEGER NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
          PRIMARY KEY(profile_id,namespace,project_id,packet_id));
        CREATE INDEX IF NOT EXISTS idx_review_packets_namespace ON review_packets(profile_id,namespace,base_version,packet_id);
        CREATE INDEX IF NOT EXISTS idx_review_packets_project ON review_packets(profile_id,project_id,base_version,packet_id);
        CREATE INDEX IF NOT EXISTS idx_review_packets_base_version ON review_packets(profile_id,base_version,packet_id);
        CREATE INDEX IF NOT EXISTS idx_review_packets_scope_version ON review_packets(profile_id,namespace,project_id,base_version,packet_id);
        CREATE TABLE IF NOT EXISTS canonical_records (
          profile_id TEXT NOT NULL, namespace TEXT NOT NULL, project_id TEXT NOT NULL,
          record_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(profile_id,namespace,project_id,record_id,version));
        CREATE INDEX IF NOT EXISTS idx_canonical_versions ON canonical_records(profile_id,namespace,project_id,record_id,version);
        CREATE TABLE IF NOT EXISTS transition_receipts (
          profile_id TEXT NOT NULL, namespace TEXT NOT NULL, project_id TEXT NOT NULL,
          receipt_id TEXT NOT NULL, record_id TEXT NOT NULL, to_version INTEGER NOT NULL, payload TEXT NOT NULL,
          PRIMARY KEY(profile_id,namespace,project_id,receipt_id));
        CREATE INDEX IF NOT EXISTS idx_receipts_record ON transition_receipts(profile_id,namespace,project_id,record_id,to_version);
        """
        with self._connect() as db:
            db.executescript(schema)

    def append_candidate(self, candidate: Candidate) -> str:
        if type(candidate) is not Candidate or candidate.scope.profile_id != self.profile_id:
            raise ValueError("candidate does not belong to store")
        identity = candidate.content_hash(); payload = _json(candidate.to_dict())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?,?,?,?,?)", (self.profile_id, candidate.scope.namespace, candidate.scope.project_id, identity, candidate.lifecycle.status.value, candidate.memory_class.value, candidate.provenance.source_type, candidate.provenance.source_id, payload, identity))
            event = "appended" if cursor.rowcount == 1 else "duplicate"
            db.execute("INSERT INTO candidate_events(profile_id,namespace,project_id,candidate_id,event) VALUES (?,?,?,?,?)", (self.profile_id, candidate.scope.namespace, candidate.scope.project_id, identity, event))
        return identity

    def count_candidates(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT count(*) FROM candidates WHERE profile_id=?", (self.profile_id,)).fetchone()[0])

    def query_candidates(self, *, status: str | None = None, memory_class: MemoryClass | None = None, namespace: str | None = None, project_id: str | None = None, source_id: str | None = None, limit: int = 100) -> list[Candidate]:
        limit = _limit(limit); status = _optional_text(status, "status"); namespace = _optional_text(namespace, "namespace"); project_id = _optional_text(project_id, "project_id"); source_id = _optional_text(source_id, "source_id")
        if status is not None:
            try:
                status = LifecycleStatus(status).value
            except ValueError as exc:
                raise ValueError("status is not supported") from exc
        if memory_class is not None and type(memory_class) is not MemoryClass:
            raise TypeError("memory_class must be MemoryClass")
        if limit == 0:
            return []
        where = ["profile_id=?"]; values: list[Any] = [self.profile_id]
        for column, value in (("status", status), ("memory_class", memory_class.value if memory_class else None), ("namespace", namespace), ("project_id", project_id), ("source_id", source_id)):
            if value is not None:
                where.append(f"{column}=?"); values.append(value)
        values.append(limit)
        with self._connect() as db:
            rows = db.execute(f"SELECT payload FROM candidates WHERE {' AND '.join(where)} ORDER BY candidate_id LIMIT ?", values).fetchall()
        return [self._candidate(json.loads(row["payload"])) for row in rows]

    def candidate_events(self, candidate_id: str, *, namespace: str, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        candidate_id = _text(candidate_id, "candidate_id"); namespace = _text(namespace, "namespace"); project_id = _text(project_id, "project_id"); limit = _limit(limit)
        if limit == 0:
            return []
        with self._connect() as db:
            rows = db.execute("SELECT event_sequence,event FROM candidate_events WHERE profile_id=? AND namespace=? AND project_id=? AND candidate_id=? ORDER BY event_sequence LIMIT ?", (self.profile_id, namespace, project_id, candidate_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def append_review_packet(self, packet: ReviewPacket) -> str:
        if type(packet) is not ReviewPacket or packet.scope.profile_id != self.profile_id:
            raise ValueError("packet does not belong to store")
        identity = packet.content_hash(); payload = _json(packet.to_dict())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO review_packets VALUES (?,?,?,?,?,?,?)", (self.profile_id, packet.scope.namespace, packet.scope.project_id, packet.packet_id, packet.base_version, payload, identity))
            row = db.execute("SELECT content_hash FROM review_packets WHERE profile_id=? AND namespace=? AND project_id=? AND packet_id=?", (self.profile_id, packet.scope.namespace, packet.scope.project_id, packet.packet_id)).fetchone()
            if row[0] != identity:
                raise ValueError("packet_id already identifies different content")
        return identity

    def review_packet(self, packet_id: str, *, namespace: str, project_id: str) -> ReviewPacket | None:
        packet_id = _text(packet_id, "packet_id"); namespace = _text(namespace, "namespace"); project_id = _text(project_id, "project_id")
        with self._connect() as db:
            row = db.execute("SELECT payload FROM review_packets WHERE profile_id=? AND namespace=? AND project_id=? AND packet_id=?", (self.profile_id, namespace, project_id, packet_id)).fetchone()
        return None if row is None else self._packet(json.loads(row["payload"]))

    def query_review_packets(self, *, base_version: int | None = None, namespace: str | None = None, project_id: str | None = None, limit: int = 100) -> list[ReviewPacket]:
        base_version = _optional_integer(base_version, "base_version", 0); namespace = _optional_text(namespace, "namespace"); project_id = _optional_text(project_id, "project_id"); limit = _limit(limit)
        if limit == 0:
            return []
        where = ["profile_id=?"]; values: list[Any] = [self.profile_id]
        for column, value in (("base_version", base_version), ("namespace", namespace), ("project_id", project_id)):
            if value is not None:
                where.append(f"{column}=?"); values.append(value)
        values.append(limit)
        with self._connect() as db:
            rows = db.execute(f"SELECT payload FROM review_packets WHERE {' AND '.join(where)} ORDER BY base_version,packet_id LIMIT ?", values).fetchall()
        return [self._packet(json.loads(row["payload"])) for row in rows]

    def commit_transition(self, record: CanonicalRecord, receipt: TransitionReceipt, *, expected_version: int, packet: ReviewPacket | None = None, approval: Approval | None = None, candidate: Candidate | None = None) -> None:
        expected_version = _integer(expected_version, "expected_version", 0)
        if type(record) is not CanonicalRecord or type(receipt) is not TransitionReceipt or record.scope.profile_id != self.profile_id or receipt.scope != record.scope:
            raise ValueError("record and receipt scope must match store")
        if record.memory_class is MemoryClass.A:
            if record.provenance.source_type != "direct_user" or record.provenance.derived:
                raise ValueError("class-A publication requires direct-user non-derived provenance")
            candidate_matches = (
                type(candidate) is Candidate
                and candidate.memory_class is MemoryClass.A
                and candidate.scope == record.scope
                and candidate.provenance == record.provenance
                and candidate.content == record.content
                and candidate.lifecycle.observed_at == record.lifecycle.observed_at
                and candidate.lifecycle.expires_at == record.lifecycle.expires_at
                and candidate.lifecycle.supersedes_record_id == record.lifecycle.supersedes_record_id
                and record.lifecycle.status is LifecycleStatus.ACTIVE
            )
            packet_matches = (
                type(candidate) is Candidate
                and type(packet) is ReviewPacket
                and packet.scope == record.scope
                and packet.base_version == expected_version
                and packet.candidate_ids == (candidate.content_hash(),)
            )
            if not candidate_matches or not packet_matches or type(approval) is not Approval or receipt.approval_hash != approval.content_hash() or validate_approval(packet, approval):
                raise ValueError("class-A publication requires an exact candidate-bound direct approval packet")
        if record.version != expected_version + 1 or receipt.record_id != record.record_id or receipt.to_version != record.version or receipt.from_version != expected_version:
            raise ValueError("receipt does not match record transition")
        key = (self.profile_id, record.scope.namespace, record.scope.project_id, record.record_id)
        record_payload = _json(record.to_dict()); receipt_payload = _json(receipt.to_dict())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_record = db.execute("SELECT payload FROM canonical_records WHERE profile_id=? AND namespace=? AND project_id=? AND record_id=? AND version=?", (*key, record.version)).fetchone()
            old_receipt = db.execute("SELECT payload FROM transition_receipts WHERE profile_id=? AND namespace=? AND project_id=? AND receipt_id=?", (self.profile_id, record.scope.namespace, record.scope.project_id, receipt.receipt_id)).fetchone()
            if old_record is not None or old_receipt is not None:
                if old_record and old_receipt and old_record[0] == record_payload and old_receipt[0] == receipt_payload:
                    return
                raise VersionConflict("conflicting replay for committed identity")
            current = int(db.execute("SELECT coalesce(max(version),0) FROM canonical_records WHERE profile_id=? AND namespace=? AND project_id=? AND record_id=?", key).fetchone()[0])
            if current != expected_version:
                raise VersionConflict(f"expected version {expected_version}, found {current}")
            db.execute("INSERT INTO canonical_records VALUES (?,?,?,?,?,?)", (*key, record.version, record_payload))
            db.execute("INSERT INTO transition_receipts VALUES (?,?,?,?,?,?,?)", (self.profile_id, record.scope.namespace, record.scope.project_id, receipt.receipt_id, receipt.record_id, receipt.to_version, receipt_payload))

    def canonical_history(self, record_id: str, *, namespace: str, project_id: str, min_version: int | None = None, max_version: int | None = None, limit: int = 100) -> list[CanonicalRecord]:
        record_id = _text(record_id, "record_id"); namespace = _text(namespace, "namespace"); project_id = _text(project_id, "project_id"); min_version = _optional_integer(min_version, "min_version", 1); max_version = _optional_integer(max_version, "max_version", 1); limit = _limit(limit)
        if min_version is not None and max_version is not None and min_version > max_version:
            raise ValueError("min_version cannot exceed max_version")
        if limit == 0:
            return []
        where = ["profile_id=?", "namespace=?", "project_id=?", "record_id=?"]; values: list[Any] = [self.profile_id, namespace, project_id, record_id]
        if min_version is not None: where.append("version>=?"); values.append(min_version)
        if max_version is not None: where.append("version<=?"); values.append(max_version)
        values.append(limit)
        with self._connect() as db:
            rows = db.execute(f"SELECT payload FROM canonical_records WHERE {' AND '.join(where)} ORDER BY version LIMIT ?", values).fetchall()
        return [self._record(json.loads(row["payload"])) for row in rows]

    def transition_receipts(self, record_id: str, *, namespace: str, project_id: str, limit: int = 100) -> list[TransitionReceipt]:
        record_id = _text(record_id, "record_id"); namespace = _text(namespace, "namespace"); project_id = _text(project_id, "project_id"); limit = _limit(limit)
        if limit == 0:
            return []
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM transition_receipts WHERE profile_id=? AND namespace=? AND project_id=? AND record_id=? ORDER BY to_version LIMIT ?", (self.profile_id, namespace, project_id, record_id, limit)).fetchall()
        return [self._receipt(json.loads(row["payload"])) for row in rows]

    @staticmethod
    def _candidate(value: dict[str, Any]) -> Candidate:
        s = value["scope"]
        return Candidate(MemoryClass(value["memory_class"]), Scope(s["profile_id"], s["namespace"], s["project_id"]), Provenance(**value["provenance"]), Lifecycle(**value["lifecycle"]), value["content"], value["confidence"])

    @staticmethod
    def _packet(value: dict[str, Any]) -> ReviewPacket:
        s = value["scope"]
        return ReviewPacket(value["packet_id"], Scope(s["profile_id"], s["namespace"], s["project_id"]), tuple(value["candidate_ids"]), value["base_version"])

    @staticmethod
    def _record(value: dict[str, Any]) -> CanonicalRecord:
        s = value["scope"]
        return CanonicalRecord(value["record_id"], value["version"], MemoryClass(value["memory_class"]), Scope(s["profile_id"], s["namespace"], s["project_id"]), Provenance(**value["provenance"]), Lifecycle(**value["lifecycle"]), value["content"])

    @staticmethod
    def _receipt(value: dict[str, Any]) -> TransitionReceipt:
        s = value.pop("scope")
        return TransitionReceipt(scope=Scope(s["profile_id"], s["namespace"], s["project_id"]), **value)
