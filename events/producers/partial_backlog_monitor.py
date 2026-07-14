"""PartialBacklogMonitor — emits TRACKER_PARTIAL_BACKLOG when partial/ grows.

Sibling of ResourcePressureMonitor: where that watches host commit/pagefile/disk,
this watches the tracker-intent-applier's partial/ queue
(~/.hermes/mailbox/tracker/partial/). A partial is an intent whose pipeline.json
write (step 3) succeeded but whose Postgres mirror (step 4, :4100) did not; the
idempotency key is unburned so it stays re-drivable. On 2026-07-13 thirteen such
partials piled up and sat ~a day UNNOTICED. This monitor closes that gap.

ALWAYS ON — independent of the auto-re-drive feature flag. Monitoring must not be
gated on the fix being live; the alert is exactly what tells an operator to act
when auto-re-drive is off or has hit the attempt cap.

Emission policy (identical to ResourcePressureMonitor): edge-triggered with a
re-arm cooldown — fire once on the rising edge of "count > threshold", stay quiet
while the backlog persists, re-ping every re_alert_cooldown_seconds if sustained,
and re-arm on the falling edge (count drops to <= threshold).

Read-only: counts files and best-effort reads job_id for a triage sample. Never
mutates the mailbox, so it is safe off the single-writer applier thread — it is
called from the SHARED subscriber poll loop, next to the resource monitor.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

DEFAULT_ALERT_THRESHOLD = 3               # fire when partial count strictly exceeds this
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 900.0  # re-ping a sustained backlog every 15 min
SAMPLE_CAP = 10                           # cap sample_job_ids so the payload stays small

_INTENT_GLOB = "*_INTENT_*.json"


@dataclass(frozen=True)
class PartialBacklogSample:
    """A point-in-time reading of the tracker partial/ queue."""

    count: int
    oldest_age_seconds: float
    sample_job_ids: List[str] = field(default_factory=list)


def _read_job_id(path: Path) -> str:
    """Best-effort top-level ``job_id`` from an intent file; else the filename stem."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        jid = data.get("job_id")
        if isinstance(jid, str) and jid:
            return jid
    except Exception:
        pass
    return path.stem


def sample_partial_backlog(
    partial_dir: Path, now: float, *, sample_cap: int = SAMPLE_CAP,
) -> PartialBacklogSample:
    """Count partial *_INTENT_*.json files and sample up to ``sample_cap`` job IDs.

    Missing dir => empty sample (count 0). Best-effort: an unreadable/corrupt file
    still counts, and its job-id sample falls back to the filename stem.
    """
    if not partial_dir.exists():
        return PartialBacklogSample(count=0, oldest_age_seconds=0.0, sample_job_ids=[])
    paths = sorted(partial_dir.glob(_INTENT_GLOB))
    oldest_age = 0.0
    sample_ids: List[str] = []
    for p in paths:
        try:
            age = now - p.stat().st_mtime
            if age > oldest_age:
                oldest_age = age
        except OSError:
            continue
        if len(sample_ids) < sample_cap:
            sample_ids.append(_read_job_id(p))
    return PartialBacklogSample(
        count=len(paths), oldest_age_seconds=oldest_age, sample_job_ids=sample_ids,
    )


class PartialBacklogMonitor:
    """Counts partial/ and emits TRACKER_PARTIAL_BACKLOG on the rising edge.

    Call check() every ~60s from the shared subscriber poll loop. The sampler and
    clock are injectable so the edge/cooldown core is fully testable without a
    real mailbox or sleeps.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        partial_dir: Optional[Path] = None,
        sampler: Optional[Callable[[], Optional[PartialBacklogSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        alert_threshold: int = DEFAULT_ALERT_THRESHOLD,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        self.partial_dir = Path(partial_dir) if partial_dir is not None else None
        self._sampler = sampler or self._default_sampler
        self._clock = clock or time.monotonic
        self.alert_threshold = alert_threshold
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds
        # Edge-trigger state.
        self._in_backlog: bool = False
        self._last_emit: Optional[float] = None

    def _default_sampler(self) -> Optional[PartialBacklogSample]:
        if self.partial_dir is None:
            return None
        # Wall-clock for file ages; the monotonic clock() drives edge/cooldown.
        return sample_partial_backlog(self.partial_dir, time.time())

    def check(self) -> Optional[str]:
        """Sample, evaluate, emit on a rising edge. Returns event_id or None.

        Swallows sampler failures: a filesystem read blowing up must never crash
        the gateway poll loop.
        """
        try:
            sample = self._sampler()
        except Exception:
            logger.exception("PartialBacklogMonitor: sampler raised")
            return None
        if sample is None:
            return None
        return self.evaluate(sample, self._clock())

    def evaluate(self, sample: PartialBacklogSample, now: float) -> Optional[str]:
        """Evaluate one sample at monotonic ``now``; emit on rising edge.

        Pure given (sample, now) + internal edge state — the testable core.
        """
        if sample.count <= self.alert_threshold:
            # Backlog clear (or never present): re-arm so the next rise fires now.
            self._in_backlog = False
            return None

        rising_edge = not self._in_backlog
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        self._in_backlog = True
        if not (rising_edge or cooldown_elapsed):
            return None

        self._last_emit = now
        return self._emit(sample)

    def _emit(self, sample: PartialBacklogSample) -> str:
        payload = {
            "count": sample.count,
            "threshold": self.alert_threshold,
            "oldest_age_seconds": round(sample.oldest_age_seconds, 1),
            "capped_count": len(sample.sample_job_ids),
            "sample_job_ids": list(sample.sample_job_ids),
        }
        logger.warning(
            "Tracker partial backlog: %d partial intents (> %d) — oldest %.0fs; sample=%s",
            sample.count, self.alert_threshold, sample.oldest_age_seconds,
            payload["sample_job_ids"],
        )
        return self.bus.emit(
            event_type=EventType.TRACKER_PARTIAL_BACKLOG,
            source="tracker-intent-applier",
            payload=payload,
            tags=["tracker", "partial", "backlog"],
        )
