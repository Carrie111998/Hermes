"""Fan out subscription usage across every authenticated provider.

Two phases, deliberately separated:

**Detect** (cheap, disk only). Which providers does this machine have a
credential for? Answered from ``$HERMES_HOME/auth.json``'s persisted
``credential_pool``, the env vars each provider declares, and a handful of
well-known credential files. It must never call ``load_pool()``: seeding is
not side-effect free — the Copilot branch exchanges a raw ``gh`` token for an
API token and logs when that degrades — so sweeping 58 registry providers on
every panel open would be a write, not a read.

**Fetch** (network). Only for detected providers whose profile actually
implements ``fetch_usage``. Credentials are resolved here, once, and handed to
the plugin; failures are per-provider, so a dead Anthropic never hides a live
Kimi.

Results are cached on disk with a per-provider TTL and served
stale-while-revalidate, so the panel paints from cache immediately instead of
holding three cross-host round-trips — which matters a lot on a metered or
tunnelled connection.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from agent.provider_usage_types import (
    STATE_NETWORK_ERROR,
    STATE_NO_USAGE_ENDPOINT,
    STATE_NOT_AUTHENTICATED,
    STATE_PARSE_ERROR,
    STATE_RATE_LIMITED,
    STATE_UNAUTHORIZED,
    ProviderUsage,
    UsageWindow,
    to_datetime,
)

logger = logging.getLogger(__name__)

# Concurrency cap: enough to hide latency behind the slowest provider, low
# enough that a handful of cross-host TLS handshakes don't stampede a metered
# link. Providers are few; this is a latency knob, not a throughput one.
_MAX_WORKERS = 6
_DEFAULT_TIMEOUT = 8.0

# Extra credential files that prove authentication without reading a secret's
# value — existence is the whole signal.
#
# Copilot is the one that is easy to miss: the pool seeds it from `gh auth
# token`, so on a machine that has never persisted it there is no auth.json
# key and no env var to find. `gh`'s hosts file exists once `gh auth login` has
# run — including the keyring-backed form, which stores no token in the file —
# so its presence, not its contents, is the signal.
_CREDENTIAL_FILES = {
    "anthropic": ("~/.claude/.credentials.json", "~/.hermes/.anthropic_oauth.json"),
    "openai-codex": ("~/.codex/auth.json",),
    "qwen-oauth": ("~/.qwen/oauth_creds.json",),
    "copilot": ("$GH_CONFIG_DIR/hosts.yml", "~/.config/gh/hosts.yml"),
}

# `openrouter` is hardcoded into the pool seeder ahead of the registry lookup
# (agent/credential_pool.py::_seed_from_env), so it is absent from
# PROVIDER_REGISTRY and a registry-only sweep silently drops it. Mirrors the
# same explicit union in hermes_cli/auth_commands.py::auth_list_command.
_EXTRA_CANDIDATES = ("openrouter",)

_CACHE_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Detection ──────────────────────────────────────────────────────────────


def _credential_pool() -> Dict[str, Any]:
    """The persisted pool, profile + global-root merged.

    ``read_credential_pool`` rather than the raw auth store: in profile mode a
    provider authenticated only at global scope lives in the root ``auth.json``,
    and reading the profile file alone makes it silently undetectable.
    """
    try:
        from hermes_cli.auth import read_credential_pool

        pool = read_credential_pool()
    except Exception:
        return {}
    return pool if isinstance(pool, dict) else {}


def _registry() -> Dict[str, Any]:
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        return dict(PROVIDER_REGISTRY)
    except Exception:
        return {}


def candidate_providers() -> List[str]:
    """Every pool key worth probing — the union auth_list_command walks."""
    names = set(_registry().keys())
    names.update(_EXTRA_CANDIDATES)

    names.update(str(key) for key in _credential_pool().keys())

    try:
        from agent.credential_pool import list_custom_pool_providers

        names.update(list_custom_pool_providers())
    except Exception:
        pass

    # Custom providers configured but never yet persisted into the pool. Without
    # this the panel and `hermes auth list` disagree about what you are signed
    # into — the two surfaces walk the same union, so they must walk all of it.
    try:
        from hermes_cli.auth_commands import _get_custom_provider_entries

        names.update(
            str(entry["provider_key"])
            for entry in _get_custom_provider_entries()
            if entry.get("provider_key")
        )
    except Exception:
        pass

    return sorted(name for name in names if name)


def _has_env_credential(provider: str, registry: Dict[str, Any]) -> bool:
    config = registry.get(provider)
    env_vars = tuple(getattr(config, "api_key_env_vars", ()) or ()) if config else ()
    if not env_vars:
        return False

    try:
        from agent.credential_pool import get_env_prefer_dotenv
    except Exception:
        return False

    return any(get_env_prefer_dotenv(var).strip() for var in env_vars)


def _has_credential_file(provider: str) -> bool:
    for candidate in _CREDENTIAL_FILES.get(provider, ()):
        expanded = os.path.expandvars(candidate)
        if "$" in expanded:  # the env var is unset — that path doesn't exist
            continue
        try:
            if Path(expanded).expanduser().is_file():
                return True
        except OSError:
            continue
    return False


def detect_providers() -> List[str]:
    """Providers this machine holds a credential for. No network, no seeding.

    The persisted ``credential_pool`` is the strongest signal — it is what
    ``load_pool()`` rehydrates from, so on a machine that has authenticated at
    least once this alone reproduces the live sweep exactly. Env vars and
    credential files cover the case where a key is present but has never been
    seeded into the pool.
    """
    registry = _registry()
    persisted = _credential_pool()
    # A key whose value is an EMPTY list is a provider that was seeded once
    # and has since been pruned — real state on a live machine. Treating the
    # key alone as proof would report "authenticated" for a provider with no
    # usable credential left.
    persisted_keys = {str(key) for key, value in persisted.items() if value}

    detected = []
    for provider in candidate_providers():
        if (
            provider in persisted_keys
            or _has_env_credential(provider, registry)
            or _has_credential_file(provider)
        ):
            detected.append(provider)

    return detected


# ── Cache ──────────────────────────────────────────────────────────────────


def _cache_path() -> Path:
    try:
        from hermes_cli.main import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "provider_usage_cache.json"


def _read_cache() -> Dict[str, Any]:
    path = _cache_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: Dict[str, Any]) -> None:
    path = _cache_path()
    try:
        from utils import atomic_write_text

        path.parent.mkdir(parents=True, exist_ok=True)
        # The shared writer, not a bare os.replace: ~/.hermes is symlinked into
        # a dotfiles repo on plenty of machines, and a plain rename replaces the
        # symlink with a regular file (#16743). It also carries the EXDEV copy
        # fallback and the Windows contended-rename retry.
        atomic_write_text(path, json.dumps(data))
    except Exception as exc:
        logger.debug("provider usage cache write failed: %s", exc)


def _cache_entry_age(entry: Dict[str, Any]) -> float:
    stored = entry.get("stored_at")
    try:
        return max(0.0, time.time() - float(stored))
    except (TypeError, ValueError):
        return float("inf")


# ── Fetch ──────────────────────────────────────────────────────────────────


def _profile(provider: str):
    try:
        from providers import get_provider_profile

        return get_provider_profile(provider)
    except Exception:
        return None


def _implements_usage(profile: Any) -> bool:
    """True when the profile overrides the base no-op hook."""
    if profile is None:
        return False
    try:
        from providers.base import ProviderProfile

        return type(profile).fetch_usage is not ProviderProfile.fetch_usage
    except Exception:
        return False


def _classify(exc: BaseException) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return STATE_UNAUTHORIZED
    if status == 429:
        return STATE_RATE_LIMITED
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return STATE_PARSE_ERROR
    return STATE_NETWORK_ERROR


def _fetch_one(provider: str, timeout: float) -> ProviderUsage:
    profile = _profile(provider)
    display_name = getattr(profile, "display_name", "") or provider

    # Resolution happens HERE, once, so plugins never touch the pool — see the
    # module docstring for why that separation is load-bearing.
    credential = None
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        # peek() answers "what would the next INFERENCE call use", so it
        # returns nothing once the entry is quarantined as exhausted. A quota
        # read is the opposite case: being rate-limited is precisely when you
        # want to see the quota. Any entry will do, healthy or not.
        credential = pool.peek() or pool.current()
        if credential is None:
            entries = pool.entries()
            credential = entries[0] if entries else None
    except Exception as exc:
        logger.debug("usage: credential resolution failed for %s: %s", provider, exc)

    # Deliberately NOT gated on `credential`: some providers self-resolve.
    # Anthropic's usage endpoint takes an OAuth token that its own resolver
    # reads from ~/.claude/.credentials.json, so it answers fine with an empty
    # pool — refusing to call the hook would report a live plan as logged out.
    try:
        usage = profile.fetch_usage(
            credential=credential,
            base_url=getattr(credential, "base_url", None),
            timeout=timeout,
        )
    except BaseException as exc:  # noqa: BLE001 — one provider must never sink the fan-out
        logger.debug("usage: fetch failed for %s: %s", provider, exc)
        return ProviderUsage(
            provider=provider,
            display_name=display_name,
            state=_classify(exc),
            message=str(exc)[:200] or None,
            fetched_at=_utc_now(),
        )

    if usage is None:
        # A None return means "nothing to report". Which of the two typed
        # reasons that is depends on whether we had a credential to offer: no
        # credential AND no self-resolved answer is a logged-out provider;
        # everything else is a provider whose endpoint had nothing for us
        # (e.g. Anthropic holding a plain API key instead of OAuth).
        return ProviderUsage(
            provider=provider,
            display_name=display_name,
            state=STATE_NOT_AUTHENTICATED if credential is None else STATE_NO_USAGE_ENDPOINT,
            fetched_at=_utc_now(),
        )

    # Fill the gaps a plugin left rather than rebuilding field by field — a
    # manual rebuild ties the display-name fallback to whether fetched_at
    # happened to be set.
    return replace(
        usage,
        provider=usage.provider or provider,
        display_name=usage.display_name or display_name,
        fetched_at=usage.fetched_at or _utc_now(),
    )


def usage_providers() -> List[str]:
    """Detected providers that can actually report usage."""
    return [provider for provider in detect_providers() if _implements_usage(_profile(provider))]


def _ttl_for(provider: str) -> int:
    profile = _profile(provider)
    try:
        return max(0, int(getattr(profile, "usage_ttl", 300) or 0))
    except (TypeError, ValueError):
        return 300


def _from_payload(payload: Optional[Dict[str, Any]]) -> Optional[ProviderUsage]:
    """Rebuild a cached snapshot. Windows come back as payload dicts, so the
    surface renders identical numbers whether they were cached or just fetched."""
    if not isinstance(payload, dict) or not payload.get("provider"):
        return None
    try:
        return ProviderUsage(
            provider=str(payload["provider"]),
            display_name=str(payload.get("display_name") or ""),
            plan=payload.get("plan"),
            windows=tuple(_window_from_payload(item) for item in payload.get("windows") or ()),
            state=str(payload.get("state") or "ok"),
            message=payload.get("message"),
            fetched_at=to_datetime(payload.get("fetched_at")),
        )
    except Exception:
        return None


def _window_from_payload(payload: Dict[str, Any]) -> UsageWindow:
    from agent.provider_usage_types import to_decimal

    return UsageWindow(
        label=str(payload.get("label") or ""),
        unit=str(payload.get("unit") or "percent"),
        used=to_decimal(payload.get("used")),
        limit=to_decimal(payload.get("limit")),
        remaining=to_decimal(payload.get("remaining")),
        reset_at=to_datetime(payload.get("reset_at")),
        currency=payload.get("currency"),
        detail=payload.get("detail"),
    )



def collect_usage(
    *,
    refresh: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
    providers: Optional[Sequence[str]] = None,
) -> List[ProviderUsage]:
    """Every usage-capable provider's plan state, freshest-known first paint.

    Fresh cache entries are returned as-is. Expired ones are returned marked
    ``stale`` AND refreshed in the same call, so a caller always gets real
    numbers; ``refresh=True`` skips the cache entirely (the manual button).
    """
    names = list(providers) if providers is not None else usage_providers()
    if not names:
        return []

    cache = _read_cache()
    results: Dict[str, ProviderUsage] = {}
    to_fetch: List[str] = []

    for provider in names:
        entry = cache.get(provider)
        if not isinstance(entry, dict):
            entry = None
        cached = _from_payload(entry.get("usage")) if entry else None

        if refresh or cached is None:
            to_fetch.append(provider)
            continue

        if _cache_entry_age(entry) <= _ttl_for(provider):
            results[provider] = cached
        else:
            # Stale-while-revalidate: keep the old numbers as the floor so a
            # slow or failing refresh degrades to "a bit old" instead of blank.
            results[provider] = replace(cached, stale=True)
            to_fetch.append(provider)

    if to_fetch:
        fetched = _fetch_many(to_fetch, timeout)
        results.update(fetched)
        _store(fetched)

    return [results[name] for name in names if name in results]


def _fetch_many(providers: Sequence[str], timeout: float) -> Dict[str, ProviderUsage]:
    out: Dict[str, ProviderUsage] = {}
    if not providers:
        return out

    workers = min(_MAX_WORKERS, len(providers))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="usage") as pool:
        futures = {pool.submit(_fetch_one, name, timeout): name for name in providers}
        for future in as_completed(futures):
            name = futures[future]
            try:
                out[name] = future.result()
            except BaseException as exc:  # noqa: BLE001 — isolation is the point
                logger.debug("usage: worker failed for %s: %s", name, exc)
                out[name] = ProviderUsage(
                    provider=name,
                    display_name=name,
                    state=_classify(exc),
                    fetched_at=_utc_now(),
                )
    return out


def _store(results: Dict[str, ProviderUsage]) -> None:
    """Persist successful snapshots only.

    A failure must not overwrite good numbers: the next call would then have no
    floor to fall back to, and a transient network blip would blank the panel.
    """
    keepers = {name: usage for name, usage in results.items() if usage.state == "ok"}
    if not keepers:
        return

    with _CACHE_LOCK:
        cache = _read_cache()
        stored_at = time.time()
        for name, usage in keepers.items():
            cache[name] = {"stored_at": stored_at, "usage": usage.to_payload()}
        _write_cache(cache)


def usage_payload(*, refresh: bool = False, timeout: float = _DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """JSON-safe fan-out result for RPC/CLI surfaces. Never raises."""
    try:
        results = collect_usage(refresh=refresh, timeout=timeout)
    except Exception as exc:
        logger.debug("usage: collection failed: %s", exc)
        return {"ok": True, "available": False, "providers": []}

    return {
        "ok": True,
        "available": any(usage.available for usage in results),
        "providers": [usage.to_payload() for usage in results],
    }
