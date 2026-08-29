"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import logging

from typing import Any

logger = logging.getLogger(__name__)


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


# Master fallback kill-switch default.
#
# True keeps the historical behaviour (automatic provider/model fallback is
# allowed) so existing suites and installs are unaffected when the
# ``fallback.enabled`` key is absent. A deployment can set
# ``fallback.enabled: false`` explicitly to run in manual mode — no silent
# provider/model route change at the call-sites that consult this flag. To
# make manual mode the hard default for ALL installs, flip this single
# constant to False.
_FALLBACK_ENABLED_DEFAULT = True

_FALSE_TOKENS = {"0", "false", "no", "off", "n", "f"}
_TRUE_TOKENS = {"1", "true", "yes", "on", "y", "t"}


def fallback_enabled(config: dict[str, Any] | None) -> bool:
    """Return whether automatic provider/model fallback is permitted.

    This is the single source of truth for the fallback master kill-switch:
    the policy signal that automatic-routing call-sites consult before
    changing provider/model. ``False`` means manual mode — recovery is
    user-driven only (retry / ``/model`` / an explicit handoff).

    It is currently consulted by the gateway's pre-agent auth fallback
    (``_try_resolve_fallback_provider``). The other automatic-routing layers
    (top-level chain, auxiliary fallback, summarizer-to-main) are expected to
    consult the same flag as each of those call-sites is wired up.

    Default is :data:`_FALLBACK_ENABLED_DEFAULT` (True) when the key is absent
    or malformed; scalar values are coerced leniently (bool / int / common
    yes-no strings).
    """

    # Defensive: a non-dict config (e.g. a loader/plugin returning a bare
    # string or int) must NOT raise here — this runs on the hot construction
    # path for every implicit agent. Treat anything that is not a mapping as
    # "key absent" -> historical default.
    if not isinstance(config, dict):
        return _FALLBACK_ENABLED_DEFAULT
    fb = config.get("fallback")
    if not isinstance(fb, dict) or "enabled" not in fb:
        return _FALLBACK_ENABLED_DEFAULT

    val = fb.get("enabled")
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        word = val.strip().lower()
        if word in _FALSE_TOKENS:
            return False
        if word in _TRUE_TOKENS:
            return True
    return _FALLBACK_ENABLED_DEFAULT


def resolve_fallback_enabled_default() -> bool:
    """Resolve the master fallback policy from the active config.

    Used as the construction-time default whenever an agent is built WITHOUT an
    explicit ``fallback_enabled`` (sentinel ``None``). This makes manual mode
    fail-safe across every construction site (gateway, CLI, one-shot, cron,
    TUI, background jobs, sub-agents and platform adapters) instead of silently
    defaulting to fallback-ON the moment a call-site forgets to thread the flag
    through.

    A config-load failure is logged loudly (never silently swallowed — this is
    a safety-critical path) and falls back to the historical default so a
    transient read error cannot hard-break startup. ``default True`` therefore
    applies only to a correctly loaded config whose ``fallback.enabled`` key is
    absent, matching :func:`fallback_enabled`.
    """

    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        if not isinstance(cfg, dict):
            # A loader/plugin/test-double returning a non-mapping must not crash
            # every implicit agent construction — log the type and default.
            logger.warning(
                "fallback policy: load_config returned %s, not a mapping; "
                "defaulting fallback.enabled=%s",
                type(cfg).__name__,
                _FALLBACK_ENABLED_DEFAULT,
            )
            return _FALLBACK_ENABLED_DEFAULT
    except Exception as exc:  # pragma: no cover - defensive, exercised via test
        logger.warning(
            "fallback policy: could not load config (%s); defaulting "
            "fallback.enabled=%s",
            exc,
            _FALLBACK_ENABLED_DEFAULT,
        )
        return _FALLBACK_ENABLED_DEFAULT
    return fallback_enabled(cfg)


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    if not isinstance(config, dict):
        config = {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain
