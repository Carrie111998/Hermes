"""Private, task-scoped output artifacts for hosted Group Chat turns."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from gateway.hosted_room_attachments import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    MAX_GATEWAY_BLOB_BYTES,
    MAX_MESSAGE_ATTACHMENT_BYTES,
)

_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class RoomArtifactError(ValueError):
    """A hosted-room output artifact failed its scope or integrity contract."""


@dataclass(frozen=True)
class RoomArtifactScope:
    """Immutable coordinates for one admitted member turn."""

    room_id: str
    task_id: str
    execution_generation: int
    member_id: str
    target_profile: str
    home_install_id: str
    target_install_id: str
    authority_gateway_id: str
    authority_epoch: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RoomArtifactScope":
        required = {
            "room_id",
            "task_id",
            "execution_generation",
            "member_id",
            "target_profile",
            "home_install_id",
            "target_install_id",
            "authority_gateway_id",
            "authority_epoch",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise RoomArtifactError("room artifact scope fields are invalid")
        strings = {
            key: str(value[key]).strip()
            for key in required
            if key not in {"execution_generation", "authority_epoch"}
        }
        if not all(strings.values()) or any(
            len(item) > 255 or _SCOPE_ID_RE.fullmatch(item) is None
            for item in strings.values()
        ):
            raise RoomArtifactError("room artifact scope contains an invalid identifier")
        generation = value["execution_generation"]
        epoch = value["authority_epoch"]
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or isinstance(epoch, bool)
            or not isinstance(epoch, int)
            or epoch < 1
        ):
            raise RoomArtifactError("room artifact scope generation is invalid")
        return cls(
            **strings,
            execution_generation=generation,
            authority_epoch=epoch,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "room_id": self.room_id,
            "task_id": self.task_id,
            "execution_generation": self.execution_generation,
            "member_id": self.member_id,
            "target_profile": self.target_profile,
            "home_install_id": self.home_install_id,
            "target_install_id": self.target_install_id,
            "authority_gateway_id": self.authority_gateway_id,
            "authority_epoch": self.authority_epoch,
        }

    @property
    def key(self) -> str:
        payload = json.dumps(self.as_mapping(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


_CURRENT_SCOPE: ContextVar[RoomArtifactScope | None] = ContextVar(
    "hosted_room_artifact_scope",
    default=None,
)


def bind_room_artifact_scope(scope: RoomArtifactScope) -> Token:
    return _CURRENT_SCOPE.set(scope)


def reset_room_artifact_scope(token: Token) -> None:
    _CURRENT_SCOPE.reset(token)


def current_room_artifact_scope() -> RoomArtifactScope | None:
    return _CURRENT_SCOPE.get()


class RoomArtifactOutbox:
    """Durable private bytes awaiting canonical import by the room home."""

    def __init__(self, db_path: Path | str, *, root: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.root = Path(root or self.db_path.parent / "hosted-room-artifact-outbox")
        self.blob_root = self.root / "blobs"
        self._lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.root, self.blob_root):
            with suppress(OSError):
                os.chmod(path, 0o700)
        with self._connect() as conn:
            self._initialize(conn)
            acknowledged = conn.execute(
                """SELECT blob_name FROM hosted_room_output_artifacts
                   WHERE acknowledged_at IS NOT NULL"""
            ).fetchall()
        for row in acknowledged:
            (self.blob_root / str(row["blob_name"])).unlink(missing_ok=True)
        cutoff = time.time() - 3600
        with self._connect() as conn:
            referenced = {
                str(row["blob_name"])
                for row in conn.execute(
                    "SELECT blob_name FROM hosted_room_output_artifacts"
                ).fetchall()
            }
        for path in self.blob_root.iterdir():
            try:
                if (
                    path.is_file()
                    and path.name not in referenced
                    and path.stat().st_mtime < cutoff
                ):
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _connect(self) -> sqlite3.Connection:
        from hermes_state import apply_wal_with_fallback

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        apply_wal_with_fallback(conn, db_label="state.db (hosted room artifact outbox)")
        return conn

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS hosted_room_output_artifacts (
                artifact_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                blob_name TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                acknowledged_at REAL,
                UNIQUE(scope_key, sha256, name)
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_hosted_room_output_scope
               ON hosted_room_output_artifacts(scope_key, created_at)"""
        )
        conn.commit()

    @staticmethod
    def _safe_name(value: str) -> str:
        name = Path(str(value or "")).name.strip()
        if (
            not name
            or name in {".", ".."}
            or len(name) > 255
            or any(char in name for char in ("/", "\\", "\x00", "\n", "\r"))
        ):
            raise RoomArtifactError("artifact name must be a bounded basename")
        return name

    @staticmethod
    def _classify(name: str, data: bytes) -> tuple[str, str]:
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if data.startswith(b"%PDF-"):
            return "pdf", "application/pdf"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image", "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image", "image/jpeg"
        if mime.startswith("image/"):
            raise RoomArtifactError("image bytes do not match their file type")
        if mime == "application/pdf":
            raise RoomArtifactError("PDF bytes do not match their file type")
        if mime.startswith("text/"):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RoomArtifactError("text artifact is not valid UTF-8") from exc
        return "file", mime

    @staticmethod
    def _manifest(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": str(row["artifact_id"]),
            "kind": str(row["kind"]),
            "name": str(row["name"]),
            "size": int(row["size"]),
            "mime": str(row["mime"]),
            "sha256": str(row["sha256"]),
        }

    def put_path(
        self,
        *,
        scope: RoomArtifactScope,
        path: Path | str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Copy one regular file into private storage before returning."""

        candidate = Path(path).resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_ATTACHMENT_BYTES:
                raise RoomArtifactError("artifact must be a bounded regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(MAX_ATTACHMENT_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(data) != info.st_size or len(data) > MAX_ATTACHMENT_BYTES:
            raise RoomArtifactError("artifact changed while it was being copied")
        safe_name = self._safe_name(name or candidate.name)
        kind, mime = self._classify(safe_name, data)
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"rart_{hashlib.sha256((scope.key + digest + safe_name).encode()).hexdigest()[:32]}"
        blob_name = f"blob_{secrets.token_hex(16)}"
        target = self.blob_root / blob_name

        with self._lock, self._connect() as conn:
            self._initialize(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT * FROM hosted_room_output_artifacts
                   WHERE scope_key=? AND sha256=? AND name=?""",
                (scope.key, digest, safe_name),
            ).fetchone()
            if existing is not None:
                return self._manifest(existing)
            totals = conn.execute(
                """SELECT COUNT(*) AS count, COALESCE(SUM(size), 0) AS bytes
                   FROM hosted_room_output_artifacts
                   WHERE scope_key=? AND acknowledged_at IS NULL""",
                (scope.key,),
            ).fetchone()
            if (
                int(totals["count"]) >= MAX_ATTACHMENTS_PER_MESSAGE
                or int(totals["bytes"]) + len(data) > MAX_MESSAGE_ATTACHMENT_BYTES
            ):
                raise RoomArtifactError("room turn artifact quota exceeded")
            gateway_bytes = int(conn.execute(
                """SELECT COALESCE(SUM(size), 0)
                   FROM hosted_room_output_artifacts
                   WHERE acknowledged_at IS NULL"""
            ).fetchone()[0])
            if gateway_bytes + len(data) > MAX_GATEWAY_BLOB_BYTES:
                raise RoomArtifactError("gateway room artifact quota exceeded")
            temp = self.blob_root / f".tmp-{secrets.token_hex(16)}"
            file_descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(file_descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
                os.chmod(target, 0o600)
                conn.execute(
                    """INSERT INTO hosted_room_output_artifacts
                       (artifact_id, scope_key, scope_json, name, kind, mime, size,
                        sha256, blob_name, created_at, acknowledged_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                    (
                        artifact_id,
                        scope.key,
                        json.dumps(scope.as_mapping(), sort_keys=True, separators=(",", ":")),
                        safe_name,
                        kind,
                        mime,
                        len(data),
                        digest,
                        blob_name,
                        time.time(),
                    ),
                )
                conn.commit()
            except Exception:
                temp.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise
            row = conn.execute(
                "SELECT * FROM hosted_room_output_artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("stored room artifact could not be reloaded")
            return self._manifest(row)

    def list(self, scope: RoomArtifactScope) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._initialize(conn)
            rows = conn.execute(
                """SELECT * FROM hosted_room_output_artifacts
                   WHERE scope_key=? AND acknowledged_at IS NULL
                   ORDER BY created_at, artifact_id""",
                (scope.key,),
            ).fetchall()
        return [self._manifest(row) for row in rows]

    def read(self, scope: RoomArtifactScope, artifact_id: str) -> tuple[dict[str, Any], bytes]:
        with self._connect() as conn:
            self._initialize(conn)
            row = conn.execute(
                """SELECT * FROM hosted_room_output_artifacts
                   WHERE scope_key=? AND artifact_id=? AND acknowledged_at IS NULL""",
                (scope.key, artifact_id),
            ).fetchone()
        if row is None:
            raise RoomArtifactError("room artifact not found")
        path = self.blob_root / str(row["blob_name"])
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != int(row["size"]):
                raise RoomArtifactError("room artifact bytes changed")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(MAX_ATTACHMENT_BYTES + 1)
        finally:
            os.close(descriptor)
        if hashlib.sha256(data).hexdigest() != str(row["sha256"]):
            raise RoomArtifactError("room artifact failed SHA-256 validation")
        return self._manifest(row), data

    def acknowledge(self, scope: RoomArtifactScope, artifact_ids: Sequence[str]) -> int:
        ids = tuple(dict.fromkeys(str(item) for item in artifact_ids if str(item)))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            self._initialize(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""SELECT artifact_id, blob_name, acknowledged_at
                    FROM hosted_room_output_artifacts
                    WHERE scope_key=? AND artifact_id IN ({placeholders})""",
                (scope.key, *ids),
            ).fetchall()
            if len(rows) != len(ids):
                raise RoomArtifactError("room artifact acknowledgement scope changed")
            changed = conn.execute(
                f"""UPDATE hosted_room_output_artifacts SET acknowledged_at=?
                    WHERE scope_key=? AND artifact_id IN ({placeholders})
                      AND acknowledged_at IS NULL""",
                (time.time(), scope.key, *ids),
            ).rowcount
            conn.commit()
        for row in rows:
            if row["acknowledged_at"] is None:
                (self.blob_root / str(row["blob_name"])).unlink(missing_ok=True)
        return int(changed)

    def discard(self, scope: RoomArtifactScope) -> int:
        """Purge every unacknowledged artifact for one cancelled attempt."""

        with self._lock, self._connect() as conn:
            self._initialize(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT artifact_id, blob_name FROM hosted_room_output_artifacts
                   WHERE scope_key=? AND acknowledged_at IS NULL""",
                (scope.key,),
            ).fetchall()
            conn.execute(
                "DELETE FROM hosted_room_output_artifacts WHERE scope_key=?",
                (scope.key,),
            )
            conn.commit()
        for row in rows:
            (self.blob_root / str(row["blob_name"])).unlink(missing_ok=True)
        return len(rows)

    def discard_claims(self, claims: Mapping[str, Any]) -> int:
        """Purge output bytes covered by one revoked room-member grant."""

        expected = {
            key: claims.get(key)
            for key in (
                "room_id",
                "home_install_id",
                "authority_gateway_id",
                "authority_epoch",
                "member_id",
                "target_install_id",
                "target_profile",
            )
        }
        rows_to_delete: list[sqlite3.Row] = []
        with self._lock, self._connect() as conn:
            self._initialize(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM hosted_room_output_artifacts WHERE acknowledged_at IS NULL"
            ).fetchall()
            for row in rows:
                try:
                    scope = RoomArtifactScope.from_mapping(json.loads(row["scope_json"]))
                except Exception:
                    continue
                mapping = scope.as_mapping()
                if all(mapping.get(key) == value for key, value in expected.items()):
                    rows_to_delete.append(row)
            if rows_to_delete:
                conn.executemany(
                    "DELETE FROM hosted_room_output_artifacts WHERE artifact_id=?",
                    ((row["artifact_id"],) for row in rows_to_delete),
                )
                conn.commit()
        for row in rows_to_delete:
            (self.blob_root / str(row["blob_name"])).unlink(missing_ok=True)
        return len(rows_to_delete)

    def discard_room(self, room_id: str) -> int:
        """Purge every unacknowledged output owned by one disbanded room."""

        rows_to_delete: list[sqlite3.Row] = []
        with self._lock, self._connect() as conn:
            self._initialize(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM hosted_room_output_artifacts WHERE acknowledged_at IS NULL"
            ).fetchall()
            for row in rows:
                try:
                    scope = RoomArtifactScope.from_mapping(json.loads(row["scope_json"]))
                except Exception:
                    continue
                if scope.room_id == room_id:
                    rows_to_delete.append(row)
            if rows_to_delete:
                conn.executemany(
                    "DELETE FROM hosted_room_output_artifacts WHERE artifact_id=?",
                    ((row["artifact_id"],) for row in rows_to_delete),
                )
                conn.commit()
        for row in rows_to_delete:
            (self.blob_root / str(row["blob_name"])).unlink(missing_ok=True)
        return len(rows_to_delete)

    def discard_superseded(self, scope: RoomArtifactScope) -> int:
        """Purge older execution generations for the same logical task."""

        rows_to_delete: list[sqlite3.Row] = []
        with self._lock, self._connect() as conn:
            self._initialize(conn)
            rows = conn.execute(
                "SELECT * FROM hosted_room_output_artifacts WHERE acknowledged_at IS NULL"
            ).fetchall()
            current = scope.as_mapping()
            for row in rows:
                try:
                    candidate = RoomArtifactScope.from_mapping(
                        json.loads(row["scope_json"])
                    )
                except Exception:
                    continue
                mapping = candidate.as_mapping()
                if (
                    candidate.execution_generation < scope.execution_generation
                    and all(
                        mapping[key] == current[key]
                        for key in current
                        if key != "execution_generation"
                    )
                ):
                    rows_to_delete.append(row)
            if rows_to_delete:
                conn.executemany(
                    "DELETE FROM hosted_room_output_artifacts WHERE artifact_id=?",
                    ((row["artifact_id"],) for row in rows_to_delete),
                )
                conn.commit()
        for row in rows_to_delete:
            (self.blob_root / str(row["blob_name"])).unlink(missing_ok=True)
        return len(rows_to_delete)


def terminal_artifact_manifest(db_path: Path | str, scope: RoomArtifactScope) -> dict[str, Any] | None:
    items = RoomArtifactOutbox(db_path).list(scope)
    if not items:
        return None
    digest = hashlib.sha256(
        json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"version": 1, "manifest_digest": digest, "items": items}


def validate_terminal_artifact_manifest(value: Any) -> list[dict[str, Any]]:
    """Validate the bounded metadata returned by one room member turn."""

    if value is None:
        return []
    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "manifest_digest",
        "items",
    }:
        raise RoomArtifactError("terminal artifact manifest fields are invalid")
    if value.get("version") != 1 or not isinstance(value.get("items"), list):
        raise RoomArtifactError("terminal artifact manifest version is unsupported")
    items = value["items"]
    if not 0 < len(items) <= MAX_ATTACHMENTS_PER_MESSAGE:
        raise RoomArtifactError("terminal artifact manifest count is invalid")
    normalized: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping) or set(raw) != {
            "artifact_id",
            "kind",
            "name",
            "size",
            "mime",
            "sha256",
        }:
            raise RoomArtifactError("terminal artifact item fields are invalid")
        artifact_id = str(raw["artifact_id"])
        sha256 = str(raw["sha256"])
        kind = str(raw["kind"])
        mime = str(raw["mime"])
        size = raw["size"]
        if (
            not artifact_id.startswith("rart_")
            or len(artifact_id) != 37
            or not all(char in "0123456789abcdef" for char in artifact_id[5:])
            or len(sha256) != 64
            or not all(char in "0123456789abcdef" for char in sha256)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= MAX_ATTACHMENT_BYTES
            or kind not in {"image", "pdf", "file"}
            or "/" not in mime
            or len(mime) > 127
            or (kind == "image" and not mime.startswith("image/"))
            or (kind == "pdf" and mime != "application/pdf")
        ):
            raise RoomArtifactError("terminal artifact item metadata is invalid")
        normalized.append({
            "artifact_id": artifact_id,
            "kind": kind,
            "name": RoomArtifactOutbox._safe_name(str(raw["name"])),
            "size": size,
            "mime": mime,
            "sha256": sha256,
        })
    if sum(item["size"] for item in normalized) > MAX_MESSAGE_ATTACHMENT_BYTES:
        raise RoomArtifactError("terminal artifact manifest byte size is invalid")
    expected = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if str(value.get("manifest_digest")) != expected:
        raise RoomArtifactError("terminal artifact manifest digest changed")
    return normalized


__all__ = [
    "RoomArtifactError",
    "RoomArtifactOutbox",
    "RoomArtifactScope",
    "bind_room_artifact_scope",
    "current_room_artifact_scope",
    "reset_room_artifact_scope",
    "terminal_artifact_manifest",
    "validate_terminal_artifact_manifest",
]
