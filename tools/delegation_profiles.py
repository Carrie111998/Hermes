"""Fail-closed named execution profiles for native Hermes delegation.

The model selects only an allowlisted profile name. Profile internals come from
trusted ``delegation.profiles`` config and are resolved before a child is
constructed, so prompts cannot override provider, model, reasoning, tools,
concurrency, or fallback policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping


_PROFILE_FIELDS = {
    "allowed_role",
    "provider",
    "runtime",
    "model",
    "reasoning",
    "tool_profile",
    "max_concurrency",
    "fallback",
}
_ALLOWED_TOOL_PROFILES = {
    "read-only-discovery": "delegation-read-only-discovery",
    "immutable-read-only-review": "delegation-immutable-read-only-review",
}
_TOOL_PROFILE_TOOLS = {
    "read-only-discovery": (
        "read_file",
        "search_files",
        "web_extract",
        "web_search",
    ),
    "immutable-read-only-review": ("read_file", "search_files"),
}
_CANONICAL_PROFILE_DEFINITIONS = MappingProxyType(
    {
        "delegate-scout": MappingProxyType(
            {
                "allowed_role": "SCOUT",
                "provider": "openai",
                "runtime": "codex",
                "model": "gpt-5.6-luna",
                "reasoning": "max",
                "tool_profile": "read-only-discovery",
                "max_concurrency": 1,
                "fallback": "NONE",
            }
        ),
        "delegate-reviewer": MappingProxyType(
            {
                "allowed_role": "REVIEWER",
                "provider": "openai",
                "runtime": "codex",
                "model": "gpt-5.6-sol",
                "reasoning": "xhigh",
                "tool_profile": "immutable-read-only-review",
                "max_concurrency": 1,
                "fallback": "NONE",
            }
        ),
    }
)


class ExecutionProfileError(ValueError):
    """Raised before child launch when a named profile is not exact or usable."""


@dataclass(frozen=True)
class ExecutionProfile:
    name: str
    allowed_role: str
    provider: str
    runtime: str
    model: str
    reasoning: str
    tool_profile: str
    max_concurrency: int
    fallback: str

    @property
    def runtime_provider(self) -> str:
        if self.runtime == "codex":
            if self.provider != "openai":
                raise ExecutionProfileError(
                    f"execution profile {self.name!r}: runtime 'codex' requires provider 'openai'"
                )
            return "openai-codex"
        return self.provider

    @property
    def enabled_toolsets(self) -> list[str]:
        return [_ALLOWED_TOOL_PROFILES[self.tool_profile]]

    def delegation_config(self, _base: Mapping[str, Any]) -> dict[str, Any]:
        """Return only profile-owned values for credential resolution.

        Legacy delegation endpoint, key, and wire-mode overrides are deliberately
        excluded: a named profile must resolve its declared runtime rather than
        inherit an unrelated direct endpoint from the profile-less path.
        """
        return {
            "provider": self.runtime_provider,
            "model": self.model,
            "reasoning_effort": self.reasoning,
        }

    def launch_contract(
        self, *, resolved_provider: str | None, runtime_mode: str | None
    ) -> dict[str, Any]:
        """Return the expected tuple used for post-construction attestation."""
        expected_provider = self.runtime_provider
        expected_runtime_mode = "codex_responses" if self.runtime == "codex" else self.runtime
        if resolved_provider != expected_provider:
            raise ExecutionProfileError(
                f"execution profile {self.name!r}: resolved provider must be exactly "
                f"{expected_provider!r}, got {resolved_provider!r}"
            )
        if runtime_mode != expected_runtime_mode:
            raise ExecutionProfileError(
                f"execution profile {self.name!r}: runtime mode must be exactly "
                f"{expected_runtime_mode!r}, got {runtime_mode!r}"
            )
        return {
            "requestedProfile": self.name,
            "allowedRole": self.allowed_role,
            "declaredProvider": self.provider,
            "runtime": self.runtime,
            "runtimeMode": expected_runtime_mode,
            "resolvedProvider": expected_provider,
            "model": self.model,
            "reasoning": self.reasoning,
            "toolProfile": self.tool_profile,
            "enabledToolsets": self.enabled_toolsets,
            "expectedTools": list(_TOOL_PROFILE_TOOLS[self.tool_profile]),
            "maxConcurrency": self.max_concurrency,
            "fallback": self.fallback,
        }


def _exact_string(raw: Mapping[str, Any], field: str, profile_name: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ExecutionProfileError(
            f"execution profile {profile_name!r}: {field} must be an exact nonblank string"
        )
    return value


def _parse_profile(name: str, raw: Any) -> ExecutionProfile:
    canonical = _CANONICAL_PROFILE_DEFINITIONS.get(name)
    if canonical is None:
        raise ExecutionProfileError(f"unknown execution_profile {name!r}")
    if not isinstance(raw, Mapping) or set(raw) != _PROFILE_FIELDS:
        raise ExecutionProfileError(
            f"execution profile {name!r} must contain exactly {sorted(_PROFILE_FIELDS)}"
        )

    allowed_role = _exact_string(raw, "allowed_role", name)
    provider = _exact_string(raw, "provider", name)
    runtime = _exact_string(raw, "runtime", name)
    model = _exact_string(raw, "model", name)
    reasoning = _exact_string(raw, "reasoning", name)
    tool_profile = _exact_string(raw, "tool_profile", name)
    fallback = _exact_string(raw, "fallback", name)
    max_concurrency = raw.get("max_concurrency")

    if type(max_concurrency) is not int:
        raise ExecutionProfileError(
            f"execution profile {name!r}: max_concurrency must be exactly integer 1"
        )

    parsed = {
        "allowed_role": allowed_role,
        "provider": provider,
        "runtime": runtime,
        "model": model,
        "reasoning": reasoning,
        "tool_profile": tool_profile,
        "max_concurrency": max_concurrency,
        "fallback": fallback,
    }
    for field, expected in canonical.items():
        actual = parsed[field]
        if type(actual) is not type(expected) or actual != expected:
            raise ExecutionProfileError(
                f"execution profile {name!r}: {field} must be exactly {expected!r}"
            )

    profile = ExecutionProfile(
        name=name,
        allowed_role=allowed_role,
        provider=provider,
        runtime=runtime,
        model=model,
        reasoning=reasoning,
        tool_profile=tool_profile,
        max_concurrency=max_concurrency,
        fallback=fallback,
    )
    # Trigger cross-field validation now, before child construction.
    profile.runtime_provider
    return profile


def configured_profile_names(delegation_config: Mapping[str, Any]) -> list[str]:
    raw = delegation_config.get("profiles")
    if raw is None:
        return []
    if not isinstance(raw, Mapping):
        raise ExecutionProfileError("delegation.profiles must be an object")
    profiles = [_parse_profile(name, definition) for name, definition in raw.items()]
    return sorted(profile.name for profile in profiles)


def resolve_execution_profile(
    delegation_config: Mapping[str, Any], name: str | None
) -> ExecutionProfile | None:
    """Resolve one allowlisted name, preserving profile-less compatibility."""
    if name is None:
        return None
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ExecutionProfileError("execution_profile must be an exact nonblank string")
    raw_profiles = delegation_config.get("profiles")
    if not isinstance(raw_profiles, Mapping) or name not in raw_profiles:
        raise ExecutionProfileError(f"unknown execution_profile {name!r}")
    return _parse_profile(name, raw_profiles[name])


_SEMAPHORE_LOCK = threading.Lock()
_PROFILE_SEMAPHORES: dict[ExecutionProfile, threading.BoundedSemaphore] = {}


def profile_semaphore(profile: ExecutionProfile) -> threading.BoundedSemaphore:
    with _SEMAPHORE_LOCK:
        semaphore = _PROFILE_SEMAPHORES.get(profile)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(profile.max_concurrency)
            _PROFILE_SEMAPHORES[profile] = semaphore
        return semaphore


_MODEL_CATALOG_LOCK = threading.Lock()
_MODEL_CATALOG_CACHE: dict[str, tuple[float, frozenset[str] | None]] = {}
_MODEL_CATALOG_TTL_SECONDS = 60.0


def require_profile_model_available(
    profile: ExecutionProfile, access_token: str | None
) -> None:
    """Fail closed unless the exact configured Codex slug is in the live catalog."""
    if profile.runtime_provider != "openai-codex":
        raise ExecutionProfileError(
            f"execution profile {profile.name!r}: unsupported resolved provider "
            f"{profile.runtime_provider!r}; named profiles require 'openai-codex'"
        )
    if not isinstance(access_token, str) or not access_token:
        raise ExecutionProfileError(
            f"execution profile {profile.name!r}: openai-codex credential unavailable"
        )

    now = time.monotonic()
    with _MODEL_CATALOG_LOCK:
        cached = _MODEL_CATALOG_CACHE.get(access_token)
        if cached is not None and now - cached[0] < _MODEL_CATALOG_TTL_SECONDS:
            exact_models = cached[1]
        else:
            from hermes_cli.codex_models import fetch_live_codex_model_ids

            live = fetch_live_codex_model_ids(access_token)
            exact_models = frozenset(live) if live is not None else None
            _MODEL_CATALOG_CACHE[access_token] = (now, exact_models)

    if exact_models is None:
        raise ExecutionProfileError(
            f"execution profile {profile.name!r}: could not verify the live "
            "openai-codex model catalog"
        )
    if profile.model not in exact_models:
        raise ExecutionProfileError(
            f"execution profile {profile.name!r}: model {profile.model!r} is not "
            "available in the live openai-codex account catalog"
        )
