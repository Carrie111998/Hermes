"""Pure outcome normalization for notification routing and presentation.

Outcome is deliberately separate from attention and destination.  This module
interprets stable evidence on an immutable Event and returns a verdict; it does
not route, deliver, mutate, or inspect external state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from events.schema import Event, EventType, Priority


class OutcomeState(Enum):
    FAILED = "failed"
    DEGRADED = "degraded"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    RECOVERED = "recovered"
    NO_WORK = "no_work"
    UNKNOWN = "unknown"


class FailureKind(Enum):
    TIMEOUT = "timeout"
    EXCEPTION = "exception"
    CONNECTION = "connection"
    VALIDATION = "validation"
    BROWSER = "browser"
    OTHER = "other"


@dataclass(frozen=True)
class OutcomeEvidence:
    code: str
    path: str
    value: object


@dataclass(frozen=True)
class OutcomeVerdict:
    state: OutcomeState
    priority_floor: Priority | None
    evidence: tuple[OutcomeEvidence, ...]
    failure_kind: FailureKind | None


_FAILURE_EVENT_TYPES = frozenset(
    {
        EventType.CRON_FAILED,
        EventType.CRON_FAILED_CONSECUTIVE,
        EventType.APPLICATION_FAILED,
        EventType.AGENT_ERROR,
        EventType.AGENT_FAILURE_CLUSTER,
        EventType.DEVFLOW_BUILD_FAILED,
        EventType.NOTIFICATION_FAILED,
        EventType.GATEWAY_STOPPED,
        EventType.AGENT_LOOP_FAULT,
        # Monodirectional bad news: these types exist ONLY as failures, so
        # the verdict belongs to the type, not to a payload field.
        #
        # CONTAINER_CRASH_LOOP fires from one call site -- watchdog_sweep.py
        # :854, `for alarm in restart_alarms` -- and has no recovery variant
        # (WATCHDOG_RECOVERED covers that separately). Deliberately NOT keyed
        # on payload.tray_state, which is very often "healthy" while the
        # alert is real: laptop-monitor's churn verdict is a one-pass
        # RestartCount delta that self-clears 600s after the last restart, so
        # a container that restarted 264 times in a morning reads green
        # (formatting.container_crash_loop_body says so at length). A
        # tray_state rule would call a real crash loop healthy for exactly
        # the cases the alert exists to catch.
        #
        # DEVFLOW_DEPLOY_FAILED sat unclassified while its sibling
        # DEVFLOW_BUILD_FAILED (above) was classified -- an omission, not a
        # decision. Both were reaching the phone headed "UNKNOWN".
        EventType.CONTAINER_CRASH_LOOP,
        EventType.DEVFLOW_DEPLOY_FAILED,
        # Security-posture losses, same monodirectional argument. Both are
        # ACT-class with wa=immediate -- they break quiet hours to wake a
        # human -- yet the header could not say whether they were good or bad.
        #
        # Type-level membership is only SAFE for a type with no recovery
        # variant, because `failed` wins over `recovery` in the precedence
        # order below: a bidirectional member would render its own recoveries
        # as red failures. Both were checked at every producer.
        # CREDENTIAL_LOSS: watchdog_sweep emits only the healthy -> down/error
        # edge (_CREDENTIAL_LOSS_BAD_STATES + a before=="healthy" guard), and
        # devflow_pr_build_poller._check_auth_transition states "Recovery is
        # logged, not emitted". SECRET_DETECTED is "a secret being *found*"
        # (schema.py:207) -- there is no secret-un-found event.
        EventType.SECRET_DETECTED,
        EventType.CREDENTIAL_LOSS,
        # The conformance canary fires when drift IS found and has no
        # drift-resolved variant -- contrast CODE_DRIFT, which does have one
        # (status=="resolved") and therefore stays payload-driven. Its sibling
        # AGENT_LOOP_FAULT, introduced in the same schema comment block, was
        # already here; this was the same omission as DEVFLOW_DEPLOY_FAILED.
        EventType.BACKEND_CONTRACT_DRIFT,
    }
)

_DEGRADED_EVENT_TYPES = frozenset(
    {
        EventType.CRON_STALE,
        EventType.RESOURCE_PRESSURE,
        EventType.WATCHDOG_SELF_DEGRADED,
        # BOOT_SUMMARY exists only when the boot had trouble --
        # laptop-start.ps1 Get-BootSummaryPayload returns $null unless
        # state=='failed' OR a step failed OR there is an error-severity
        # anomaly. So DEGRADED is its FLOOR, not its verdict; the
        # failed-step rule below escalates it to FAILED. Membership here
        # rather than a bare payload rule means a boot that alerted for
        # anomalies alone still reads as trouble.
        EventType.BOOT_SUMMARY,
    }
)

_PENDING_EVENT_TYPES = frozenset(
    {
        EventType.APPROVAL_REQUEST,
        EventType.APPLICATION_READY,
        EventType.APPLICATION_BLOCKED,
        EventType.APPLY_PACKET,
        EventType.DEVFLOW_APPROVAL_REQUESTED,
        EventType.DEVFLOW_PR_REVIEW_REQUESTED,
        EventType.TRACKER_PARTIAL_BACKLOG,
        # Good news that still needs a human. These were the last ACT-class
        # types with no verdict, and they are the same shape as the seven
        # above: ACT because Diego must DO something, not because anything
        # broke. PENDING says "awaiting you", which is the honest reading --
        # SUCCEEDED would claim an outcome that has not happened yet (an
        # interview is offered, not won) and any failure state would be
        # plainly wrong. Third instance of the same sibling asymmetry that
        # left DEVFLOW_DEPLOY_FAILED and BACKEND_CONTRACT_DRIFT unclassified.
        #
        # NOTE this changes the LABEL only: marker_for_verdict renders PENDING
        # and UNKNOWN with the same amber dot, so the header goes from
        # "UNKNOWN INTERVIEW SIGNAL" to "PENDING INTERVIEW SIGNAL" and the
        # colour is unchanged. PENDING also carries priority_floor=None, so
        # routing, priority and escalation are untouched.
        EventType.INTERVIEW_SIGNAL,
        EventType.OFFER_SIGNAL,
        EventType.FOLLOWUP_DUE,
    }
)

_SUCCESS_EVENT_TYPES = frozenset(
    {
        EventType.CRON_COMPLETED,
        EventType.APPLICATION_SUBMITTED,
        EventType.DEVFLOW_RUN_COMPLETED,
        EventType.DEVFLOW_BUILD_SUCCEEDED,
        EventType.NOTIFICATION_DELIVERED,
    }
)

_RECOVERY_EVENT_TYPES = frozenset(
    {
        EventType.GATEWAY_STARTED,
        EventType.WATCHDOG_RECOVERED,
    }
)

_FAILED_VALUES = frozenset(
    {
        "error",
        "failed",
        "failure",
        "fatal",
        "timed_out",
        "timeout",
        "down",
        "unhealthy",
        # MODEL_RATE_LIMITED outcomes meaning no model is left to call. Both
        # are FAILED; formatting.py words them apart only because the REMEDY
        # differs (chain_exhausted = every configured alternative is also
        # down; no_fallback = none was ever configured).
        #
        # VALUE-level on purpose. MODEL_RATE_LIMITED must NOT join
        # _FAILURE_EVENT_TYPES: it is bidirectional and already partly
        # classified -- outcome=="recovered" resolves to RECOVERED via
        # _RECOVERED_VALUES (5 of 44 real events), and `failed` wins over
        # recovery in the precedence order below, so a type-level rule would
        # render those genuine recoveries as red failures.
        "chain_exhausted",
        "no_fallback",
    }
)
# "diverted" is the successful mitigation: the fallback model took the call,
# so the system is running-but-worse rather than broken. Calling it FAILED
# would cry wolf on the very path that saved the request.
_DEGRADED_VALUES = frozenset({"degraded", "partial", "impaired", "diverted"})
_PENDING_VALUES = frozenset(
    {"pending", "awaiting_approval", "awaiting_decision", "blocked"}
)
_SUCCESS_VALUES = frozenset(
    {"ok", "passed", "success", "succeeded", "complete", "completed", "healthy", "up"}
)
_RECOVERED_VALUES = frozenset({"recovered", "resolved", "restored"})
_UNHEALTHY_VALUES = frozenset(
    {"down", "unhealthy", "degraded", "failed", "failure", "error"}
)
_HEALTHY_VALUES = frozenset({"up", "healthy", "recovered", "resolved", "restored"})

_VALUE_FIELDS = ("status", "reason", "outcome", "result", "conclusion", "message_type")
_PREVIOUS_FIELDS = ("before", "previous_status", "previous", "from")

_CRON_FAILURE_RE = re.compile(
    r"(?:\b(?:RED|FAILED|FAILURE|CRITICAL)\b"
    r"|\berrors?=[1-9]\d*\b"
    r"|\bfirst error\b"
    r"|\brc=[1-9]\d*\b"
    r"|\bexit(?:_code)?=(?:-[1-9]\d*|[1-9]\d*)\b)"
)
_CRON_HISTORICAL_INVENTORY_RE = re.compile(
    r"\bby_state\s*=\s*\{[^{}]*\}",
    re.IGNORECASE,
)


def legacy_cron_output_is_failure(output_summary: object, *, limit: int = 400) -> bool:
    """Return whether legacy cron prose proves the current run failed.

    Historical state inventories are context, not a verdict for this run. Strip
    their flat JSON/Python-dict segment before applying the compatibility
    markers; structured event fields remain authoritative elsewhere.
    """
    text = str(output_summary or "")[:limit]
    current_run_text = _CRON_HISTORICAL_INVENTORY_RE.sub("", text)
    return bool(_CRON_FAILURE_RE.search(current_run_text))




def _normalized(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _exit_code_failure(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return False


def _evidence(code: str, path: str, value: object) -> OutcomeEvidence:
    return OutcomeEvidence(code=code, path=path, value=value)


def _failure_kind(payload: dict[str, Any], evidence: list[OutcomeEvidence]) -> FailureKind:
    values = [item.value for item in evidence]
    values.extend(
        payload.get(field)
        for field in ("error", "message", "detail", "phase", "failure_type", "error_code")
        if payload.get(field) is not None
    )
    text = " ".join(str(value) for value in values).lower()
    evidence_codes = {item.code for item in evidence}
    if "timeout" in evidence_codes or re.search(
        r"timed?[ _-]?out|timeout|deadline|\brc=124\b", text
    ):
        return FailureKind.TIMEOUT
    if re.search(r"connection|connect(?:ion)? refused|econnrefused|network|socket", text):
        return FailureKind.CONNECTION
    if re.search(r"validation|invalid|duplicate|uniqueness|constraint", text):
        return FailureKind.VALIDATION
    if re.search(r"browser|playwright|captcha|navigation|workday|greenhouse", text):
        return FailureKind.BROWSER
    if re.search(r"exception|traceback|stack trace", text):
        return FailureKind.EXCEPTION
    return FailureKind.OTHER


def _is_recovery_transition(event: Event, payload: dict[str, Any]) -> OutcomeEvidence | None:
    if event.event_type in _RECOVERY_EVENT_TYPES:
        return _evidence("recovery_event_type", "event.event_type", event.event_type.type_string)

    if event.event_type is EventType.WATCHDOG_BURST:
        # A burst carries state in a ``transitions`` list, not a top-level
        # ``after``/``status``. When every real change is a recovery (none
        # failing; "unknown" == probe skipped, not a verdict) the sweep is
        # good news and must read green, mirroring formatting.watchdog_burst_body.
        transitions = [t for t in (payload.get("transitions") or []) if isinstance(t, dict)]
        failing = [t for t in transitions if t.get("after") not in ("healthy", "unknown")]
        recovered = [t for t in transitions if t.get("after") == "healthy"]
        if recovered and not failing:
            return _evidence("watchdog_burst_recovered", "payload.transitions", len(recovered))
        return None

    status = _normalized(payload.get("status", ""))
    after = _normalized(payload.get("after", ""))
    previous = next(
        (_normalized(payload.get(field)) for field in _PREVIOUS_FIELDS if payload.get(field) is not None),
        "",
    )
    current = after or status

    if current in _HEALTHY_VALUES and previous in _UNHEALTHY_VALUES:
        path = "payload.after" if after else "payload.status"
        return _evidence("healthy_transition", path, payload.get("after") if after else payload.get("status"))
    if event.event_type is EventType.GATEWAY_HEALTH and status == "up":
        return _evidence("gateway_recovered", "payload.status", payload.get("status"))
    if event.event_type is EventType.CODE_DRIFT and status == "resolved":
        return _evidence("drift_resolved", "payload.status", payload.get("status"))
    if event.event_type is EventType.WATCHDOG_PROBE_TRANSITION and after == "healthy":
        return _evidence("probe_recovered", "payload.after", payload.get("after"))
    return None


def evaluate_outcome(event: Event) -> OutcomeVerdict:
    """Return a deterministic failure-wins verdict for *event*.

    All recognized evidence is retained, but the first non-empty precedence
    class determines the state: failed, degraded, pending, recovered, success,
    then unknown.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    failed: list[OutcomeEvidence] = []
    degraded: list[OutcomeEvidence] = []
    pending: list[OutcomeEvidence] = []
    succeeded: list[OutcomeEvidence] = []

    if event.event_type in _FAILURE_EVENT_TYPES:
        failed.append(
            _evidence("failure_event_type", "event.event_type", event.event_type.type_string)
        )
    if event.event_type in _DEGRADED_EVENT_TYPES:
        degraded.append(
            _evidence("degraded_event_type", "event.event_type", event.event_type.type_string)
        )
    if event.event_type in _PENDING_EVENT_TYPES:
        pending.append(
            _evidence("pending_event_type", "event.event_type", event.event_type.type_string)
        )
    if event.event_type in _SUCCESS_EVENT_TYPES:
        succeeded.append(
            _evidence("success_event_type", "event.event_type", event.event_type.type_string)
        )

    if _exit_code_failure(payload.get("exit_code")):
        failed.append(_evidence("nonzero_exit_code", "payload.exit_code", payload["exit_code"]))
    counters = payload.get("counters")
    if isinstance(counters, dict) and _exit_code_failure(counters.get("exit_code")):
        failed.append(
            _evidence(
                "nonzero_exit_code",
                "payload.counters.exit_code",
                counters["exit_code"],
            )
        )

    if payload.get("timeout") is True:
        failed.append(_evidence("timeout", "payload.timeout", True))

    for field in _VALUE_FIELDS:
        if field not in payload:
            continue
        value = payload[field]
        normalized = _normalized(value)
        path = f"payload.{field}"
        if normalized in _FAILED_VALUES:
            failed.append(_evidence("explicit_failure", path, value))
        elif normalized in _DEGRADED_VALUES:
            degraded.append(_evidence("explicit_degradation", path, value))
        elif normalized in _PENDING_VALUES:
            pending.append(_evidence("explicit_pending", path, value))
        elif normalized in _RECOVERED_VALUES:
            succeeded.append(_evidence("explicit_recovery", path, value))
        elif normalized == "no_work":
            succeeded.append(_evidence("explicit_no_work", path, value))
        elif normalized in _SUCCESS_VALUES:
            succeeded.append(_evidence("explicit_success", path, value))

    # A transition whose CURRENT state is unhealthy is a FAILURE -- for every
    # event type, not one more per-type branch. This file already read
    # `payload.after`, but only for the single value "healthy" on
    # WATCHDOG_PROBE_TRANSITION (_is_recovery_transition). The mirror image
    # never existed, so a probe going healthy -> down produced no evidence at
    # all and fell through to UNKNOWN. Observed live 2026-08-19: a real
    # critical ":8642 down" alert reached the phone headed "UNKNOWN SYSTEM
    # HEALTH ALERT" -- the same fall-through already fixed for burst
    # recoveries, in the direction that actually matters.
    #
    # Classified through the same value sets `status` uses, NOT through
    # _UNHEALTHY_VALUES: that set contains "degraded", so keying on it would
    # over-escalate a partial outage into a hard failure. "unknown" is in
    # neither set and stays UNKNOWN, which is correct -- it means the probe
    # was SKIPPED, the reading watchdog_burst_body already applies.
    #
    # `status` is deliberately not consulted here; _VALUE_FIELDS above
    # already covers it, and re-reading it would double-count the evidence.
    if "after" in payload:
        _after = _normalized(payload.get("after"))
        if _after in _FAILED_VALUES:
            failed.append(_evidence("transition_failed", "payload.after", payload.get("after")))
        elif _after in _DEGRADED_VALUES:
            degraded.append(_evidence("transition_degraded", "payload.after", payload.get("after")))

    # BOOT_SUMMARY severity comes from the FAILED STEP COUNT, never from
    # `state`. `state` reports whether the boot SEQUENCE completed, not
    # whether it was healthy: the real 2026-08-19 event carries
    # state=="done" alongside 66 error anomalies, and the producer would not
    # have emitted at all had the boot been clean. Reading "done" as success
    # -- the obvious move, and the one first proposed when this was
    # triaged -- would paint that boot green. state=='failed' is honoured
    # too, since the producer's own gate treats it as sufficient.
    if event.event_type is EventType.BOOT_SUMMARY:
        _failed_steps = payload.get("failed")
        if isinstance(_failed_steps, bool):
            _failed_steps = None
        if isinstance(_failed_steps, (int, float)) and _failed_steps > 0:
            failed.append(
                _evidence("boot_steps_failed", "payload.failed", _failed_steps)
            )
        elif _normalized(payload.get("state", "")) == "failed":
            failed.append(
                _evidence("boot_state_failed", "payload.state", payload.get("state"))
            )

    if event.event_type is EventType.CRON_COMPLETED:
        output_summary = str(payload.get("output_summary", ""))[:400]
        if legacy_cron_output_is_failure(output_summary):
            failed.append(
                _evidence("legacy_cron_failure", "payload.output_summary", output_summary)
            )

    if failed:
        return OutcomeVerdict(
            state=OutcomeState.FAILED,
            priority_floor=Priority.HIGH,
            evidence=tuple(failed + degraded + pending + succeeded),
            failure_kind=_failure_kind(payload, failed),
        )

    if degraded:
        return OutcomeVerdict(
            state=OutcomeState.DEGRADED,
            priority_floor=Priority.HIGH,
            evidence=tuple(degraded + pending + succeeded),
            failure_kind=None,
        )

    if pending or payload.get("action_required") is True or payload.get("decision_required") is True:
        if payload.get("action_required") is True:
            pending.append(_evidence("action_required", "payload.action_required", True))
        if payload.get("decision_required") is True:
            pending.append(_evidence("decision_required", "payload.decision_required", True))
        return OutcomeVerdict(
            state=OutcomeState.PENDING,
            priority_floor=None,
            evidence=tuple(pending + succeeded),
            failure_kind=None,
        )

    recovery = _is_recovery_transition(event, payload)
    if recovery is not None:
        return OutcomeVerdict(
            state=OutcomeState.RECOVERED,
            priority_floor=None,
            evidence=(recovery, *succeeded),
            failure_kind=None,
        )

    if succeeded:
        recovered = [item for item in succeeded if item.code == "explicit_recovery"]
        no_work = [item for item in succeeded if item.code == "explicit_no_work"]
        if no_work:
            state = OutcomeState.NO_WORK
        elif recovered:
            state = OutcomeState.RECOVERED
        else:
            state = OutcomeState.SUCCEEDED
        return OutcomeVerdict(
            state=state,
            priority_floor=None,
            evidence=tuple(succeeded),
            failure_kind=None,
        )

    return OutcomeVerdict(
        state=OutcomeState.UNKNOWN,
        priority_floor=None,
        evidence=(
            _evidence("no_recognized_evidence", "event", event.event_type.type_string),
        ),
        failure_kind=None,
    )


def marker_for_verdict(verdict: OutcomeVerdict, effective_priority: Priority) -> str:
    """Return the non-ambiguous status marker for a computed verdict."""
    if verdict.state is OutcomeState.FAILED:
        return "🔴" if effective_priority is Priority.CRITICAL else "🟠"
    if verdict.state is OutcomeState.DEGRADED:
        return "🟠"
    if verdict.state in {OutcomeState.SUCCEEDED, OutcomeState.RECOVERED, OutcomeState.NO_WORK}:
        return "🟢"
    return "🟡"
