"""Core helpers for routing memory lookups to policy-defined domains.

This module is intentionally provider-free. It turns a user query plus an
optional structured LLM hint into a small, deterministic route object that later
memory-provider integrations can consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re

try:  # PyYAML is a core dependency, but keep this helper import-safe.
    import yaml
except Exception:  # pragma: no cover - only for severely trimmed envs.
    yaml = None  # type: ignore[assignment]


ROUTE_SOURCE_NONE = "none"
ROUTE_SOURCE_DETERMINISTIC = "deterministic"
ROUTE_SOURCE_LLM = "llm"

_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "features"
    / "memory-routing"
    / "memory-routing-policy.yaml"
)

_DEFAULT_POLICY: dict[str, Any] = {
    "domains": {
        "investing": {
            "slots": ["holdings", "average_cost", "watchlist"],
            "triggers": ["stock", "stocks", "invest", "investing", "portfolio", "주식", "종목"],
        },
        "kr_stock": {
            "extends": ["investing"],
            "slots": ["kr_ticker", "broker_opinion"],
            "triggers": ["하이닉스", "삼성전자", "코스피", "코스닥", "sk하이닉스", "000660", "005930"],
        },
    },
    "safety": {
        "high_risk_triggers": [
            "delete",
            "remove",
            "erase",
            "purge",
            "forget",
            "reset",
            "삭제",
            "제거",
            "잊어",
            "초기화",
        ]
    },
}


@dataclass
class MemoryRoute:
    """A provider-neutral memory route decision for one user query."""

    domains: list[str] = field(default_factory=list)
    slots: list[str] = field(default_factory=list)
    risk: str = "low"
    confidence: float = 0.0
    route_source: str = ROUTE_SOURCE_NONE
    reason: str = ""
    new_topic_candidate: str | None = None
    suggested_slots: list[str] = field(default_factory=list)
    should_update_policy: bool = False
    gate_required: bool = False
    rewritten_query: str | None = None


def parse_llm_route(payload: Any) -> MemoryRoute:
    """Normalize a structured LLM routing payload into :class:`MemoryRoute`.

    Invalid or missing fields degrade to safe defaults. The returned route is a
    hint only; deterministic policy matches may still take precedence in
    :func:`build_memory_route`.
    """

    if not isinstance(payload, dict):
        return MemoryRoute(route_source=ROUTE_SOURCE_LLM)

    risk = str(payload.get("risk") or "low").strip().lower() or "low"
    confidence = _clamp_confidence(payload.get("confidence", 0.0))
    new_topic_candidate = _optional_string(payload.get("new_topic_candidate"))
    suggested_slots = _strings(payload.get("suggested_slots"))
    should_update_policy = bool(payload.get("should_update_policy")) or bool(new_topic_candidate)
    gate_required = bool(payload.get("gate_required")) or risk in {"high", "critical"}

    return MemoryRoute(
        domains=_strings(payload.get("domains")),
        slots=_strings(payload.get("slots")),
        risk=risk,
        confidence=confidence,
        route_source=ROUTE_SOURCE_LLM,
        reason=str(payload.get("reason") or ""),
        new_topic_candidate=new_topic_candidate,
        suggested_slots=suggested_slots,
        should_update_policy=should_update_policy,
        gate_required=gate_required,
        rewritten_query=_optional_string(payload.get("rewritten_query")),
    )


def build_memory_route(
    query: str,
    llm_payload: Any | None = None,
    recent_turns: list[Any] | None = None,
) -> MemoryRoute:
    """Build a deterministic memory route, optionally enriched by an LLM hint."""

    del recent_turns  # Reserved for future provider integration; keep API stable.

    query_text = str(query or "")
    policy = _load_policy()
    llm_route = parse_llm_route(llm_payload) if llm_payload is not None else MemoryRoute()
    deterministic = _deterministic_route(query_text, policy)

    if deterministic.route_source == ROUTE_SOURCE_NONE and llm_payload is not None:
        route = llm_route
    else:
        route = deterministic
        if llm_payload is not None:
            route.new_topic_candidate = llm_route.new_topic_candidate
            route.suggested_slots = _dedupe([*route.suggested_slots, *llm_route.suggested_slots])
            route.should_update_policy = route.should_update_policy or llm_route.should_update_policy
            if llm_route.gate_required:
                route.gate_required = True
            if llm_route.risk in {"high", "critical"} and route.risk == "low":
                route.risk = llm_route.risk
            if not route.reason and llm_route.reason:
                route.reason = llm_route.reason

    route.rewritten_query = rewrite_memory_query(query_text, route)
    return route


def rewrite_memory_query(query: str, route: MemoryRoute | None = None) -> str:
    """Rewrite a memory lookup query with route metadata, never slot values."""

    if route is None:
        route = build_memory_route(query)

    parts = [str(query or "")]
    if route.domains:
        parts.append(f"domains: {', '.join(route.domains)}")
    if route.slots:
        parts.append(f"slots: {', '.join(route.slots)}")
    return " | ".join(parts)


def _deterministic_route(query: str, policy: dict[str, Any]) -> MemoryRoute:
    route = MemoryRoute()
    domain_config = policy.get("domains") if isinstance(policy, dict) else None
    domains = domain_config if isinstance(domain_config, dict) else {}

    matched_reasons: list[str] = []
    for domain_name, config in domains.items():
        if not isinstance(config, dict):
            continue
        trigger = _first_matching_trigger(query, _domain_triggers(config))
        if trigger is None:
            continue
        domain_risk = str(config.get("risk_level") or config.get("risk") or "low").lower()
        route.domains = _dedupe([*route.domains, str(domain_name)])
        route.slots = _dedupe([*route.slots, *_domain_slots(str(domain_name), domains)])
        if domain_risk in {"medium", "high", "critical"} and _risk_rank(domain_risk) > _risk_rank(route.risk):
            route.risk = domain_risk
        if domain_risk in {"high", "critical"}:
            route.gate_required = True
        matched_reasons.append(f"{domain_name} matched {trigger!r}")

    safety = policy.get("safety") if isinstance(policy, dict) else None
    high_risk_triggers = safety.get("high_risk_triggers") if isinstance(safety, dict) else []
    risk_trigger = _first_matching_trigger(query, high_risk_triggers)
    if risk_trigger is not None:
        route.risk = "high"
        route.gate_required = True
        matched_reasons.append(f"safety matched {risk_trigger!r}")

    if route.domains or route.gate_required:
        route.route_source = ROUTE_SOURCE_DETERMINISTIC
        route.confidence = 0.85 if route.domains else 0.7
        route.reason = "; ".join(matched_reasons)

    return route


def _domain_slots(domain_name: str, domains: dict[str, Any], seen: set[str] | None = None) -> list[str]:
    if seen is None:
        seen = set()
    if domain_name in seen:
        return []
    seen.add(domain_name)

    config = domains.get(domain_name)
    if not isinstance(config, dict):
        return []

    slots: list[str] = []
    for parent in _strings(config.get("extends")):
        slots.extend(_domain_slots(parent, domains, seen))
    slots.extend(_strings(config.get("slots")))
    slots.extend(_strings(config.get("required_if_available")))
    slots.extend(_strings(config.get("recommended_if_available")))
    return _dedupe(slots)


def _domain_triggers(config: dict[str, Any]) -> list[str]:
    triggers = config.get("triggers")
    if isinstance(triggers, dict):
        return _dedupe([*_strings(triggers.get("keywords")), *_strings(triggers.get("semantic_hints"))])
    return _strings(triggers)


def _risk_rank(risk: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(str(risk).lower(), 0)


def _load_policy() -> dict[str, Any]:
    if _POLICY_PATH.exists() and yaml is not None:
        try:
            loaded = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return _merge_default_policy(loaded)
        except Exception:
            pass
    return _DEFAULT_POLICY


def _merge_default_policy(loaded: dict[str, Any]) -> dict[str, Any]:
    """Overlay documented policy on top of test-safe deterministic defaults."""

    merged = dict(loaded)
    raw_loaded_domains = loaded.get("domains")
    loaded_domains = raw_loaded_domains if isinstance(raw_loaded_domains, dict) else {}
    domains: dict[str, Any] = {name: dict(config) for name, config in loaded_domains.items() if isinstance(config, dict)}
    for name, default_config in _DEFAULT_POLICY["domains"].items():
        current = dict(domains.get(name, {}))
        current["extends"] = _dedupe([*_strings(default_config.get("extends")), *_strings(current.get("extends"))])
        current["slots"] = _dedupe([*_strings(default_config.get("slots")), *_strings(current.get("slots"))])
        current["triggers"] = _dedupe([*_strings(_domain_triggers(default_config)), *_strings(_domain_triggers(current))])
        domains[name] = current
    merged["domains"] = domains

    raw_loaded_safety = loaded.get("safety")
    loaded_safety = raw_loaded_safety if isinstance(raw_loaded_safety, dict) else {}
    raw_default_safety = _DEFAULT_POLICY.get("safety", {})
    default_safety = raw_default_safety if isinstance(raw_default_safety, dict) else {}
    merged["safety"] = {
        **loaded_safety,
        "high_risk_triggers": _dedupe([
            *_strings(default_safety.get("high_risk_triggers")),
            *_strings(loaded_safety.get("high_risk_triggers")),
        ]),
    }
    return merged


def _first_matching_trigger(query: str, triggers: Any) -> str | None:
    for trigger in _strings(triggers):
        if _token_boundary_match(query, trigger):
            return trigger
    return None


def _token_boundary_match(text: str, needle: str) -> bool:
    """Return True only when ``needle`` appears on token boundaries.

    This deliberately avoids raw substring matching: ``cat`` does not match
    ``communicate`` and ``pr`` does not match ``approach``.
    """

    needle = needle.strip().lower()
    if not needle:
        return False
    tokens = _tokens(text)
    needle_tokens = _tokens(needle)
    if not needle_tokens:
        return False
    width = len(needle_tokens)
    for index in range(0, len(tokens) - width + 1):
        if tokens[index : index + width] == needle_tokens:
            return True
    return False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_]+|[가-힣]+", str(text or "").lower())


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                result.append(item.strip())
        return _dedupe(result)
    return []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


__all__ = [
    "MemoryRoute",
    "build_memory_route",
    "parse_llm_route",
    "rewrite_memory_query",
]
