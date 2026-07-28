"""Klib search plugin for Telegram and other Hermes gateway sessions.

The ``/klib <query>`` command calls klib's ``GET /query`` endpoint using
lexical search by default.  When ``klib.key_file`` is configured, the file's
trimmed contents are sent as an ``Authorization: Bearer <key>`` request header.

The command is intentionally fail-closed and never raises to its caller:
missing or disabled configuration, authentication-file failures, HTTP errors,
malformed responses, and unexpected formatting errors become user-facing
messages instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10.0
_RESULT_LIMIT = 5
_MAX_REPLY_LENGTH = 2800
_SNIPPET_LENGTH = 150


def _load_klib_config() -> dict[str, Any]:
    """Load the top-level klib config block, failing closed on any error."""
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        block = config.get("klib")
        return block if isinstance(block, dict) else {}
    except Exception:
        logger.warning("klib: failed to load config; treating as disabled")
        return {}


def _truncate(value: str, length: int) -> str:
    value = " ".join(value.split())
    if len(value) <= length:
        return value
    return value[: max(0, length - 1)].rstrip() + "…"


def _value_from_item(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _format_results(query: str, results: list[Any]) -> str:
    """Format at most five results while keeping the Telegram reply bounded."""
    total = len(results)
    if total == 0:
        return f"klib: no results found for {_truncate(query, 500)!r}."

    lines = [f"klib results for {_truncate(query, 300)!r}:"]
    shown = min(total, _RESULT_LIMIT)
    for index, item in enumerate(results[:_RESULT_LIMIT], start=1):
        if isinstance(item, dict):
            label = _value_from_item(item, ("path", "file", "title"))
            snippet = _value_from_item(item, ("snippet", "text", "content"))
            if not label and not snippet:
                snippet = str(item)
        else:
            label = ""
            snippet = str(item)

        label = _truncate(label or f"Result {index}", 180)
        snippet = _truncate(snippet, _SNIPPET_LENGTH)
        line = f"{index}. {label}"
        if snippet:
            line += f" — {snippet}"
        lines.append(line)

    if total > shown:
        lines.append(f"Showing top {shown} of {total} results.")

    reply = "\n".join(lines)
    if len(reply) <= _MAX_REPLY_LENGTH:
        return reply
    return reply[: _MAX_REPLY_LENGTH - 1].rstrip() + "…"


async def _query_klib(
    query: str,
    cfg: dict[str, Any],
) -> str:
    """Run the configured request and convert every expected failure to text."""
    base_url = cfg.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return (
            "klib: not configured or disabled. An admin must set "
            "klib.enabled: true and klib.base_url in config.yaml first."
        )
    base_url = base_url.strip()

    headers: dict[str, str] = {}
    key_file = cfg.get("key_file")
    if key_file:
        try:
            api_key = Path(str(key_file)).read_text(encoding="utf-8").strip()
            headers["Authorization"] = f"Bearer {api_key}"
        except Exception:
            return "klib: could not read the configured API key file."

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/query",
                params={
                    "q": query,
                    "mode": "lexical",
                    "limit": _RESULT_LIMIT,
                },
                headers=headers,
            )
    except httpx.TimeoutException:
        return "klib: query timed out."
    except httpx.RequestError:
        return "klib: could not reach the klib service."

    if not 200 <= response.status_code < 300:
        return f"klib: service returned HTTP status {response.status_code}."

    try:
        payload = response.json()
    except Exception:
        return "klib: received an invalid JSON response."

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return "klib: received an invalid response format."
    return _format_results(query, payload["results"])


async def _handle_klib(raw_args: str) -> str:
    """Handle ``/klib`` and never propagate an exception to the gateway."""
    try:
        query = raw_args.strip()
        if not query:
            return (
                "Usage: /klib <query>\n"
                "Search the klib knowledge library using lexical search."
            )

        cfg = _load_klib_config()
        if not cfg.get("enabled") or not cfg.get("base_url"):
            return (
                "klib: not configured or disabled. An admin must set "
                "klib.enabled: true and klib.base_url in config.yaml first."
            )
        return await _query_klib(query, cfg)
    except Exception:
        logger.exception("klib: unexpected command failure")
        return "klib: an unexpected error occurred while processing the query."


def register(ctx) -> None:
    ctx.register_command(
        "klib",
        handler=_handle_klib,
        description="Search the klib knowledge library.",
        args_hint="<query>",
    )
