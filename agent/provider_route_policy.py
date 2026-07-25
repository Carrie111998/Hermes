"""Subscription-only route policy for autonomous orchestration.

This module is deliberately small and local-only. It classifies configured
routes by credential provenance and metadata; it never performs inference or
vendor protocol probes.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


class RouteRole(enum.Enum):
    BUILDER = "builder"
    ATTACKER = "attacker"
    DIAGNOSTICIAN = "diagnostician"


class Capability(enum.Enum):
    READ = "read"
    TEST = "test"
    WRITE = "write"
    REPAIR = "repair"


class RouteAvailability(enum.Enum):
    HEALTHY_REUSABLE = "healthy_reusable"
    TEMPORARY_RATE_LIMIT = "temporary_rate_limit"
    EXPIRED_ACCESS_REFRESH_AVAILABLE = "expired_access_refresh_available"
    EXPIRED_ACCESS_MISSING_REFRESH = "expired_access_missing_refresh"
    REVOKED_DEAD = "revoked_dead"
    BROWSER_OAUTH_TIMEOUT = "browser_oauth_timeout"
    UNAVAILABLE_CLI_MODEL = "unavailable_cli_model"
    PROVIDER_OUTAGE_UNKNOWN_TRANSPORT = "provider_outage_unknown_transport"


_WRITE_CAPABILITIES = {Capability.WRITE, Capability.REPAIR}
_FORBIDDEN_PROVIDERS = {
    "openai",
    "openrouter",
    "vertex",
    "google-vertex",
    "anthropic-api",
    "fable",
}
_CODEX_BUILDER_SOURCES = {"manual:device_code", "device_code", "chatgpt_oauth", "chatgpt", "codex_oauth", "oauth"}
_CLAUDE_SUBSCRIPTION_SOURCES = {
    "claude_code",
    "hermes_pkce",
    "anthropic_oauth",
    "oauth",
    "env:anthropic_token",
    "env:claude_code_oauth_token",
}
_ATTACKER_SOURCES = {"claude_code", "hermes_pkce", "anthropic_oauth", "oauth", "env:anthropic_token", "env:claude_code_oauth_token"}
_DIAGNOSTICIAN_PROVIDERS = {"gemini-code-assist", "google-code-assist", "gemini-cli"}
_DIAGNOSTICIAN_SOURCES = {"google_code_assist", "gemini_cli", "oauth"}


def orchestrator_subscription_only(config: dict[str, Any] | None) -> bool:
    orch = (config or {}).get("orchestrator")
    return (
        isinstance(orch, dict)
        and bool(orch.get("enabled"))
        and str(orch.get("billing_policy") or "").strip().lower() == "subscription_only"
    )


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def _has_refresh(entry: dict[str, Any]) -> bool:
    return bool(str(entry.get("refresh_token") or "").strip()) or bool(entry.get("has_refresh_token"))


def _parse_expiry(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        return raw / 1000.0 if raw > 10_000_000_000 else raw
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class RouteDecision:
    allowed: bool
    role: str
    capability: str
    provider: str
    model: str
    reason: str


@dataclass(frozen=True, repr=False)
class RouteHealth:
    availability: RouteAvailability
    provider: str = ""
    model: str = ""
    safe_reason: str = ""
    reset_at: float | None = None
    fresh_login_required: bool = False
    reusable_saved_credential: bool = False

    def __repr__(self) -> str:
        return (
            "RouteHealth("
            f"availability={self.availability.value!r}, provider={self.provider!r}, "
            f"model={self.model!r}, safe_reason={self.safe_reason!r}, reset_at={self.reset_at!r}, "
            f"fresh_login_required={self.fresh_login_required!r}, "
            f"reusable_saved_credential={self.reusable_saved_credential!r})"
        )

    def to_summary(self) -> dict[str, Any]:
        return {
            "availability": self.availability.value,
            "provider": self.provider,
            "model": self.model,
            "safe_reason": self.safe_reason,
            "reset_at": self.reset_at,
            "fresh_login_required": self.fresh_login_required,
            "reusable_saved_credential": self.reusable_saved_credential,
        }


def classify_route_health(entry: dict[str, Any], *, now: float | None = None) -> RouteHealth:
    now = time.time() if now is None else float(now)
    status = _normalize(entry.get("status") or entry.get("last_status"))
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    reset_at = _parse_expiry(entry.get("last_error_reset_at") or entry.get("reset_at"))

    if status in {"dead", "revoked", "token_revoked", "token_invalidated"}:
        return RouteHealth(RouteAvailability.REVOKED_DEAD, provider, model, "credential revoked or dead", fresh_login_required=True)
    if status in {"browser_timeout", "browser_oauth_timeout"}:
        return RouteHealth(RouteAvailability.BROWSER_OAUTH_TIMEOUT, provider, model, "browser OAuth timed out", fresh_login_required=True)
    if status in {"unavailable_cli", "unavailable_model", "missing_cli", "missing_model"}:
        return RouteHealth(RouteAvailability.UNAVAILABLE_CLI_MODEL, provider, model, "local CLI or model unavailable")
    if status in {"unknown_transport", "provider_outage", "transport_unknown"}:
        return RouteHealth(RouteAvailability.PROVIDER_OUTAGE_UNKNOWN_TRANSPORT, provider, model, "provider outage or unknown transport")
    if status == "exhausted" and int(entry.get("last_error_code") or 0) == 429:
        return RouteHealth(RouteAvailability.TEMPORARY_RATE_LIMIT, provider, model, "temporary rate limit", reset_at=reset_at)

    expiry = _parse_expiry(entry.get("expires_at") or entry.get("expires_at_ms"))
    if expiry is not None and expiry <= now:
        if _has_refresh(entry):
            return RouteHealth(
                RouteAvailability.EXPIRED_ACCESS_REFRESH_AVAILABLE,
                provider,
                model,
                "access token expired; refresh token present",
                reusable_saved_credential=True,
            )
        return RouteHealth(
            RouteAvailability.EXPIRED_ACCESS_MISSING_REFRESH,
            provider,
            model,
            "access token expired without refresh token",
            fresh_login_required=True,
        )

    return RouteHealth(
        RouteAvailability.HEALTHY_REUSABLE,
        provider,
        model,
        "saved credential reusable",
        reusable_saved_credential=True,
    )


class SubscriptionRoutePolicy:
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = orchestrator_subscription_only(self.config)

    def evaluate(
        self,
        route: dict[str, Any],
        *,
        role: RouteRole | str = RouteRole.BUILDER,
        capability: Capability | str = Capability.WRITE,
        builder_provider: str | None = None,
    ) -> RouteDecision:
        role = role if isinstance(role, RouteRole) else RouteRole(str(role))
        capability = capability if isinstance(capability, Capability) else Capability(str(capability))
        provider = _normalize(route.get("provider"))
        model = str(route.get("model") or "").strip()
        auth_type = _normalize(route.get("auth_type") or route.get("credential_type"))
        source = _normalize(route.get("source") or route.get("credential_source") or route.get("provenance"))

        if not self.enabled:
            return RouteDecision(True, role.value, capability.value, provider, model, "orchestrator_disabled")

        if provider in _FORBIDDEN_PROVIDERS or provider.startswith("vertex"):
            return RouteDecision(False, role.value, capability.value, provider, model, "forbidden_paid_provider")
        if (
            auth_type == "api_key"
            or route.get("api_key")
            or route.get("key_env")
            or route.get("api_key_env")
        ):
            return RouteDecision(False, role.value, capability.value, provider, model, "api_key_forbidden")

        if role == RouteRole.BUILDER:
            # Config-only fallback entries generally have no credential
            # provenance. Keep approved provider names in the chain, then
            # require the runtime caller to re-evaluate the selected pooled
            # credential before assigning the turn.
            if provider == "openai-codex":
                allowed = not source or source in _CODEX_BUILDER_SOURCES
                reason = (
                    "approved_builder_subscription"
                    if source
                    else "runtime_subscription_check_required"
                )
                return RouteDecision(
                    allowed,
                    role.value,
                    capability.value,
                    provider,
                    model,
                    reason if allowed else "builder_requires_chatgpt_codex_oauth",
                )
            if provider == "anthropic":
                allowed = (not source and not auth_type) or (
                    auth_type == "oauth" and source in _CLAUDE_SUBSCRIPTION_SOURCES
                )
                reason = (
                    "approved_claude_fallback_builder"
                    if source
                    else "runtime_subscription_check_required"
                )
                return RouteDecision(
                    allowed,
                    role.value,
                    capability.value,
                    provider,
                    model,
                    reason if allowed else "builder_requires_claude_subscription_oauth",
                )
            return RouteDecision(False, role.value, capability.value, provider, model, "builder_requires_approved_subscription_oauth")

        if role == RouteRole.ATTACKER:
            if capability in _WRITE_CAPABILITIES:
                return RouteDecision(False, role.value, capability.value, provider, model, "attacker_read_only")
            if builder_provider and provider == _normalize(builder_provider):
                return RouteDecision(False, role.value, capability.value, provider, model, "self_attack_forbidden")
            allowed = provider == "anthropic" and source in _ATTACKER_SOURCES
            return RouteDecision(allowed, role.value, capability.value, provider, model, "approved_anthropic_subscription" if allowed else "attacker_requires_claude_subscription_oauth")

        if role == RouteRole.DIAGNOSTICIAN:
            if capability in _WRITE_CAPABILITIES:
                return RouteDecision(False, role.value, capability.value, provider, model, "diagnostician_read_only")
            allowed = provider in _DIAGNOSTICIAN_PROVIDERS and source in _DIAGNOSTICIAN_SOURCES
            return RouteDecision(allowed, role.value, capability.value, provider, model, "approved_google_code_assist_oauth" if allowed else "diagnostician_requires_code_assist_oauth")

        return RouteDecision(False, role.value, capability.value, provider, model, "unknown_role")

    def filter_routes(
        self,
        routes: Iterable[dict[str, Any]],
        *,
        role: RouteRole | str = RouteRole.BUILDER,
        capability: Capability | str = Capability.WRITE,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return [dict(r) for r in routes]
        filtered: list[dict[str, Any]] = []
        for route in routes:
            if self.evaluate(route, role=role, capability=capability).allowed:
                filtered.append(dict(route))
        return filtered


def sanitized_startup_summary(routes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [classify_route_health(route).to_summary() for route in routes]
