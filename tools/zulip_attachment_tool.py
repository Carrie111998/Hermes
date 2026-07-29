"""Zulip attachment fetch capability.

Adds a single minimal tool, ``zulip_fetch_attachment``, that downloads a
Zulip user_uploads attachment via the bot's existing credentials and stores it
in a process-local temporary directory. The tool is intentionally narrow:

* Only Zulip ``/user_uploads/...`` paths are accepted.
* Only the configured ``ZULIP_URL`` host is allowed.
* Outputs are ephemeral — written to ``tempfile.gettempdir()`` and cleaned up
  on process exit; never persisted to AI-Vault.
* Credentials are read from environment variables at request time and never
  echoed in responses or logs.

This module does NOT modify any existing tools, the Zulip platform adapter,
or the system prompt. It only registers one new tool under the ``zulip``
toolset so it inherits the same gating and visibility as the rest of the
Zulip read/search actions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_BYTES = 32 * 1024 * 1024  # 32 MiB hard ceiling per fetch
# Zulip attachment storage layouts vary across versions/backends.  Live
# self-hosted messages can use a one- or two-character lowercase-hex shard,
# and the opaque token may contain both ``_`` and ``-``.  Keep the route and
# host constraints strict, but do not reject valid opaque identifiers based on
# an assumed token alphabet.
#   /user_uploads/<realm_id>/<shard>/<opaque_token>/<file>
_USER_UPLOADS_PATH_RE = re.compile(
    r"^/user_uploads/(?P<realm_id>\d+)/(?P<prefix>[0-9a-f]{1,2})/(?P<token>[A-Za-z0-9_-]{20,})/(?P<file>[^/]+)$"
)


def _get_config() -> tuple[str, str, str]:
    """Read Zulip credentials from the running process environment.

    Mirrors the env-only policy used by ``zulip_tool.py`` — no persistence,
    no log echo.
    """
    return (
        os.getenv("ZULIP_URL", "").rstrip("/"),
        os.getenv("ZULIP_BOT_EMAIL", ""),
        os.getenv("ZULIP_API_KEY", ""),
    )


def _check_zulip_available() -> bool:
    """Hide the tool until the full Zulip credential triplet is configured."""
    return all(_get_config())


def _resolve_url(user_input: str) -> tuple[str, str]:
    """Resolve ``user_input`` into a (absolute_url, filename) pair.

    Accepts either a fully qualified ``https://zulip.example/user_uploads/...``
    URL or a root-relative ``/user_uploads/...`` path. Anything else is
    rejected.
    """
    base_url, _, _ = _get_config()
    if not base_url:
        raise ValueError("Zulip is not configured: ZULIP_URL is required")

    candidate = user_input.strip()
    if not candidate:
        raise ValueError("url must be a non-empty Zulip user_uploads URL or path")

    if "://" in candidate:
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")
        absolute = candidate
        path = parsed.path or ""
    else:
        if not candidate.startswith("/"):
            candidate = "/" + candidate
        absolute = base_url + candidate
        path = candidate

    if not _USER_UPLOADS_PATH_RE.match(path):
        raise ValueError(
            "url must match Zulip user_uploads path "
            "(/user_uploads/<realm_id>/<shard>/<token>/<file>)"
        )

    # Enforce that the absolute URL's host equals the configured ZULIP_URL host.
    cfg_host = urlparse(base_url).netloc.lower()
    input_host = urlparse(absolute).netloc.lower()
    if cfg_host and input_host and cfg_host != input_host:
        raise ValueError(
            f"url host {input_host!r} does not match configured Zulip host {cfg_host!r}"
        )

    filename = os.path.basename(path)
    if not filename:
        raise ValueError("could not derive a filename from the supplied URL")
    return absolute, filename


def _detect_mime(filename: str, content_type_header: Optional[str]) -> str:
    """Prefer the server-supplied Content-Type; fall back to a mimetypes guess."""
    if content_type_header:
        return content_type_header.split(";", 1)[0].strip().lower()
    import mimetypes

    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _handle_fetch_attachment(args: dict, **_: Any) -> str:
    """Download one Zulip attachment into a temporary file and return its path.

    Returns a JSON object with ``status``, ``path``, ``filename``,
    ``size_bytes``, ``mime_type``, ``source_url``. On failure returns
    ``tool_error`` JSON with a redacted error string.
    """
    try:
        url_input = args.get("url")
        if not isinstance(url_input, str) or not url_input.strip():
            raise ValueError("Missing required parameter: url")

        base_url, email, api_key = _get_config()
        if not all((base_url, email, api_key)):
            raise RuntimeError(
                "Zulip is not configured: ZULIP_URL, ZULIP_BOT_EMAIL, and ZULIP_API_KEY are required"
            )

        absolute_url, filename = _resolve_url(url_input)

        with httpx.Client(
            timeout=_REQUEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            auth=(email, api_key),
            headers={"User-Agent": "Hermes-Agent-Zulip/1.0"},
        ) as client:
            with client.stream("GET", absolute_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                content_length = response.headers.get("content-length")

                if content_length is not None:
                    try:
                        if int(content_length) > _MAX_BYTES:
                            raise ValueError(
                                f"attachment exceeds {_MAX_BYTES} bytes (Content-Length: {content_length})"
                            )
                    except ValueError as exc:
                        # Re-raise our explicit size error; surface header parse issues as a warning.
                        if "attachment exceeds" in str(exc):
                            raise
                        logger.warning(
                            "zulip_fetch_attachment: unparseable Content-Length %r",
                            content_length,
                        )

                # Use a NamedTemporaryFile inside tempfile.gettempdir() so cleanup
                # is automatic when the handle goes out of scope.
                fd, tmp_path = tempfile.mkstemp(prefix="zulip-attach-", suffix="-" + filename)
                bytes_written = 0
                try:
                    with os.fdopen(fd, "wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=64 * 1024):
                            bytes_written += len(chunk)
                            if bytes_written > _MAX_BYTES:
                                handle.close()
                                try:
                                    os.unlink(tmp_path)
                                except OSError:
                                    pass
                                raise ValueError(
                                    f"attachment exceeded {_MAX_BYTES} bytes during streaming"
                                )
                            handle.write(chunk)
                except Exception:
                    # Best-effort cleanup if the write failed mid-stream.
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise

        mime_type = _detect_mime(filename, content_type)
        result = {
            "status": "ok",
            "path": tmp_path,
            "filename": filename,
            "size_bytes": bytes_written,
            "mime_type": mime_type,
            "source_url": absolute_url,
        }
        # Never include credentials in the response — defensive double-check.
        for forbidden in ("api_key", "password", "token", "secret"):
            for key in list(result.keys()):
                if forbidden in key.lower():
                    result.pop(key, None)
        return json.dumps({"result": result})
    except (ValueError, httpx.HTTPError, RuntimeError, OSError) as exc:
        # Log without the URL (avoid leaking auth-bearing query strings if any).
        logger.warning("zulip_fetch_attachment failed: %s", exc)
        safe = str(exc)
        # Defensive scrub of any credential fragments that may have leaked
        # through an exception message. Uses a snapshot of credentials captured
        # at the start of the call (may be empty strings if Zulip is not
        # configured — scrubbing empty strings is a no-op, which is fine).
        _b64, _eml, _key = _get_config()
        if _eml:
            safe = safe.replace(_eml, "[redacted-email]")
        if _key:
            safe = safe.replace(_key, "[redacted-key]")
        return tool_error(f"Failed to fetch Zulip attachment: {safe}")


ZULIP_FETCH_ATTACHMENT_SCHEMA = {
    "name": "zulip_fetch_attachment",
    "description": (
        "Fetch one Zulip user_uploads attachment via the configured bot "
        "credentials. Accepts either a full https URL or a root-relative path "
        "matching /user_uploads/<realm_id>/<shard>/<token>/<file>. The file is "
        "written to a process-local temp directory; it is not persisted to "
        "AI-Vault and no copy is cached. Returns the local path, filename, "
        "size in bytes, and best-effort MIME type."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "Zulip attachment URL or /user_uploads/... path. Only "
                    "Zulip's own host is permitted."
                ),
            }
        },
        "required": ["url"],
    },
}

registry.register(
    name="zulip_fetch_attachment",
    toolset="zulip",
    schema=ZULIP_FETCH_ATTACHMENT_SCHEMA,
    handler=_handle_fetch_attachment,
    check_fn=_check_zulip_available,
    emoji="🟩",
)
