"""Controls for optional Delegate Skills and Agent Reach integrations.

This module is intentionally pure data/normalization logic.  The external
projects are optional canaries; Hermes must not expose a blanket adapter surface
or treat adapter summaries as execution proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

DELEGATE_BRIDGE_SCHEMA = "omh_delegate_bridge.v1"
EXECUTION_RECEIPT_SCHEMA = "hermes_execution_receipt.v1"
AGENT_REACH_PROVENANCE_SCHEMA = "agent_reach_provenance.v1"

DELEGATE_ADAPTER_CAPABILITIES: dict[str, str] = {
    "codex": "delegate.codex",
    "claude": "delegate.claude",
}

AGENT_REACH_READ_CAPABILITIES: dict[str, str] = {
    "web": "agent_reach.web.read",
    "youtube": "agent_reach.youtube.read",
    "rss": "agent_reach.rss.read",
    "github": "agent_reach.github.read",
    "reddit": "agent_reach.reddit.read",
    "x": "agent_reach.x.read",
    "linkedin": "agent_reach.linkedin.read",
}

SOCIAL_AGENT_REACH_SOURCES = frozenset({"reddit", "x", "linkedin"})
_SOCIAL_MUTATION_SUFFIXES = (".write", ".post", ".reply", ".dm", ".follow", ".react")


class DelegateControlError(ValueError):
    """Raised when a Delegate adapter request violates the Hermes policy."""


class AgentReachControlError(ValueError):
    """Raised when an Agent Reach source request violates the Hermes policy."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enabled_delegate_adapters(config: Mapping[str, Any] | None) -> dict[str, str]:
    """Return enabled Delegate adapters, restricted to the initial allowlist.

    Only ``codex`` and ``claude`` are first-class capabilities.  Unknown keys are
    ignored rather than converted into generic delegation reach, which prevents a
    config typo or future upstream adapter from silently broadening authority.
    """

    delegation = _mapping(_mapping(config).get("delegation"))
    adapters = _mapping(delegation.get("adapters"))
    enabled: dict[str, str] = {}
    for adapter, capability in DELEGATE_ADAPTER_CAPABILITIES.items():
        if _truthy(_mapping(adapters.get(adapter)).get("enabled")):
            enabled[adapter] = capability
    return enabled


def build_omh_delegate_bridge(
    *,
    adapter: str,
    objective: str,
    context: str = "",
    allowed_actions: Sequence[str] = (),
    required_evidence: Sequence[str] = (),
) -> dict[str, Any]:
    """Build the OMH Delegate Skills bridge payload for one adapter task."""

    adapter = str(adapter or "").strip().lower()
    if adapter not in DELEGATE_ADAPTER_CAPABILITIES:
        raise DelegateControlError(
            "Delegate adapter must be one of "
            f"{sorted(DELEGATE_ADAPTER_CAPABILITIES)}, got {adapter!r}"
        )
    if not str(objective or "").strip():
        raise DelegateControlError("Delegate objective is required")

    return {
        "schema": DELEGATE_BRIDGE_SCHEMA,
        "adapter": adapter,
        "capability": DELEGATE_ADAPTER_CAPABILITIES[adapter],
        "objective": str(objective),
        "context": str(context or ""),
        "constraints": {
            "delegate_must_not_commit": True,
            "delegate_final_text_is_not_proof": True,
            "orchestrator_must_verify_independently": True,
            "reviewer_must_verify_independently": True,
        },
        "allowed_actions": list(allowed_actions),
        "required_evidence": list(required_evidence),
    }


def normalize_delegate_execution_receipt(
    *,
    bridge_payload: Mapping[str, Any],
    delegate_result: Mapping[str, Any] | str | None,
    verification: Mapping[str, Any] | None = None,
    delegate_committed: bool = False,
) -> dict[str, Any]:
    """Normalize a Delegate Skills result into a Hermes execution receipt.

    The delegate's final text is carried as untrusted display evidence only.  The
    receipt becomes verified solely from an explicit independent verification
    payload supplied by the orchestrator/reviewer lane.
    """

    bridge = _mapping(bridge_payload)
    if bridge.get("schema") != DELEGATE_BRIDGE_SCHEMA:
        raise DelegateControlError("bridge_payload must use omh_delegate_bridge.v1")

    result: Mapping[str, Any]
    if isinstance(delegate_result, Mapping):
        result = delegate_result
        final_text = str(result.get("final_response") or result.get("final_text") or "")
    elif delegate_result is None:
        result = {}
        final_text = ""
    else:
        result = {"final_text": str(delegate_result)}
        final_text = str(delegate_result)

    verification_map = _mapping(verification)
    independently_verified = _truthy(verification_map.get("independently_verified"))
    status = "verified" if independently_verified and not delegate_committed else "needs_verification"
    if delegate_committed:
        status = "policy_violation"

    return {
        "schema": EXECUTION_RECEIPT_SCHEMA,
        "source_schema": DELEGATE_BRIDGE_SCHEMA,
        "adapter": bridge.get("adapter"),
        "capability": bridge.get("capability"),
        "status": status,
        "created_at": _now_iso(),
        "delegate": {
            "final_text": final_text,
            "final_text_is_execution_proof": False,
            "committed": bool(delegate_committed),
            "raw_result": dict(result),
        },
        "verification": {
            "independently_verified": independently_verified,
            "commands": list(verification_map.get("commands") or ()),
            "evidence": list(verification_map.get("evidence") or ()),
        },
        "policy": {
            "delegate_must_not_commit": True,
            "orchestrator_verified_independently": independently_verified,
            "reviewer_verified_independently": _truthy(
                verification_map.get("reviewer_verified_independently")
            ),
        },
    }


def social_mutation_capabilities_enabled(config: Mapping[str, Any] | None) -> list[str]:
    """Return explicitly enabled social write capabilities.

    The default is an empty list.  Even when a caller opts in, only social-source
    capabilities with a mutation suffix are returned; read capabilities are not
    upgraded to write authority.
    """

    agent_reach = _mapping(_mapping(config).get("agent_reach"))
    if not _truthy(agent_reach.get("social_mutation_enabled")):
        return []
    requested = agent_reach.get("mutation_capabilities") or ()
    enabled: list[str] = []
    for capability in requested if isinstance(requested, Sequence) and not isinstance(requested, str) else ():
        cap = str(capability).strip().lower()
        source = cap.split(".", 1)[0]
        if source in SOCIAL_AGENT_REACH_SOURCES and cap.endswith(_SOCIAL_MUTATION_SUFFIXES):
            enabled.append(cap)
    return enabled


def enabled_agent_reach_sources(config: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return read-only Agent Reach source capabilities enabled by config."""

    agent_reach = _mapping(_mapping(config).get("agent_reach"))
    sources = _mapping(agent_reach.get("sources"))
    enabled: dict[str, dict[str, Any]] = {}
    for source, capability in AGENT_REACH_READ_CAPABILITIES.items():
        cfg = _mapping(sources.get(source))
        if _truthy(cfg.get("enabled")):
            enabled[source] = {
                "capability": capability,
                "required": _truthy(cfg.get("required")),
                "backend": str(cfg.get("backend") or "agent-reach"),
                "mutation_enabled": False,
            }
    return enabled


def resolve_agent_reach_plan(
    *,
    source: str,
    config: Mapping[str, Any] | None,
    canonical_connector_available: bool = False,
) -> dict[str, Any]:
    """Choose canonical connector first, Agent Reach fallback second."""

    source = str(source or "").strip().lower()
    if source not in AGENT_REACH_READ_CAPABILITIES:
        raise AgentReachControlError(
            f"Agent Reach source must be one of {sorted(AGENT_REACH_READ_CAPABILITIES)}, got {source!r}"
        )
    if canonical_connector_available:
        return {
            "source": source,
            "selected": "canonical_connector",
            "fallback_available": source in enabled_agent_reach_sources(config),
            "read_capability": AGENT_REACH_READ_CAPABILITIES[source],
            "mutation_capabilities": [],
        }
    enabled = enabled_agent_reach_sources(config)
    if source in enabled:
        return {
            "source": source,
            "selected": "agent_reach_fallback",
            "source_health": "available",
            "read_capability": enabled[source]["capability"],
            "mutation_capabilities": [],
        }
    return {
        "source": source,
        "selected": "unavailable",
        "source_health": "unavailable",
        "read_capability": AGENT_REACH_READ_CAPABILITIES[source],
        "mutation_capabilities": [],
    }


@dataclass(frozen=True)
class AgentReachProvenance:
    source: str
    source_id: str
    url: str
    backend: str
    account_session_class: str
    raw_path: str
    normalized_path: str
    retrieved_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": AGENT_REACH_PROVENANCE_SCHEMA,
            "platform": self.source,
            "source_id": self.source_id,
            "url": self.url,
            "retrieved_at": self.retrieved_at,
            "backend": self.backend,
            "account_session_class": self.account_session_class,
            "raw_path": self.raw_path,
            "normalized_path": self.normalized_path,
        }


def build_agent_reach_provenance(
    *,
    source: str,
    source_id: str,
    url: str,
    backend: str,
    account_session_class: str,
    raw_path: str | Path,
    normalized_path: str | Path,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    """Build the required source provenance envelope for Agent Reach reads."""

    source = str(source or "").strip().lower()
    if source not in AGENT_REACH_READ_CAPABILITIES:
        raise AgentReachControlError(
            f"Agent Reach source must be one of {sorted(AGENT_REACH_READ_CAPABILITIES)}, got {source!r}"
        )
    return AgentReachProvenance(
        source=source,
        source_id=str(source_id or ""),
        url=str(url or ""),
        backend=str(backend or "agent-reach"),
        account_session_class=str(account_session_class or "anonymous"),
        raw_path=str(raw_path),
        normalized_path=str(normalized_path),
        retrieved_at=retrieved_at or _now_iso(),
    ).to_dict()
