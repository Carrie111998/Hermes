import logging

logger = logging.getLogger("tools.mcp_tool")


def _mcp_image_extension_for_mime_type(mime_type: str) -> str:
    """Return a reasonable file extension for an MCP image MIME type."""
    import mimetypes
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return mimetypes.guess_extension(normalized) or ".png"


def _cache_mcp_image_block(block) -> str:
    """Cache an MCP ``ImageContent`` block to the shared image cache and
    return a ``MEDIA:<path>`` tag that Hermes gateways know how to render.

    Returns an empty string when *block* is not an image, when the base64
    payload is malformed, or when the cache helper rejects the bytes (e.g.
    non-image MIME masquerading as an image). Errors are logged, not raised:
    a single bad block shouldn't kill the tool result, and the caller will
    fall through to any text blocks that did parse.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = getattr(block, "mimeType", None)
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if data is None or not normalized_mime.startswith("image/"):
        return ""

    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP image block decode failed (%s): %s", normalized_mime, exc)
        return ""

    try:
        from gateway.platforms.base import cache_image_from_bytes

        image_path = cache_image_from_bytes(
            raw_bytes,
            ext=_mcp_image_extension_for_mime_type(normalized_mime),
        )
    except ImportError:
        # gateway.platforms.base not importable in this process (e.g. cron
        # without gateway deps). Fall back to silently dropping — callers
        # get any text blocks that did parse.
        logger.debug("MCP image caching skipped — gateway.platforms.base unavailable")
        return ""
    except Exception as exc:
        logger.warning("MCP image block cache failed: %s", exc)
        return ""

    return f"MEDIA:{image_path}"
