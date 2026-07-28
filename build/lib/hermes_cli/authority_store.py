"""Authority-store capability contract and deployment safety checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


class AuthorityStoreConfigurationError(RuntimeError):
    """Raised when the configured topology exceeds the store's guarantees."""


@dataclass(frozen=True)
class StoreCapabilities:
    backend: str
    durable: bool
    transactional: bool
    single_host_multi_process: bool
    multi_host: bool
    atomic_event_claims: bool
    resource_fencing: bool


SQLITE_CAPABILITIES = StoreCapabilities(
    backend="sqlite",
    durable=True,
    transactional=True,
    single_host_multi_process=True,
    multi_host=False,
    atomic_event_claims=True,
    resource_fencing=True,
)


def capabilities(config: Mapping[str, Any]) -> StoreCapabilities:
    agentic = config.get("agentic") or {}
    store = agentic.get("authority_store") or {}
    backend = str(store.get("backend") or "sqlite").lower()
    if backend != "sqlite":
        raise AuthorityStoreConfigurationError(
            f"authority store backend {backend!r} is not installed; "
            "refusing to fall back to SQLite"
        )
    return SQLITE_CAPABILITIES


def validate_topology(config: Mapping[str, Any]) -> StoreCapabilities:
    """Fail closed when deployment claims exceed authoritative guarantees."""
    agentic = config.get("agentic") or {}
    store = agentic.get("authority_store") or {}
    scope = str(store.get("deployment_scope") or "single_host").lower()
    if scope not in {"single_process", "single_host", "multi_host"}:
        raise AuthorityStoreConfigurationError(
            "agentic.authority_store.deployment_scope must be "
            "single_process, single_host, or multi_host"
        )
    result = capabilities(config)
    if scope == "single_host" and not result.single_host_multi_process:
        raise AuthorityStoreConfigurationError(
            f"{result.backend} does not support single-host worker coordination"
        )
    if scope == "multi_host" and not result.multi_host:
        raise AuthorityStoreConfigurationError(
            f"{result.backend} cannot safely coordinate multi-host workers; "
            "configure a backend with transactional row locking"
        )
    return result


def status(config: Mapping[str, Any]) -> dict[str, Any]:
    agentic = config.get("agentic") or {}
    store = agentic.get("authority_store") or {}
    scope = str(store.get("deployment_scope") or "single_host").lower()
    try:
        result = validate_topology(config)
    except AuthorityStoreConfigurationError as exc:
        backend = str(store.get("backend") or "sqlite").lower()
        declared = (
            asdict(SQLITE_CAPABILITIES)
            if backend == "sqlite"
            else {"backend": backend}
        )
        return {
            **declared,
            "deployment_scope": scope,
            "ready": False,
            "error": str(exc),
        }
    return {
        **asdict(result),
        "deployment_scope": scope,
        "ready": True,
        "error": None,
    }
