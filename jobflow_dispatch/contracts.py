"""Which mailbox messages are worth waking a semantic worker for.

Derived from observed traffic, not from the message vocabulary in the
abstract. Two facts drive the shape of this table:

1. **The destination inbox is authoritative.** A filename's trailing token is
   the SENDER (``SCORE_REQUEST_tracker`` is tracker *asking* for a score), so
   routing on it would wake the wrong worker. Routing keys on
   ``(message_type, destination)``.
2. **Most traffic is results, not work.** ``main/inbox`` carries ERROR,
   SCORE_RESULT, NOTIFICATION, SCORE_BATCH_SUMMARY and PIPELINE_UPDATE at
   high volume. None of it is actionable. Anything not explicitly listed
   here routes to nothing, so a new message type cannot start waking models
   by accident.

Activity IDs match ``activity_policy/policies.yaml`` so an activation, its
policy, and its telemetry all name the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: ``(message_type, destination)`` -> activity IDs to activate.
#: Both keys are upper/lower-normalised before lookup.
ROUTES: dict[tuple[str, str], tuple[str, ...]] = {
    ("SCORE_REQUEST", "matcher"): ("cron.jobflow.matcher",),
    ("TAILOR_REQUEST", "tailor"): ("jobflow.tailor.generate",),
    ("TAILOR_MODULE_REQUEST", "tailor"): ("jobflow.tailor.generate",),
    ("TAILOR_REVISION", "tailor"): ("jobflow.tailor.generate",),
    ("RESEARCH_REQUEST", "researcher"): ("cron.jobflow.researcher",),
    # The applier drains SUBMIT_REQUEST / SUBMIT_CONFIRM / QUESTION_ANSWER
    # (cron/README.md), but only SUBMIT_REQUEST has ever arrived: every message
    # in applier/inbox since July is one, while the other two have no producer
    # anywhere in the tree. Routing only the latter two left the lane
    # unreachable on BOTH paths — the reconciler derives its scanned types from
    # this table too — so the 2026-08-17 shadow gate recorded zero applier
    # dispatches and it read as an idle lane rather than a dead route.
    #
    # Waking on SUBMIT_REQUEST cannot submit anything: the payload is a
    # dry-run request (`idempotency_key: applier-dry-run:...`) and the applier
    # independently re-gates real submission behind `approved_for_submission`
    # plus a completed dry-run. The wake only moves its 3-hourly sweep earlier.
    ("SUBMIT_REQUEST", "applier"): ("cron.jobflow.applier",),
    ("SUBMIT_CONFIRM", "applier"): ("cron.jobflow.applier",),
    ("QUESTION_ANSWER", "applier"): ("cron.jobflow.applier",),
}


@dataclass(frozen=True)
class Activation:
    """One idempotent request to wake one activity for one message."""

    activity_id: str
    profile: str
    message_key: str
    correlation_id: str | None
    reason: str

    def __post_init__(self) -> None:
        for name in ("activity_id", "profile", "message_key", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if self.correlation_id is not None and (
            not isinstance(self.correlation_id, str) or not self.correlation_id.strip()
        ):
            raise ValueError("correlation_id must be non-empty when provided")


def message_key(raw: Any) -> str:
    """Canonical ledger key for one physical mailbox file.

    Two producers derive this independently: ``MailboxWatcher`` from
    ``Path.relative_to()`` (OS-native separators — backslashes on Windows) and
    the reconciler by assembling ``destination/inbox/name``. Without one
    normalisation the same file yields two ledger rows, and the reconciler
    re-dispatches work the subscriber already claimed — duplicate model calls
    with no error surfaced anywhere.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("message_key must be a non-empty string")
    return raw.strip().replace("\\", "/")


def _key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def route_mailbox(
    message_type: Any,
    destination: Any,
    payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return the activities a mailbox message should wake — usually none."""
    mtype = _key(message_type)
    dest = _key(destination)
    if mtype is None or dest is None:
        return ()
    return ROUTES.get((mtype.upper(), dest.lower()), ())
