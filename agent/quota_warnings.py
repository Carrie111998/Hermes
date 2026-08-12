"""Configurable quota warning engine (Hermes agent, issue #6567).

Builds on :mod:`agent.account_usage` — which already fetches per-provider
account-usage snapshots (``AccountUsageSnapshot`` / ``AccountUsageWindow``) —
and adds:

* configurable percentage thresholds (warning / strong / critical),
* a single highest-level warning line per snapshot,
* a *pre-turn* suppression gate (``quota.suppress_warnings``) for the
  steady-state turns, and a startup variant that always fires,
* a small module-level TTL cache so the pre-turn probe doesn't hit the
  provider network on every turn.

All threshold logic is pure: ``get_quota_warnings`` takes a snapshot +
thresholds and returns lines.  The config-aware wrappers
(``quota_warning_lines`` / ``startup_warning_lines``) are thin shims over it
so the pure function stays easily unit-testable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from time import monotonic
from typing import Any, Optional

# Re-exported so callers (and the test seam) bind ``fetch_account_usage`` on
# *this* module — tests patch ``agent.quota_warnings.fetch_account_usage``.
from agent.account_usage import (
    AccountUsageSnapshot,
    AccountUsageWindow,
    _format_reset,
    fetch_account_usage,
)

# Design defaults — mirrored by config_defaults.py (Task A owns that file).
_DEFAULT_WARNING = 80.0
_DEFAULT_STRONG = 90.0
_DEFAULT_CRITICAL = 95.0

# Pre-turn probe cadence (issue #6567 design review): reuse a freshly fetched
# snapshot for up to 10 minutes so the warning probe doesn't hammer the
# provider network on every turn.
_DEFAULT_CACHE_TTL = 600.0


@dataclass(frozen=True)
class QuotaThresholds:
    """Percentage thresholds for the quota warning ladder.

    Ordered low→high; a snapshot's peak utilization is compared against all
    three with ``>=`` and mapped to the single highest level it reaches.
    """

    warning: float
    strong: float
    critical: float


def _quota_section(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return the ``quota`` sub-dict from a config dict, or ``{}`` on mismatch."""
    if not isinstance(config, dict):
        return {}
    section = config.get("quota")
    if not isinstance(section, dict):
        return {}
    return section


def _coerce_threshold(section: dict[str, Any], key: str, default: float) -> float:
    """Read a threshold from the quota section, falling back to ``default``.

    Missing/non-numeric values (str "abc", None, etc.) fall back to the
    default.  Real numbers (int/float, including numeric strings) are coerced
    via ``float()``.
    """
    raw: Any = section.get(key)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def quota_thresholds(config: Optional[dict] = None) -> QuotaThresholds:
    """Build :class:`QuotaThresholds` from a config dict.

    Reads ``quota.warning_threshold`` / ``quota.strong_threshold`` /
    ``quota.critical_threshold``; missing or non-numeric values fall back to
    the design defaults 80 / 90 / 95 (``float``-coerced).
    """
    section = _quota_section(config)
    return QuotaThresholds(
        warning=_coerce_threshold(section, "warning_threshold", _DEFAULT_WARNING),
        strong=_coerce_threshold(section, "strong_threshold", _DEFAULT_STRONG),
        critical=_coerce_threshold(section, "critical_threshold", _DEFAULT_CRITICAL),
    )


def _peak_window(snapshot: AccountUsageSnapshot) -> Optional[AccountUsageWindow]:
    """The window with the highest finite ``used_percent``, or ``None``."""
    peak: Optional[AccountUsageWindow] = None
    peak_pct: Optional[float] = None
    for window in snapshot.windows:
        if window.used_percent is None:
            continue
        pct = float(window.used_percent)
        if peak_pct is None or pct > peak_pct:
            peak_pct = pct
            peak = window
    return peak


def get_quota_warnings(
    snapshot: Optional[AccountUsageSnapshot],
    *,
    thresholds: QuotaThresholds,
) -> list[str]:
    """Pure threshold evaluation — one line for the highest level reached.

    * ``None`` snapshot, an unavailable snapshot, or a snapshot with no
      usable ``used_percent`` window → ``[]``.
    * Windows with ``None`` ``used_percent`` are skipped; the *maximum*
      finite percent across the remaining windows drives the comparison.
    * Uses ``>=`` comparisons against the thresholds (80/90/95 by default),
      so a value exactly on a boundary trips that level.

    If the peak window carries a ``reset_at``, a `` — resets <…>`` suffix
    is appended using :func:`agent.account_usage._format_reset`.
    """
    if snapshot is None or not snapshot.available:
        return []

    peak = _peak_window(snapshot)
    if peak is None or peak.used_percent is None:
        return []

    pct = float(peak.used_percent)
    warning, strong, critical = thresholds.warning, thresholds.strong, thresholds.critical

    if pct >= critical:
        line = f"  🚨 Critical quota warning: {pct:.0f}% used (threshold {critical:.0f}%)"
    elif pct >= strong:
        line = f"  ⚠⚠ Strong quota warning: {pct:.0f}% used (threshold {strong:.0f}%)"
    elif pct >= warning:
        line = f"  ⚠ Quota warning: {pct:.0f}% used (threshold {warning:.0f}%)"
    else:
        return []

    if peak.reset_at is not None:
        line += f" — resets {_format_reset(peak.reset_at)}"
    return [line]


def quota_warning_lines(
    snapshot: Optional[AccountUsageSnapshot],
    config: Optional[dict] = None,
) -> list[str]:
    """Pre-turn quota warning lines, honoring ``quota.suppress_warnings``.

    Returns ``[]`` when suppression is enabled (the pre-turn probe is silenced
    for this turn — issue #6567).  Otherwise delegates to
    :func:`get_quota_warnings` with thresholds parsed from ``config``.
    """
    if _quota_section(config).get("suppress_warnings"):
        return []
    return get_quota_warnings(snapshot, thresholds=quota_thresholds(config))


def startup_warning_lines(
    snapshot: Optional[AccountUsageSnapshot],
    config: Optional[dict] = None,
) -> list[str]:
    """Startup quota warning lines — always shown, ignoring suppression.

    Per issue #6567 the *first* probe of a session must always surface a
    critical warning to the user even when ``quota.suppress_warnings`` is set,
    so the user is never blinded at session start.
    """
    return get_quota_warnings(snapshot, thresholds=quota_thresholds(config))


# ── TTL cache ─────────────────────────────────────────────────────────────


# Cache: {(provider, base_url): (timestamp, snapshot)}
_quota_cache: dict[tuple, tuple[float, AccountUsageSnapshot]] = {}
_quota_cache_lock = threading.Lock()


def fetch_quota_snapshot(
    provider: Optional[str],
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_age: float = _DEFAULT_CACHE_TTL,
) -> Optional[AccountUsageSnapshot]:
    """TTL-cached wrapper around :func:`agent.account_usage.fetch_account_usage`.

    The cache key is ``(provider, base_url)`` (``api_key`` is intentionally
    excluded — the same account is reused across turns).  A cached snapshot
    younger than ``max_age`` seconds (default 600s = 10 min) is returned
    without hitting the network.

    Fail-open: ``fetch_account_usage`` exceptions return ``None`` and are
    *not* cached, so the next call retries rather than serving a stale failure.
    Likewise a ``None`` snapshot (unsupported provider / no creds) is not
    cached.
    """
    key = (provider, base_url)
    now = monotonic()
    with _quota_cache_lock:
        entry = _quota_cache.get(key)
        if entry is not None:
            cached_ts, cached_snapshot = entry
            if (now - cached_ts) < max_age:
                return cached_snapshot

    # Network I/O happens outside the lock so a slow provider can't stall
    # threads operating on a different cache key.
    try:
        snapshot = fetch_account_usage(provider, base_url=base_url, api_key=api_key)
    except Exception:
        return None

    if snapshot is not None:
        with _quota_cache_lock:
            _quota_cache[key] = (monotonic(), snapshot)
    return snapshot


def clear_quota_cache() -> None:
    """Empty the quota TTL cache.

    Called at REPL session start so each fresh session re-probes the provider
    instead of reusing the warm cache from a previous session (design-review
    requirement).
    """
    with _quota_cache_lock:
        _quota_cache.clear()
