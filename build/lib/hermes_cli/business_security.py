"""Fail-closed production-readiness checks for autonomous Business OS mode."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


ISOLATED_TERMINAL_BACKENDS = frozenset(
    {"docker", "modal", "daytona", "singularity"}
)


@dataclass(frozen=True)
class SecurityReadiness:
    ready: bool
    violations: tuple[str, ...]


def evaluate_security_readiness(config: Mapping[str, Any]) -> SecurityReadiness:
    charter = config.get("agentic") or {}
    security = charter.get("security") or {}
    violations: list[str] = []
    if not bool(security.get("enforce_isolated_execution", True)):
        violations.append("isolated execution enforcement is disabled")
    backend = str((config.get("terminal") or {}).get("backend", "local"))
    if backend not in ISOLATED_TERMINAL_BACKENDS:
        violations.append(
            f"terminal backend {backend!r} is not an approved isolated backend"
        )
    secret_config = config.get("secrets") or {}
    external_secret_source = any(
        bool((secret_config.get(name) or {}).get("enabled"))
        for name in ("bitwarden", "onepassword")
    )
    if bool(security.get("require_external_secret_manager", True)) and not external_secret_source:
        violations.append("no external secret manager is enabled")
    if not bool((config.get("security") or {}).get("redact_secrets", True)):
        violations.append("secret redaction is disabled")
    payment_config = (charter.get("finance") or {}).get("payments") or {}
    if payment_config.get("custody_model") != "non_custodial":
        violations.append("payment custody model is not non-custodial")
    if bool(payment_config.get("store_raw_financial_credentials", False)):
        violations.append("raw financial credential storage is enabled")
    return SecurityReadiness(not violations, tuple(violations))
