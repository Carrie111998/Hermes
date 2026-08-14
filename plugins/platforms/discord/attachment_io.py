"""Discord attachment routing, preflight, and bounded-read contracts (feature M6).

Pure logic — no network I/O. Every function validates its inputs and raises
:class:`AttachmentError` (a :class:`ValueError` subclass) on contract
violations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "AttachmentMeta",
    "AttachmentError",
    "THREAD_TARGETS",
    "route_attachment",
    "preflight_attachment",
    "bounded_read_request",
]


class AttachmentError(ValueError):
    """Raised when an attachment routing/preflight/bounded-read contract is violated."""


@dataclass(frozen=True)
class AttachmentMeta:
    """Metadata describing a Discord message attachment."""

    url: str
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    filename: Optional[str] = None


#: Target kinds whose attachments are delivered into a thread context.
THREAD_TARGETS = frozenset({"text", "file", "image", "video", "document", "voice"})

_SNOWFLAKE_MAX = 2**63 - 1


def _is_snowflake(value: object) -> bool:
    """Return True if *value* looks like a Discord snowflake (int or digit string)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= _SNOWFLAKE_MAX
    if isinstance(value, str):
        return value.isdigit() and 0 <= int(value) <= _SNOWFLAKE_MAX
    return False


def route_attachment(
    meta: AttachmentMeta,
    *,
    thread_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    target: str = "text",
) -> str:
    """Decide which Discord channel/thread an attachment should be routed to.

    Thread-related targets (``text``/``file``/``image``/``video``/``document``/
    ``voice``) keep the attachment in the thread whenever a ``thread_id`` is
    available; any other target falls back to ``parent_channel_id``.

    Raises :class:`AttachmentError` when both ``thread_id`` and
    ``parent_channel_id`` are None, or when a provided id is not a snowflake.
    """
    # ``meta`` is part of the contract signature (callers pass what they have);
    # routing decisions depend on the target kind and the channel context.
    if thread_id is not None and not _is_snowflake(thread_id):
        raise AttachmentError(f"thread_id is not a valid snowflake: {thread_id!r}")
    if parent_channel_id is not None and not _is_snowflake(parent_channel_id):
        raise AttachmentError(
            f"parent_channel_id is not a valid snowflake: {parent_channel_id!r}"
        )
    if thread_id is None and parent_channel_id is None:
        raise AttachmentError(
            "cannot route attachment: neither thread_id nor parent_channel_id given"
        )
    if target in THREAD_TARGETS and thread_id is not None:
        return thread_id
    return parent_channel_id  # type: ignore[return-value]  # at least one is set


def preflight_attachment(
    meta: AttachmentMeta,
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> None:
    """Reject attachments whose declared size exceeds *max_bytes*.

    Attachments with an unknown size (``size_bytes is None``) always pass.
    """
    if meta.size_bytes is not None and meta.size_bytes > max_bytes:
        raise AttachmentError(
            f"attachment {meta.url!r} is {meta.size_bytes} bytes, "
            f"exceeds limit of {max_bytes} bytes"
        )


def bounded_read_request(
    meta: AttachmentMeta,
    *,
    limit_bytes: int = 4 * 1024 * 1024,
) -> dict:
    """Build a bounded read request for an attachment URL.

    The returned ``limit_bytes`` never exceeds *limit_bytes*; when the
    declared size is known and smaller, the smaller value is used so the
    downloader can stop early.

    Raises :class:`AttachmentError` if the URL is not http(s).
    """
    url = meta.url
    if not (url.startswith("http://") or url.startswith("https://")):
        raise AttachmentError(f"attachment URL is not http(s): {url!r}")
    read_limit = min(meta.size_bytes or limit_bytes, limit_bytes)
    return {"url": url, "limit_bytes": read_limit}
