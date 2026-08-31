"""Normalized subscription-usage model shared by every provider.

One shape for "how much of my plan is left", across providers that measure in
completely different units: Anthropic and Codex report **percent** of a rolling
window, OpenRouter reports **dollars**, a seat-metered plan reports **counts**
of interactions, MiniMax reports **tokens**.

Two rules make that work:

1. **The unit is explicit.** A window carries ``unit``, and surfaces pick their
   rendering from it. Forcing everything to a percentage invents precision that
   the provider never gave.
2. **The numbers are the provider's own.** ``used`` / ``limit`` / ``remaining``
   are stored exactly as reported, and a percentage is *derived* only when the
   arithmetic is unambiguous. This is why a provider whose field names are
   unclear (Kimi labels a field ``used`` that behaves like "remaining") can be
   surfaced honestly instead of guessed at.

Failure is a typed state, never free text: at community scale an untranslatable
string is both an i18n hole and an unactionable bug report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

# ── Units ──────────────────────────────────────────────────────────────────
UNIT_PERCENT = "percent"
UNIT_CURRENCY = "currency"
UNIT_COUNT = "count"
UNIT_TOKENS = "tokens"

# ── States ─────────────────────────────────────────────────────────────────
# `no_usage_endpoint` is NOT an error: it is the normal answer for most of the
# provider registry, and surfaces should render it quietly or not at all.
STATE_OK = "ok"
STATE_NOT_AUTHENTICATED = "not_authenticated"
STATE_NO_USAGE_ENDPOINT = "no_usage_endpoint"
STATE_UNAUTHORIZED = "unauthorized"
STATE_RATE_LIMITED = "rate_limited"
STATE_NETWORK_ERROR = "network_error"
STATE_PARSE_ERROR = "parse_error"

def to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a provider field to Decimal, or None when it isn't a number.

    Providers send numbers as strings (Kimi: ``"100"``), floats, or ints. None
    and empty string mean "not reported" — distinct from zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None
    return None


def to_datetime(value: Any) -> Optional[datetime]:
    """Coerce a provider timestamp to an aware datetime, or None.

    Providers send ISO-8601 with a ``Z`` suffix, ISO with an offset, or epoch
    seconds. Naive values are read as UTC — every endpoint here reports UTC.
    Shared with the plugins (and re-exported for third-party ones) so a new
    provider does not hand-roll a fifth copy that forgets one of those forms.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None

    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decimal_str(value: Optional[Decimal]) -> Optional[str]:
    """Serialize money/counts as strings — JSON floats lose cents."""
    return None if value is None else format(value, "f")


@dataclass(frozen=True)
class UsageWindow:
    """One metered window: a rolling session, a billing month, a credit pool.

    ``label`` is a stable identifier the UI translates (``"5h"``, ``"weekly"``,
    ``"monthly"``, ``"credits"``, or a provider-specific slug); ``detail`` is
    free text for anything that has no field of its own.
    """

    label: str
    unit: str = UNIT_PERCENT
    used: Optional[Decimal] = None
    limit: Optional[Decimal] = None
    remaining: Optional[Decimal] = None
    reset_at: Optional[datetime] = None
    currency: Optional[str] = None
    detail: Optional[str] = None

    @property
    def used_percent(self) -> Optional[float]:
        """Percent consumed, or None when the numbers don't support one.

        Deliberately conservative. A window that reports only ``remaining``
        with no ``limit`` has no percentage — returning a made-up one would
        paint a bar that means nothing.
        """
        if self.unit == UNIT_PERCENT and self.used is not None:
            return max(0.0, min(100.0, float(self.used)))

        limit = self.limit
        if limit is None or limit <= 0:
            return None

        used = self.used
        if used is None and self.remaining is not None:
            used = limit - self.remaining
        if used is None:
            return None

        return max(0.0, min(100.0, float(used / limit * 100)))

    def to_payload(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "unit": self.unit,
            "used": _decimal_str(self.used),
            "limit": _decimal_str(self.limit),
            "remaining": _decimal_str(self.remaining),
            "used_percent": self.used_percent,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "currency": self.currency,
            "detail": self.detail,
        }


def from_account_snapshot(
    snapshot: Any,
    *,
    provider: str,
    display_name: str = "",
) -> Optional["ProviderUsage"]:
    """Adapt a legacy ``agent.account_usage.AccountUsageSnapshot``.

    Anthropic, Codex and OpenRouter already had working fetchers behind the
    ``/usage`` slash command. Rather than move them — which would mean a lossy
    percent-only compat shim under the CLI/TUI/gateway renderers that still
    read the old shape — the plugins wrap them. Percent → percent is lossless
    in this direction, so nothing is invented; ``details`` that have no field
    of their own ride along as a window's ``detail``.
    """
    if snapshot is None:
        return None

    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        windows.append(
            UsageWindow(
                label=str(getattr(window, "label", "") or ""),
                unit=UNIT_PERCENT,
                used=to_decimal(getattr(window, "used_percent", None)),
                limit=Decimal(100),
                reset_at=getattr(window, "reset_at", None),
                detail=getattr(window, "detail", None),
            )
        )

    details = tuple(getattr(snapshot, "details", ()) or ())
    if details and not windows:
        windows.append(UsageWindow(label="plan", unit=UNIT_COUNT, detail=" · ".join(details)))

    return ProviderUsage(
        provider=provider,
        display_name=display_name or getattr(snapshot, "title", "") or provider,
        plan=getattr(snapshot, "plan", None),
        windows=tuple(windows),
        state=STATE_OK if windows else STATE_NO_USAGE_ENDPOINT,
        fetched_at=getattr(snapshot, "fetched_at", None),
    )


@dataclass(frozen=True)
class ProviderUsage:
    """One provider's plan state, or the typed reason there isn't one."""

    provider: str
    display_name: str = ""
    plan: Optional[str] = None
    windows: Tuple[UsageWindow, ...] = ()
    state: str = STATE_OK
    message: Optional[str] = None
    fetched_at: Optional[datetime] = None
    # Served from cache past its TTL while a refresh runs behind it, so the
    # panel can paint instantly and mark the figure as catching up.
    stale: bool = False

    @property
    def available(self) -> bool:
        return self.state == STATE_OK and bool(self.windows)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name or self.provider,
            "plan": self.plan,
            "windows": [window.to_payload() for window in self.windows],
            "state": self.state,
            "message": self.message,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "stale": self.stale,
            "available": self.available,
        }
