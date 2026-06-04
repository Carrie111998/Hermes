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
import subprocess
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


_TIMELINE_MARKER = "<!-- timeline -->"


def merge_compiled_truth(page_md: str, facts: list[str]) -> str:
    """Insert facts into the compiled-truth block (above the timeline marker).

    Facts already present (by exact line match, ignoring a leading bullet) are
    skipped. If the page has no timeline marker, one is appended and facts go
    above it. Each new fact is added as its own bullet line.
    """
    if _TIMELINE_MARKER in page_md:
        above, _, below = page_md.partition(_TIMELINE_MARKER)
    else:
        above, below = page_md.rstrip() + "\n\n", "\n"

    def _unbullet(s: str) -> str:
        s = s.strip()
        return s[2:].strip() if s.startswith("- ") else s

    seen = {_unbullet(ln) for ln in above.splitlines()}
    additions = []
    for fact in facts:
        line = fact.strip()
        if line and line not in seen:
            seen.add(line)
            additions.append(f"- {line}")
    if additions:
        above = above.rstrip() + "\n" + "\n".join(additions) + "\n\n"
    return f"{above}{_TIMELINE_MARKER}{below}"


_GBRAIN_TIMEOUT = 15


class GBrainAdapter:
    """Thin wrapper over the `gbrain` CLI. All methods are best-effort."""

    def get_page(self, slug: str) -> str | None:
        """Return the page markdown, or None if the page/CLI is unavailable.

        Note: an existing-but-empty page yields "" (falsy), not None.
        """
        try:
            r = subprocess.run(
                ["gbrain", "get", slug],
                capture_output=True, text=True, timeout=_GBRAIN_TIMEOUT,
            )
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("gbrain get %s failed: %s", slug, e)
            return None

    def put_page(self, slug: str, markdown: str) -> bool:
        try:
            r = subprocess.run(
                ["gbrain", "put", slug],
                input=markdown, capture_output=True, text=True, timeout=_GBRAIN_TIMEOUT,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("gbrain put %s failed: %s", slug, e)
            return False

    def add_timeline(self, slug: str, date: str, text: str) -> bool:
        try:
            r = subprocess.run(
                ["gbrain", "timeline-add", slug, date, text],
                capture_output=True, text=True, timeout=_GBRAIN_TIMEOUT,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("gbrain timeline-add %s failed: %s", slug, e)
            return False


BRIDGE_SESSION = "hermes-autonomous"
EVENTS_PEER = "hermes-events"


def build_manager():
    """Construct a HonchoSessionManager from the active honcho.json config.

    Returns None if Honcho is not configured/available — callers skip.
    """
    try:
        from plugins.memory.honcho.client import HonchoClientConfig, get_honcho_client
        from plugins.memory.honcho.session import HonchoSessionManager
        cfg = HonchoClientConfig.from_global_config()
        if not cfg.enabled or not (cfg.api_key or cfg.base_url):
            return None
        client = get_honcho_client(cfg)
        return HonchoSessionManager(honcho=client, config=cfg)
    except Exception as e:  # SDK missing, paused backend, bad config
        logger.warning("Honcho manager unavailable for bridge: %s", e)
        return None


class HonchoAdapter:
    """Read/write wrapper over HonchoSessionManager for the bridge."""

    def __init__(self, manager, session_key: str = BRIDGE_SESSION):
        self._m = manager
        self._key = session_key

    def _ensure(self) -> None:
        self._m.get_or_create(self._key)

    def read_user_facts(self) -> list[str]:
        self._ensure()
        return self._m.get_peer_card(self._key, peer="user")

    def run_dialectic(self, query: str) -> str:
        self._ensure()
        return self._m.dialectic_query(self._key, query, peer="user")

    def write_conclusion(self, content: str, peer: str = "user") -> bool:
        self._ensure()
        return self._m.create_conclusion(self._key, content, peer=peer)
