"""Canonical markdown memstore — the provider-agnostic file tree.

The most portable memory a fleet of agents can share is a directory of plain
markdown files. Any framework can read them, they diff cleanly in git, and a
human can edit them by hand. This module makes that file tree a first-class
memstore that the seeding/dreams engine (``agent.memstore_seeding``) can write.

Layout (all under ``HERMES_HOME``, profile-scoped):

    SOUL.md                     agent identity prose (read by Hermes at boot)
    memories/
      IDENTITY.md               structured agent persona/identity
      USER.md                   the user profile  (Hermes-native)
      AGENTS.md                 operating instructions  (kept out of the repo's
                                own AGENTS.md — this one is memstore-scoped)
      TOOLS.md                  tools & environment
      BOOTSTRAP.md              first-run / setup notes
      HEARTBEAT.md              periodic state
      MEMORY.md                 rolled-up agent notes + insights
      daily/
        2026-07-16.md           one digest per day, built from that day's
        2026-07-17.md           transcript — the base layer of the tree

Two layers, matching the "break transcripts down by day, then build on top"
model:

* **Daily digests** (``write_daily_digests``) are the base layer — each day's
  conversation distilled into a dated markdown file.
* **Roll-up** (``roll_up``) is the higher layer — it reads recent daily files,
  runs the dream consolidation, and folds the result into ``MEMORY.md`` and
  ``USER.md``.

Every write is idempotent: facts live inside ``<!-- hermes:seed ... -->``
managed blocks, so re-seeding merges (de-duplicating bullets) instead of
appending duplicates, and any hand-written content outside the blocks is
preserved untouched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from agent.memstore_seeding import (
    CATEGORY_GENERAL,
    CATEGORY_IDENTITY,
    CATEGORY_INSIGHT,
    CATEGORY_PROJECT,
    CATEGORY_TOOL,
    CATEGORY_USER,
    DreamConsolidator,
    FactCorpus,
    SeedFact,
    _normalize,
    parse_transcript,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical file / section routing
# ---------------------------------------------------------------------------

# category -> (filename, section heading). ``filename`` resolves via
# CanonicalMemstore.path_for (SOUL.md lives at the HERMES_HOME root; the rest
# under memories/).
_ROUTE: dict[str, tuple[str, str]] = {
    CATEGORY_IDENTITY: ("IDENTITY.md", "Identity"),
    CATEGORY_USER: ("USER.md", "Preferences"),
    CATEGORY_PROJECT: ("AGENTS.md", "Operating Notes"),
    CATEGORY_TOOL: ("TOOLS.md", "Tools & Environment"),
    CATEGORY_INSIGHT: ("MEMORY.md", "Insights"),
    CATEGORY_GENERAL: ("MEMORY.md", "Notes"),
}
_DEFAULT_ROUTE = ("MEMORY.md", "Notes")

# All canonical files, for scaffolding / status.
CANONICAL_FILES = (
    "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md",
    "TOOLS.md", "BOOTSTRAP.md", "HEARTBEAT.md", "MEMORY.md",
)

# The daily digest groups fact categories into these sections, in order.
_DAILY_SECTIONS: tuple[tuple[str, str], ...] = (
    (CATEGORY_IDENTITY, "Identity"),
    (CATEGORY_USER, "Preferences & Profile"),
    (CATEGORY_PROJECT, "Decisions"),
    (CATEGORY_TOOL, "Tools & Environment"),
    (CATEGORY_GENERAL, "Notes"),
)


# ---------------------------------------------------------------------------
# Idempotent managed-block markdown editing
# ---------------------------------------------------------------------------

_BLOCK_RE_TMPL = (
    r"<!--\s*hermes:seed:begin\s+{slug}\s*-->\n(?P<body>.*?)\n?<!--\s*hermes:seed:end\s+{slug}\s*-->"
)


class MarkdownDoc:
    """A markdown file with idempotent, section-scoped managed blocks.

    A managed block looks like::

        ## Preferences
        <!-- hermes:seed:begin preferences -->
        - the user prefers concise answers
        <!-- hermes:seed:end preferences -->

    ``upsert_bullets`` merges new bullets into the block for a section
    (de-duplicating by normalised content) and leaves everything outside the
    markers alone.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def bullets_in(self, section: str) -> list[str]:
        slug = _slug(section)
        m = re.search(_BLOCK_RE_TMPL.format(slug=re.escape(slug)), self.text, re.DOTALL)
        if not m:
            return []
        return _parse_bullets(m.group("body"))

    def upsert_bullets(self, section: str, bullets: Iterable[str]) -> int:
        """Merge ``bullets`` into ``section``'s managed block. Returns count added."""
        slug = _slug(section)
        new = [b.strip() for b in bullets if b and b.strip()]
        if not new:
            return 0

        existing = self.bullets_in(section)
        seen = {_normalize(b) for b in existing}
        added: list[str] = []
        for b in new:
            key = _normalize(b)
            if key and key not in seen:
                seen.add(key)
                added.append(b)
        if not added and self._has_block(slug):
            return 0

        merged = existing + added
        block_body = "\n".join(f"- {b}" for b in merged)
        block = (
            f"<!-- hermes:seed:begin {slug} -->\n"
            f"{block_body}\n"
            f"<!-- hermes:seed:end {slug} -->"
        )

        pattern = _BLOCK_RE_TMPL.format(slug=re.escape(slug))
        if self._has_block(slug):
            self.text = re.sub(pattern, lambda _m: block, self.text, count=1, flags=re.DOTALL)
        else:
            heading = f"## {section}"
            prefix = "" if not self.text or self.text.endswith("\n\n") else (
                "\n" if self.text.endswith("\n") else "\n\n"
            )
            if not self.text:
                # New file — give it an H1 title.
                title = self.path.stem.replace("_", " ").title()
                self.text = f"# {title}\n\n{heading}\n{block}\n"
            else:
                self.text = f"{self.text}{prefix}{heading}\n{block}\n"
        return len(added)

    def _has_block(self, slug: str) -> bool:
        return re.search(_BLOCK_RE_TMPL.format(slug=re.escape(slug)), self.text, re.DOTALL) is not None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = self.text if self.text.endswith("\n") else self.text + "\n"
        self.path.write_text(text, encoding="utf-8")


def _parse_bullets(body: str) -> list[str]:
    out: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r"^[-*+]\s+(.*)$", line)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return out


def _slug(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "notes"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class FileSeedReport:
    files_touched: dict[str, int] = field(default_factory=dict)
    total_added: int = 0

    def note(self, filename: str, added: int) -> None:
        if added:
            self.files_touched[filename] = self.files_touched.get(filename, 0) + added
            self.total_added += added

    def summary(self) -> str:
        if not self.files_touched:
            return "no new facts (all already present)"
        parts = ", ".join(f"{k}+{v}" for k, v in sorted(self.files_touched.items()))
        return f"wrote {self.total_added} fact(s): {parts}"


@dataclass
class RollUpReport:
    days_read: int = 0
    facts_in: int = 0
    dream_summary: str = ""
    seed: FileSeedReport = field(default_factory=FileSeedReport)

    def summary(self) -> str:
        return (
            f"rolled up {self.days_read} day(s) / {self.facts_in} fact(s) → "
            f"{self.seed.summary()}"
        )


# ---------------------------------------------------------------------------
# The canonical memstore
# ---------------------------------------------------------------------------


class CanonicalMemstore:
    """A directory of canonical markdown memory files.

    ``root`` defaults to ``HERMES_HOME``. SOUL.md sits at the root (Hermes reads
    it as identity); every other canonical file lives under ``memories/``.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            from hermes_constants import get_hermes_home
            root = get_hermes_home()
        self.root = Path(root).expanduser()
        self.memories = self.root / "memories"
        self.daily = self.memories / "daily"

    # -- Paths ---------------------------------------------------------------

    def path_for(self, filename: str) -> Path:
        # SOUL.md is the Hermes-native identity file at the HERMES_HOME root.
        if filename == "SOUL.md":
            return self.root / "SOUL.md"
        return self.memories / filename

    def daily_path(self, date_str: str) -> Path:
        return self.daily / f"{date_str}.md"

    # -- Seeding facts into the canonical files ------------------------------

    def seed_facts(self, corpus: FactCorpus | Iterable[SeedFact]) -> FileSeedReport:
        """Route every fact to its canonical file/section and upsert it."""
        facts = corpus if isinstance(corpus, FactCorpus) else FactCorpus(corpus)
        report = FileSeedReport()

        # Group by (filename, section) so each file opens once.
        grouped: dict[str, dict[str, list[str]]] = {}
        soul_identity: list[str] = []
        for fact in facts:
            filename, section = _ROUTE.get(fact.category, _DEFAULT_ROUTE)
            grouped.setdefault(filename, {}).setdefault(section, []).append(fact.content)
            if fact.category == CATEGORY_IDENTITY:
                soul_identity.append(fact.content)

        for filename, sections in grouped.items():
            doc = MarkdownDoc(self.path_for(filename))
            for section, bullets in sections.items():
                added = doc.upsert_bullets(section, bullets)
                report.note(filename, added)
            doc.save()

        # Mirror identity into SOUL.md so Hermes actually reads it at boot.
        if soul_identity:
            soul = MarkdownDoc(self.path_for("SOUL.md"))
            added = soul.upsert_bullets("Learned Identity", soul_identity)
            soul.save()
            report.note("SOUL.md", added)

        return report

    # -- Daily digests (base layer) ------------------------------------------

    def write_daily_digests(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        default_date: str | None = None,
    ) -> dict[str, int]:
        """Split a transcript by day and write one digest file per day.

        Returns ``{date_str: fact_count}``. Messages are grouped by their
        timestamp field; undated messages fall under ``default_date`` (today
        when omitted).
        """
        by_day = group_transcript_by_day(messages, default_date=default_date)
        written: dict[str, int] = {}
        for date_str, day_msgs in sorted(by_day.items()):
            facts = parse_transcript(day_msgs, source=f"daily:{date_str}")
            count = self._write_daily_file(date_str, day_msgs, facts)
            written[date_str] = count
        return written

    def _write_daily_file(
        self, date_str: str, messages: Sequence[dict], facts: list[SeedFact]
    ) -> int:
        doc = MarkdownDoc(self.daily_path(date_str))
        if not doc.text:
            doc.text = f"# Daily Memory — {date_str}\n"

        user_turns = sum(
            1 for m in messages if str(m.get("role", "")).lower() in ("user", "human")
        )
        doc.upsert_bullets(
            "Summary",
            [f"{user_turns} user turn(s); {len(facts)} fact(s) captured on {date_str}"],
        )

        by_cat: dict[str, list[str]] = {}
        for f in facts:
            by_cat.setdefault(f.category, []).append(f.content)
        for category, section in _DAILY_SECTIONS:
            if by_cat.get(category):
                doc.upsert_bullets(section, by_cat[category])
        doc.save()
        return len(facts)

    def load_daily_corpus(self, days: int | None = None) -> tuple[FactCorpus, int]:
        """Read recent daily files back into a corpus for consolidation.

        Returns ``(corpus, days_read)``. ``days`` limits to the most recent N
        dated files (by filename); ``None`` reads all.
        """
        corpus = FactCorpus()
        if not self.daily.is_dir():
            return corpus, 0
        files = sorted(
            (p for p in self.daily.glob("*.md") if _DATE_RE.match(p.stem)),
            reverse=True,
        )
        if days is not None:
            files = files[:days]
        for path in files:
            date_str = path.stem
            doc = MarkdownDoc(path)
            for category, section in _DAILY_SECTIONS:
                for bullet in doc.bullets_in(section):
                    corpus.add(SeedFact(
                        content=bullet,
                        category=category,
                        source=f"daily:{date_str}",
                        entities=_entities(bullet),
                    ))
        return corpus, len(files)

    # -- Roll-up (higher layer / dreams) -------------------------------------

    def roll_up(
        self,
        *,
        days: int | None = None,
        consolidator: DreamConsolidator | None = None,
    ) -> RollUpReport:
        """Consolidate recent daily digests into MEMORY.md / USER.md.

        The base layer (daily files) is read, dream-consolidated, and the
        distilled facts (including synthesised insights) are folded back into
        the canonical files — the "build on top of the daily breakdown" step.
        """
        corpus, days_read = self.load_daily_corpus(days=days)
        report = RollUpReport(days_read=days_read, facts_in=len(corpus))
        if not corpus:
            return report
        consolidator = consolidator or DreamConsolidator()
        refined, dream = consolidator.consolidate(corpus)
        report.dream_summary = dream.summary()
        report.seed = self.seed_facts(refined)
        return report

    # -- Scaffolding / status ------------------------------------------------

    def existing_files(self) -> dict[str, bool]:
        return {name: self.path_for(name).exists() for name in CANONICAL_FILES}


# ---------------------------------------------------------------------------
# Transcript day-grouping
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_FIELDS = ("timestamp", "created_at", "ts", "time", "date", "datetime")


def group_transcript_by_day(
    messages: Sequence[dict[str, Any]],
    *,
    default_date: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Group messages into ``{YYYY-MM-DD: [messages]}`` by timestamp field.

    Recognised timestamp fields: timestamp, created_at, ts, time, date,
    datetime — accepting epoch seconds/millis or ISO-8601 strings. Messages
    without a parseable timestamp fall under ``default_date`` (today if None).
    """
    fallback = default_date or today_str()
    out: dict[str, list[dict[str, Any]]] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        date_str = _msg_date(msg) or fallback
        out.setdefault(date_str, []).append(msg)
    return out


def _msg_date(msg: dict[str, Any]) -> str | None:
    for fieldname in _TS_FIELDS:
        if fieldname in msg and msg[fieldname] not in (None, ""):
            parsed = _parse_date(msg[fieldname])
            if parsed:
                return parsed
    return None


def _parse_date(value: Any) -> str | None:
    # Epoch (seconds or milliseconds).
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            num = float(value)
            if num > 1e12:  # milliseconds
                num /= 1000.0
            dt = datetime.fromtimestamp(num, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return None
    # ISO-8601 string.
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            # Bare YYYY-MM-DD prefix?
            if len(text) >= 10 and _DATE_RE.match(text[:10]):
                return text[:10]
    return None


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _entities(text: str) -> tuple[str, ...]:
    from agent.memstore_seeding import extract_entities
    return extract_entities(text)
