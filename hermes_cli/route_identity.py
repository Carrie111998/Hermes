"""Fail-closed URL identity normalization for model/provider routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit


# A configured Codex context pin is normally owned by one exact model.  These
# three GPT-5.6 Codex tiers are the deliberate exception: the Codex route
# allocates the same user-selected window to Sol, Terra, and Luna, so moving
# between the tiers must retain that explicit allocation.  Keep this set
# narrow; a pin must never escape to another Codex family, provider, or route.
_CODEX_GPT56_CONTEXT_PEERS = frozenset({
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
})


def _model_slug(model: Any) -> str:
    """Return the bare, lower-case model identity used for pin scoping."""
    return str(model or "").strip().lower().rsplit("/", 1)[-1]


def _is_codex_gpt56_context_peer(
    configured_model: Any,
    active_model: Any,
    configured_provider: Any,
    active_provider: Any,
) -> bool:
    """Whether two model identities share the explicit GPT-5.6 Codex pin."""
    return (
        str(configured_provider or "").strip().lower() == "openai-codex"
        and str(active_provider or "").strip().lower() == "openai-codex"
        and _model_slug(configured_model) in _CODEX_GPT56_CONTEXT_PEERS
        and _model_slug(active_model) in _CODEX_GPT56_CONTEXT_PEERS
    )


def normalize_route_base_url(base_url: Any) -> str:
    """Canonicalize only proven-equivalent endpoint URL components."""
    raw = str(base_url or "")
    if not raw:
        return ""
    if any(ord(char) <= 0x20 for char in raw):
        return raw
    had_query_delimiter = "?" in raw.split("#", 1)[0]
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return raw
        scheme = parsed.scheme.lower()
        if "%" in hostname:
            address, zone = hostname.split("%", 1)
            host = f"{address.lower()}%{zone}"
        else:
            host = hostname.lower()
        port = parsed.port
    except (TypeError, ValueError):
        return raw

    route_host = parsed.netloc.rsplit("@", 1)[-1]
    if route_host.startswith("[") or ":" in host:
        host = f"[{host}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    if "@" in parsed.netloc:
        host = f"{parsed.netloc.rsplit('@', 1)[0]}@{host}"

    path = parsed.path
    if path.endswith("/") and not had_query_delimiter:
        path = path[:-1]

    normalized = urlunsplit((scheme, host, path, parsed.query, ""))
    if had_query_delimiter and not parsed.query:
        normalized += "?"
    return normalized


def should_clear_context_pin(
    configured_model: Any,
    active_model: Any,
    configured_base_url: Any,
    active_base_url: Any,
    configured_provider: Any,
    active_provider: Any,
    *,
    already_normalized_route: bool = False,
) -> bool:
    """True when a configured ``model.context_length`` pin no longer matches its runtime route.

    Fail-closed: any error during route comparison returns ``True`` (drop the pin)
    so a stale window never silently inflates the compression threshold.
    """
    configured_model = str(configured_model or "").strip()
    active_model = str(active_model or "").strip()
    if (
        configured_model
        and configured_model != active_model
        and not _is_codex_gpt56_context_peer(
            configured_model,
            active_model,
            configured_provider,
            active_provider,
        )
    ):
        return True
    try:
        from agent.agent_init import _context_route_mismatch

        return _context_route_mismatch(
            configured_base_url,
            active_base_url,
            configured_provider,
            active_provider,
            already_normalized=already_normalized_route,
        )
    except Exception:
        return True


def resolve_model_context_pin(
    model_config: Any,
    *,
    active_model: Any,
    active_base_url: Any,
    active_provider: Any,
) -> int | None:
    """Resolve a global ``model.context_length`` pin for one live runtime.

    The pin applies only when its configured model/provider/route owns the
    active runtime.  ``should_clear_context_pin`` keeps the normal exact-model
    guard and narrowly admits the GPT-5.6 Codex peer group above.
    """
    if not isinstance(model_config, dict):
        return None
    raw_context = model_config.get("context_length")
    if raw_context is None:
        return None
    try:
        context_length = int(raw_context)
    except (TypeError, ValueError):
        return None
    if context_length <= 0:
        return None

    configured_model = model_config.get("default") or model_config.get("model")
    configured_provider = model_config.get("provider")
    configured_base_url = model_config.get("base_url")
    if (
        configured_model
        or configured_provider
        or configured_base_url
    ) and should_clear_context_pin(
        configured_model,
        active_model,
        configured_base_url,
        active_base_url,
        configured_provider,
        active_provider,
    ):
        return None
    return context_length


async def should_clear_context_pin_async(
    configured_model: Any,
    active_model: Any,
    configured_base_url: Any,
    active_base_url: Any,
    configured_provider: Any,
    active_provider: Any,
    *,
    already_normalized_route: bool = False,
) -> bool:
    """Async wrapper for ``should_clear_context_pin``.

    Offloads the route comparison to a worker thread so async gateway
    handlers never run it on the event loop — the resolution chain is
    cache-only (``allow_network=False``) but can still do cold-start disk
    I/O. Shares all logic with the sync version — no code duplication.
    """
    import asyncio

    return await asyncio.to_thread(
        should_clear_context_pin,
        configured_model,
        active_model,
        configured_base_url,
        active_base_url,
        configured_provider,
        active_provider,
        already_normalized_route=already_normalized_route,
    )
