"""Sanitized, versioned usage/account contract for Hermes clients.

The contract deliberately keeps provider-reported quota, credential routing
health, and local session analytics in separate objects. Credentials are read
only to perform provider requests; no secret-bearing field is serialized.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from agent.account_usage import (
    ACCOUNT_USAGE_PROVIDERS,
    AccountUsageSnapshot,
    fetch_account_usage_for_credential,
)
from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED
from hermes_cli.auth import read_credential_pool

USAGE_CONTRACT_NAME = "usage.accounts"
USAGE_CONTRACT_VERSION = 1

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


def _quota_payload(snapshot: AccountUsageSnapshot) -> dict[str, Any]:
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
    return {
        "status": "available",
        "plan": snapshot.plan,
        "source": "provider_reported",
        "windows": windows,
        "details": list(snapshot.details),
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
    """Build the canonical sanitized usage contract for the active profile."""
    generated_at = now or _utc_now()
    raw_pool = read_credential_pool()
    providers: list[dict[str, Any]] = []

    for raw_provider in sorted(raw_pool):
        provider = str(raw_provider or "").strip().lower()
        raw_entries = raw_pool.get(raw_provider)
        if not provider or not isinstance(raw_entries, list):
            continue
        usage_supported = provider in ACCOUNT_USAGE_PROVIDERS
        accounts: list[dict[str, Any]] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            account_id = sanitized_account_id(provider, raw_entry)
            health = _health(raw_entry, generated_at)
            if not usage_supported:
                quota = {"status": "unsupported", "windows": []}
            elif not fetch_provider_usage:
                quota = {"status": "unavailable", "reason": "Usage was not refreshed", "windows": []}
            elif health["status"] in {"cooldown", "expired", "error", "unavailable"}:
                reason = "Credential is cooling down" if health["status"] == "cooldown" else "Credential is not usable"
                quota = {"status": "unavailable", "reason": reason, "windows": []}
            else:
                try:
                    snapshot = fetcher(
                        provider,
                        api_key=str(raw_entry.get("access_token") or ""),
                        base_url=str(raw_entry.get("base_url") or "") or None,
                    )
                except Exception:
                    quota = {"status": "error", "reason": "Provider usage request failed", "windows": []}
                else:
                    quota = (
                        _quota_payload(snapshot)
                        if snapshot is not None
                        else {"status": "unavailable", "reason": "No provider usage data was returned", "windows": []}
                    )
            accounts.append(
                {
                    "account_id": account_id,
                    "health": health,
                    "quota": quota,
                    "routing": {
                        "priority": int(raw_entry.get("priority") or 0),
                        "request_count": int(raw_entry.get("request_count") or 0),
                    },
                }
            )
        accounts.sort(key=lambda account: (account["routing"]["priority"], account["account_id"]))
        if not accounts:
            continue
        providers.append(
            {
                "provider": provider,
                "usage_capability": "supported" if usage_supported else "unsupported",
                "routing": _routing_summary(accounts),
                "accounts": accounts,
            }
        )

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
