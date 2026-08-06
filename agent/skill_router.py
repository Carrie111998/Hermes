"""FIX-011: Skill-Index RAG + Lazy Loader.

Bis einschliesslich hermes-2026.07.x wurden Skill-Beschreibungen in
den System-Prompt geladen (alle Skills, vollstaendige Description) und
bei jedem ``skill_view``-Aufruf der gesamte SKILL.md noch einmal.
Das verbrauchte zwischen 8 kB und 25 kB System-Tokens pro Run,
selbst wenn der Skill gar nicht aufgerufen wurde.

FIX-011 aendert das in zwei Schritten:

1. ``SkillIndex.scan()`` scannt einmal alle Skills und persistiert
   einen kompakten Index unter ``~/.hermes/state/skill_index.json``.
   Jeder Eintrag enthaelt nur ``name``, ``short_desc`` (<= 240 chars),
   ``triggers`` (Liste von Schluesselwoertern) und ``sha256`` der
   vollen ``SKILL.md``. Nicht im Index: der Body der SKILL.md.

2. ``SkillIndex.select_relevant(prompt, top_k)`` macht eine triviale
   Keyword/RAG-Score-Suche ueber Indexeintraege und liefert die
   ``top_k``-Kandidaten. System-Prompt kann dann nur diese
   Kandidaten einbetten, der Rest wird ueber ``SkillIndex.lazy_load``
   erst beim ``skill_view`` geladen.

Diese Implementierung ist absichtlich einfach gehalten (keine
Embedding-Abhaengigkeit, keine externe Vektor-DB). Sie ersetzt
nicht skills_hub.search, sondern stellt eine System-Prompt-freundliche
Vor-Auswahl bereit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("hermes.skill_router")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SkillIndexEntry:
    """Kompakter Eintrag im Skill-Index. Body ist NICHT enthalten."""
    name: str
    path: str
    short_desc: str
    triggers: List[str] = field(default_factory=list)
    sha256: str = ""
    body_size: int = 0
    last_seen: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "short_desc": self.short_desc,
            "triggers": self.triggers,
            "sha256": self.sha256,
            "body_size": self.body_size,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "SkillIndexEntry":
        return cls(
            name=str(d.get("name", "")),
            path=str(d.get("path", "")),
            short_desc=str(d.get("short_desc", "")),
            triggers=list(d.get("triggers", []) or []),
            sha256=str(d.get("sha256", "")),
            body_size=int(d.get("body_size", 0) or 0),
            last_seen=str(d.get("last_seen", "")),
        )


class SkillIndex:
    """FIX-011: Lazy Skill-Index fuer den System-Prompt.

    Usage:
        index = SkillIndex()                 # auto-resolved Pfad
        index.scan()                          # scannt Disk und baut Index
        top = index.select_relevant(prompt)   # 5 beste Treffer
        full = index.lazy_load("git-clone-audit")
    """

    DEFAULT_INDEX_PATH = Path.home() / ".hermes" / "state" / "skill_index.json"
    SKILL_FILE = "SKILL.md"
    DEFAULT_TRIGGERS = ("name:", "trigger:", "use when")
    SHORT_DESC_MAX_CHARS = 240

    def __init__(self, index_path: Optional[Path] = None) -> None:
        self.index_path = Path(index_path) if index_path else self.DEFAULT_INDEX_PATH
        self._entries: Dict[str, SkillIndexEntry] = {}
        self._loaded_at: Optional[str] = None

    # --- I/O ---------------------------------------------------------

    def load(self) -> bool:
        """Laedt den persistenten Index. True bei Erfolg."""
        if not self.index_path.exists():
            return False
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("FIX-011: index load failed %s: %s", self.index_path, exc)
            return False
        self._entries = {
            k: SkillIndexEntry.from_dict(v)  # type: ignore[arg-type]
            for k, v in (data.get("entries") or {}).items()
        }
        self._loaded_at = data.get("loaded_at")
        return True

    def save(self) -> bool:
        """Persistiert den Index. True bei Erfolg."""
        payload = {
            "loaded_at": _now_iso(),
            "schema_version": "2026-07-27",
            "entries": {k: v.to_dict() for k, v in self._entries.items()},
        }
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.index_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("FIX-011: index save failed %s: %s", self.index_path, exc)
            return False
        return True

    # --- Discovery ---------------------------------------------------

    def _skill_dirs(self) -> Iterable[Path]:
        """Iteriert ueber alle bekannten Skill-Pfade."""
        candidates: List[Path] = []
        # User-Profile Skills
        candidates.append(Path.home() / ".hermes" / "skills")
        # Repo-bundled Skills (falls vorhanden)
        for env_var in ("HERMES_ROOT", "HERMES_HOME"):
            root = os.environ.get(env_var)
            if root:
                candidates.append(Path(root) / "skills")
        # Hermes Agent Repo (falls vorhanden)
        candidates.append(Path("/home/bratan/.hermes/hermes-agent/skills"))
        for base in candidates:
            if base.exists() and base.is_dir():
                yield base

    def _iter_skill_dirs(self) -> Iterable[Path]:
        """Iteriert ueber jeden Skill-Unterordner."""
        seen = set()
        for base in self._skill_dirs():
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                if child in seen:
                    continue
                seen.add(child)
                if (child / self.SKILL_FILE).is_file():
                    yield child

    def scan(self, persist: bool = True) -> int:
        """Scannt alle Skills und baut den Index. Returnt Anzahl Eintraege."""
        now = _now_iso()
        new_entries: Dict[str, SkillIndexEntry] = {}
        for d in self._iter_skill_dirs():
            name = d.name
            skill_file = d / self.SKILL_FILE
            try:
                body = skill_file.read_text(encoding="utf-8")
            except OSError:
                continue
            sha = _sha256_file(skill_file)
            short_desc, triggers = self._extract_metadata(body)
            new_entries[name] = SkillIndexEntry(
                name=name,
                path=str(skill_file),
                short_desc=short_desc,
                triggers=triggers,
                sha256=sha,
                body_size=len(body),
                last_seen=now,
            )
        self._entries = new_entries
        self._loaded_at = now
        if persist:
            self.save()
        return len(self._entries)

    @staticmethod
    def _extract_metadata(body: str) -> Tuple[str, List[str]]:
        """Holt Kurzbeschreibung + Trigger aus dem YAML-Frontmatter."""
        text = body.strip()
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end > 0:
                front = text[3:end]
                body_only = text[end + 4:].lstrip()
            else:
                front = text
                body_only = text
        else:
            front = ""
            body_only = text

        desc = ""
        triggers: List[str] = []
        for line in front.splitlines():
            stripped = line.strip()
            # Beschreibung
            if not desc and re.match(r"^(?:description|desc)\s*:", stripped, re.IGNORECASE):
                rest = re.split(r":", stripped, maxsplit=1)[1].strip()
                desc = rest.strip("'\"")
                continue
            # Trigger
            m_use = re.match(r"^use when\s*:", stripped, re.IGNORECASE)
            m_trigger = re.match(r"^triggers?\s*:", stripped, re.IGNORECASE)
            if m_use or m_trigger:
                rest = re.split(r":", stripped, maxsplit=1)[1].strip()
                triggers.extend(
                    t.strip().strip("'\"")
                    for t in re.split(r"[,;|]", rest)
                    if t.strip()
                )

        if not desc:
            # Fallback: erste nicht-leere Zeile des Body
            for line in body_only.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    desc = line
                    break
        if not desc:
            desc = "(no description)"

        short = desc if len(desc) <= SkillIndex.SHORT_DESC_MAX_CHARS else desc[: SkillIndex.SHORT_DESC_MAX_CHARS - 1] + "…"
        return short, triggers

    # --- Selection ---------------------------------------------------

    @staticmethod
    def _score(prompt: str, entry: SkillIndexEntry) -> float:
        """Trivialer Keyword-Score: Treffer in Name + triggers + short_desc."""
        p = prompt.lower()
        score = 0.0
        if entry.name.lower() in p:
            score += 5.0
        for trig in entry.triggers:
            if trig and trig.lower() in p:
                score += 2.0
        words = set(re.findall(r"[a-z]{4,}", entry.short_desc.lower()))
        for w in words:
            if w in p:
                score += 0.5
        return score

    def select_relevant(self, prompt: str, top_k: int = 5) -> List[SkillIndexEntry]:
        """Liefert die top_k Skills nach Keyword-Score."""
        if not self._entries:
            self.load()
        scored = [(self._score(prompt, e), e) for e in self._entries.values()]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for s, e in scored[:top_k] if s > 0]

    # --- Lazy load ---------------------------------------------------

    def lazy_load(self, name: str) -> Optional[str]:
        """Laedt den vollen Skill-Body erst on-demand."""
        entry = self._entries.get(name)
        if not entry:
            # Lazy Reload vom Index
            self.load()
            entry = self._entries.get(name)
        if not entry:
            return None
        path = Path(entry.path)
        if not path.exists():
            return None
        current_sha = _sha256_file(path)
        if current_sha != entry.sha256:
            logger.info("FIX-011: skill body changed since index: %s", name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("FIX-011: lazy load failed %s: %s", path, exc)
            return None

    # --- Convenience -------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._entries

    def names(self) -> List[str]:
        return sorted(self._entries.keys())


__all__ = [
    "SkillIndex",
    "SkillIndexEntry",
]