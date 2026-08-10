"""Markdown/frontmatter boundary for the managed Obsidian memory area."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

from .contracts import Authority, MemoryRecord, MemoryStatus, Verification
from .store import SqliteMemoryStore
from .security import assert_safe_to_persist


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
    def __init__(self, vault_path: Path, managed_folder: str):
        self.vault_path = Path(vault_path)
        self.managed_root = self.vault_path / managed_folder

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

    def _managed_path(self, record: MemoryRecord) -> Path:
        folder = self.managed_root / record.memory_type
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{record.memory_id}.md"

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
        result = self.scan_managed_changes(store)
        return RebuildResult(
            scanned=len(list(self.managed_root.rglob("*.md"))),
            reparsed=len(result.reparsed_paths),
            malformed=len(result.malformed_paths),
        )
