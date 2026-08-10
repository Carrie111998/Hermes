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
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
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
# Process-wide admission bound: futures submitted but not yet finished
# (running + queued). Builds that cannot acquire a permit serve stale /
# negative cache / an immediate error instead of queueing unboundedly.
_MAX_IN_FLIGHT = 8
_CACHE_FRESH_SECONDS = 60.0
_CACHE_STALE_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 128
_NEGATIVE_TTL_SECONDS = 30.0
_NEGATIVE_TTL_MAX_SECONDS = 120.0
_CACHE_LOCK = threading.RLock()
_SUBMITTED = 0  # futures handed to the shared executor and not yet finished
_CACHE: "OrderedDict[str, tuple[float, AccountUsageSnapshot]]" = OrderedDict()
_NEGATIVE: "dict[str, tuple[float, dict[str, Any]]]" = {}
# Singleflight registry: cache_key -> {"future", "deadline", "generation"}.
# Only request threads that observe an outcome within their deadline may
# write caches or remove entries (compare-and-remove by generation).
_INFLIGHT: "dict[str, dict[str, Any]]" = {}
_GENERATION = 0
_ADMISSION = threading.Semaphore(_MAX_IN_FLIGHT)
_EXECUTOR: "Optional[DaemonThreadPoolExecutor]" = None
_monotonic = time.monotonic


def _shared_executor() -> DaemonThreadPoolExecutor:
    global _EXECUTOR
    with _CACHE_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = DaemonThreadPoolExecutor(
                max_workers=_FETCH_MAX_WORKERS,
                thread_name_prefix="usage-account",
            )
        return _EXECUTOR

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
    """Bind the cache identity to the *current* runtime secret.

    The persisted ``secret_fingerprint`` can lag a rotation (the env value was
    swapped but the pool record was not rewritten yet), so it must never anchor
    the cache identity — a stale fingerprint would let a rotated credential
    inherit its predecessor's cached quota. The fingerprint is a secret-derived
    identifier: it stays inside the in-memory hash seed and is never logged or
    serialized into the contract.
    """
    del entry  # persisted fingerprints are intentionally not consulted
    if not runtime_token:
        return "missing"
    # Domain-separated so the digest can never be confused with a hash of the
    # same secret computed for another purpose.
    return hashlib.sha256(f"usage.accounts/v1|{runtime_token}".encode()).hexdigest()


def _canonical_endpoint(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return "<provider-default>"
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        if not scheme or not host:
            return value.rstrip("/")
        # userinfo, query, and fragment never identify the usage endpoint and
        # may carry secrets or short-lived signatures — exclude them from the
        # cache identity.
        explicit_port = parsed.port
        default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        authority = host if explicit_port is None or explicit_port == default_port else f"{host}:{explicit_port}"
        path = "/" + "/".join(part for part in parsed.path.split("/") if part)
        return urlunsplit((scheme, authority, path.rstrip("/") or "/", "", ""))
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
    global _EXECUTOR
    with _CACHE_LOCK:
        _CACHE.clear()
        _NEGATIVE.clear()
        _INFLIGHT.clear()
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False, cancel_futures=True)
            _EXECUTOR = None


def _negative_read(key: Optional[str]) -> Optional[dict[str, Any]]:
    if key is None:
        return None
    now = _monotonic()
    with _CACHE_LOCK:
        entry = _NEGATIVE.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if now >= expires_at:
            _NEGATIVE.pop(key, None)
            return None
        return dict(payload)


def _negative_write(key: Optional[str], payload: dict[str, Any], ttl: float) -> None:
    if key is None:
        return
    ttl = max(1.0, min(ttl, _NEGATIVE_TTL_MAX_SECONDS))
    with _CACHE_LOCK:
        if len(_NEGATIVE) >= _CACHE_MAX_ENTRIES:
            oldest = min(_NEGATIVE, key=lambda k: _NEGATIVE[k][0])
            _NEGATIVE.pop(oldest, None)
        _NEGATIVE[key] = (_monotonic() + ttl, dict(payload))


def _retry_after_seconds(exc: httpx.HTTPStatusError) -> float:
    """Parse Retry-After (delta-seconds or HTTP-date), capped at the max TTL."""
    raw = exc.response.headers.get("retry-after", "").strip()
    if raw:
        try:
            return max(1.0, min(float(raw), _NEGATIVE_TTL_MAX_SECONDS))
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                target = parsedate_to_datetime(raw)
                delta = (target - datetime.now(timezone.utc)).total_seconds()
                return max(1.0, min(delta, _NEGATIVE_TTL_MAX_SECONDS))
            except (TypeError, ValueError):
                pass
    return _NEGATIVE_TTL_SECONDS


def _failure_outcome(exc: BaseException, stale: Optional[AccountUsageSnapshot]) -> tuple[dict[str, Any], Optional[float]]:
    """Map a fetch failure to (quota payload, negative-cache TTL or None).

    401/403 -> unavailable with a safe auth reason (short negative TTL to
    short-circuit pointless repeats; never served from stale). 429 -> error
    with Retry-After-aware negative TTL. Retryable transient/upstream faults
    serve an eligible stale snapshot when present; otherwise an error plus a
    short negative entry. Local misconfiguration (UnsupportedProtocol /
    LocalProtocolError) and programming errors are never masked and are not
    negatively cached (they need a fix, not a cooldown).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return (
                {
                    "status": "unavailable",
                    "reason": f"Credential authentication failed (HTTP {status})",
                    "windows": [],
                },
                _NEGATIVE_TTL_SECONDS,
            )
        if status == 429:
            return (
                {"status": "error", "reason": "Provider rate limited the usage request", "windows": []},
                _retry_after_seconds(exc),
            )
        if status >= 500:
            if stale is not None:
                return _quota_payload(stale, stale=True), None
            return (
                {"status": "error", "reason": "Provider usage request failed", "windows": []},
                _NEGATIVE_TTL_SECONDS,
            )
        return {"status": "error", "reason": "Provider usage request failed", "windows": []}, None
    if isinstance(exc, httpx.TimeoutException):
        if stale is not None:
            return _quota_payload(stale, stale=True), None
        return (
            {"status": "error", "reason": "Provider usage request timed out", "windows": []},
            _NEGATIVE_TTL_SECONDS,
        )
    if _is_retryable_fetch_error(exc):
        if stale is not None:
            return _quota_payload(stale, stale=True), None
        return (
            {"status": "error", "reason": "Provider usage request failed", "windows": []},
            _NEGATIVE_TTL_SECONDS,
        )
    return {"status": "error", "reason": "Provider usage request failed", "windows": []}, None


def _is_retryable_fetch_error(exc: BaseException) -> bool:
    """Classify failures that may be masked by a stale cached snapshot.

    Retryable: timeouts, network failures (connect/read/write/close), remote
    protocol and proxy failures (upstream/transit-side), and 5xx responses.
    Never maskable: 401/403/429 (auth and rate limiting must surface) and
    local faults — ``UnsupportedProtocol``/``LocalProtocolError`` indicate a
    misconfigured endpoint or call-site bug that staleness would hide.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
        ),
    )


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


_PROVIDER_LABELS = {
    "anthropic": "Claude",
    "kimi-coding": "Kimi",
    "nous": "Nous",
    "openai-codex": "Codex",
    "openrouter": "OpenRouter",
    "xai": "xAI",
}


def _provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider) or provider.replace("-", " ").replace("_", " ").title()


def _display_names(provider: str, entries: list[Mapping[str, Any]]) -> dict[int, str]:
    """Stable per-account display names: ``Codex 1``/``Codex 2``...

    The ordinal comes from sorting by ``sanitized_account_id`` only — never by
    priority, cooldown state, request counts, or array position — so a name
    cannot change because routing, health, or input order shifted. Legacy
    no-id rows sort by their non-secret metadata hash on the same path. No
    label, email, or credential source ever reaches the display string.
    """
    order = sorted(range(len(entries)), key=lambda i: sanitized_account_id(provider, entries[i]))
    return {entry_index: f"{_provider_label(provider)} {ordinal}" for ordinal, entry_index in enumerate(order, start=1)}


def _flight_key(job: dict[str, Any]) -> str:
    """Dedupe identity for every fetch, including cache-less native refreshes.

    ``cache_key`` is reused when present. Codex native-resolver fetches have
    ``cache_key=None`` (their post-rotation identity is unknowable, so caching
    is disabled) — but they still singleflight on a non-secret identity of
    profile + provider + sanitized account id.
    """
    if job["cache_key"] is not None:
        return str(job["cache_key"])
    seed = "|".join(
        (
            str(get_hermes_home().resolve()),
            str(job["provider"]),
            str(job["account"].get("account_id") or ""),
        )
    )
    return "flight:" + hashlib.sha256(seed.encode()).hexdigest()


def _release_admission(_f: Future) -> None:
    # Worker-side callbacks only ever release admission and (un)count — they
    # never touch caches, the registry, or the contract.
    global _SUBMITTED
    with _CACHE_LOCK:
        _SUBMITTED = max(0, _SUBMITTED - 1)
    _ADMISSION.release()


def _usage_fetch_stats_for_tests() -> dict[str, int]:
    """Read-only introspection for tests; contains no secrets."""
    with _CACHE_LOCK:
        return {
            "in_flight_submitted": _SUBMITTED,
            "registered_flights": len(_INFLIGHT),
            "negative_entries": len(_NEGATIVE),
            "cached_snapshots": len(_CACHE),
        }


def _run_fetch(
    fetcher: UsageFetcher,
    provider: str,
    api_key: Optional[str],
    base_url: Optional[str],
) -> tuple[float, bool, Any]:
    """Worker wrapper: record the real settle time with the outcome.

    The request thread may consume the result long after the deadline; what
    decides timeout-vs-render is when the fetch ACTUALLY settled, not when the
    consumer looked. Workers stay pure — this only stamps time.
    """
    try:
        value = fetcher(provider, api_key=api_key, base_url=base_url)
        return (_monotonic(), True, value)
    except Exception as exc:
        return (_monotonic(), False, exc)


def _begin_fetch(job: dict[str, Any], fetcher: UsageFetcher, deadline: float) -> dict[str, Any]:
    """Start or join the account's flight. Never blocks on the future.

    Per flight_key: an existing entry with time left is joined; otherwise —
    inside ONE critical section — a non-blocking admission acquire, submit,
    and generation-stamped registration, so each flight submits at most once.
    Negative classifications short-circuit before any of that.
    """
    global _GENERATION, _SUBMITTED
    key = job["cache_key"]
    flight = _flight_key(job)
    negative = _negative_read(key)
    if negative is not None:
        return {"mode": "done", "outcome": negative}
    with _CACHE_LOCK:
        # Re-check both caches inside the critical section: a concurrent build
        # may have completed this flight (positive) or classified its failure
        # (negative) between our unlocked reads at job creation and now.
        # RLock makes the nested reads safe.
        fresh, _stale_again = _cache_read(key)
        if fresh is not None:
            return {"mode": "done", "outcome": _quota_payload(fresh)}
        negative = _negative_read(key)
        if negative is not None:
            return {"mode": "done", "outcome": negative}
        entry = _INFLIGHT.get(flight)
        if entry is not None and _monotonic() < entry["deadline"]:
            return {"mode": "join", "future": entry["future"], "deadline": entry["deadline"]}
        if not _ADMISSION.acquire(blocking=False):
            return {"mode": "busy"}
        try:
            future = _shared_executor().submit(
                _run_fetch,
                fetcher,
                job["provider"],
                job["api_key"],
                job["base_url"],
            )
        except Exception:
            # Never leak the permit on a broken executor; surface a safe error.
            _ADMISSION.release()
            return {
                "mode": "done",
                "outcome": {"status": "error", "reason": "Provider usage request failed", "windows": []},
            }
        _SUBMITTED += 1
        future.add_done_callback(_release_admission)
        _GENERATION += 1
        _INFLIGHT[flight] = {"future": future, "deadline": deadline, "generation": _GENERATION}
        return {
            "mode": "owner",
            "future": future,
            "flight": flight,
            "generation": _GENERATION,
            "deadline": deadline,
        }


def _finish_fetch(job: dict[str, Any], handle: dict[str, Any], deadline: Optional[float] = None) -> dict[str, Any]:
    """Render the flight's outcome for this build.

    Only the owner, only within its deadline, writes positive/negative cache
    and compare-and-removes its registry entry. Joiners and timed-out owners
    never write; an orphaned future's late settle writes nothing and cannot
    remove a replacement entry. Admission pressure falls back to a fresh
    negative classification, then eligible stale, then an immediate error.
    The flight's own deadline (captured at registration) wins over the
    caller's, so a late finish can never masquerade as in-deadline.
    """
    key = job["cache_key"]
    stale = job["stale"]
    mode = handle["mode"]
    if mode == "done":
        return handle["outcome"]
    if mode == "busy":
        negative = _negative_read(key)
        if negative is not None:
            return negative
        if stale is not None:
            return _quota_payload(stale, stale=True)
        return {"status": "error", "reason": "Provider usage request timed out", "windows": []}

    future = handle["future"]
    owner = mode == "owner"
    deadline = handle.get("deadline") or deadline or _monotonic()
    remaining = deadline - _monotonic()
    outcome: dict[str, Any]
    if remaining <= 0:
        # Consumer is past the deadline without a settle record: check whether
        # the future already completed in time before declaring a timeout.
        if future.done():
            settled_at, ok, value = future.result()
            if settled_at <= deadline:
                outcome = _settled_outcome(job, owner, ok, value, settled_at, deadline)
                self_outcome = outcome
                if owner:
                    _finish_owner_cleanup(handle)
                return self_outcome
        outcome = (
            _quota_payload(stale, stale=True)
            if stale is not None
            else {"status": "error", "reason": "Provider usage request timed out", "windows": []}
        )
    else:
        try:
            settled_at, ok, value = future.result(timeout=remaining)
        except FuturesTimeoutError:
            # No negative write here: the flight is still running and the next
            # build past the deadline must be free to register a replacement.
            outcome = (
                _quota_payload(stale, stale=True)
                if stale is not None
                else {"status": "error", "reason": "Provider usage request timed out", "windows": []}
            )
        else:
            outcome = _settled_outcome(job, owner, ok, value, settled_at, deadline)

    if owner:
        _finish_owner_cleanup(handle)
    return outcome


def _settled_outcome(
    job: dict[str, Any],
    owner: bool,
    ok: bool,
    value: Any,
    settled_at: float,
    deadline: float,
) -> dict[str, Any]:
    """Render a settled fetch. The settle timestamp — not the consumption
    time — decides validity: a result that settled at/before the flight
    deadline is real and cacheable by its owner; one that settled after is a
    timeout/orphan and writes nothing."""
    key = job["cache_key"]
    stale = job["stale"]
    if settled_at > deadline:
        return (
            _quota_payload(stale, stale=True)
            if stale is not None
            else {"status": "error", "reason": "Provider usage request timed out", "windows": []}
        )
    if not ok:
        outcome, negative_ttl = _failure_outcome(value, stale)
        if owner and negative_ttl is not None:
            _negative_write(key, outcome, negative_ttl)
        return outcome
    if value is None:
        return {
            "status": "unavailable",
            "reason": "No provider usage data was returned",
            "windows": [],
        }
    if owner:
        _cache_write(key, value)
    return _quota_payload(value)


def _finish_owner_cleanup(handle: dict[str, Any]) -> None:
    """Compare-and-remove the owner's registry entry by generation."""
    with _CACHE_LOCK:
        current = _INFLIGHT.get(handle["flight"])
        if current is not None and current["generation"] == handle["generation"]:
            _INFLIGHT.pop(handle["flight"], None)


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
        display_names = _display_names(provider, valid_entries)
        for entry_index, raw_entry in enumerate(valid_entries):
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
                            "display_name": display_names[entry_index],
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
                    negative = _negative_read(cache_key)
                    if negative is not None:
                        # Short-circuit: recent failure classification (auth,
                        # rate limit, transient) is still true; do not re-hit.
                        quota = negative
                    else:
                        quota = {"status": "loading", "windows": []}

            account = {
                "account_id": account_id,
                "display_name": display_names[entry_index],
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
        deadline = _monotonic() + _FETCH_DEADLINE_SECONDS
        handles = [_begin_fetch(job, fetcher, deadline) for job in jobs]
        for job, handle in zip(jobs, handles):
            job["account"]["quota"] = _finish_fetch(job, handle, deadline)

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
