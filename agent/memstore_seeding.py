"""Provider-agnostic memstore seeding and "dreams" consolidation.

This module bootstraps any active memory provider with knowledge *before*
the first conversation, and runs an offline consolidation pass ("dreams")
that distils, de-conflicts, and synthesises what the agent already knows.

Two halves:

1. **Seeding** — turn human-authored sources into durable memories.
   * ``parse_persona_doc`` chunks an "about the user/project" markdown file
     into facts (headings become categories, bullets/sentences become facts).
   * ``parse_transcript`` mines prior conversation logs for memory-worthy
     statements (preferences, identity, decisions, explicit "remember ..."
     cues).
   * ``MemstoreSeeder`` writes the resulting :class:`FactCorpus` through the
     *provider-agnostic* ``on_memory_write`` contract that every external
     memory provider implements (see ``agent/memory_provider.py``). That
     means the same seed runs against Honcho, Supermemory, Holographic, etc.

2. **Dreams** — :class:`DreamConsolidator` reviews a corpus the way sleep
   consolidates memory: it de-duplicates near-identical facts, flags
   contradictions, decays/prunes low-trust or stale facts, and synthesises
   higher-level ``insight`` facts from clusters of related observations.
   The refined corpus is flushed back through the same seeder, so dreams
   are likewise provider-agnostic.

The engine deliberately depends only on the standard library and the
``on_memory_write`` sink, so it is fully unit-testable without booting a
full agent or any external service.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

logger = logging.getLogger(__name__)

# Categories carry semantic intent. ``user_pref`` and user-identity facts
# default to the USER memory target; agent ``identity`` and everything else
# land in the agent's own memory.
CATEGORY_USER = "user_pref"
CATEGORY_PROJECT = "project"
CATEGORY_TOOL = "tool"
CATEGORY_GENERAL = "general"
CATEGORY_INSIGHT = "insight"
CATEGORY_IDENTITY = "identity"    # agent persona/identity (SOUL.md / IDENTITY.md)
CATEGORY_BOOTSTRAP = "bootstrap"  # first-run / setup steps (BOOTSTRAP.md)

_USER_TARGET_CATEGORIES = {CATEGORY_USER}

# Trust priors for extracted facts (0.0 – 1.0).
_TRUST_EXPLICIT = 0.75   # "remember that ...", "note that ..."
_TRUST_IDENTITY = 0.7    # "my name is ...", "call me ..."
_TRUST_PREFERENCE = 0.6  # "I prefer/always/never ..."
_TRUST_DECISION = 0.6    # "we decided ...", "the project uses ..."
_TRUST_DEFAULT = 0.5


# ---------------------------------------------------------------------------
# Fact model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedFact:
    """A single durable memory destined for a memory provider.

    ``target`` maps onto the ``on_memory_write`` target — ``"user"`` for the
    USER profile, ``"memory"`` for the agent's notes. When not given it is
    derived from ``category``.
    """

    content: str
    category: str = CATEGORY_GENERAL
    target: str = ""
    tags: tuple[str, ...] = ()
    trust: float = _TRUST_DEFAULT
    source: str = ""
    entities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # ``frozen=True`` requires object.__setattr__ for normalisation.
        content = (self.content or "").strip()
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "trust", _clamp(self.trust))
        if not self.target:
            target = "user" if self.category in _USER_TARGET_CATEGORIES else "memory"
            object.__setattr__(self, "target", target)
        object.__setattr__(self, "tags", tuple(dict.fromkeys(t for t in self.tags if t)))
        object.__setattr__(self, "entities", tuple(dict.fromkeys(e for e in self.entities if e)))

    @property
    def key(self) -> str:
        """Normalised dedup key — case/whitespace/punctuation insensitive."""
        return _normalize(self.content)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "category": self.category,
            "target": self.target,
            "tags": list(self.tags),
            "trust": round(self.trust, 4),
            "source": self.source,
            "entities": list(self.entities),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeedFact":
        return cls(
            content=data.get("content", ""),
            category=data.get("category", CATEGORY_GENERAL),
            target=data.get("target", ""),
            tags=tuple(data.get("tags", ()) or ()),
            trust=float(data.get("trust", _TRUST_DEFAULT)),
            source=data.get("source", ""),
            entities=tuple(data.get("entities", ()) or ()),
        )


class FactCorpus:
    """Ordered, de-duplicated collection of :class:`SeedFact`.

    Adding a fact whose normalised content already exists *merges* it into
    the existing entry (union of tags/entities, max trust, first-seen source)
    rather than creating a duplicate.
    """

    def __init__(self, facts: Iterable[SeedFact] | None = None) -> None:
        self._by_key: dict[str, SeedFact] = {}
        if facts:
            self.extend(facts)

    def add(self, fact: SeedFact) -> bool:
        """Add a fact. Returns True if new, False if merged into an existing one."""
        if not fact.content:
            return False
        existing = self._by_key.get(fact.key)
        if existing is None:
            self._by_key[fact.key] = fact
            return True
        self._by_key[fact.key] = _merge_facts(existing, fact)
        return False

    def extend(self, facts: Iterable[SeedFact]) -> int:
        """Add many facts. Returns the count of genuinely new facts."""
        added = 0
        for fact in facts:
            if self.add(fact):
                added += 1
        return added

    def remove(self, key: str) -> bool:
        return self._by_key.pop(key, None) is not None

    def replace(self, fact: SeedFact) -> None:
        self._by_key[fact.key] = fact

    def __iter__(self) -> Iterator[SeedFact]:
        return iter(self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)

    def __bool__(self) -> bool:
        return bool(self._by_key)

    def facts(self) -> list[SeedFact]:
        return list(self._by_key.values())

    # -- Serialisation -------------------------------------------------------

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(f.to_dict(), ensure_ascii=False) for f in self)

    @classmethod
    def from_jsonl(cls, text: str) -> "FactCorpus":
        corpus = cls()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                corpus.add(SeedFact.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug("Skipping malformed corpus line: %s", exc)
        return corpus


def _merge_facts(a: SeedFact, b: SeedFact) -> SeedFact:
    """Merge two facts with the same normalised content. Keeps the longer
    surface form, the max trust, and the union of tags/entities/sources."""
    content = a.content if len(a.content) >= len(b.content) else b.content
    sources = [s for s in (a.source, b.source) if s]
    return SeedFact(
        content=content,
        category=a.category if a.category != CATEGORY_GENERAL else b.category,
        target=a.target,
        tags=a.tags + b.tags,
        trust=max(a.trust, b.trust),
        source=" + ".join(dict.fromkeys(sources)),
        entities=a.entities + b.entities,
    )


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_RE_WS = re.compile(r"\s+")
_RE_NONWORD = re.compile(r"[^\w\s]")
_RE_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
_RE_CAPITALIZED = re.compile(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*)\b")
_RE_QUOTED = re.compile(r"[\"'`]([^\"'`]{2,40})[\"'`]")
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")

# Stopwords that should never be treated as entities.
_ENTITY_STOPWORDS = {
    "I", "The", "A", "An", "This", "That", "These", "Those", "It", "We",
    "You", "They", "My", "Our", "Your", "Their", "He", "She", "If", "When",
    "But", "And", "Or", "So", "For", "To", "In", "On", "At", "Of",
}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return _TRUST_DEFAULT


def _normalize(text: str) -> str:
    """Lower-case, strip punctuation, collapse whitespace — for dedup keys."""
    text = _RE_NONWORD.sub(" ", text.lower())
    return _RE_WS.sub(" ", text).strip()


def _tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b) if (a or b) else 0.0


def extract_entities(text: str, limit: int = 6) -> tuple[str, ...]:
    """Lightweight entity extraction — capitalised phrases and quoted terms.

    Mirrors the heuristic used by the holographic store so seeded facts cluster
    on the same entity vocabulary the agent uses at recall time.
    """
    seen: dict[str, None] = {}
    for match in _RE_CAPITALIZED.finditer(text):
        phrase = match.group(1).strip()
        first = phrase.split()[0]
        if first in _ENTITY_STOPWORDS and " " not in phrase:
            continue
        seen.setdefault(phrase, None)
    for match in _RE_QUOTED.finditer(text):
        term = match.group(1).strip()
        if term:
            seen.setdefault(term, None)
    return tuple(list(seen.keys())[:limit])


# ---------------------------------------------------------------------------
# Source parser: persona / "about" documents
# ---------------------------------------------------------------------------

# Heading hints routing persona-doc sections to a category. Agent-identity
# hints are checked first so "About the agent" doesn't get mistaken for the
# user's own profile.
_IDENTITY_HEADING_HINTS = (
    "about you", "about the agent", "about the assistant", "agent identity",
    "your identity", "who you are", "persona", "soul", "personality", "voice",
    "assistant style",
)
_USER_HEADING_HINTS = (
    "user", "about me", "about the user", "profile", "preference",
    "who i am", "bio", "communication", "my style",
)
_PROJECT_HEADING_HINTS = ("project", "codebase", "repo", "stack", "architecture", "build", "operating")
_TOOL_HEADING_HINTS = ("tool", "environment", "workflow", "command")
# Checked before tools so "Setup" / "Getting started" / "Onboarding" route to
# bootstrap rather than being caught by the tool "environment" hint. "setup"
# lives here (not in tool hints) so a "## Setup" section reaches BOOTSTRAP.md,
# whose section is literally named "Setup".
_BOOTSTRAP_HEADING_HINTS = (
    "bootstrap", "setup", "first run", "first-run", "getting started",
    "getting-started", "onboarding", "installation", "how to start",
    "prerequisite", "quickstart", "quick start",
)


def _category_for_heading(heading: str) -> str:
    low = heading.lower()
    if any(h in low for h in _IDENTITY_HEADING_HINTS):
        return CATEGORY_IDENTITY
    if any(h in low for h in _USER_HEADING_HINTS):
        return CATEGORY_USER
    if any(h in low for h in _BOOTSTRAP_HEADING_HINTS):
        return CATEGORY_BOOTSTRAP
    if any(h in low for h in _PROJECT_HEADING_HINTS):
        return CATEGORY_PROJECT
    if any(h in low for h in _TOOL_HEADING_HINTS):
        return CATEGORY_TOOL
    return CATEGORY_GENERAL


def parse_persona_doc(
    text: str,
    *,
    default_category: str = CATEGORY_USER,
    source: str = "persona",
    min_len: int = 8,
    max_len: int = 400,
) -> list[SeedFact]:
    """Chunk an "about the user/project" markdown doc into facts.

    Markdown headings set the category for the facts beneath them; bullet
    points become one fact each; paragraphs are split into sentences. A fact
    keeps the nearest heading as a tag so related memories stay linked.
    """
    facts: list[SeedFact] = []
    current_category = default_category
    current_heading = ""
    line_no = 0
    paragraph: list[str] = []
    paragraph_start_line = 0

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        blob = " ".join(paragraph).strip()
        paragraph = []
        for sentence in _split_sentences(blob):
            _emit(sentence, override_line_no=paragraph_start_line)

    def _emit(chunk: str, override_line_no: int | None = None) -> None:
        chunk = chunk.strip().lstrip("-*+ ").strip()
        if len(chunk) < min_len:
            return
        chunk = chunk[:max_len].strip()
        tags = ("persona",)
        if current_heading:
            tags = tags + (_slug(current_heading),)
        src_line = override_line_no if override_line_no is not None else line_no
        facts.append(
            SeedFact(
                content=chunk,
                category=current_category,
                tags=tags,
                trust=_TRUST_PREFERENCE if current_category == CATEGORY_USER else _TRUST_DEFAULT,
                source=f"{source}:L{src_line}",
                entities=extract_entities(chunk),
            )
        )

    for raw in text.splitlines():
        line_no += 1
        line = raw.rstrip()
        heading_match = _RE_HEADING.match(line)
        if heading_match:
            flush_paragraph()
            current_heading = heading_match.group(2).strip()
            current_category = _category_for_heading(current_heading)
            continue
        bullet_match = _RE_BULLET.match(line)
        if bullet_match:
            flush_paragraph()
            _emit(bullet_match.group(1))
            continue
        if not line.strip():
            flush_paragraph()
            continue
        if not paragraph:
            paragraph_start_line = line_no
        paragraph.append(line.strip())

    flush_paragraph()
    return facts


def _split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [s.strip() for s in _RE_SENTENCE.split(text) if s.strip()]


def _slug(text: str, max_len: int = 24) -> str:
    slug = re.sub(r"[^\w]+", "-", text.lower()).strip("-")
    return slug[:max_len]


# ---------------------------------------------------------------------------
# Source parser: conversation transcripts
# ---------------------------------------------------------------------------

# (pattern, category, trust) — applied to user messages.
_EXTRACTION_RULES: list[tuple[re.Pattern[str], str, float]] = [
    # Explicit memory cues — strongest signal.
    (re.compile(r"\b(?:please\s+)?remember(?:\s+that)?\s+(.+)", re.I), CATEGORY_GENERAL, _TRUST_EXPLICIT),
    (re.compile(r"\bnote\s+(?:that|down)\s+(.+)", re.I), CATEGORY_GENERAL, _TRUST_EXPLICIT),
    (re.compile(r"\bfor\s+(?:future\s+)?reference[,:]?\s+(.+)", re.I), CATEGORY_GENERAL, _TRUST_EXPLICIT),
    (re.compile(r"\bdon'?t\s+forget\s+(?:that\s+)?(.+)", re.I), CATEGORY_GENERAL, _TRUST_EXPLICIT),
    # Identity.
    (re.compile(r"\bmy\s+name\s+is\s+(.+)", re.I), CATEGORY_USER, _TRUST_IDENTITY),
    (re.compile(r"\b(?:please\s+)?call\s+me\s+(.+)", re.I), CATEGORY_USER, _TRUST_IDENTITY),
    (re.compile(r"\bI'?m\s+(?:a|an|the)\s+(.+)", re.I), CATEGORY_USER, _TRUST_IDENTITY),
    (re.compile(r"\bI\s+work\s+(?:at|on|as)\s+(.+)", re.I), CATEGORY_USER, _TRUST_IDENTITY),
    # Preferences.
    (re.compile(r"\bI\s+(?:prefer|like|love|favou?r|enjoy)\s+(.+)", re.I), CATEGORY_USER, _TRUST_PREFERENCE),
    (re.compile(r"\bI\s+(?:hate|dislike|avoid|can'?t\s+stand)\s+(.+)", re.I), CATEGORY_USER, _TRUST_PREFERENCE),
    (re.compile(r"\bI\s+(?:always|never|usually|tend\s+to)\s+(.+)", re.I), CATEGORY_USER, _TRUST_PREFERENCE),
    (re.compile(r"\bmy\s+(?:favou?rite|preferred|default|go-to)\s+(.+)", re.I), CATEGORY_USER, _TRUST_PREFERENCE),
    (re.compile(r"\bI\s+(?:use|run|work\s+with)\s+(.+)", re.I), CATEGORY_TOOL, _TRUST_PREFERENCE),
    # Project / decisions.
    (re.compile(r"\bwe\s+(?:decided|agreed|chose)\s+(?:to\s+)?(.+)", re.I), CATEGORY_PROJECT, _TRUST_DECISION),
    (re.compile(r"\bthe\s+project\s+(?:uses|needs|requires|targets)\s+(.+)", re.I), CATEGORY_PROJECT, _TRUST_DECISION),
    (re.compile(r"\bthis\s+(?:repo|codebase|app)\s+(?:uses|is|has)\s+(.+)", re.I), CATEGORY_PROJECT, _TRUST_DECISION),
    (re.compile(r"\blet'?s\s+(?:always|standardi[sz]e\s+on|use)\s+(.+)", re.I), CATEGORY_PROJECT, _TRUST_DECISION),
]

_TRIVIAL = {
    "ok", "okay", "thanks", "thank you", "yes", "no", "sure", "great",
    "cool", "nice", "got it", "k", "yep", "nope", "hi", "hello", "hey",
}


def parse_transcript(
    messages: Sequence[dict[str, Any]],
    *,
    source: str = "transcript",
    user_roles: Sequence[str] = ("user", "human"),
    min_len: int = 12,
    max_len: int = 400,
) -> list[SeedFact]:
    """Mine memory-worthy facts from a conversation transcript.

    Scans user turns for preference / identity / decision / explicit-memory
    patterns. Each match becomes a fact carrying provenance back to the
    originating message index.
    """
    facts: list[SeedFact] = []
    user_roles_l = {r.lower() for r in user_roles}

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "")).lower()
        if role not in user_roles_l:
            continue
        content = _message_text(msg.get("content"))
        if not content:
            continue
        for sentence in _split_sentences(content):
            stripped = sentence.strip()
            if len(stripped) < min_len or _normalize(stripped) in _TRIVIAL:
                continue
            for pattern, category, trust in _EXTRACTION_RULES:
                if pattern.search(stripped):
                    chunk = stripped[:max_len].strip()
                    facts.append(
                        SeedFact(
                            content=chunk,
                            category=category,
                            tags=("transcript",),
                            trust=trust,
                            source=f"{source}:msg{idx}",
                            entities=extract_entities(chunk),
                        )
                    )
                    break  # one fact per sentence — first (strongest) rule wins
    return facts


def _message_text(content: Any) -> str:
    """Normalise a message ``content`` into plain text.

    Handles plain strings and the list-of-parts shape used by multimodal
    transcripts (``[{"type": "text", "text": "..."}, ...]``).
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(p for p in parts if p).strip()
    return ""


def load_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Load a transcript from a JSON file into a list of ``{role, content}``.

    Accepts a bare list of messages, a ``{"messages": [...]}`` wrapper (the
    common chat-completions / hermes session shape), or a JSONL file with one
    message per line.
    """
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    text = raw.strip()
    if not text:
        return []

    # Try whole-file JSON first.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to JSONL.
        out: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except json.JSONDecodeError:
                continue
        return out

    if isinstance(data, dict):
        for key in ("messages", "conversation", "history", "turns"):
            value = data.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
        return []
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return []


# ---------------------------------------------------------------------------
# Provider-agnostic writer
# ---------------------------------------------------------------------------


@dataclass
class SeedReport:
    """Outcome of a seeding run."""

    written: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = False
    by_target: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verb = "Would write" if self.dry_run else "Wrote"
        targets = ", ".join(f"{k}={v}" for k, v in sorted(self.by_target.items())) or "—"
        line = f"{verb} {self.written} fact(s) [{targets}]"
        if self.skipped:
            line += f", skipped {self.skipped}"
        if self.failed:
            line += f", {self.failed} failed"
        return line


class MemstoreSeeder:
    """Write a :class:`FactCorpus` to any memory provider.

    ``sink`` is anything exposing ``on_memory_write(action, target, content,
    metadata=None)`` — a :class:`~agent.memory_manager.MemoryManager` (which
    fans out to the active external provider) or a single provider instance.
    The seeder probes the sink's signature so it works against both the new
    4-arg metadata contract and the legacy 3-arg form.
    """

    def __init__(self, sink: Any) -> None:
        if not hasattr(sink, "on_memory_write"):
            raise TypeError(
                "seeder sink must expose on_memory_write(action, target, content, metadata=None)"
            )
        self._sink = sink
        self._accepts_metadata = _accepts_metadata(sink.on_memory_write)

    def seed(self, corpus: FactCorpus | Iterable[SeedFact], *, dry_run: bool = False) -> SeedReport:
        report = SeedReport(dry_run=dry_run)
        facts = corpus if isinstance(corpus, FactCorpus) else FactCorpus(corpus)
        for fact in facts:
            if not fact.content:
                report.skipped += 1
                continue
            if dry_run:
                report.written += 1
                report.by_target[fact.target] = report.by_target.get(fact.target, 0) + 1
                continue
            try:
                self._write(fact)
                report.written += 1
                report.by_target[fact.target] = report.by_target.get(fact.target, 0) + 1
            except Exception as exc:  # provider failures must not abort the run
                report.failed += 1
                report.errors.append(f"{fact.source or fact.content[:40]}: {exc}")
                logger.warning("Seed write failed for %r: %s", fact.content[:60], exc)
        return report

    def _write(self, fact: SeedFact) -> None:
        metadata = {
            "write_origin": "memstore_seeding",
            "category": fact.category,
            "tags": list(fact.tags),
            "trust": fact.trust,
            "source": fact.source,
            "entities": list(fact.entities),
        }
        if self._accepts_metadata:
            self._sink.on_memory_write("add", fact.target, fact.content, metadata=metadata)
        else:
            self._sink.on_memory_write("add", fact.target, fact.content)


def _accepts_metadata(fn: Any) -> bool:
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    params = list(sig.parameters.values())
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params):
        return True
    return "metadata" in sig.parameters


# ---------------------------------------------------------------------------
# Dreams — offline consolidation
# ---------------------------------------------------------------------------

# Antonym pairs used for lightweight contradiction detection.
_ANTONYMS = [
    ("prefer", "avoid"), ("like", "hate"), ("like", "dislike"),
    ("love", "hate"), ("always", "never"), ("use", "avoid"),
    ("enable", "disable"), ("want", "don't want"), ("on", "off"),
]
_RE_NEGATION = re.compile(r"\b(?:no|not|never|don'?t|doesn'?t|avoid|without)\b", re.I)


@dataclass
class DreamAction:
    kind: str            # "dedupe" | "contradiction" | "decay" | "prune" | "insight"
    detail: str
    facts: tuple[str, ...] = ()


@dataclass
class DreamReport:
    actions: list[DreamAction] = field(default_factory=list)
    kept: int = 0
    pruned: int = 0
    merged: int = 0
    insights: int = 0
    contradictions: int = 0

    def add(self, action: DreamAction) -> None:
        self.actions.append(action)

    def summary(self) -> str:
        return (
            f"kept {self.kept}, merged {self.merged}, pruned {self.pruned}, "
            f"{self.contradictions} contradiction(s), {self.insights} new insight(s)"
        )


class DreamConsolidator:
    """Consolidate a :class:`FactCorpus` the way sleep consolidates memory.

    The pass is deterministic and provider-agnostic — it operates on the
    corpus only, returning a refined corpus plus a :class:`DreamReport`.
    Pipeline (each step is independently configurable):

    1. **dedupe** — merge near-identical facts (Jaccard ≥ ``dedupe_threshold``).
    2. **contradict** — flag fact pairs about the same entity that assert
       opposing things; demote the lower-trust / older one.
    3. **decay** — multiply trust by ``decay_factor`` to model forgetting.
    4. **prune** — drop facts whose trust falls below ``min_trust``.
    5. **synthesise** — emit ``insight`` facts summarising clusters of
       ``min_cluster``+ related observations (shared entity or category).
    """

    def __init__(
        self,
        *,
        dedupe_threshold: float = 0.7,
        decay_factor: float = 1.0,
        min_trust: float = 0.25,
        min_cluster: int = 3,
        synthesize: bool = True,
    ) -> None:
        self.dedupe_threshold = dedupe_threshold
        self.decay_factor = decay_factor
        self.min_trust = min_trust
        self.min_cluster = min_cluster
        self.synthesize = synthesize

    def consolidate(self, corpus: FactCorpus | Iterable[SeedFact]) -> tuple[FactCorpus, DreamReport]:
        facts = list(corpus if isinstance(corpus, FactCorpus) else FactCorpus(corpus))
        report = DreamReport()

        facts = self._dedupe(facts, report)
        self._detect_contradictions(facts, report)
        facts = self._decay_and_prune(facts, report)
        insights = self._synthesize(facts, report) if self.synthesize else []

        result = FactCorpus(facts)
        result.extend(insights)
        report.kept = len(facts)
        return result, report

    # -- 1. dedupe -----------------------------------------------------------

    def _dedupe(self, facts: list[SeedFact], report: DreamReport) -> list[SeedFact]:
        kept: list[SeedFact] = []
        kept_tokens: list[set[str]] = []
        for fact in facts:
            toks = _tokens(fact.content)
            merged_into = None
            for i, other_toks in enumerate(kept_tokens):
                if _jaccard(toks, other_toks) >= self.dedupe_threshold:
                    merged_into = i
                    break
            if merged_into is None:
                kept.append(fact)
                kept_tokens.append(toks)
            else:
                kept[merged_into] = _merge_facts(kept[merged_into], fact)
                kept_tokens[merged_into] = _tokens(kept[merged_into].content)
                report.merged += 1
                report.add(DreamAction(
                    "dedupe", f"merged near-duplicate into {kept[merged_into].content[:50]!r}",
                    (fact.content[:50], kept[merged_into].content[:50]),
                ))
        return kept

    # -- 2. contradictions ---------------------------------------------------

    def _detect_contradictions(self, facts: list[SeedFact], report: DreamReport) -> None:
        by_entity: dict[str, list[int]] = defaultdict(list)
        for i, fact in enumerate(facts):
            for entity in fact.entities:
                by_entity[entity.lower()].append(i)

        seen_pairs: set[tuple[int, int]] = set()
        for indices in by_entity.values():
            for a in indices:
                for b in indices:
                    if a >= b:
                        continue
                    pair = (a, b)
                    if pair in seen_pairs:
                        continue
                    if self._contradicts(facts[a].content, facts[b].content):
                        seen_pairs.add(pair)
                        report.contradictions += 1
                        # Demote the weaker fact's trust so prune can act on it.
                        loser = a if facts[a].trust <= facts[b].trust else b
                        facts[loser] = replace(
                            facts[loser], trust=_clamp(facts[loser].trust - 0.15)
                        )
                        report.add(DreamAction(
                            "contradiction",
                            f"{facts[a].content[:40]!r} ⊥ {facts[b].content[:40]!r}",
                            (facts[a].content[:40], facts[b].content[:40]),
                        ))

    @staticmethod
    def _contradicts(a: str, b: str) -> bool:
        # Only ever called on facts that already share an entity, so even a
        # small amount of additional lexical overlap is meaningful.
        la, lb = a.lower(), b.lower()
        neg_a = bool(_RE_NEGATION.search(la))
        neg_b = bool(_RE_NEGATION.search(lb))
        shared = _jaccard(_tokens(a), _tokens(b))
        # Opposite negation polarity over a shared subject is a likely conflict.
        if shared >= 0.15 and neg_a != neg_b:
            return True
        # Word-boundary matching so substring antonyms don't false-positive
        # (e.g. "like" inside "dislike", "enable" inside "disable").
        for x, y in _ANTONYMS:
            px = rf"\b{re.escape(x)}\b"
            py = rf"\b{re.escape(y)}\b"
            has_xa, has_yb = re.search(px, la), re.search(py, lb)
            has_ya, has_xb = re.search(py, la), re.search(px, lb)
            if (has_xa and has_yb) or (has_ya and has_xb):
                if shared >= 0.15:
                    return True
        return False

    # -- 3. decay + prune ----------------------------------------------------

    def _decay_and_prune(self, facts: list[SeedFact], report: DreamReport) -> list[SeedFact]:
        kept: list[SeedFact] = []
        for fact in facts:
            trust = fact.trust
            if self.decay_factor != 1.0:
                trust = _clamp(trust * self.decay_factor)
                if trust != fact.trust:
                    fact = replace(fact, trust=trust)
                    report.add(DreamAction("decay", f"trust→{trust:.2f} for {fact.content[:40]!r}"))
            if trust < self.min_trust:
                report.pruned += 1
                report.add(DreamAction("prune", f"dropped low-trust {fact.content[:50]!r}"))
                continue
            kept.append(fact)
        return kept

    # -- 4. synthesis --------------------------------------------------------

    def _synthesize(self, facts: list[SeedFact], report: DreamReport) -> list[SeedFact]:
        clusters: dict[str, list[SeedFact]] = defaultdict(list)
        for fact in facts:
            if fact.category == CATEGORY_INSIGHT:
                continue
            for entity in fact.entities:
                clusters[f"entity:{entity.lower()}"].append(fact)

        insights: list[SeedFact] = []
        used_keys: set[str] = set()
        for cluster_key, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
            if len(members) < self.min_cluster:
                continue
            member_keys = frozenset(m.key for m in members)
            if member_keys & used_keys:
                continue  # avoid overlapping insights over the same facts
            used_keys |= member_keys
            entity = cluster_key.split(":", 1)[1]
            # Label with the member entity that matches this cluster (preserving
            # its original casing) — not just the first entity of the first
            # member, which may belong to a different cluster.
            label = entity
            for m in members:
                match = next((e for e in m.entities if e.lower() == entity), None)
                if match:
                    label = match
                    break
            avg_trust = sum(m.trust for m in members) / len(members)
            content = (
                f"{label}: recurring theme across {len(members)} related memories — "
                + "; ".join(_clip(m.content, 60) for m in members[:3])
            )
            insights.append(
                SeedFact(
                    content=content,
                    category=CATEGORY_INSIGHT,
                    target="memory",
                    tags=("dream", "insight", _slug(label)),
                    trust=_clamp(min(0.7, avg_trust + 0.1)),
                    source="dream:synthesis",
                    entities=(label,),
                )
            )
            report.insights += 1
            report.add(DreamAction("insight", f"synthesised insight about {label!r} from {len(members)} facts"))
        return insights


def _clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------


def build_corpus_from_sources(
    *,
    persona_paths: Sequence[str | Path] = (),
    transcript_paths: Sequence[str | Path] = (),
) -> FactCorpus:
    """Build a :class:`FactCorpus` from persona docs and transcript files."""
    corpus = FactCorpus()
    for path in persona_paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read persona doc %s: %s", path, exc)
            continue
        corpus.extend(parse_persona_doc(text, source=f"persona:{Path(path).name}"))
    for path in transcript_paths:
        try:
            messages = load_transcript(path)
        except OSError as exc:
            logger.warning("Could not read transcript %s: %s", path, exc)
            continue
        corpus.extend(parse_transcript(messages, source=f"transcript:{Path(path).name}"))
    return corpus


def seed_and_dream(
    sink: Any,
    corpus: FactCorpus,
    *,
    dream: bool = True,
    dry_run: bool = False,
    consolidator: DreamConsolidator | None = None,
) -> tuple[SeedReport, DreamReport | None]:
    """Optionally consolidate a corpus, then seed it into the provider.

    Returns the :class:`SeedReport` and (if dreaming) the :class:`DreamReport`.
    """
    dream_report: DreamReport | None = None
    if dream:
        consolidator = consolidator or DreamConsolidator()
        corpus, dream_report = consolidator.consolidate(corpus)
    seed_report = MemstoreSeeder(sink).seed(corpus, dry_run=dry_run)
    return seed_report, dream_report


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
