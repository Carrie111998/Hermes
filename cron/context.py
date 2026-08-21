"""Small, task-local contracts shared by cron attestation and delivery.

The scheduler is the only producer of these values.  Keeping the values in
ContextVars lets a cron turn cross the agent worker thread without putting
execution identity in process-global environment state.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEDULED_ON_TIME = "SCHEDULED_ON_TIME"
PROVIDER_SCHEDULED = "PROVIDER_SCHEDULED"
OPERATOR_TRIGGERED = "OPERATOR_TRIGGERED"
RECOVERY_CATCHUP = "RECOVERY_CATCHUP"
UNKNOWN = "UNKNOWN"

INVOCATION_KINDS = frozenset({
    SCHEDULED_ON_TIME,
    PROVIDER_SCHEDULED,
    OPERATOR_TRIGGERED,
    RECOVERY_CATCHUP,
    UNKNOWN,
})
GRADUATION_ELIGIBLE_INVOCATION_KINDS = frozenset({
    SCHEDULED_ON_TIME,
    PROVIDER_SCHEDULED,
})
ON_TIME_GRACE_SECONDS = 5 * 60

# Identity capabilities are private object identities, not caller-controlled
# labels.  The jobs store consumes them while holding its atomic lock.
_BUILTIN_SCHEDULER_ADMISSION = object()
_AUTHENTICATED_PROVIDER_ADMISSION = object()
_RECOVERY_SCHEDULER_ADMISSION = object()

# The raw nonce is carried only by this private scheduler capability until it
# is installed in the execution ContextVar. Its repr intentionally omits the
# raw value so accidental diagnostics cannot disclose the token.
_CRON_ATTESTATION_CAPABILITY = object()


@dataclass(frozen=True, repr=False)
class _CronAttestationCapability:
    execution_id: str
    token: str
    digest: str
    capability: object = _CRON_ATTESTATION_CAPABILITY

    def __repr__(self) -> str:
        return (
            "_CronAttestationCapability("
            f"execution_id={self.execution_id!r}, digest={self.digest!r})"
        )

DELIVERY_STATUSES = frozenset({
    "NOT_ATTEMPTED",
    "SUPPRESSED",
    "NOT_CONFIGURED",
    "FAILED",
    "PROVIDER_ACCEPTED",
    "UNKNOWN",
})

_DELIVERY_DETAILS: ContextVar[dict[str, Any] | None] = ContextVar(
    "HERMES_CRON_DELIVERY_DETAILS", default=None
)


def normalize_invocation_kind(value: Any) -> str:
    """Normalize untrusted/internal labels to the closed origin vocabulary."""
    kind = str(value or "").strip().upper()
    return kind if kind in INVOCATION_KINDS else UNKNOWN


def normalize_delivery_status(value: Any) -> str:
    status = str(value or "").strip().upper()
    return status if status in DELIVERY_STATUSES else "UNKNOWN"


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_scheduled_fire(
    intended_fire_at: Any,
    *,
    now: Any = None,
    provider: bool = False,
) -> str:
    """Classify a claimed scheduled instant with the fixed five-minute grace."""
    intended = _parse_timestamp(intended_fire_at)
    current = _parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    if intended is None or current is None:
        return UNKNOWN
    age = (current - intended).total_seconds()
    if age < 0:
        return UNKNOWN
    if age <= ON_TIME_GRACE_SECONDS:
        return PROVIDER_SCHEDULED if provider else SCHEDULED_ON_TIME
    return RECOVERY_CATCHUP


def canonical_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value), sort_keys=True, separators=(",", ":"))


def file_attestation(path: Any) -> tuple[str | None, str | None]:
    """Return the exact path spelling and SHA-256 for a saved regular file."""
    if path is None:
        return None, None
    candidate = Path(path)
    try:
        if not candidate.is_file():
            return str(candidate), None
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return str(candidate), digest.hexdigest()
    except OSError:
        return str(candidate), None


@contextmanager
def cron_execution_context(
    execution_id: Any,
    invocation_kind: Any,
    attestation: Any = None,
) -> Iterator[None]:
    """Bind one scheduler attestation for this turn and restore prior state."""
    from gateway.session_context import bind_cron_execution_context

    attestation_token = None
    if (
        isinstance(attestation, _CronAttestationCapability)
        and attestation.capability is _CRON_ATTESTATION_CAPABILITY
        and attestation.execution_id == str(execution_id or "")
    ):
        attestation_token = attestation.token
    tokens = bind_cron_execution_context(
        str(execution_id or ""),
        normalize_invocation_kind(invocation_kind),
        attestation_token,
    )
    try:
        yield
    finally:
        from gateway.session_context import reset_cron_execution_context

        reset_cron_execution_context(tokens)


@contextmanager
def delivery_details() -> Iterator[dict[str, Any]]:
    """Collect receipt metadata without changing ``_deliver_result``'s API."""
    details: dict[str, Any] = {}
    token = _DELIVERY_DETAILS.set(details)
    try:
        yield details
    finally:
        _DELIVERY_DETAILS.reset(token)


def set_delivery_detail(name: str, value: Any) -> None:
    details = _DELIVERY_DETAILS.get()
    if details is not None and value is not None:
        details[name] = value
