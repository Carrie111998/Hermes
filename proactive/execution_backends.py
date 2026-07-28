"""Deterministic execution-backend selection and lifecycle policy.

ClawOps owns the routing decision. Hermes, Codex, and OpenClaw are execution
backends evaluated against the same declared requirements. The returned
decision is JSON-safe so it can be persisted with every Kanban attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import yaml


DEFAULT_BACKEND_REGISTRY = Path(__file__).with_name("execution-backends.yaml")
TERMINAL_BACKEND_STATUSES = frozenset({"succeeded", "failed", "blocked"})
ACTIVE_BACKEND_STATUSES = frozenset({"queued", "running"})
VALID_BACKEND_STATUSES = ACTIVE_BACKEND_STATUSES | TERMINAL_BACKEND_STATUSES
STATUS_ORDER = {"queued": 0, "running": 1, "succeeded": 2, "failed": 2, "blocked": 2}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class ExecutionRequirements:
    capabilities: tuple[str, ...]
    semantic_class: str | None = None
    risk_level: str = "low"
    credential_policy: str | None = None
    workspace_policy: str | None = None
    session_policy: str | None = None
    max_runtime_seconds: int = 300
    preferred_backend: str | None = None

    @classmethod
    def build(
        cls,
        *,
        capabilities: Sequence[str],
        semantic_class: str | None = None,
        risk_level: str = "low",
        credential_policy: str | None = None,
        workspace_policy: str | None = None,
        session_policy: str | None = None,
        max_runtime_seconds: int = 300,
        preferred_backend: str | None = None,
    ) -> "ExecutionRequirements":
        normalized_capabilities = tuple(
            sorted({str(item).strip() for item in capabilities if str(item).strip()})
        )
        clean_risk = str(risk_level or "low").strip().lower()
        if clean_risk not in RISK_ORDER:
            raise ValueError(f"Unsupported risk_level={risk_level!r}.")
        if int(max_runtime_seconds) <= 0:
            raise ValueError("max_runtime_seconds must be positive.")
        return cls(
            capabilities=normalized_capabilities,
            semantic_class=_optional_string(semantic_class),
            risk_level=clean_risk,
            credential_policy=_optional_string(credential_policy),
            workspace_policy=_optional_string(workspace_policy),
            session_policy=_optional_string(session_policy),
            max_runtime_seconds=int(max_runtime_seconds),
            preferred_backend=_optional_string(preferred_backend),
        )


def _optional_string(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def load_backend_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path else DEFAULT_BACKEND_REGISTRY
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("backends"), dict):
        raise ValueError(f"Execution backend registry must define backends: {registry_path}")
    return raw


def _candidate_reasons(
    backend_id: str,
    backend: Mapping[str, Any],
    requirements: ExecutionRequirements,
    *,
    circuit_state: str,
) -> list[str]:
    reasons: list[str] = []
    if not bool(backend.get("enabled", False)):
        reasons.append("disabled")
    if circuit_state == "open":
        reasons.append("circuit_open")
    elif circuit_state == "half_open":
        reasons.append("circuit_half_open_probe_required")
    available = {str(item) for item in backend.get("capabilities") or []}
    missing = sorted(set(requirements.capabilities) - available)
    if missing:
        reasons.append("missing_capabilities:" + ",".join(missing))
    semantic_classes = {
        str(item).strip()
        for item in backend.get("semantic_classes") or []
        if str(item).strip()
    }
    if (
        requirements.semantic_class
        and requirements.semantic_class not in semantic_classes
    ):
        reasons.append(
            "semantic_class_mismatch:"
            f"{requirements.semantic_class}"
        )
    limit = str(backend.get("risk_level_limit") or "low").lower()
    if limit not in RISK_ORDER or RISK_ORDER[requirements.risk_level] > RISK_ORDER[limit]:
        reasons.append(f"risk_exceeds_limit:{limit}")
    if (
        requirements.credential_policy
        and backend.get("credential_policy") != requirements.credential_policy
    ):
        reasons.append(
            "credential_policy_mismatch:"
            f"{backend.get('credential_policy') or '<none>'}"
        )
    if (
        requirements.workspace_policy
        and requirements.workspace_policy not in (backend.get("workspace_policies") or [])
    ):
        reasons.append(
            "workspace_policy_mismatch:"
            f"{requirements.workspace_policy}"
        )
    if (
        requirements.session_policy
        and requirements.session_policy not in (backend.get("session_policies") or [])
    ):
        reasons.append(
            "session_policy_mismatch:"
            f"{requirements.session_policy}"
        )
    if requirements.max_runtime_seconds > int(backend.get("max_runtime_seconds") or 0):
        reasons.append(
            "runtime_exceeds_limit:"
            f"{backend.get('max_runtime_seconds') or 0}"
        )
    return reasons


def route_execution_backend(
    requirements: ExecutionRequirements,
    *,
    registry: Mapping[str, Any] | None = None,
    circuit_states: Mapping[str, str] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    data = dict(registry or load_backend_registry())
    backends = data.get("backends")
    policy = data.get("policy")
    if not isinstance(backends, Mapping) or not isinstance(policy, Mapping):
        raise ValueError("Execution backend registry is missing policy or backends.")
    selection_order = [str(item) for item in policy.get("selection_order") or []]
    if requirements.preferred_backend:
        selection_order = [
            requirements.preferred_backend,
            *[
                backend_id
                for backend_id in selection_order
                if backend_id != requirements.preferred_backend
            ],
        ]
    circuits = dict(circuit_states or {})
    candidates: list[dict[str, Any]] = []
    selected: str | None = None
    for backend_id in selection_order:
        backend = backends.get(backend_id)
        if not isinstance(backend, Mapping):
            candidates.append(
                {
                    "backend": backend_id,
                    "eligible": False,
                    "reasons": ["not_registered"],
                }
            )
            continue
        reasons = _candidate_reasons(
            backend_id,
            backend,
            requirements,
            circuit_state=str(circuits.get(backend_id) or "closed"),
        )
        eligible = not reasons
        candidate = {
            "backend": backend_id,
            "eligible": eligible,
            "reasons": reasons or ["requirements_matched"],
            "cost_tier": backend.get("cost_tier"),
            "supports_async": bool(backend.get("supports_async", False)),
        }
        if requirements.semantic_class:
            candidate["semantic_compatible"] = (
                requirements.semantic_class
                in {
                    str(item).strip()
                    for item in backend.get("semantic_classes") or []
                    if str(item).strip()
                }
            )
        candidates.append(candidate)
        if selected is None and eligible:
            selected = backend_id
    decision = {
        "version": 2,
        "decided_at": int(now if now is not None else time.time()),
        "mode": "shadow" if bool(data.get("shadow_mode", False)) else "enforced",
        "requirements": {
            "capabilities": list(requirements.capabilities),
            "semantic_class": requirements.semantic_class,
            "risk_level": requirements.risk_level,
            "credential_policy": requirements.credential_policy,
            "workspace_policy": requirements.workspace_policy,
            "session_policy": requirements.session_policy,
            "max_runtime_seconds": requirements.max_runtime_seconds,
            "preferred_backend": requirements.preferred_backend,
        },
        "selected_backend": selected,
        "selection_reason": (
            f"first eligible backend in deterministic order: {selected}"
            if selected
            else "no backend satisfied all declared requirements"
        ),
        "candidates": candidates,
        "fallback_order": [
            item["backend"]
            for item in candidates
            if item["eligible"] and item["backend"] != selected
        ],
    }
    return decision


def select_semantic_fallback(
    decision: Mapping[str, Any],
    *,
    failed_backend: str,
) -> str | None:
    """Return the next eligible fallback without crossing a semantic boundary."""
    clean_failed = str(failed_backend or "").strip()
    if not clean_failed:
        raise ValueError("failed_backend is required.")
    candidates = decision.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("routing decision candidates must be a sequence.")
    ordered = [
        item
        for item in candidates
        if isinstance(item, Mapping)
    ]
    failed_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if item.get("backend") == clean_failed
        ),
        -1,
    )
    search = ordered[failed_index + 1 :] if failed_index >= 0 else ordered
    for item in search:
        if (
            bool(item.get("eligible"))
            and item.get("semantic_compatible", True) is True
            and item.get("backend") != clean_failed
        ):
            return str(item["backend"])
    return None


def build_shadow_comparison_report(
    decision: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic, side-effect-free comparison of backend outcomes."""
    selected = _optional_string(decision.get("selected_backend"))
    if not selected:
        raise ValueError("shadow comparison requires a selected backend.")
    selected_observation = observations.get(selected)
    if not isinstance(selected_observation, Mapping):
        raise ValueError("shadow comparison requires a selected-backend observation.")

    selected_status = _optional_string(selected_observation.get("status"))
    selected_digest = _optional_string(selected_observation.get("evidence_digest"))
    candidates = decision.get("candidates")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("routing decision candidates must be a sequence.")

    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        backend_id = str(candidate.get("backend") or "").strip()
        observation = observations.get(backend_id)
        if not backend_id or not isinstance(observation, Mapping):
            continue
        status = _optional_string(observation.get("status"))
        evidence_digest = _optional_string(observation.get("evidence_digest"))
        is_selected = backend_id == selected
        rows.append(
            {
                "backend": backend_id,
                "selected": is_selected,
                "eligible": bool(candidate.get("eligible")),
                "semantic_compatible": candidate.get("semantic_compatible", True),
                "status": status,
                "duration_ms": observation.get("duration_ms"),
                "cost_units": observation.get("cost_units"),
                "evidence_digest": evidence_digest,
                "outcome_parity": (
                    None if is_selected else status == selected_status
                ),
                "evidence_parity": (
                    None
                    if is_selected or selected_digest is None or evidence_digest is None
                    else evidence_digest == selected_digest
                ),
            }
        )

    alternatives = [row for row in rows if not row["selected"]]
    return {
        "version": 1,
        "mode": decision.get("mode"),
        "selected_backend": selected,
        "semantic_class": (
            decision.get("requirements", {}).get("semantic_class")
            if isinstance(decision.get("requirements"), Mapping)
            else None
        ),
        "observations": rows,
        "summary": {
            "observed_backends": len(rows),
            "comparable_backends": len(alternatives),
            "outcome_matches": sum(
                1 for row in alternatives if row["outcome_parity"] is True
            ),
            "evidence_matches": sum(
                1 for row in alternatives if row["evidence_parity"] is True
            ),
        },
    }


def assert_backend_transition(previous: str | None, current: str) -> None:
    if current not in VALID_BACKEND_STATUSES:
        raise ValueError(f"Unsupported backend status={current!r}.")
    if previous is None:
        if current not in {"queued", "running", "succeeded", "failed", "blocked"}:
            raise ValueError(f"Invalid initial backend status={current!r}.")
        return
    if previous not in VALID_BACKEND_STATUSES:
        raise ValueError(f"Unsupported previous backend status={previous!r}.")
    if previous in TERMINAL_BACKEND_STATUSES and current != previous:
        raise ValueError(f"Terminal backend status {previous!r} cannot transition to {current!r}.")
    if STATUS_ORDER[current] < STATUS_ORDER[previous]:
        raise ValueError(f"Backend status cannot regress from {previous!r} to {current!r}.")
    if previous == "queued" and current in VALID_BACKEND_STATUSES:
        return
    if previous == "running" and current in {"running", *TERMINAL_BACKEND_STATUSES}:
        return
    if previous == current:
        return
    raise ValueError(f"Invalid backend transition from {previous!r} to {current!r}.")


def next_poll_delay_seconds(
    poll_count: int,
    *,
    initial_delay_seconds: int = 2,
    max_delay_seconds: int = 30,
) -> int:
    if poll_count < 0:
        raise ValueError("poll_count cannot be negative.")
    return min(max_delay_seconds, initial_delay_seconds * (2 ** poll_count))
