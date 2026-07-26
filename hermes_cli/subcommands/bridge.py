"""CLI visibility and explicit health probes for the ChatGPT Pro bridge."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any

from hermes_cli.cost import (
    bridge_config,
    bridge_state,
    gate_integration,
    turns_ledger,
)


_PROBE_PROMPT = "Reply with exactly the two words: bridge ok"
_DEFAULT_PROBE_MODEL = "gpt-5.6-sol"
_EXIT_CODES = {
    "healthy": 0,
    "degraded": 1,
    "exhausted": 2,
    "error": 3,
}


@dataclass(frozen=True)
class ProbeResult:
    """Compact outcome returned by the shared manual/nightly probe runner."""

    outcome: str
    exit_code: int
    turn_outcome: str
    response_text: str | None
    model_observed: str | None
    latency_ms: int
    error_class: str | None = None
    error_message: str | None = None


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", "") if message is not None else ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "".join(parts).strip()
    return str(content or "").strip()


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    text = str(exc).lower()
    return (
        "429" in text
        or "rate limit" in text
        or "rate_limit" in text
        or "quota" in text
    )


def _perform_pro_bridge_call() -> Any:
    """Make the single real bridge request used only by confirmed CLI paths."""
    from agent.auxiliary_client import _build_codex_client, _read_main_model

    requested_model = str(_read_main_model() or "").strip()
    if not requested_model:
        requested_model = _DEFAULT_PROBE_MODEL
    client, resolved_model = _build_codex_client(requested_model)
    if client is None or not resolved_model:
        raise RuntimeError("ChatGPT Pro bridge authentication is unavailable")
    try:
        return client.chat.completions.create(
            model=resolved_model,
            messages=[{"role": "user", "content": _PROBE_PROMPT}],
            max_tokens=16,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _run_probe(
    *,
    source: str,
    db_path=None,
    call=None,
) -> ProbeResult:
    """Run one call, then durably record both its turn and health outcome."""
    if call is None:
        call = _perform_pro_bridge_call
    started = time.monotonic()
    response = None
    error: Exception | None = None
    try:
        response = call()
    except Exception as exc:
        error = exc
    latency_ms = max(0, round((time.monotonic() - started) * 1000))

    if error is not None:
        rate_limited = _is_rate_limit_error(error)
        health_outcome = "exhausted" if rate_limited else "error"
        turn_outcome = "rate_limited" if rate_limited else "failure"
        gate_integration.record_bridge_turn(
            task_id=f"system:bridge-{source}",
            lane="platform",
            outcome=turn_outcome,
            bridge_tier="pro",
            model_requested=_DEFAULT_PROBE_MODEL,
            latency_ms=latency_ms,
            error_class=type(error).__name__,
            error_message=str(error),
            raw_response_meta={"source": source},
            db_path=db_path,
        )
        cap_status = turns_ledger.check_bridge_caps(db_path)
        turns_ledger.record_health(
            source=source,
            outcome=health_outcome,
            tier_observed="unknown",
            latency_ms=latency_ms,
            turns_used_today=cap_status["turns_used"],
            turns_cap_daily=cap_status["hard_cap"],
            note=str(error),
            raw={"error_class": type(error).__name__},
            db_path=db_path,
        )
        return ProbeResult(
            outcome=health_outcome,
            exit_code=_EXIT_CODES[health_outcome],
            turn_outcome=turn_outcome,
            response_text=None,
            model_observed=None,
            latency_ms=latency_ms,
            error_class=type(error).__name__,
            error_message=str(error),
        )

    response_text = _response_text(response)
    model_observed = str(
        getattr(response, "model", None) or _DEFAULT_PROBE_MODEL
    )
    exact_match = response_text.lower() == "bridge ok"
    slow = latency_ms > bridge_config.BRIDGE_CAPS.degraded_latency_ms
    degraded = slow or not exact_match
    turn_outcome = "degraded" if degraded else "success"
    note_parts = []
    if slow:
        note_parts.append("latency exceeded degraded threshold")
    if not exact_match:
        note_parts.append("response did not exactly match bridge ok")
    gate_integration.record_bridge_turn(
        task_id=f"system:bridge-{source}",
        lane="platform",
        outcome=turn_outcome,
        bridge_tier="pro",
        model_reported=model_observed,
        model_requested=_DEFAULT_PROBE_MODEL,
        latency_ms=latency_ms,
        request_id=getattr(response, "id", None),
        raw_response_meta={
            "source": source,
            "response_match": exact_match,
        },
        db_path=db_path,
    )
    cap_status = turns_ledger.check_bridge_caps(db_path)
    if cap_status["hard_hit"]:
        health_outcome = "exhausted"
        note_parts.append("daily turns hard cap reached")
    else:
        health_outcome = "degraded" if degraded else "healthy"
    turns_ledger.record_health(
        source=source,
        outcome=health_outcome,
        tier_observed="pro",
        model_observed=model_observed,
        latency_ms=latency_ms,
        turns_used_today=cap_status["turns_used"],
        turns_cap_daily=cap_status["hard_cap"],
        note="; ".join(note_parts) or "bridge ok",
        raw={"response_text": response_text[:80]},
        db_path=db_path,
    )
    if (
        source == "nightly"
        and health_outcome == "healthy"
        and cap_status["turns_used"] < cap_status["soft_cap"]
    ):
        bridge_state.set_fallthrough_disabled(
            False,
            reason="nightly probe healthy",
            db_path=db_path,
        )
    return ProbeResult(
        outcome=health_outcome,
        exit_code=_EXIT_CODES[health_outcome],
        turn_outcome=turn_outcome,
        response_text=response_text,
        model_observed=model_observed,
        latency_ms=latency_ms,
    )


def _cmd_status(_args: argparse.Namespace) -> int:
    caps = turns_ledger.check_bridge_caps()
    lane_counts = turns_ledger.turns_today_by_lane()
    health = turns_ledger.last_health()
    disabled, reason = bridge_state.is_fallthrough_disabled()
    print(
        "turns used today: "
        f"{caps['turns_used']} (soft cap {caps['soft_cap']}, "
        f"hard cap {caps['hard_cap']})"
    )
    print(
        "per-lane turns: "
        + " ".join(
            f"{lane}={lane_counts[lane]}"
            for lane in (
                "green_captains",
                "dayroute",
                "tihna",
                "platform",
                "reserve",
                "escalation",
            )
        )
    )
    if health is None:
        print("last probe: never")
    else:
        latency = health.get("latency_ms")
        latency_text = f"{latency}ms" if latency is not None else "unknown"
        print(
            f"last probe: {health['ts']} outcome={health['outcome']} "
            f"tier={health.get('tier_observed') or 'unknown'} "
            f"latency={latency_text}"
        )
    state_text = "yes" if disabled else "no"
    if disabled and reason:
        state_text += f" ({reason})"
    print(f"fallthrough disabled: {state_text}")
    print(f"last 1h degraded rate: {caps['degraded_rate_pct']:.2f}%")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    if not args.confirm:
        print(
            "Would make ONE real ChatGPT Pro bridge call and record its "
            "turn/health result. Re-run with --confirm to proceed."
        )
        return 0
    result = _run_probe(source="probe")
    print(
        json.dumps(
            {
                "outcome": result.outcome,
                "latency_ms": result.latency_ms,
                "model": result.model_observed,
                "response": result.response_text,
                "error_class": result.error_class,
                "error_message": result.error_message,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return result.exit_code


def _cmd_nightly_check(_args: argparse.Namespace) -> int:
    return _run_probe(source="nightly").exit_code


def register_cli(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "bridge",
        help="Inspect or explicitly probe the ChatGPT Pro bridge.",
    )
    commands = parser.add_subparsers(dest="bridge_command", required=True)

    status = commands.add_parser(
        "status",
        help="Show today's turns, bridge health, and fallthrough state.",
    )
    status.set_defaults(func=_cmd_status)

    probe = commands.add_parser(
        "probe",
        help="Make one confirmed real Pro-bridge health call.",
    )
    probe.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm the single real Pro-bridge call.",
    )
    probe.set_defaults(func=_cmd_probe)

    nightly = commands.add_parser(
        "nightly-check",
        help="Run one non-interactive probe for cron.",
    )
    nightly.set_defaults(func=_cmd_nightly_check)


__all__ = [
    "ProbeResult",
    "_cmd_nightly_check",
    "_cmd_probe",
    "_cmd_status",
    "_perform_pro_bridge_call",
    "_run_probe",
    "register_cli",
]
