"""Honcho <-> GBrain/MemPalace bidirectional bridge.

Export: Honcho conclusions -> GBrain (Diego page) + MemPalace.
Seed:   GBrain compiled-truth facts -> Honcho (user peer).
Loop prevention: bidirectional provenance tags + per-direction state hashes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^\s*\[source:(?P<src>[a-z0-9_-]+)\]\s*")


def tag_fact(text: str, source: str) -> str:
    """Prefix a fact with a provenance tag, e.g. '[source:honcho] ...'."""
    return f"[source:{source.lower()}] {strip_tag(text)}"


def has_source(text: str, source: str) -> bool:
    """True if text carries the given provenance tag."""
    m = _TAG_RE.match(text or "")
    return bool(m and m.group("src") == source)


def strip_tag(text: str) -> str:
    """Remove any leading provenance tag."""
    return _TAG_RE.sub("", text or "").strip()


def fact_hash(text: str) -> str:
    """Stable hash of a fact's semantic text, ignoring provenance tags."""
    return hashlib.sha256(strip_tag(text).encode("utf-8")).hexdigest()[:16]


def load_state(path: Path) -> set[str]:
    """Load a set of seen hashes from a JSON file (empty set if missing/unreadable/wrong-type)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return set(data)


def save_state(path: Path, hashes: Iterable[str]) -> None:
    """Persist a set of seen hashes to a JSON file."""
    p = path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(hashes)), encoding="utf-8")
