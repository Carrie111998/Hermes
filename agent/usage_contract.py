"""Sanitized, versioned usage/account contract for Hermes clients.

The contract deliberately keeps provider-reported quota, credential routing
health, and local session analytics in separate objects. Credentials are read
only to perform provider requests; no secret-bearing field is serialized.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, wait
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from agent.account_usage import (
    ACCOUNT_USAGE_PROVIDERS,
    AccountUsageSnapshot,
    fetch_account_usage_for_credential,
)
from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, get_env_prefer_dotenv
from hermes_cli.auth import _codex_access_token_is_expiring, read_credential_pool
from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor

USAGE_CONTRACT_NAME = "usage.accounts"
USAGE_CONTRACT_VERSION = 1

_FETCH_DEADLINE_SECONDS = 6.5
_FETCH_MAX_WORKERS = 4
_CACHE_FRESH_SECONDS = 60.0
_CACHE_STALE_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 128
_CACHE_LOCK = threading.Lock()
_CACHE: "OrderedDict[str, tuple[float, AccountUsageSnapshot]]" = OrderedDict()
_monotonic = time.monotonic

UsageFetcher = Callable[..., Optional[AccountUsageSnapshot]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_expiry(entry: Mapping[str, Any]) -> Optional[datetime]:
    raw_ms = entry.get("expires_at_ms")
    if isinstance(raw_ms, (int, float)) and math.isfinite(float(raw_ms)):
        return datetime.fromtimestamp(float(raw_ms) / 1000, tz=timezone.utc)
    raw = entry.get("expires_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sanitized_account_id(provider: str, entry: Mapping[str, Any]) -> str:
    """Return a stable opaque id without hashing credential material.

    Current pool rows have a persisted random ``id``. The fallback covers old
    rows using only non-secret metadata; array position and token bytes are
    intentionally excluded from both paths.
    """
    persisted_id = str(entry.get("id") or "").strip()
    if persisted_id:
        seed = f"id:{persisted_id}"
    else:
        seed = ":".join(
            (
                "legacy",
                str(entry.get("source") or "unknown").strip(),
                str(entry.get("auth_type") or "unknown").strip(),
                str(entry.get("label") or "unknown").strip(),
            )
        )
    digest = hashlib.blake2s(
        f"{USAGE_CONTRACT_NAME}:v{USAGE_CONTRACT_VERSION}:{provider}:{seed}".encode(),
        digest_size=8,
    ).hexdigest()
    return f"acct_{digest}"


def _runtime_access_token(entry: Mapping[str, Any]) -> str:
    """Return the credential's live secret, re-reading env-backed entries.

    Env-sourced entries persist only a reference (``env:<VAR>``) plus a
    fingerprint — never the secret itself — so the on-disk payload alone would
    wrongly mark every API-key credential unavailable.
    """
    token = str(entry.get("access_token") or "").strip()
    if token:
        return token
    source = str(entry.get("source") or "").strip()
    if source.startswith("env:"):
        env_var = source[len("env:") :].strip()
        if env_var:
            try:
                return str(get_env_prefer_dotenv(env_var) or "").strip()
            except Exception:
                return ""
    return ""


def _credential_fingerprint(entry: Mapping[str, Any], runtime_token: str) -> str:
    persisted = str(entry.get("secret_fingerprint") or "").strip()
    if persisted:
        return persisted
    return hashlib.sha256(runtime_token.encode()).hexdigest() if runtime_token else "missing"


def _canonical_endpoint(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return "<provider-default>"
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if not scheme or not host or parsed.username or parsed.password:
            return value.rstrip("/")
        explicit_port = parsed.port
        effective_port = explicit_port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        authority = host if effective_port is None else f"{host}:{effective_port}"
        path = "/" + "/".join(part for part in parsed.path.split("/") if part)
        return urlunsplit((scheme, authority, path.rstrip("/") or "/", parsed.query, ""))
    except (TypeError, ValueError):
        return value.rstrip("/")


def _cache_identity(
    provider: str,
    entry: Mapping[str, Any],
    runtime_token: str,
) -> Optional[str]:
    # Empty fetch keys trigger a native resolver that may rotate to another
    # Codex account. Without the final credential identity caching is unsafe.
    if not runtime_token:
        return None
    seed = {
        "profile": str(get_hermes_home().resolve()),
        "provider": provider,
        "entry_id": str(entry.get("id") or ""),
        "source": str(entry.get("source") or ""),
        "endpoint": _canonical_endpoint(entry.get("base_url")),
        "credential": _credential_fingerprint(entry, runtime_token),
    }
    canonical = json.dumps(seed, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _cache_read(key: Optional[str]) -> tuple[Optional[AccountUsageSnapshot], Optional[AccountUsageSnapshot]]:
    """Return (fresh, stale) while lazily evicting expired entries."""
    if key is None:
        return None, None
    now = _monotonic()
    with _CACHE_LOCK:
        expired = [cache_key for cache_key, (saved_at, _) in _CACHE.items() if now - saved_at > _CACHE_STALE_SECONDS]
        for cache_key in expired:
            _CACHE.pop(cache_key, None)
        cached = _CACHE.get(key)
        if cached is None:
            return None, None
        _CACHE.move_to_end(key)
        age = max(0.0, now - cached[0])
        if age <= _CACHE_FRESH_SECONDS:
            return cached[1], None
        return None, cached[1]


def _cache_write(key: Optional[str], snapshot: AccountUsageSnapshot) -> None:
    if key is None or not snapshot.available or snapshot.unavailable_reason:
        return
    now = _monotonic()
    with _CACHE_LOCK:
        expired = [cache_key for cache_key, (saved_at, _) in _CACHE.items() if now - saved_at > _CACHE_STALE_SECONDS]
        for cache_key in expired:
            _CACHE.pop(cache_key, None)
        _CACHE[key] = (now, snapshot)
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)


def _clear_usage_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _health(entry: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    expiry = _parse_expiry(entry)
    last_status = str(entry.get("last_status") or "").strip().lower()
    has_runtime_credential = bool(str(entry.get("access_token") or "").strip())

    if expiry is not None and expiry <= now:
        status = "expired"
    elif last_status == STATUS_DEAD:
        status = "error"
    elif last_status == STATUS_EXHAUSTED:
        status = "cooldown"
    elif not has_runtime_credential:
        status = "unavailable"
    else:
        status = "ready"

    result: dict[str, Any] = {
        "status": status,
        "auth_type": str(entry.get("auth_type") or "api_key"),
    }
    if expiry is not None:
        result["expires_at"] = _iso(expiry)
    retry_at = entry.get("last_error_reset_at")
    if status == "cooldown" and isinstance(retry_at, (int, float)) and math.isfinite(float(retry_at)):
        result["retry_at"] = _iso(datetime.fromtimestamp(float(retry_at), tz=timezone.utc))
    return result


def _quota_payload(snapshot: AccountUsageSnapshot, *, stale: bool = False) -> dict[str, Any]:
    if snapshot.unavailable_reason:
        return {"status": "unavailable", "reason": snapshot.unavailable_reason, "windows": []}
    if not snapshot.available:
        return {"status": "unavailable", "reason": "No provider usage data was returned", "windows": []}
    windows = []
    for window in snapshot.windows:
        used = window.used_percent
        windows.append(
            {
                "label": window.label,
                "used_percent": None if used is None else min(100.0, max(0.0, float(used))),
                "reset_at": _iso(window.reset_at) if window.reset_at else None,
                "detail": window.detail,
            }
        )
    details = list(snapshot.details)
    if stale:
        details.append(f"Cached · {_iso(snapshot.fetched_at)}")
    return {
        "status": "available",
        "plan": snapshot.plan,
        "source": "provider_reported",
        "fetched_at": _iso(snapshot.fetched_at),
        "stale": stale,
        "windows": windows,
        "details": details,
    }


def _routing_summary(accounts: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"ready": 0, "cooldown": 0, "expired": 0, "error": 0, "unavailable": 0}
    for account in accounts:
        status = account["health"]["status"]
        summary[status] = summary.get(status, 0) + 1
    return summary


def _local_analytics(
    usage: Optional[Mapping[str, Any]],
    *,
    provider: Optional[str],
    model: Optional[str],
) -> dict[str, Any]:
    if not usage and not provider and not model:
        return {"status": "unavailable"}
    usage = usage or {}
    return {
        "status": "available",
        "provider": str(provider) if provider else None,
        "model": str(model) if model else None,
        "calls": int(usage.get("calls") or 0),
        "tokens": {
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
            "total": int(usage.get("total") or 0),
        },
    }


def build_usage_contract(
    *,
    session_usage: Optional[Mapping[str, Any]] = None,
    session_provider: Optional[str] = None,
    session_model: Optional[str] = None,
    fetch_provider_usage: bool = True,
    fetcher: UsageFetcher = fetch_account_usage_for_credential,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build the canonical sanitized usage contract for the active profile.

    Independent provider reads run concurrently behind a hard request deadline.
    Workers are deliberately pure: only the request thread reads or mutates the
    process cache, so a detached late worker cannot alter state after return.
    """
    generated_at = now or _utc_now()
    raw_pool = read_credential_pool()
    providers: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []

    normalized_providers = sorted(
        (
            (str(raw_provider or "").strip().lower(), raw_provider)
            for raw_provider in raw_pool
        ),
        key=lambda item: item[0],
    )
    for provider, raw_provider in normalized_providers:
        raw_entries = raw_pool.get(raw_provider)
        if not provider or not isinstance(raw_entries, list):
            continue
        usage_supported = provider in ACCOUNT_USAGE_PROVIDERS
        accounts: list[dict[str, Any]] = []
        valid_entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        for raw_entry in valid_entries:
            account_id = sanitized_account_id(provider, raw_entry)
            runtime_token = _runtime_access_token(raw_entry)
            health = _health({**raw_entry, "access_token": runtime_token}, generated_at)
            quota: dict[str, Any]
            fetch_key: Optional[str] = runtime_token
            cache_key: Optional[str] = None

            if not usage_supported:
                quota = {"status": "unsupported", "windows": []}
            elif not fetch_provider_usage:
                quota = {"status": "unavailable", "reason": "Usage was not refreshed", "windows": []}
            elif health["status"] in {"cooldown", "expired", "error", "unavailable"}:
                reason = "Credential is cooling down" if health["status"] == "cooldown" else "Credential is not usable"
                quota = {"status": "unavailable", "reason": reason, "windows": []}
            else:
                if (
                    provider == "openai-codex"
                    and runtime_token
                    and _codex_access_token_is_expiring(runtime_token, 0)
                ):
                    if len(valid_entries) == 1:
                        # The fetcher's empty-key path delegates to the native
                        # resolver. The final rotated identity is unknown here,
                        # so this request intentionally bypasses the cache.
                        fetch_key = None
                    else:
                        quota = {
                            "status": "unavailable",
                            "reason": "Credential token expired",
                            "windows": [],
                        }
                        account = {
                            "account_id": account_id,
                            "health": health,
                            "quota": quota,
                            "routing": {
                                "priority": int(raw_entry.get("priority") or 0),
                                "request_count": int(raw_entry.get("request_count") or 0),
                            },
                        }
                        accounts.append(account)
                        continue

                cache_key = _cache_identity(provider, raw_entry, runtime_token) if fetch_key is not None else None
                fresh, stale = _cache_read(cache_key)
                if fresh is not None:
                    quota = _quota_payload(fresh)
                else:
                    quota = {"status": "loading", "windows": []}

            account = {
                "account_id": account_id,
                "health": health,
                "quota": quota,
                "routing": {
                    "priority": int(raw_entry.get("priority") or 0),
                    "request_count": int(raw_entry.get("request_count") or 0),
                },
            }
            accounts.append(account)
            if quota.get("status") == "loading":
                jobs.append(
                    {
                        "account": account,
                        "provider": provider,
                        "api_key": fetch_key,
                        "base_url": str(raw_entry.get("base_url") or "") or None,
                        "cache_key": cache_key,
                        "stale": stale,
                    }
                )

        accounts.sort(key=lambda account: (account["routing"]["priority"], account["account_id"]))
        if accounts:
            providers.append(
                {
                    "provider": provider,
                    "usage_capability": "supported" if usage_supported else "unsupported",
                    "routing": _routing_summary(accounts),
                    "accounts": accounts,
                }
            )

    if jobs:
        executor = DaemonThreadPoolExecutor(
            max_workers=min(_FETCH_MAX_WORKERS, len(jobs)),
            thread_name_prefix="usage-account",
        )
        future_jobs: dict[Future, dict[str, Any]] = {}
        try:
            for job in jobs:
                future = executor.submit(
                    fetcher,
                    job["provider"],
                    api_key=job["api_key"],
                    base_url=job["base_url"],
                )
                future_jobs[future] = job
            done, pending = wait(future_jobs, timeout=_FETCH_DEADLINE_SECONDS)

            for future in done:
                job = future_jobs[future]
                try:
                    snapshot = future.result()
                except httpx.TimeoutException:
                    cached = job["stale"]
                    job["account"]["quota"] = (
                        _quota_payload(cached, stale=True)
                        if cached is not None
                        else {"status": "error", "reason": "Provider usage request timed out", "windows": []}
                    )
                except Exception:
                    job["account"]["quota"] = {
                        "status": "error",
                        "reason": "Provider usage request failed",
                        "windows": [],
                    }
                else:
                    if snapshot is None:
                        job["account"]["quota"] = {
                            "status": "unavailable",
                            "reason": "No provider usage data was returned",
                            "windows": [],
                        }
                    else:
                        job["account"]["quota"] = _quota_payload(snapshot)
                        _cache_write(job["cache_key"], snapshot)

            for future in pending:
                job = future_jobs[future]
                cached = job["stale"]
                job["account"]["quota"] = (
                    _quota_payload(cached, stale=True)
                    if cached is not None
                    else {"status": "error", "reason": "Provider usage request timed out", "windows": []}
                )
                future.cancel()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    return {
        "contract": {"name": USAGE_CONTRACT_NAME, "version": USAGE_CONTRACT_VERSION},
        "capabilities": {
            "provider_usage": {
                "per_account": True,
                "providers": sorted(ACCOUNT_USAGE_PROVIDERS),
            },
            "credential_pool_health": True,
            "local_session_analytics": True,
        },
        "generated_at": _iso(generated_at),
        "providers": providers,
        "local": _local_analytics(
            session_usage,
            provider=session_provider,
            model=session_model,
        ),
    }
