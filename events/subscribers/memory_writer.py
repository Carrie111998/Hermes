"""MemoryWriter subscriber -- routes high-signal events to memory layers.

Follows CLAUDE.md memory routing rules:
  - GBrain: world knowledge (companies, jobs, applications)
  - MemPalace: verbatim evidence (interview signals, offers)
  - Agent MEMORY.md: operational notes (failures, outages)

Rate-limited: max 10 GBrain writes/hour, 5 MemPalace writes/hour.
"""

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Maps EventType -> {targets: [layer], action: str}
MEMORY_ROUTING: Dict[EventType, Dict[str, Any]] = {
    EventType.JOB_HIGH_SCORE: {
        "targets": ["gbrain"],
        "action": "put_page_and_timeline",
    },
    EventType.APPLICATION_SUBMITTED: {
        "targets": ["gbrain"],
        "action": "add_timeline_entry",
    },
    EventType.APPLICATION_FAILED: {
        "targets": ["gbrain", "memory_md"],
        "action": "add_timeline_and_note",
    },
    EventType.INTERVIEW_SIGNAL: {
        "targets": ["gbrain", "mempalace"],
        "action": "timeline_and_evidence",
    },
    EventType.OFFER_SIGNAL: {
        "targets": ["gbrain", "mempalace"],
        "action": "timeline_and_evidence",
    },
    EventType.STAGE_TRANSITION: {
        "targets": ["gbrain"],
        "action": "add_timeline_entry",
    },
    EventType.CRON_FAILED_CONSECUTIVE: {
        "targets": ["memory_md"],
        "action": "operational_note",
    },
    EventType.GATEWAY_HEALTH: {
        "targets": ["memory_md"],
        "action": "operational_note",
    },
    EventType.FOLLOWUP_DUE: {
        "targets": ["mempalace"],
        "action": "add_drawer",
    },
}

RATE_LIMITS = {
    "gbrain": {"max_per_hour": 10, "window": 3600},
    "mempalace": {"max_per_hour": 5, "window": 3600},
    "memory_md": {"max_per_hour": 20, "window": 3600},
}


class MemoryWriter(BaseSubscriber):
    subscriber_id = "memory-writer"
    poll_interval_seconds = 60

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self._rate_counters: Dict[str, List[float]] = defaultdict(list)
        self._seen_correlation_ids: Dict[str, float] = {}  # correlation_id → timestamp
        self._DEDUP_WINDOW = 86400  # 24 hours
        # Cached MemPalace Chroma collection — None means "not loaded yet".
        # False means "mempalace is not installed; don't try again".
        self._mempalace_collection: Any = None

    def handle(self, event: Event) -> None:
        routing = MEMORY_ROUTING.get(event.event_type)
        if not routing:
            return

        # Dedup via correlation_id to avoid duplicate memory writes
        if event.correlation_id:
            now = time.monotonic()
            # Clean stale entries
            self._seen_correlation_ids = {
                cid: ts for cid, ts in self._seen_correlation_ids.items()
                if now - ts < self._DEDUP_WINDOW
            }
            if event.correlation_id in self._seen_correlation_ids:
                logger.debug("MemoryWriter: skipping duplicate correlation_id %s",
                             event.correlation_id)
                return
            self._seen_correlation_ids[event.correlation_id] = now

        for target in routing["targets"]:
            if not self._check_rate_limit(target):
                logger.warning("MemoryWriter: rate limit hit for %s, queuing", target)
                continue
            try:
                content = self._build_content(event, target)
                self._write_to_target(target, event, content)
            except Exception:
                logger.exception("MemoryWriter: failed to write to %s for %s",
                                 target, event.event_type.type_string)

    def _check_rate_limit(self, target: str) -> bool:
        """Check if we're within rate limits for this target."""
        limits = RATE_LIMITS.get(target, {"max_per_hour": 20, "window": 3600})
        now = time.monotonic()
        window = limits["window"]

        # Clean old entries
        self._rate_counters[target] = [
            t for t in self._rate_counters[target] if now - t < window
        ]

        if len(self._rate_counters[target]) >= limits["max_per_hour"]:
            return False

        self._rate_counters[target].append(now)
        return True

    def _build_content(self, event: Event, target: str) -> str:
        """Build content string for the target memory layer."""
        p = event.payload
        et = event.event_type

        if target == "gbrain":
            if et == EventType.JOB_HIGH_SCORE:
                return (f"High-score job discovered: {p.get('title', '?')} at "
                        f"{p.get('company', '?')} (score: {p.get('score', '?')})")
            if et == EventType.APPLICATION_SUBMITTED:
                return (f"Application submitted for {p.get('title', '?')} at "
                        f"{p.get('company', '?')} via {p.get('platform', '?')} "
                        f"on {event.timestamp[:10]}")
            if et == EventType.APPLICATION_FAILED:
                return (f"Application failed for {p.get('title', '?')} at "
                        f"{p.get('company', '?')}: {p.get('error', 'unknown')}")
            if et in (EventType.INTERVIEW_SIGNAL, EventType.OFFER_SIGNAL):
                return (f"{et.type_string}: {p.get('company', '?')} — "
                        f"{p.get('detail', 'no detail')}")
            if et == EventType.STAGE_TRANSITION:
                return (f"Pipeline: {p.get('company', '?')} moved to "
                        f"{p.get('new_stage', '?')} from {p.get('old_stage', '?')}")

        if target == "mempalace":
            # Verbatim evidence
            return (f"[{event.timestamp}] {et.type_string} from {event.source}: "
                    f"{p.get('company', '?')} — {p.get('detail', str(p)[:300])}")

        if target == "memory_md":
            if et == EventType.CRON_FAILED_CONSECUTIVE:
                return (f"{p.get('job_name', '?')} failing since {event.timestamp[:10]} "
                        f"({p.get('consecutive_errors', '?')} consecutive) — "
                        f"{p.get('error', 'investigate')}")
            if et == EventType.GATEWAY_HEALTH:
                return (f"{p.get('platform', '?')} gateway went {p.get('status', '?')} "
                        f"at {event.timestamp[:19]}")
            if et == EventType.APPLICATION_FAILED:
                return (f"Application to {p.get('company', '?')} failed: "
                        f"{p.get('error', 'unknown')} — investigate {p.get('platform', '?')} compatibility")

        return f"{et.type_string}: {str(p)[:200]}"

    def _write_to_target(self, target: str, event: Event, content: str) -> None:
        """Write content to the specified memory layer."""
        if target == "gbrain":
            self._write_gbrain(event, content)
        elif target == "mempalace":
            self._write_mempalace(event, content)
        elif target == "memory_md":
            self._write_memory_md(event, content)

    def _write_gbrain(self, event: Event, content: str) -> None:
        """Write to GBrain via MCP tools (best-effort)."""
        try:
            import subprocess
            company = event.payload.get("company", "")
            if not company:
                logger.debug("MemoryWriter: no company in event, skipping GBrain write")
                return
            subprocess.run(
                ["gbrain", "timeline", "add", company, content],
                capture_output=True, timeout=10,
            )
            logger.info("MemoryWriter: wrote GBrain timeline entry for %s", company)
        except FileNotFoundError:
            logger.debug("MemoryWriter: gbrain CLI not found, skipping")
        except Exception as e:
            logger.warning("MemoryWriter: GBrain write failed: %s", e)

    def _write_mempalace(self, event: Event, content: str) -> None:
        """Store verbatim event evidence as a MemPalace drawer.

        Uses the mempalace Python package directly (the same palace the
        MCP server and CLI write to).  The collection is cached after
        first use so subsequent writes don't re-open the ChromaDB handle.

        Wing: ``hermes-events`` (isolates event-bus writes from other wings).
        Room: the event type string (one room per event kind).
        source_file: ``event-bus:<event_id>`` — unique identifier that
            mempalace's dedup logic keys on.

        Best-effort: if mempalace isn't installed, logs at debug and returns.
        """
        collection = self._get_mempalace_collection()
        if collection is False:
            return  # mempalace not available

        try:
            from mempalace.miner import add_drawer
            add_drawer(
                collection,
                wing="hermes-events",
                room=event.event_type.type_string,
                content=content,
                source_file=f"event-bus:{event.event_id}",
                chunk_index=0,
                agent="hermes-event-bus",
            )
            logger.info("MemoryWriter: filed MemPalace drawer for %s", event.event_type.type_string)
        except Exception as e:
            logger.warning("MemoryWriter: MemPalace write failed: %s", e)

    def _get_mempalace_collection(self) -> Any:
        """Return the cached MemPalace collection, or load it on first use.

        Returns False if mempalace isn't installed — caller should bail.
        Returns a ChromaCollection-like object on success.
        """
        if self._mempalace_collection is not None:
            return self._mempalace_collection

        try:
            from mempalace.palace import get_collection
            import os
            from pathlib import Path

            # Default palace path per mempalace convention; respects
            # MEMPALACE_HOME override used by the CLI.
            palace_root = os.environ.get("MEMPALACE_HOME") or str(Path.home() / ".mempalace" / "palace")
            self._mempalace_collection = get_collection(palace_root)
            return self._mempalace_collection
        except ImportError:
            logger.debug("MemoryWriter: mempalace not installed, skipping MemPalace writes")
            self._mempalace_collection = False
            return False
        except Exception as e:
            logger.warning("MemoryWriter: could not open MemPalace collection: %s", e)
            # Don't cache the failure — transient errors (e.g. locked DB) may clear
            return False

    def _write_memory_md(self, event: Event, content: str) -> None:
        """Append operational note to agent MEMORY.md."""
        try:
            from hermes_constants import get_hermes_home
            memory_path = get_hermes_home() / "memories" / "MEMORY.md"
            memory_path.parent.mkdir(parents=True, exist_ok=True)

            existing = ""
            if memory_path.exists():
                existing = memory_path.read_text(encoding="utf-8")

            # Append with date header
            date_header = f"\n## Event {event.timestamp[:10]}\n"
            if date_header.strip() not in existing:
                existing += date_header
            existing += f"- {content}\n"

            memory_path.write_text(existing, encoding="utf-8")
            logger.info("MemoryWriter: appended to MEMORY.md: %s", content[:80])
        except Exception as e:
            logger.warning("MemoryWriter: MEMORY.md write failed: %s", e)
