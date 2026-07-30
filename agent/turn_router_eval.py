"""Deterministic observe-only evaluation for the native turn router."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.turn_router import (
    RouteDecision,
    RouteTarget,
    classify_ambiguous_turn,
    decide_turn_route,
    enforce_hard_budget_target,
)


DEFAULT_EVAL_CONFIG: dict[str, Any] = {
    "mode": "observe",
    "default_route": "current",
    "routes": {
        "deep": {"kind": "moa", "preset": "deep"},
    },
    "lanes": {"plain": "current", "deep": "deep"},
    "classifier": {"enabled": False},
}

CLASSIFIER_EVAL_CONFIG: dict[str, Any] = {
    "mode": "observe",
    "default_route": "current",
    "routes": {
        "fast": {
            "kind": "model",
            "provider": "openai-codex",
            "model": "gpt-5.6-luna",
        },
        "standard": {
            "kind": "model",
            "provider": "kimi-coding",
            "model": "k3-256k",
        },
        "deep": {"kind": "moa", "preset": "deep"},
    },
    "lanes": {
        "plain": "current",
        "fast": "fast",
        "standard": "standard",
        "deep": "deep",
    },
    "classifier": {
        "enabled": True,
        "provider": "local-simulation",
        "model": "strict-schema-fixture",
        "timeout_seconds": 0.1,
        "min_confidence": 0.8,
    },
}


def load_eval_corpus(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("turn-router eval corpus must be a JSON list")
    return [dict(item) for item in data if isinstance(item, Mapping)]


def evaluate_turn_router(
    corpus: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None = None,
    classifier_corpus: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate production routing functions without applying any route."""

    eval_config = dict(config or DEFAULT_EVAL_CONFIG)
    if str(eval_config.get("mode") or "").strip().lower() != "observe":
        raise ValueError("evaluation requires routing.mode=observe")

    rows: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    language_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    false_positives = 0
    false_negatives = 0
    unsafe_escalations = 0
    abstentions = 0

    for raw in corpus:
        expected = str(raw.get("expected") or "current")
        started = time.perf_counter()
        decision = decide_turn_route(raw.get("input"), eval_config)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latencies_ms.append(elapsed_ms)

        actual = decision.route
        correct = actual == expected
        language = str(raw.get("language") or "unknown")
        language_counts[language][1] += 1
        language_counts[language][0] += int(correct)
        false_positives += int(expected == "current" and actual != "current")
        false_negatives += int(expected != "current" and actual == "current")
        abstentions += int(actual == "current")

        target = enforce_hard_budget_target(RouteTarget.from_mapping(decision.target))
        unsafe_escalation = bool(target.budgeted)
        unsafe_escalations += int(unsafe_escalation)
        rows.append(
            {
                "id": str(raw.get("id") or ""),
                "language": language,
                "expected": expected,
                "actual": actual,
                "correct": correct,
                "hostile": bool(raw.get("hostile", False)),
                "source": decision.source,
                "reason_code": decision.reason_code,
                "confidence": decision.confidence,
                "should_apply": decision.should_apply,
                "unsafe_escalation": unsafe_escalation,
                "latency_ms": round(elapsed_ms, 4),
            }
        )

    total = len(rows)
    ordered = sorted(latencies_ms)
    report: dict[str, Any] = {
        "contract": {
            "mode": "observe",
            "production_decision_engine": "agent.turn_router.decide_turn_route",
            "classifier": "disabled-not-tested",
            "provider_dispatch": False,
            "route_application": False,
            "live_auto": False,
        },
        "metrics": {
            "total": total,
            "correct": sum(int(row["correct"]) for row in rows),
            "accuracy": _ratio(sum(int(row["correct"]) for row in rows), total),
            "false_positives": false_positives,
            "false_negatives": false_negatives,
            "unsafe_escalations": unsafe_escalations,
            "abstentions": abstentions,
            "abstention_rate": _ratio(abstentions, total),
            "latency_ms": {
                "p50": round(_percentile(ordered, 0.50), 4),
                "p95": round(_percentile(ordered, 0.95), 4),
                "max": round(max(ordered, default=0.0), 4),
            },
            "by_language": {
                language: {
                    "correct": values[0],
                    "total": values[1],
                    "accuracy": _ratio(values[0], values[1]),
                }
                for language, values in sorted(language_counts.items())
            },
        },
        "rows": rows,
    }

    if classifier_corpus is not None:
        report["contract"].update(
            {
                "classifier": "simulated-local-adapter",
                "coverage": {
                    "tested": ["deterministic_observe_policy"],
                    "simulated": [
                        "classifier_adapter_schema_timeout_and_injection",
                        "session_affinity_and_failure_fail_off",
                    ],
                    "not_tested": [
                        "cache_domain_isolation",
                        "live_auto",
                        "live_observe_rollout",
                        "provider_dispatch",
                        "remote_classifier_latency",
                        "route_application",
                    ],
                },
            }
        )
        report["metrics"].update(
            {
                "route_confusion_matrix": _confusion_matrix(rows),
                "unsafe_escalation_rate": _ratio(unsafe_escalations, total),
                "under_routing_rate": _ratio(
                    false_negatives,
                    sum(int(row["expected"] != "current") for row in rows),
                ),
            }
        )
        report["classifier_simulation"] = _evaluate_classifier_simulation(
            classifier_corpus
        )
        report["session_simulation"] = _evaluate_session_simulation()

    return report


def _evaluate_classifier_simulation(
    corpus: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Exercise the production classifier adapter with deterministic local I/O.

    This intentionally does not pretend to measure network/provider latency.
    The report labels adapter latency as simulated and remote latency as not
    tested so a green schema test cannot be mistaken for rollout evidence.
    """

    rows: list[dict[str, Any]] = []
    latencies_ms: list[float] = []
    fail_open_count = 0
    unsafe_escalations = 0
    grok_authorization_attempts = 0
    total_calls = 0

    for raw in corpus:
        calls: list[dict[str, Any]] = []

        def _call(**kwargs):
            calls.append(dict(kwargs))
            if raw.get("error") == "timeout":
                raise TimeoutError("simulated classifier timeout")
            if isinstance(raw.get("raw_response"), str):
                content = str(raw["raw_response"])
            else:
                content = json.dumps(
                    raw.get("response"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

        started = time.perf_counter()
        decision = classify_ambiguous_turn(
            raw.get("input"),
            CLASSIFIER_EVAL_CONFIG,
            call=_call,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        latencies_ms.append(elapsed_ms)
        total_calls += len(calls)

        if not isinstance(decision, RouteDecision):
            decision = RouteDecision(
                route="current",
                target=RouteTarget(kind="current"),
                mode="observe",
                source="classifier",
                reason_code="classifier_unavailable",
                confidence=0.0,
                should_apply=False,
            )
        target = enforce_hard_budget_target(decision.target)
        unsafe = bool(target.budgeted)
        unsafe_escalations += int(unsafe)
        grok_authorization_attempts += int(
            decision.authorization is not None or decision.requires_confirmation
        )
        if decision.reason_code in {
            "classifier_low_confidence",
            "classifier_unavailable",
            "classifier_unsafe_target",
        }:
            fail_open_count += 1

        prompt_isolated = False
        if len(calls) == 1:
            try:
                messages = calls[0]["messages"]
                prompt_isolated = (
                    messages[0]["role"] == "system"
                    and json.loads(messages[1]["content"])
                    == {"untrusted_user_text": raw.get("input")}
                )
            except (KeyError, IndexError, TypeError, ValueError):
                prompt_isolated = False

        expected = str(raw.get("expected") or "current")
        rows.append(
            {
                "id": str(raw.get("id") or ""),
                "language": str(raw.get("language") or "unknown"),
                "expected": expected,
                "actual": decision.route,
                "correct": decision.route == expected,
                "hostile": bool(raw.get("hostile", False)),
                "source": decision.source,
                "reason_code": decision.reason_code,
                "confidence": decision.confidence,
                "should_apply": decision.should_apply,
                "unsafe_escalation": unsafe,
                "call_count": len(calls),
                "prompt_isolated": prompt_isolated,
                "latency_ms": round(elapsed_ms, 4),
            }
        )

    total = len(rows)
    ordered = sorted(latencies_ms)
    return {
        "contract": {
            "mode": "observe",
            "measurement": "simulated",
            "provider_dispatch": False,
            "remote_classifier": False,
        },
        "metrics": {
            "total": total,
            "correct": sum(int(row["correct"]) for row in rows),
            "accuracy": _ratio(sum(int(row["correct"]) for row in rows), total),
            "route_confusion_matrix": _confusion_matrix(rows),
            "unsafe_escalations": unsafe_escalations,
            "grok_authorization_attempts": grok_authorization_attempts,
            "fail_open_count": fail_open_count,
            "extra_call_frequency": _ratio(total_calls, total),
            "latency_ms": {
                "measurement": "local_adapter_simulation",
                "p50": round(_percentile(ordered, 0.50), 4),
                "p95": round(_percentile(ordered, 0.95), 4),
                "max": round(max(ordered, default=0.0), 4),
            },
            "remote_latency_ms": "not_tested",
        },
        "rows": rows,
    }


def _evaluate_session_simulation() -> dict[str, Any]:
    from agent.turn_routing_runtime import (
        TurnRoutingLifecycle,
        TurnRoutingRequest,
        TurnRoutingSessionState,
    )

    target = RouteTarget(
        kind="model",
        provider="kimi-coding",
        model="k3-256k",
    )
    state = TurnRoutingSessionState(affinity_window=2, failure_limit=3)
    request = TurnRoutingRequest(surface="eval", session_state=state)
    agent = SimpleNamespace(provider="kimi-coding", model="k3")
    route_sequence: list[str] = []

    first = TurnRoutingLifecycle(agent=agent, request=request)
    first.decision = RouteDecision(
        route="deep",
        target=target,
        mode="observe",
        source="rule",
        reason_code="architecture_complexity",
        confidence=0.9,
        should_apply=False,
    )
    first.finish()
    route_sequence.append("deep")
    for _ in range(2):
        sticky = TurnRoutingLifecycle(agent=agent, request=request)
        sticky.decision = RouteDecision(
            route="deep",
            target=target,
            mode="observe",
            source="affinity",
            reason_code="sticky_route",
            confidence=1.0,
            should_apply=False,
        )
        sticky.finish()
        route_sequence.append("deep")

    failed_state = TurnRoutingSessionState(affinity_window=2, failure_limit=3)
    failed_request = TurnRoutingRequest(surface="eval", session_state=failed_state)
    failures = 0
    while not failed_state.fail_off and failures < 10:
        failed = TurnRoutingLifecycle(agent=agent, request=failed_request)
        failed.decision = RouteDecision(
            route="deep",
            target=target,
            mode="auto",
            source="classifier",
            reason_code="classifier_deep",
            confidence=0.9,
            should_apply=True,
        )
        failed.mark_turn_failed("simulated_provider_failure")
        failures += 1

    return {
        "affinity_window": 2,
        "automatic_failures_before_fail_off": failures,
        "cache_domain_switches": "not_tested",
        "fail_off_activated": failed_state.fail_off,
        "route_flapping": sum(
            int(previous != current)
            for previous, current in zip(route_sequence, route_sequence[1:])
        ),
        "route_sequence": route_sequence,
    }


def _confusion_matrix(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for row in rows:
        expected = str(row.get("expected") or "current")
        actual = str(row.get("actual") or "current")
        bucket = matrix.setdefault(expected, {})
        bucket[actual] = bucket.get(actual, 0) + 1
    return matrix


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _percentile(ordered: Sequence[float], quantile: float) -> float:
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * quantile) - 1))
    return ordered[index]
