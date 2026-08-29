"""Selective lossless compression for HTTP responses.

Only data formats that are normally text-heavy and safe to compress are
included. HTML is intentionally excluded because the dashboard HTML carries
session material and may contain attacker-influenced values; JSON API
responses and exports are the primary bandwidth target.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware, GZipResponder


_COMPRESSIBLE_CONTENT_TYPES = (
    "application/json",
    "application/javascript",
    "application/xml",
    "image/svg+xml",
    "text/css",
    "text/plain",
)
_EXCLUDED_PATH_PREFIXES = (
    "/api/env/reveal",
    "/api/config/raw",
    "/api/auth/",
    "/auth/",
)


def _is_compressible_content_type(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type.startswith(_COMPRESSIBLE_CONTENT_TYPES) or media_type.endswith(
        "+json"
    )


def _accepts_gzip(value: str) -> bool:
    """Return whether ``Accept-Encoding`` permits gzip compression."""
    wildcard_allowed = False
    for item in value.split(","):
        parts = [part.strip() for part in item.split(";")]
        encoding = parts[0].lower()
        quality = 1.0
        for parameter in parts[1:]:
            name, separator, raw_value = parameter.partition("=")
            if name.lower() == "q" and separator:
                try:
                    quality = float(raw_value)
                except ValueError:
                    quality = 0.0
        if encoding == "gzip":
            return quality > 0
        if encoding == "*":
            wildcard_allowed = quality > 0
    return wildcard_allowed


def _is_excluded_path(path: str) -> bool:
    return path.startswith(_EXCLUDED_PATH_PREFIXES)


class _SelectiveGZipResponder(GZipResponder):
    """Starlette's streaming gzip responder with a content-type allowlist."""

    async def _send_with_selective_compression(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            # Delay response.start until the first body chunk, as the parent
            # responder does, but make the exclusion decision from our safer
            # allowlist rather than Starlette's event-stream-only default.
            self.initial_message = message
            headers = Headers(raw=self.initial_message["headers"])
            self.content_encoding_set = "content-encoding" in headers
            self.content_type_is_excluded = not _is_compressible_content_type(
                headers.get("content-type", "")
            )
            return

        # Starlette renamed this hook from ``send_with_gzip`` to
        # ``send_with_compression``. Resolve the parent hook dynamically so the
        # middleware keeps its allowlist on both supported API generations.
        parent = super()
        send_hook = getattr(parent, "send_with_compression", None)
        if send_hook is None:
            send_hook = parent.send_with_gzip
        await send_hook(message)

    async def send_with_compression(self, message: dict[str, Any]) -> None:
        await self._send_with_selective_compression(message)

    async def send_with_gzip(self, message: dict[str, Any]) -> None:
        await self._send_with_selective_compression(message)


class SelectiveGZipMiddleware(GZipMiddleware):
    """Compress eligible JSON/text responses while preserving streaming."""

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or _is_excluded_path(scope.get("path", ""))
            or not _accepts_gzip(
                Headers(scope=scope).get("Accept-Encoding", "")
            )
        ):
            await self.app(scope, receive, send)
            return

        responder = _SelectiveGZipResponder(
            self.app,
            self.minimum_size,
            compresslevel=self.compresslevel,
        )
        await responder(scope, receive, send)
