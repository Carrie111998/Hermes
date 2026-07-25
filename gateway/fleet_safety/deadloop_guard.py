"""Runaway-loop detector for active agent sessions.

The detector watches a stream of per-session :class:`SessionObservation`
samples (one per housekeeping tick) and trips on any of four independent
runaway signals — the four failure modes of the 2026-07-25 incident:

  (a) **Rate.** Model-call count *or* estimated token spend over a threshold in
      a rolling window (default: >100 calls or >4,000,000 tokens per 15 min on
      one session). The incident's token rate (~5.5M tokens / 15 min) trips
      this; a healthy interactive session does not.
  (b) **Repeated non-retryable error.** The same non-retryable (4xx) status
      code observed on ``>=K`` consecutive samples (default 3). A 4xx does not
      get better on retry — hammering it is pure waste.
  (c) **Wall-clock.** One continuous turn running longer than a hard cap
      (default 60 min). The incident ran ~13 h.
  (d) **No-forward-progress.** A huge context (>150k tokens) re-sent across
      ``>=M`` consecutive samples (default 3) with an unchanged state hash —
      i.e. the loop keeps paying for a giant context but produces no new tool
      results / state change.

Determinism: :meth:`RunawayGuard.observe` takes ``now`` explicitly and holds
all mutable state on the guard instance. Given the same sequence of
``(observation, now)`` pairs it always trips at the same point on the same
reason, in the fixed priority order below. It never reads the clock itself.
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional


class TripReason(str, enum.Enum):
    """Why a session was flagged as a runaway. Ordered by evaluation priority."""

    WALL_CLOCK = "wall_clock_runtime_exceeded"
    TOKEN_RATE = "token_spend_rate_exceeded"
    CALL_RATE = "model_call_rate_exceeded"
    REPEATED_ERROR = "repeated_non_retryable_error"
    NO_PROGRESS = "huge_context_no_forward_progress"


@dataclass(frozen=True)
class GuardThresholds:
    """Trip thresholds. All windows/caps are in seconds; spend in tokens.

    Defaults are chosen so the 2026-07-25 incident trips well before a full
    wallet drain, while a normal long interactive/agentic session does not.
    """

    window_seconds: float = 900.0            # rolling window for rate checks (15 min)
    max_calls_per_window: int = 100          # (a) call-rate ceiling in window
    max_tokens_per_window: int = 4_000_000   # (a) token-rate ceiling in window
    max_runtime_seconds: float = 3600.0      # (c) continuous wall-clock cap (60 min)
    repeated_error_limit: int = 3            # (b) same 4xx on N consecutive samples
    huge_context_tokens: int = 150_000       # (d) "huge" context threshold
    no_progress_samples: int = 3             # (d) consecutive stalled samples to trip
    enabled: bool = True

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "GuardThresholds":
        """Build from a ``fleet_safety.deadloop_guard`` config dict.

        Unknown keys are ignored; missing keys fall back to the class default.
        Values are coerced defensively so a malformed user config can never
        crash the housekeeping tick — a bad value falls back to the default.
        """
        cfg = cfg or {}

        def _num(key: str, default, cast):
            try:
                val = cfg.get(key, default)
                return cast(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        return cls(
            window_seconds=_num("window_seconds", cls.window_seconds, float),
            max_calls_per_window=_num("max_calls_per_window", cls.max_calls_per_window, int),
            max_tokens_per_window=_num("max_tokens_per_window", cls.max_tokens_per_window, int),
            max_runtime_seconds=_num("max_runtime_seconds", cls.max_runtime_seconds, float),
            repeated_error_limit=_num("repeated_error_limit", cls.repeated_error_limit, int),
            huge_context_tokens=_num("huge_context_tokens", cls.huge_context_tokens, int),
            no_progress_samples=_num("no_progress_samples", cls.no_progress_samples, int),
            enabled=bool(cfg.get("enabled", cls.enabled)),
        )


@dataclass(frozen=True)
class SessionObservation:
    """One sampled snapshot of an active session.

    Cumulative counters (``api_call_count``, ``tokens_used``) are lifetime
    totals for the session; the guard differences them across the rolling
    window to get a rate. ``state_hash`` is any value that changes when the
    session makes forward progress (new tool result / new activity); the guard
    only cares whether it changed, not what it is.
    """

    session_id: str
    started_at: float                       # epoch seconds the turn began
    api_call_count: int = 0                 # cumulative model calls this session
    tokens_used: int = 0                    # cumulative estimated tokens this session
    context_tokens: int = 0                 # size of the context on the latest call
    state_hash: Optional[str] = None        # changes iff the session made progress
    error_code: Optional[int] = None        # non-retryable status on the latest call, else None
    provider: str = ""
    model: str = ""
    effort: str = ""


@dataclass(frozen=True)
class Trip:
    """A fired detection. Carries everything the report needs, no live handles."""

    session_id: str
    reason: TripReason
    detail: str
    estimated_tokens: int
    estimated_calls: int
    runtime_seconds: float
    provider: str
    model: str
    effort: str
    last_state: Optional[str]


@dataclass
class _SessionState:
    """Per-session accumulator held on the guard across ticks."""

    samples: Deque[tuple] = field(default_factory=deque)  # (now, api_call_count, tokens_used)
    last_error_code: Optional[int] = None
    repeated_error_count: int = 0
    last_state_hash: Optional[str] = None
    stalled_samples: int = 0
    tripped: bool = False   # latched — a killed session is not re-reported every tick


class RunawayGuard:
    """Stateful, deterministic runaway detector across many sessions.

    Feed it one :class:`SessionObservation` per active session per tick via
    :meth:`observe`; it returns a :class:`Trip` the first time a session
    crosses any threshold and ``None`` otherwise. Once a session trips it is
    latched (subsequent observations return ``None``) so a killed loop is
    reported exactly once. Call :meth:`forget` when a session ends to release
    its state.
    """

    def __init__(self, thresholds: Optional[GuardThresholds] = None) -> None:
        self.thresholds = thresholds or GuardThresholds()
        self._sessions: Dict[str, _SessionState] = {}

    # -- lifecycle -------------------------------------------------------

    def forget(self, session_id: str) -> None:
        """Drop tracking for a session that has ended (or after a kill)."""
        self._sessions.pop(session_id, None)

    def active_session_ids(self) -> list:
        return list(self._sessions.keys())

    # -- core ------------------------------------------------------------

    def observe(self, obs: SessionObservation, now: float) -> Optional[Trip]:
        """Record a sample and return a :class:`Trip` if this session crosses
        a threshold for the first time. Deterministic in ``(obs, now)``."""
        if not self.thresholds.enabled or not obs.session_id:
            return None

        st = self._sessions.get(obs.session_id)
        if st is None:
            st = _SessionState()
            self._sessions[obs.session_id] = st

        if st.tripped:
            return None

        self._update_samples(st, obs, now)
        self._update_error_streak(st, obs)
        self._update_progress(st, obs)

        trip = self._evaluate(st, obs, now)
        if trip is not None:
            st.tripped = True
        return trip

    # -- accumulators ----------------------------------------------------

    def _update_samples(self, st: _SessionState, obs: SessionObservation, now: float) -> None:
        st.samples.append((now, int(obs.api_call_count), int(obs.tokens_used)))
        cutoff = now - self.thresholds.window_seconds
        # Keep one sample just before the cutoff so the window delta spans the
        # full window rather than only the samples strictly inside it.
        while len(st.samples) > 1 and st.samples[1][0] < cutoff:
            st.samples.popleft()

    def _update_error_streak(self, st: _SessionState, obs: SessionObservation) -> None:
        code = obs.error_code
        if code is None:
            # A clean (non-erroring) sample breaks the streak — the session is
            # making calls that don't hit the same wall.
            st.last_error_code = None
            st.repeated_error_count = 0
            return
        if code == st.last_error_code:
            st.repeated_error_count += 1
        else:
            st.last_error_code = code
            st.repeated_error_count = 1

    def _update_progress(self, st: _SessionState, obs: SessionObservation) -> None:
        huge = obs.context_tokens >= self.thresholds.huge_context_tokens
        made_progress = obs.state_hash != st.last_state_hash
        st.last_state_hash = obs.state_hash
        if huge and not made_progress:
            st.stalled_samples += 1
        else:
            st.stalled_samples = 0

    # -- decision --------------------------------------------------------

    def _window_deltas(self, st: _SessionState) -> tuple:
        """(calls, tokens) accumulated across the retained window."""
        if len(st.samples) < 2:
            return 0, 0
        first = st.samples[0]
        last = st.samples[-1]
        calls = max(0, last[1] - first[1])
        tokens = max(0, last[2] - first[2])
        return calls, tokens

    def _evaluate(self, st: _SessionState, obs: SessionObservation, now: float) -> Optional[Trip]:
        t = self.thresholds
        runtime = max(0.0, now - obs.started_at)
        calls_in_window, tokens_in_window = self._window_deltas(st)

        def _mk(reason: TripReason, detail: str) -> Trip:
            return Trip(
                session_id=obs.session_id,
                reason=reason,
                detail=detail,
                estimated_tokens=int(obs.tokens_used),
                estimated_calls=int(obs.api_call_count),
                runtime_seconds=runtime,
                provider=obs.provider,
                model=obs.model,
                effort=obs.effort,
                last_state=obs.state_hash,
            )

        # Priority order: the most unambiguous / cheapest-to-justify first.
        if runtime > t.max_runtime_seconds:
            return _mk(
                TripReason.WALL_CLOCK,
                f"turn ran {runtime / 60:.1f} min (cap {t.max_runtime_seconds / 60:.0f} min)",
            )
        if tokens_in_window > t.max_tokens_per_window:
            return _mk(
                TripReason.TOKEN_RATE,
                f"{tokens_in_window:,} tokens in {t.window_seconds / 60:.0f} min "
                f"(cap {t.max_tokens_per_window:,})",
            )
        if calls_in_window > t.max_calls_per_window:
            return _mk(
                TripReason.CALL_RATE,
                f"{calls_in_window} model calls in {t.window_seconds / 60:.0f} min "
                f"(cap {t.max_calls_per_window})",
            )
        if st.repeated_error_count >= t.repeated_error_limit:
            return _mk(
                TripReason.REPEATED_ERROR,
                f"HTTP {st.last_error_code} repeated {st.repeated_error_count}x "
                f"(limit {t.repeated_error_limit})",
            )
        if st.stalled_samples >= t.no_progress_samples:
            return _mk(
                TripReason.NO_PROGRESS,
                f"{obs.context_tokens:,}-token context re-sent {st.stalled_samples}x "
                f"with no forward progress",
            )
        return None
