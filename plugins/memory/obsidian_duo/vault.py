"""Markdown/frontmatter boundary for the managed Obsidian memory area."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Iterator

import yaml

from .contracts import Authority, MemoryRecord, MemoryStatus, Verification
from .store import SqliteMemoryStore
from .security import assert_safe_to_persist, redact_secrets


@dataclass(frozen=True)
class ParsedNote:
    path: Path
    memory_id: str
    metadata: dict
    body: str


@dataclass(frozen=True)
class ScanResult:
    reparsed_paths: tuple[Path, ...]
    malformed_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RebuildResult:
    scanned: int
    reparsed: int
    malformed: int


class ObsidianVault:
    MEMORY_TYPE_FOLDERS = {
        "project": "Projects",
        "decision": "Decisions",
        "research": "Research",
        "person": "People",
        "preference": "Preferences",
        "lesson": "Lessons",
        "workflow": "Workflows",
        "task": "Tasks",
        "entity": "Entities",
        "fact": "Entities",
        "candidate": "Inbox",
        "conflict": "Conflicts",
    }

    def __init__(self, vault_path: Path, managed_folder: str):
        self.vault_path = Path(vault_path)
        raw_folder = str(managed_folder or "")
        folder_path = Path(raw_folder)
        if (
            not raw_folder.strip()
            or folder_path.is_absolute()
            or PureWindowsPath(raw_folder).is_absolute()
            or PureWindowsPath(raw_folder).drive
            or ".." in folder_path.parts
        ):
            raise ValueError("managed_folder must be a relative path inside the vault")
        self.vault_root = self.vault_path.resolve()
        self.managed_root = (self.vault_path / folder_path).resolve()
        self._assert_inside(self.managed_root, self.vault_root, "managed_folder")

    @staticmethod
    def _assert_inside(path: Path, root: Path, label: str) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} escapes the configured vault") from exc

    def ensure_managed_structure(self) -> None:
        for name in (
            "Projects", "Decisions", "Research", "People", "Preferences",
            "Lessons", "Workflows", "Tasks", "Entities", "Conflicts", "Inbox",
        ):
            (self.managed_root / name).mkdir(parents=True, exist_ok=True)

    def catalog_markdown_paths(self) -> Iterator[Path]:
        if not self.vault_path.exists():
            return
        yield from sorted(path for path in self.vault_path.rglob("*.md") if path.is_file())

    def catalog_external_markdown_paths(self) -> Iterator[Path]:
        ignored = {".git", ".trash", ".obsidian", "node_modules", "__pycache__"}
        managed = self.managed_root.resolve()
        for path in self.catalog_markdown_paths() or ():
            resolved = path.resolve()
            if resolved == managed or managed in resolved.parents:
                continue
            if any(part in ignored or part.startswith(".") and part in {".cache", ".tmp"} for part in path.parts):
                continue
            yield path

    def _managed_path(self, record: MemoryRecord) -> Path:
        memory_type = str(record.memory_type or "").strip().lower()
        if memory_type not in self.MEMORY_TYPE_FOLDERS:
            raise ValueError(f"unsupported memory_type: {record.memory_type!r}")
        folder = (self.managed_root / self.MEMORY_TYPE_FOLDERS[memory_type]).resolve()
        self._assert_inside(folder, self.managed_root, "managed memory folder")
        self._assert_inside(folder, self.vault_root, "managed memory folder")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{record.memory_id}.md"
        self._assert_inside(path.resolve(), self.managed_root, "managed note")
        self._assert_inside(path.resolve(), self.vault_root, "managed note")
        return path

    def parse_note(self, path: Path) -> ParsedNote:
        text = Path(path).read_text(encoding="utf-8")
        metadata = {}
        body = text
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end == -1:
                raise ValueError(f"malformed frontmatter: {path}")
            metadata = yaml.safe_load(text[4:end]) or {}
            if not isinstance(metadata, dict):
                raise ValueError(f"frontmatter must be a mapping: {path}")
            body = text[end + 5:]
        memory_id = str(metadata.get("memory_id") or "").strip()
        if not memory_id:
            raise ValueError(f"missing memory_id: {path}")
        return ParsedNote(Path(path), memory_id, metadata, body.rstrip("\n"))

    def _render(self, record: MemoryRecord) -> str:
        metadata = {
            "memory_id": record.memory_id,
            "memory_type": record.memory_type,
            "scope": record.scope,
            "status": record.status.value,
            "authority": record.authority.value,
            "verification": record.verification.value,
            "confidence": record.confidence,
            "importance": record.importance,
            "evidence_ids": list(record.evidence_ids),
            "relationships": list(record.relationships),
            "source_session_id": record.source_session_id,
            "task_id": record.task_id,
            "project_id": record.project_id,
            "child_session_id": record.child_session_id,
            "mission_id": record.mission_id,
            "agent_id": record.agent_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }
        return "---\n" + yaml.safe_dump(metadata, sort_keys=False).rstrip() + "\n---\n" + record.content.rstrip() + "\n"

    def write_managed_note(self, record: MemoryRecord) -> Path:
        self.ensure_managed_structure()
        path = self._managed_path(record)
        text = self._render(record)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return path

    @staticmethod
    def _hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def scan_managed_changes(self, store: SqliteMemoryStore) -> ScanResult:
        self.ensure_managed_structure()
        changed = []
        malformed = []
        conn = store.connection()
        for path in sorted(self.managed_root.rglob("*.md")):
            stat = path.stat()
            previous = conn.execute(
                "SELECT memory_id,mtime_ns,size,content_hash FROM note_index WHERE path=?",
                (str(path),),
            ).fetchone()
            if previous and previous["mtime_ns"] == stat.st_mtime_ns and previous["size"] == stat.st_size:
                continue
            content_hash = self._hash(path)
            if previous and previous["content_hash"] == content_hash:
                store.set_note_index(str(path), previous["memory_id"] if previous else None, stat.st_mtime_ns, stat.st_size, content_hash)
                continue
            try:
                note = self.parse_note(path)
                assert_safe_to_persist(note.body)
                if previous is not None:
                    store.set_note_index(str(path), previous["memory_id"], stat.st_mtime_ns, stat.st_size, content_hash, "manual_pending")
                    changed.append(path)
                    continue
                metadata = note.metadata
                record = MemoryRecord(
                    memory_id=note.memory_id,
                    content=note.body,
                    memory_type=str(metadata.get("memory_type") or path.parent.name),
                    scope=str(metadata.get("scope") or "global"),
                    status=MemoryStatus(str(metadata.get("status") or "active")),
                    authority=Authority(str(metadata.get("authority") or "agent")),
                    verification=Verification(str(metadata.get("verification") or "unverified")),
                    confidence=float(metadata.get("confidence") or 0.0),
                    importance=float(metadata.get("importance") or 0.0),
                    evidence_ids=tuple(metadata.get("evidence_ids") or ()),
                    relationships=tuple(metadata.get("relationships") or ()),
                    source_session_id=str(metadata.get("source_session_id") or ""),
                    task_id=str(metadata.get("task_id") or ""),
                    project_id=str(metadata.get("project_id") or ""),
                    child_session_id=str(metadata.get("child_session_id") or ""),
                    mission_id=str(metadata.get("mission_id") or ""),
                    agent_id=str(metadata.get("agent_id") or ""),
                    created_at=str(metadata.get("created_at") or ""),
                    updated_at=str(metadata.get("updated_at") or ""),
                )
                store.upsert_memory(record, "vault scan")
                store.set_note_index(str(path), note.memory_id, stat.st_mtime_ns, stat.st_size, content_hash)
                changed.append(path)
            except (ValueError, yaml.YAMLError, OSError, TypeError):
                memory_id = previous["memory_id"] if previous is not None else None
                store.set_note_index(str(path), memory_id, stat.st_mtime_ns, stat.st_size, content_hash, "needs_attention")
                malformed.append(path)
        return ScanResult(tuple(changed), tuple(malformed))

    def rebuild_from_vault(self, store: SqliteMemoryStore, *, full: bool) -> RebuildResult:
        if full:
            with store.connection():
                store.connection().execute("DELETE FROM note_index")
                store.connection().execute("DELETE FROM external_index")
                store.connection().execute("DELETE FROM memory_fts WHERE memory_id LIKE 'external_%'")
                store.connection().execute("DELETE FROM memories WHERE memory_id LIKE 'external_%'")
        result = self.scan_managed_changes(store)
        external_reparsed = 0
        external_paths = tuple(self.catalog_external_markdown_paths() or ())
        for path in external_paths:
            stat = path.stat()
            previous = store.connection().execute(
                "SELECT memory_id,mtime_ns,size,content_hash FROM external_index WHERE path=?",
                (str(path),),
            ).fetchone()
            if previous and previous["mtime_ns"] == stat.st_mtime_ns and previous["size"] == stat.st_size:
                continue
            content_hash = self._hash(path)
            if previous and previous["content_hash"] == content_hash:
                store.set_external_index(str(path), previous["memory_id"], stat.st_mtime_ns, stat.st_size, content_hash)
                continue
            content = redact_secrets(path.read_text(encoding="utf-8")[:12000])
            memory_id = "external_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
            store.upsert_memory(MemoryRecord(
                memory_id, content, "fact", "external", authority=Authority.SOURCE,
                verification=Verification.SOURCE_SUPPORTED,
                relationships=(f"source_path:{path}",),
            ), "external markdown index")
            store.set_external_index(str(path), memory_id, stat.st_mtime_ns, stat.st_size, content_hash)
            external_reparsed += 1
        return RebuildResult(
            scanned=len(list(self.managed_root.rglob("*.md"))) + len(external_paths),
            reparsed=len(result.reparsed_paths) + external_reparsed,
            malformed=len(result.malformed_paths),
        )
