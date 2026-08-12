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
import re
import subprocess
from pathlib import Path
from typing import Iterable

from hermes_cli._subprocess_compat import run_text_capture

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
_SYNTHESIS_START = "<!-- honcho-synthesis:start -->"
_SYNTHESIS_END = "<!-- honcho-synthesis:end -->"


def merge_dialectic_synthesis(page_md: str, lines: list[str]) -> str:
    """Replace the single Honcho dialectic-synthesis block with the latest lines.

    Dialectic answers are non-deterministic — their content hash differs every
    run, so they can never be deduped and would grow the timeline unbounded if
    appended. Instead they live in ONE block (delimited by _SYNTHESIS_START /
    _SYNTHESIS_END) in the compiled-truth area above the timeline marker. Each
    run fully REPLACES the block, so it stays bounded to `len(lines)` entries.

    An empty `lines` removes the block. The transform is idempotent: re-running
    with identical lines returns an identical page.
    """
    if lines:
        block = "\n".join([_SYNTHESIS_START]
                          + [f"- {ln.strip()}" for ln in lines if ln.strip()]
                          + [_SYNTHESIS_END])
    else:
        block = ""

    if _SYNTHESIS_START in page_md and _SYNTHESIS_END in page_md:
        start = page_md.index(_SYNTHESIS_START)
        end = page_md.index(_SYNTHESIS_END) + len(_SYNTHESIS_END)
        if block:
            return page_md[:start] + block + page_md[end:]
        # Removal: drop the block and collapse the surrounding blank lines.
        return (page_md[:start].rstrip() + "\n\n" + page_md[end:].lstrip()).rstrip() + "\n"

    if not block:
        return page_md
    if _TIMELINE_MARKER in page_md:
        above, _, below = page_md.partition(_TIMELINE_MARKER)
        return above.rstrip() + "\n\n" + block + "\n\n" + _TIMELINE_MARKER + below
    return page_md.rstrip() + "\n\n" + block + "\n"


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


# Per-call budget for the `gbrain` CLI. This is a FLOOR, not a bound:
# run_text_capture's timeout path adds a synchronous tree-kill plus, if that
# kill misses, a dead pipe drain and an uncapped close/wait — see its docstring.
# A wedged `gbrain` can therefore cost this plus tens of seconds, so don't size
# this as though 15 were the ceiling.
_GBRAIN_TIMEOUT = 15


class GBrainAdapter:
    """Thin wrapper over the `gbrain` CLI. All methods are best-effort."""

    def get_page(self, slug: str) -> str | None:
        """Return the page markdown, or None if the page/CLI is unavailable.

        Note: an existing-but-empty page yields "" (falsy), not None.
        """
        try:
            r = run_text_capture(["gbrain", "get", slug], timeout=_GBRAIN_TIMEOUT)
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("gbrain get %s failed: %s", slug, e)
            return None

    def put_page(self, slug: str, markdown: str) -> bool:
        try:
            # Pass content via --content (argv), NOT piped stdin: `gbrain put`
            # reads stdin by opening '/dev/stdin', which does not exist on
            # Windows and fails with ENOENT. --content is cross-platform.
            r = run_text_capture(
                ["gbrain", "put", slug, "--content", markdown],
                timeout=_GBRAIN_TIMEOUT,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("gbrain put %s failed: %s", slug, e)
            return False

    def add_timeline(self, slug: str, date: str, text: str) -> bool:
        try:
            r = run_text_capture(
                ["gbrain", "timeline-add", slug, date, text],
                timeout=_GBRAIN_TIMEOUT,
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


def _compiled_state_path(state_path: Path) -> Path:
    """Sibling state file tracking which facts reached the compiled-truth block.

    Timeline-add and the single compiled-truth put_page have independent failure
    modes: the timeline append can succeed while the compiled merge fails. They
    therefore need separate seen-sets so a failed compiled put_page can retry
    next run WITHOUT re-adding (duplicating) the timeline entry that already
    landed. The original `state_path` keeps tracking timeline-add for backward
    compatibility (existing hashes are interpreted as timeline-seen).
    """
    return state_path.with_name(state_path.stem + "_compiled" + state_path.suffix)


def run_export(honcho, gbrain, *, slug, date, dialectic_queries, state_path, dry_run):
    """Export Honcho conclusions to GBrain (timeline + compiled) and MemPalace.

    Timeline-seen and compiled-seen hashes are tracked separately so a failed
    compiled-truth put_page retries on the next run without re-adding the
    already-written timeline entries (gbrain timeline-add appends — it would
    duplicate). Compiled hashes are persisted ONLY after a successful put_page.
    """
    compiled_state_path = _compiled_state_path(state_path)
    timeline_seen = load_state(state_path)
    compiled_seen = load_state(compiled_state_path)
    res = {"exported": 0, "deduped": 0, "loop_skipped": 0, "write_failed": 0,
           "synthesized": 0}
    new_timeline_hashes: set[str] = set()
    new_compiled_hashes: set[str] = set()
    compiled_facts: list[str] = []
    synthesis_lines: list[str] = []

    def _consider(text: str):
        # Peer-card facts are high-confidence compiled truth: deduped by hash,
        # written to both the timeline and the compiled-truth block.
        if has_source(text, "gbrain"):
            res["loop_skipped"] += 1
            return
        h = fact_hash(text)
        need_timeline = h not in timeline_seen and h not in new_timeline_hashes
        need_compiled = h not in compiled_seen and h not in new_compiled_hashes
        if not need_timeline and not need_compiled:
            res["deduped"] += 1
            return
        tagged = tag_fact(text, "honcho")
        if need_timeline:
            if not dry_run:
                if not gbrain.add_timeline(slug, date, tagged):
                    res["write_failed"] += 1
                    return  # transient failure — don't record hash, retry next run
            new_timeline_hashes.add(h)
        if need_compiled:
            compiled_facts.append(tagged)
            new_compiled_hashes.add(h)
        res["exported"] += 1

    for fact in honcho.read_user_facts():
        if fact and fact.strip():
            _consider(fact)
    # Dialectic answers are non-deterministic — their hash differs every run, so
    # they can never be deduped and would grow the timeline (and MemPalace)
    # unbounded if appended. Instead they fully REPLACE a single synthesis block
    # on the page each run, keeping the "what changed recently" signal current
    # without churn.
    for q in dialectic_queries:
        answer = honcho.run_dialectic(q)
        if not (answer and answer.strip()):
            continue
        if has_source(answer, "gbrain"):
            res["loop_skipped"] += 1
            continue
        synthesis_lines.append(tag_fact(answer, "honcho"))
        res["synthesized"] += 1

    # The compiled-truth fold-in either fully succeeds (persist its hashes) or
    # fails/can't-read-page (leave compiled hashes unpersisted so it retries).
    # The synthesis block is merged into the same put_page (replace, no hashes).
    compiled_ok = True
    if (compiled_facts or synthesis_lines) and not dry_run:
        page = gbrain.get_page(slug)
        if page is None:
            compiled_ok = False  # couldn't read page — retry the merge next run
        else:
            new_page = merge_compiled_truth(page, compiled_facts) if compiled_facts else page
            if synthesis_lines:  # empty answer this run leaves any prior block intact
                new_page = merge_dialectic_synthesis(new_page, synthesis_lines)
            if new_page != page and not gbrain.put_page(slug, new_page):
                compiled_ok = False
                logger.warning("Honcho bridge: compiled-truth put_page failed for %s", slug)

    if not dry_run:
        if new_timeline_hashes:
            save_state(state_path, timeline_seen | new_timeline_hashes)
        if compiled_ok and new_compiled_hashes:
            save_state(compiled_state_path, compiled_seen | new_compiled_hashes)
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
        if line.startswith("<!--"):
            continue  # skip block markers (e.g. the honcho-synthesis delimiters)
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
