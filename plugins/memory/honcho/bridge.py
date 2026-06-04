"""Honcho <-> GBrain/MemPalace bidirectional bridge.

Export: Honcho conclusions -> GBrain (Diego page) + MemPalace.
Seed:   GBrain compiled-truth facts -> Honcho (user peer).
Loop prevention: bidirectional provenance tags + per-direction state hashes.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
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
            # Pass content via --content (argv), NOT piped stdin: `gbrain put`
            # reads stdin by opening '/dev/stdin', which does not exist on
            # Windows and fails with ENOENT. --content is cross-platform.
            r = subprocess.run(
                ["gbrain", "put", slug, "--content", markdown],
                capture_output=True, text=True, timeout=_GBRAIN_TIMEOUT,
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


def _write_mempalace_drawer(room: str, content: str) -> bool:
    """Best-effort MemPalace drawer write into the honcho-conclusions wing."""
    try:
        from mempalace.palace import get_collection
        from mempalace.miner import add_drawer
        palace_root = os.environ.get("MEMPALACE_HOME") or str(
            Path.home() / ".mempalace" / "palace"
        )
        collection = get_collection(palace_root)
        add_drawer(
            collection, wing="honcho-conclusions", room=room, content=content,
            source_file=f"honcho:{fact_hash(content)}", chunk_index=0,
            agent="honcho-bridge",
        )
        return True
    except Exception as e:
        logger.warning("MemPalace drawer write failed: %s", e)
        return False


def run_export(honcho, gbrain, *, slug, date, dialectic_queries, state_path, dry_run):
    """Export Honcho conclusions to GBrain (timeline + compiled) and MemPalace."""
    seen = load_state(state_path)
    res = {"exported": 0, "deduped": 0, "loop_skipped": 0, "write_failed": 0}
    new_hashes: set[str] = set()
    compiled_facts: list[str] = []

    def _consider(text: str, high_conf: bool):
        if has_source(text, "gbrain"):
            res["loop_skipped"] += 1
            return
        h = fact_hash(text)
        if h in seen or h in new_hashes:
            res["deduped"] += 1
            return
        tagged = tag_fact(text, "honcho")
        if not dry_run:
            if not gbrain.add_timeline(slug, date, tagged):
                res["write_failed"] += 1
                return  # transient failure — don't record hash, retry next run
            if high_conf:
                compiled_facts.append(tagged)
            else:
                _write_mempalace_drawer(room="conclusion", content=tagged)
        elif high_conf:
            compiled_facts.append(tagged)
        new_hashes.add(h)
        res["exported"] += 1

    for fact in honcho.read_user_facts():
        if fact and fact.strip():
            _consider(fact, high_conf=True)
    for q in dialectic_queries:
        answer = honcho.run_dialectic(q)
        if answer and answer.strip():
            _consider(answer, high_conf=False)

    if compiled_facts and not dry_run:
        page = gbrain.get_page(slug)
        if page is not None:
            if not gbrain.put_page(slug, merge_compiled_truth(page, compiled_facts)):
                logger.warning("Honcho bridge: compiled-truth put_page failed for %s", slug)

    if not dry_run and new_hashes:
        save_state(state_path, seen | new_hashes)
    return res


def parse_compiled_facts(page_md: str) -> list[str]:
    """Return compiled-truth fact lines (above the timeline marker).

    Includes bullet lines ('- ...') and non-empty prose lines, excluding the
    H1 title, frontmatter, and anything tagged [source:honcho].
    """
    above = page_md.split(_TIMELINE_MARKER, 1)[0]
    facts: list[str] = []
    in_frontmatter = False
    for raw in above.splitlines():
        line = raw.strip()
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or not line or line.startswith("#"):
            continue
        text = line[2:].strip() if line.startswith("- ") else line
        if text and not has_source(text, "honcho"):
            facts.append(text)
    return facts


def run_seed(honcho, gbrain, *, slug, state_path, dry_run):
    """Seed GBrain compiled-truth facts into the Honcho user peer."""
    seen = load_state(state_path)
    res = {"seeded": 0, "deduped": 0, "loop_skipped": 0, "write_failed": 0}
    new_hashes: set[str] = set()

    page = gbrain.get_page(slug)
    if page is None:
        return res

    for fact in parse_compiled_facts(page):
        # Defensive backstop: parse_compiled_facts already filters [source:honcho]
        # lines, so this guard is normally a no-op — it protects against direct
        # callers that bypass parse_compiled_facts.
        if has_source(fact, "honcho"):
            res["loop_skipped"] += 1
            continue
        h = fact_hash(fact)
        if h in seen or h in new_hashes:
            res["deduped"] += 1
            continue
        if not dry_run:
            if not honcho.write_conclusion(tag_fact(fact, "gbrain"), peer="user"):
                res["write_failed"] += 1
                continue  # transient failure — don't record hash, retry next run
        new_hashes.add(h)
        res["seeded"] += 1

    if not dry_run and new_hashes:
        save_state(state_path, seen | new_hashes)
    return res


def _today() -> str:
    return _dt.date.today().isoformat()


def _state_dir() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "state"


def _load_bridge_config() -> dict:
    try:
        from plugins.memory.honcho.client import HonchoClientConfig
        cfg = HonchoClientConfig.from_global_config()
        raw = cfg.raw or {}
        return raw.get("bridge", {}) or {}
    except Exception as e:
        logger.warning("Could not load bridge config: %s", e)
        return {}


def _load_capture_config() -> dict:
    """Return the honcho.json `capture` config block, {} on any error."""
    try:
        from plugins.memory.honcho.client import HonchoClientConfig
        return (HonchoClientConfig.from_global_config().raw or {}).get("capture", {}) or {}
    except Exception:
        return {}


def run_bridge(dry_run: bool = False) -> dict:
    """Run a full bidirectional reconciliation cycle. Best-effort, idempotent."""
    bcfg = _load_bridge_config()
    if not bcfg.get("enabled"):
        return {"status": "disabled", "dry_run": dry_run}

    slug = bcfg.get("diegoPageSlug", "hindsight/diego")
    queries = bcfg.get("dialecticQueries", []) or []
    sdir = _state_dir()
    out: dict = {"status": "ok", "dry_run": dry_run, "export": {}, "seed": {}}

    manager = build_manager()
    if manager is None:
        return {"status": "honcho-unavailable", "dry_run": dry_run}
    honcho = HonchoAdapter(manager)
    gbrain = GBrainAdapter()

    if bcfg.get("export", {}).get("enabled"):
        out["export"] = run_export(
            honcho, gbrain, slug=slug, date=_today(),
            dialectic_queries=queries,
            state_path=sdir / "honcho_bridge_export.json", dry_run=dry_run,
        )
    if bcfg.get("seed", {}).get("enabled"):
        out["seed"] = run_seed(
            honcho, gbrain, slug=slug,
            state_path=sdir / "honcho_bridge_seed.json", dry_run=dry_run,
        )
    logger.info("Honcho bridge: %s", out)
    return out
