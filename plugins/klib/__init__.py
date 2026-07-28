"""Klib search/read plugin for Telegram and other Hermes gateway sessions.

The ``/klib <query>`` command calls klib's ``GET /query`` endpoint using
lexical search by default.  When ``klib.key_file`` is configured, the file's
trimmed contents are sent as an ``Authorization: Bearer <key>`` request header.
The ``/klib read <path>`` command calls klib's ``GET /read`` endpoint.
The ``/klib semantic <query>`` form requests semantic search.

The command is intentionally fail-closed and never raises to its caller:
missing or disabled configuration, authentication-file failures, HTTP errors,
malformed responses, and unexpected formatting errors become user-facing
messages instead.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 10.0
_RESULT_LIMIT = 5
_PAGE_SIZE = 5
# Klib lexical search returns hits file-by-file and early-returns when the
# limit is reached, so one file with many matching lines can fill the entire
# raw budget before the server even examines a second file. Overfetching gives
# client-side dedup real material from additional files to work with.
_FETCH_LIMIT = 25
_MAX_REPLY_LENGTH = 2800
_SNIPPET_LENGTH = 150
_SESSION_TTL = 1800

_PAGINATION_SESSIONS: dict[str, dict[str, Any]] = {}


def _truncate_display(value: str, length: int) -> str:
    """Normalize and truncate a dynamic value without changing its text."""
    normalized = " ".join(value.split())
    return _truncate_reply(normalized, length)


def _static_reply(value: str) -> str:
    """Return raw plugin text for the downstream platform formatter."""
    return value


_NOT_CONFIGURED_REPLY = (
    "klib: not configured or disabled. An admin must set "
    "klib.enabled: true and klib.base_url in config.yaml first."
)

_INVALID_PAGINATION_REPLY = (
    "klib: pagination session expired or is invalid."
)
_NO_MORE_RESULTS_REPLY = "klib: no more results."

try:
    from hermes_cli.plugins import InlineKeyboardMarkup
except (ImportError, AttributeError):  # pragma: no cover - defensive fallback
    class InlineKeyboardMarkup:  # type: ignore[no-redef]
        pass

try:
    from telegram import InlineKeyboardButton
    if not isinstance(InlineKeyboardButton, type):
        raise ImportError
except (ImportError, AttributeError):
    class InlineKeyboardButton:  # type: ignore[no-redef]
        """Small optional-dependency fallback used by plugin-only tests."""

        def __init__(self, text: str, callback_data: str):
            self.text = text
            self.callback_data = callback_data


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


def _truncate_reply(value: str, length: int) -> str:
    """Truncate reply text without modifying its characters."""
    if len(value) <= length:
        return value
    truncated = value[: max(0, length - 1)].rstrip()
    return truncated + "…"


def _value_from_item(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _deduplicate_results(results: list[Any]) -> list[Any]:
    """Deduplicate file results using the first hit for each identity."""
    distinct_results: list[Any] = []
    seen_identities: set[str] = set()
    # First-hit-wins is deliberate: the tested response shape has no reliable
    # score field, so retaining the original klib result order is deterministic.
    for item in results:
        identity = ""
        if isinstance(item, dict):
            identity = _value_from_item(item, ("path", "file", "title"))
        if identity and identity in seen_identities:
            continue
        if identity:
            seen_identities.add(identity)
        distinct_results.append(item)
    return distinct_results


def _format_result_lines(
    query: str,
    results: list[Any],
    start_index: int = 1,
) -> list[str]:
    lines = [f"klib results for '{_truncate_display(query, 300)}':"]
    for index, item in enumerate(results, start=start_index):
        if isinstance(item, dict):
            label = _value_from_item(item, ("path", "file", "title"))
            snippet = _value_from_item(item, ("snippet", "text", "content"))
            if not label and not snippet:
                snippet = str(item)
        else:
            label = ""
            snippet = str(item)

        label = _truncate_display(label or f"Result {index}", 180)
        snippet = _truncate_display(snippet, _SNIPPET_LENGTH)
        line = f"{index}. **{label}**"
        if snippet:
            line += f" — {snippet}"
        lines.append(line)
    return lines


def _format_results(query: str, results: list[Any]) -> str:
    """Format a single-page result while keeping the existing reply unchanged."""
    distinct_results = _deduplicate_results(results)

    # Report distinct files, not raw line hits, so "top N of M" is not
    # misleading when several hits came from the same file.
    total = len(distinct_results)
    if total == 0:
        return (
            f"klib: no results found for '{_truncate_display(query, 500)}'"
            f"{_static_reply('.')}"
        )

    shown = min(total, _RESULT_LIMIT)
    lines = _format_result_lines(query, distinct_results[:_RESULT_LIMIT])

    if total > shown:
        lines.append(
            f"Showing top {shown} of {total} distinct files."
        )

    reply = "\n".join(lines)
    return _truncate_reply(reply, _MAX_REPLY_LENGTH)


def _format_page(query: str, results: list[Any], page: int) -> str:
    """Format one page of already-deduplicated results."""
    total_pages = (len(results) + _PAGE_SIZE - 1) // _PAGE_SIZE
    start = (page - 1) * _PAGE_SIZE
    page_results = results[start : start + _PAGE_SIZE]
    lines = [
        f"Page {page} of {total_pages}.",
    ]
    lines.extend(_format_result_lines(query, page_results, start_index=start + 1))
    return _truncate_reply("\n".join(lines), _MAX_REPLY_LENGTH)


def _make_pagination_keyboard(
    session_id: str,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    buttons = []
    if page > 1:
        buttons.append(
            InlineKeyboardButton(
                "Prev", callback_data=f"klib:page:{page - 1}:{session_id}"
            )
        )
    if page < total_pages:
        buttons.append(
            InlineKeyboardButton(
                "Next", callback_data=f"klib:page:{page + 1}:{session_id}"
            )
        )
    try:
        return InlineKeyboardMarkup([buttons])
    except TypeError:
        # hermes_cli.plugins exposes a no-dependency marker class when Telegram
        # is unavailable. Preserve the keyboard shape for plugin-only callers.
        keyboard = InlineKeyboardMarkup()
        keyboard.inline_keyboard = [buttons]
        return keyboard


def _current_chat_id() -> str:
    """Read the gateway's task-local chat id without requiring gateway startup."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env("HERMES_SESSION_CHAT_ID", "")
    except Exception:
        return ""


def _new_pagination_session(chat_id: int | str, query: str, results: list[Any]) -> str:
    session_id = hashlib.sha256(
        f"{chat_id}:{query}:{time.time()}".encode()
    ).hexdigest()[:8]
    _PAGINATION_SESSIONS[session_id] = {
        "chat_id": chat_id,
        "query": query,
        "distinct_results": results,
        "expires_at": time.time() + _SESSION_TTL,
    }
    return session_id


async def _handle_klib_callback(
    callback_data: str,
    chat_id: int | str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render a stored page, rejecting invalid, expired, or cross-chat data."""
    try:
        parts = callback_data.split(":")
        if len(parts) != 4 or parts[0] != "klib" or parts[1] != "page":
            return _INVALID_PAGINATION_REPLY, None
        try:
            page = int(parts[2])
        except (TypeError, ValueError):
            return _INVALID_PAGINATION_REPLY, None
        if page <= 0:
            return _INVALID_PAGINATION_REPLY, None

        session_id = parts[3]
        session = _PAGINATION_SESSIONS.get(session_id)
        if (
            not session
            or time.time() > session.get("expires_at", 0)
            or str(session.get("chat_id")) != str(chat_id)
        ):
            return _INVALID_PAGINATION_REPLY, None

        results = session.get("distinct_results")
        query = session.get("query")
        if not isinstance(results, list) or not isinstance(query, str):
            return _INVALID_PAGINATION_REPLY, None

        # Phase 1 intentionally pages only the _FETCH_LIMIT=25 overfetch: at
        # most floor(25 / 5) = 5 pages. There is no offset re-fetch here.
        total_pages = (len(results) + _PAGE_SIZE - 1) // _PAGE_SIZE
        if page > total_pages or page > _FETCH_LIMIT // _PAGE_SIZE:
            return _NO_MORE_RESULTS_REPLY, None

        keyboard = _make_pagination_keyboard(session_id, page, total_pages)
        return _format_page(query, results, page), keyboard
    except Exception:
        logger.exception("klib: unexpected pagination callback failure")
        return _INVALID_PAGINATION_REPLY, None


async def _query_klib(
    query: str,
    cfg: dict[str, Any],
    mode: str = "lexical",
    chat_id: int | str = "",
) -> str | tuple[str, InlineKeyboardMarkup]:
    """Run the configured request and convert every expected failure to text."""
    base_url = cfg.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return _NOT_CONFIGURED_REPLY
    base_url = base_url.strip()

    headers: dict[str, str] = {}
    key_file = cfg.get("key_file")
    if key_file:
        try:
            api_key = Path(str(key_file)).read_text(encoding="utf-8").strip()
            headers["Authorization"] = f"Bearer {api_key}"
        except Exception:
            return _static_reply("klib: could not read the configured API key file.")

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/query",
                params={
                    "q": query,
                    "mode": mode,
                    "limit": _FETCH_LIMIT,
                },
                headers=headers,
            )
    except httpx.TimeoutException:
        return _static_reply("klib: query timed out.")
    except httpx.RequestError:
        return _static_reply("klib: could not reach the klib service.")

    if not 200 <= response.status_code < 300:
        return (
            "klib: service returned HTTP status "
            f"{response.status_code}."
        )

    try:
        payload = response.json()
    except Exception:
        return _static_reply("klib: received an invalid JSON response.")

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return _static_reply("klib: received an invalid response format.")
    distinct_results = _deduplicate_results(payload["results"])
    if len(distinct_results) <= _PAGE_SIZE:
        return _format_results(query, payload["results"])

    total_pages = (len(distinct_results) + _PAGE_SIZE - 1) // _PAGE_SIZE
    session_id = _new_pagination_session(chat_id, query, distinct_results)
    return (
        _format_page(query, distinct_results, 1),
        _make_pagination_keyboard(session_id, 1, total_pages),
    )


async def _read_klib(
    path: str,
    cfg: dict[str, Any],
) -> str:
    """Read a klib page and convert every expected failure to text."""
    base_url = cfg.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return _NOT_CONFIGURED_REPLY
    base_url = base_url.strip()

    headers: dict[str, str] = {}
    key_file = cfg.get("key_file")
    if key_file:
        try:
            api_key = Path(str(key_file)).read_text(encoding="utf-8").strip()
            headers["Authorization"] = f"Bearer {api_key}"
        except Exception:
            return _static_reply("klib: could not read the configured API key file.")

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/read",
                params={"path": path},
                headers=headers,
            )
    except httpx.TimeoutException:
        return _static_reply("klib: read request timed out.")
    except httpx.RequestError:
        return _static_reply("klib: could not reach the klib service.")

    if response.status_code == 404:
        return (
            f"klib: page not found for '{_truncate_display(path, 500)}'"
            f"{_static_reply('.')}"
        )
    if not 200 <= response.status_code < 300:
        return (
            "klib: service returned HTTP status "
            f"{response.status_code}."
        )

    try:
        payload = response.json()
    except Exception:
        return _static_reply("klib: received an invalid JSON response.")

    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("path"), str)
        or not isinstance(payload.get("raw"), str)
    ):
        return _static_reply("klib: received an invalid response format.")

    reply = f"klib: {payload['path'].strip()}\n{payload['raw']}"
    return _truncate_reply(reply, _MAX_REPLY_LENGTH)


async def _handle_klib(
    raw_args: str,
    chat_id: int | str | None = None,
) -> str | tuple[str, InlineKeyboardMarkup]:
    """Handle ``/klib`` and never propagate an exception to the gateway."""
    try:
        query = raw_args.strip()
        if not query:
            return _static_reply(
                "Usage: /klib <query>\n"
                "Usage: /klib read <path>\n"
                "Search the klib knowledge library or read a full page."
            )

        # This is a case-sensitive literal "read " prefix.  A search for a
        # phrase such as "read the manual" is therefore treated as a read;
        # that ambiguity is an accepted tradeoff for simple slash dispatch.
        is_read = query == "read" or query.startswith("read ")
        if is_read:
            path = query[5:].strip() if query.startswith("read ") else ""
            if not path:
                return _static_reply(
                    "Usage: /klib read <path>\nRead the full text of a klib page."
                )

        mode = "lexical"
        if query == "semantic" or query.startswith("semantic "):
            # This is a case-sensitive literal "semantic " prefix.  A search
            # phrase beginning with those exact characters is therefore
            # interpreted as a semantic-mode query; that ambiguity is an
            # accepted tradeoff for simple slash dispatch.  It cannot collide
            # with the separate case-sensitive "read " prefix above.
            query = (
                query[len("semantic ") :].strip()
                if query.startswith("semantic ")
                else ""
            )
            if not query:
                return _static_reply("Usage: /klib semantic <query>")
            mode = "semantic"

        cfg = _load_klib_config()
        if not cfg.get("enabled") or not cfg.get("base_url"):
            return _NOT_CONFIGURED_REPLY
        if is_read:
            return await _read_klib(path, cfg)
        return await _query_klib(
            query,
            cfg,
            mode=mode,
            chat_id=_current_chat_id() if chat_id is None else chat_id,
        )
    except Exception:
        logger.exception("klib: unexpected command failure")
        return _static_reply(
            "klib: an unexpected error occurred while processing the query."
        )


def register(ctx) -> None:
    ctx.register_command(
        "klib",
        handler=_handle_klib,
        description="Search the klib knowledge library or read a full page.",
        args_hint="<query> | read <path> | semantic <query>",
    )
    ctx.register_callback_handler("klib:", _handle_klib_callback)
