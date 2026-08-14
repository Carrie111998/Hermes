"""Noise guards for chat delivery (v3 principle P4/P6: silence is not a
message; a flap is one incident, not N messages).

Three guards, all applied by TelegramNotifier BEFORE delivery (events
always stay on the bus for audit/critic consumers — these only gate chat):

  * is_noop_cron_output(): the measured 93%-of-cron-firehose case —
    ``[SILENT]`` markers, empty output, "no work". Content after a
    marker survives (a real error inside a [SILENT]-prefixed run must
    not be swallowed).
  * RepeatGuard: verbatim-repeat suppression per (thread, text) within a
    window — the "devflow-bridge: [SILENT]" ×1,522/week class, and
    defense-in-depth against WAL-contention redelivery floods
    (2026-04-28 incident shape).
  * FlapGuard: state-transition collapse for up/down signals — the
    WhatsApp-bridge flap delivered 898 alternating messages/week.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_SILENT_MARKER_RE = re.compile(r"\[SILENT\]", re.IGNORECASE)
_NOOP_PHRASES = frozenset({
    "no work", "no work.", "no-op", "noop", "nothing to do",
    "nothing to do.", "ok", "done", "no output",
})
# <AGENT_ITERATION_JSON>{...}</AGENT_ITERATION_JSON> machine-telemetry
# blocks ride inside cron output_summary on nearly every agent run — they
# are for the Critic/dashboards, never for a human message, and their
# presence made every no-op run look substantive. Also strip an unclosed
# trailing block (truncated summaries).
_AGENT_ITERATION_JSON_RE = re.compile(
    r"<AGENT_ITERATION_JSON>.*?(?:</AGENT_ITERATION_JSON>|\Z)", re.DOTALL)
_DIGIT_RUN_RE = re.compile(r"\d+(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")


def strip_agent_iteration_json(text: str) -> str:
    """Remove machine-telemetry AGENT_ITERATION_JSON blocks from output."""
    return _AGENT_ITERATION_JSON_RE.sub("", str(text or ""))


def normalize_for_fingerprint(text: str) -> str:
    """Canonical form for repeat detection: telemetry blocks out, digit
    runs collapsed to 'N' (durations, timestamps, counters, percentages
    all vary run-to-run without changing what the message MEANS), and
    whitespace collapsed."""
    text = strip_agent_iteration_json(text)
    text = _DIGIT_RUN_RE.sub("N", text)
    return _WS_RE.sub(" ", text).strip().lower()


def is_noop_cron_output(output_summary: str) -> bool:
    """True iff a cron run's output carries no information worth a chat
    message: empty, only [SILENT] markers / telemetry blocks, or a bare
    no-op phrase. Anything else — including real content FOLLOWING a
    [SILENT] marker — is substantive and must be delivered."""
    text = strip_agent_iteration_json(output_summary)
    text = _SILENT_MARKER_RE.sub("", text).strip()
    if not text:
        return True
    return text.lower() in _NOOP_PHRASES


def is_sustained_resource_repeat(event) -> bool:
    """True iff this RESOURCE_PRESSURE event is an unchanged re-ping.

    The producer re-samples a live episode every 900s so it stays
    reconstructable on the bus after the fact — that sampling is what made the
    2026-08-14 delivery audit possible and is deliberately kept. But an
    unchanged sample is not a message: only ``rising_edge`` / ``band_change`` /
    ``reasons_change`` reach chat. Bus-only, exactly like the cron lifecycle
    types in ``_CRON_BUS_ONLY``.

    Deliberately duck-typed (no ``events.schema`` import) so this module stays
    dependency-free, and defaulting to FALSE for events with no ``change`` key
    keeps pre-band producers delivering.
    """
    payload = getattr(event, "payload", None) or {}
    type_string = getattr(getattr(event, "event_type", None), "type_string", "")
    return (type_string == "resource_pressure"
            and payload.get("change") == "sustained_repeat")


class RepeatGuard:
    """Suppress verbatim repeats of (key, text) within ``window_seconds``.

    Never apply to ACT-class routes (call-site responsibility): an
    operator action item must always land even if worded identically.
    """

    def __init__(self, window_seconds: float = 1800.0, max_entries: int = 512):
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self._seen: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self.suppressed_count = 0

    @staticmethod
    def _fingerprint(text: str) -> str:
        # Normalized (digits→N, telemetry stripped): a message that only
        # differs by duration/timestamp/counter is the SAME message. The
        # pre-v3 repeat flood survived exact-match dedup precisely because
        # the header timestamp made every copy unique.
        return hashlib.sha1(
            normalize_for_fingerprint(text).encode("utf-8", "replace")
        ).hexdigest()

    def is_repeat(self, key: str, text: str, now: Optional[float] = None) -> bool:
        """Record and decide in one call: True → caller should suppress."""
        now = time.monotonic() if now is None else now
        fp = (key, self._fingerprint(text))
        last = self._seen.get(fp)
        if last is not None and (now - last) < self.window_seconds:
            self._seen[fp] = now  # sliding window: a message that keeps
            self._seen.move_to_end(fp)  # repeating stays suppressed
            self.suppressed_count += 1
            return True
        self._seen[fp] = now
        self._seen.move_to_end(fp)
        while len(self._seen) > self.max_entries:
            self._seen.popitem(last=False)
        return False


@dataclass
class FlapDecision:
    deliver: bool
    note: Optional[str] = None  # appended to the message when set


class FlapGuard:
    """Per-key state-change tracker for up/down style signals.

    Rules:
      * same state re-announced → suppress;
      * state change → deliver;
      * >= ``flap_threshold`` changes within ``window_seconds`` → deliver
        ONE "flapping" summary, then mute the key for ``mute_seconds``;
      * first observation after the mute expires → deliver with a
        stabilization note carrying the final state.
    """

    def __init__(
        self,
        window_seconds: float = 900.0,
        flap_threshold: int = 4,
        mute_seconds: float = 1800.0,
        max_mute_seconds: float = 86400.0,
    ):
        self.window_seconds = window_seconds
        self.flap_threshold = flap_threshold
        self.mute_seconds = mute_seconds
        self.max_mute_seconds = max_mute_seconds
        self._last_state: Dict[str, str] = {}
        self._transitions: Dict[str, List[float]] = {}
        self._muted_until: Dict[str, float] = {}
        self._flap_count_at_mute: Dict[str, int] = {}
        # Escalating mute: a key that re-enters flapping right after a
        # mute expires doubles its next mute (capped) — a multi-DAY flap
        # (the 2026-07 WhatsApp bridge) costs a handful of messages, not
        # 2 per mute cycle forever.
        self._mute_streak: Dict[str, int] = {}

    def observe(self, key: str, state: str, now: Optional[float] = None) -> FlapDecision:
        now = time.monotonic() if now is None else now
        prev = self._last_state.get(key)
        muted_until = self._muted_until.get(key, 0.0)

        if prev == state:
            # Re-announcement of an unchanged state is never a message —
            # muted or not.
            return FlapDecision(deliver=False)

        self._last_state[key] = state
        times = self._transitions.setdefault(key, [])
        times.append(now)
        cutoff = now - self.window_seconds
        while times and times[0] < cutoff:
            times.pop(0)

        if now < muted_until:
            # Still inside the mute window: swallow, remember via
            # _last_state so the post-mute summary reports the final state.
            return FlapDecision(deliver=False)

        if muted_until and now >= muted_until:
            # First observation after mute expiry.
            self._muted_until.pop(key, None)
            flips = self._flap_count_at_mute.pop(key, 0)
            return FlapDecision(
                deliver=True,
                note=(
                    f"stabilized after flapping ({flips} state changes "
                    f"suppressed); current: {state}"
                ),
            )

        if len(times) >= self.flap_threshold:
            streak = self._mute_streak.get(key, 0)
            # Reset the escalation streak if the key stayed calm for a
            # full window after its last mute would have expired.
            mute = min(
                self.mute_seconds * (2 ** streak), self.max_mute_seconds)
            self._mute_streak[key] = streak + 1
            self._muted_until[key] = now + mute
            self._flap_count_at_mute[key] = len(times)
            return FlapDecision(
                deliver=True,
                note=(
                    f"⚠ flapping: {len(times)} state changes in "
                    f"{int(self.window_seconds // 60)} min — muting this "
                    f"signal for {int(mute // 60)} min"
                ),
            )

        # A genuine (non-flapping) transition resets the escalation streak.
        self._mute_streak.pop(key, None)
        return FlapDecision(deliver=True)
