"""Session-scoped provenance registry for model-invoked media tools.

The registry is deliberately independent of filesystem existence.  A path or
URL is usable by ``vision_analyze`` only after a trusted boundary registered it
for the same conversation session.  This prevents a model-generated reference
from becoming trusted merely because it happens to resolve on the host.

Registration is process-local and fail-closed.  Gateway attachments and the
current user turn are registered before the agent loop; explicitly designated
media-producing tools are registered after successful execution.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import ntpath
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit


_MAX_SESSIONS = 256
_MAX_REFS_PER_SESSION = 512
_MEDIA_EXTENSIONS = (
    "avif", "bmp", "gif", "heic", "heif", "jpeg", "jpg", "png", "svg",
    "tif", "tiff", "webp",
)
_URL_RE = re.compile(r"https?://[^\s<>\]\[{}\"']+", re.IGNORECASE)
_FILE_URL_RE = re.compile(r"file://[^\s<>\]\[{}\"']+", re.IGNORECASE)
_QUOTED_PATH_RE = re.compile(r"[`\"']([^`\"']+)[`\"']")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.])((?:/[^\s<>`\"']+)|(?:[A-Za-z]:[\\/][^\s<>`\"']+))"
)
_POSIX_PATH_RE = re.compile(
    rf"(?<![\w.])(/[^\n\r<>\"']+?\.(?:{'|'.join(_MEDIA_EXTENSIONS)}))"
    r"(?=$|[\s,;:!?)}\]])",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(
    rf"(?<![\w.])([A-Za-z]:[\\/][^\n\r<>\"']+?\.(?:{'|'.join(_MEDIA_EXTENSIONS)}))"
    r"(?=$|[\s,;:!?)}\]])",
    re.IGNORECASE,
)
_RELATIVE_PATH_RE = re.compile(
    rf"(?<![\w./\\])((?:\.{1,2}[\\/])?[^\s<>\"']+\.(?:{'|'.join(_MEDIA_EXTENSIONS)}))"
    r"(?=$|[\s,;:!?)}\]])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MediaProvenance:
    """Audit-safe provenance metadata for one registered media reference."""

    origin: str
    producer: Optional[str] = None


_lock = threading.RLock()
_sessions: "OrderedDict[str, OrderedDict[str, MediaProvenance]]" = OrderedDict()


def _session_key(session_id: Any) -> str:
    return str(session_id or "").strip()


def canonical_media_reference(reference: Any) -> Optional[str]:
    """Return a stable, non-secret identity key for a media reference."""
    if not isinstance(reference, str):
        return None
    value = reference.strip()
    if not value:
        return None

    lowered = value.lower()
    if lowered.startswith("data:"):
        # Never retain a potentially multi-megabyte inline image in memory.
        return "data:sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    if lowered.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(value)
            if not parsed.netloc:
                return None
            host = (parsed.hostname or "").lower()
            if parsed.port:
                host = f"{host}:{parsed.port}"
            if parsed.username:
                # Userinfo is part of identity but never stored in clear.
                userinfo = hashlib.sha256(
                    f"{parsed.username}:{parsed.password or ''}".encode("utf-8")
                ).hexdigest()[:16]
                host = f"userinfo-sha256-{userinfo}@{host}"
            identity = urlunsplit(
                (parsed.scheme.lower(), host, parsed.path, parsed.query, "")
            )
            return "url:sha256:" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError):
            return None
    if lowered.startswith("file://"):
        value = value[7:]
    if re.match(r"^[A-Za-z]:[\\/]", value):
        normalized = ntpath.normcase(ntpath.normpath(value))
        return "file:sha256:" + hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
    try:
        normalized = str(Path(value).expanduser().resolve(strict=False))
        return "file:sha256:" + hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return None


def register_trusted_media(
    session_id: Any,
    references: Iterable[Any],
    *,
    origin: str,
    producer: Optional[str] = None,
) -> int:
    """Register references for exactly one session, returning the new count."""
    session = _session_key(session_id)
    if not session:
        return 0
    keys = [canonical_media_reference(ref) for ref in references]
    keys = [key for key in keys if key]
    if not keys:
        return 0

    added = 0
    with _lock:
        bucket = _sessions.setdefault(session, OrderedDict())
        _sessions.move_to_end(session)
        for key in keys:
            if key not in bucket:
                added += 1
            bucket[key] = MediaProvenance(origin=origin, producer=producer)
            bucket.move_to_end(key)
        while len(bucket) > _MAX_REFS_PER_SESSION:
            bucket.popitem(last=False)
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)
    return added


def trusted_media_provenance(
    session_id: Any,
    reference: Any,
) -> Optional[MediaProvenance]:
    """Return provenance only when *reference* belongs to *session_id*."""
    session = _session_key(session_id)
    key = canonical_media_reference(reference)
    if not session or not key:
        return None
    with _lock:
        bucket = _sessions.get(session)
        if bucket is None:
            return None
        provenance = bucket.get(key)
        if provenance is not None:
            # Both caps use insertion order as LRU order. A reference that is
            # actively reused should not be evicted ahead of dormant entries.
            bucket.move_to_end(key)
            _sessions.move_to_end(session)
        return provenance


def is_trusted_media(session_id: Any, reference: Any) -> bool:
    return trusted_media_provenance(session_id, reference) is not None


def clear_media_provenance(session_id: Any = None) -> None:
    """Clear one session, or all sessions when called without an id (tests)."""
    with _lock:
        if session_id is None:
            _sessions.clear()
        else:
            _sessions.pop(_session_key(session_id), None)


def _strip_trailing_url_punctuation(value: str) -> str:
    # Closing punctuation is commonly adjacent in prose.  Preserve URL query
    # bytes; trim only characters which cannot safely identify the resource.
    return value.rstrip(".,;:!?)]}")


def _iter_content_references(content: Any) -> Iterator[str]:
    if isinstance(content, str):
        if content.lstrip().lower().startswith("data:image/"):
            yield content.strip()
            return
        url_spans: list[tuple[int, int]] = []
        for url_pattern in (_URL_RE, _FILE_URL_RE):
            for match in url_pattern.finditer(content):
                yield _strip_trailing_url_punctuation(match.group(0))
                url_spans.append(match.span())
        # Do not reinterpret a URL path as a local POSIX/relative path.
        path_text = content
        for start, end in reversed(url_spans):
            path_text = path_text[:start] + (" " * (end - start)) + path_text[end:]
        for match in _QUOTED_PATH_RE.finditer(path_text):
            candidate = match.group(1).strip()
            if candidate.startswith(("/", "./", "../")) or re.match(
                r"^[A-Za-z]:[\\/]", candidate
            ):
                yield candidate
        for pattern in (
            _ABSOLUTE_PATH_RE,
            _WINDOWS_PATH_RE,
            _POSIX_PATH_RE,
            _RELATIVE_PATH_RE,
        ):
            for match in pattern.finditer(path_text):
                yield _strip_trailing_url_punctuation(match.group(1))
        return
    if isinstance(content, (list, tuple)):
        for item in content:
            yield from _iter_content_references(item)
        return
    if not isinstance(content, dict):
        return

    block_type = str(content.get("type") or "").lower()
    if block_type in {"image", "image_url", "input_image"}:
        for key in ("url", "path", "image_url", "file_path"):
            value = content.get(key)
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                nested = value.get("url") or value.get("path")
                if isinstance(nested, str):
                    yield nested
        source = content.get("source")
        if isinstance(source, dict):
            value = source.get("url") or source.get("path") or source.get("data")
            if isinstance(value, str):
                if source.get("type") == "base64" and not value.startswith("data:"):
                    media_type = source.get("media_type") or "image/jpeg"
                    value = f"data:{media_type};base64,{value}"
                yield value
        return

    # Plain user-message dictionaries may wrap the actual content.
    if "content" in content:
        yield from _iter_content_references(content.get("content"))


def register_user_media_references(session_id: Any, content: Any) -> int:
    """Register media references explicitly present in the current user turn."""
    return register_trusted_media(
        session_id,
        _iter_content_references(content),
        origin="user_explicit",
    )


def rehydrate_media_references(session_id: Any, messages: Any) -> int:
    """Rebuild trust from persisted user input and verified producer results.

    A tool result is accepted only when its ``tool_call_id`` maps to a preceding
    assistant call with the same tool name. The producer registry applies the
    second allow-list check. Arbitrary assistant text, unmatched tool rows, and
    ordinary tool results therefore remain untrusted after a restart.
    """
    if not isinstance(messages, (list, tuple)):
        return 0
    added = 0
    pending_tool_names: dict[str, str] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            pending_tool_names.clear()
            added += register_user_media_references(
                session_id, message.get("content")
            )
            continue
        if role == "assistant":
            # A tool result is grounded only in the immediately active
            # assistant tool-call batch. A new assistant row closes any older
            # incomplete batch instead of letting an ID match far later.
            pending_tool_names.clear()
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                tool_call_id = tool_call.get("id")
                function = tool_call.get("function")
                tool_name = (
                    function.get("name") if isinstance(function, dict) else None
                )
                if isinstance(tool_call_id, str) and isinstance(tool_name, str):
                    pending_tool_names[tool_call_id] = tool_name
            continue
        if role != "tool":
            pending_tool_names.clear()
            continue
        tool_call_id = message.get("tool_call_id")
        tool_name = message.get("tool_name") or message.get("name")
        if (
            not isinstance(tool_call_id, str)
            or not isinstance(tool_name, str)
            or pending_tool_names.pop(tool_call_id, None) != tool_name
        ):
            continue
        added += register_trusted_tool_result(
            session_id, tool_name, message.get("content")
        )
    return added


def _decoded_tool_payload(tool_name: str, result: Any) -> Any:
    if isinstance(result, str):
        candidate = result
        # Session persistence replaces image parts in multimodal tool messages
        # with this marker. Remove only the storage marker; never treat it as
        # media provenance itself.
        while candidate.endswith("\n[screenshot]"):
            candidate = candidate[: -len("\n[screenshot]")]
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            # High-risk browser results are persisted inside the exact
            # untrusted-data envelope added by make_tool_result_message().
            # Recover only a well-formed outer envelope whose source matches
            # the verified tool call; embedded forged delimiters are already
            # neutralized before persistence.
            opening = f'<untrusted_tool_result source="{tool_name}">\n'
            closing = "\n</untrusted_tool_result>"
            if candidate.startswith(opening) and candidate.endswith(closing):
                _, separator, payload = candidate.partition("\n\n")
                if not separator:
                    return None
                candidate = payload[: -len(closing)]
                try:
                    return json.loads(candidate)
                except (TypeError, ValueError):
                    pass
            return candidate
    return result


def _trusted_tool_references(tool_name: str, result: Any) -> list[str]:
    payload = _decoded_tool_payload(tool_name, result)
    if isinstance(payload, (list, tuple)):
        return list(_iter_content_references(payload))
    if isinstance(payload, str) and tool_name == "browser_vision":
        # Native browser_vision results persist only their text summary. Hermes
        # appends the real screenshot path at the very end; anchor there so a
        # path-like string in page/vision text cannot gain provenance.
        match = re.search(r"(?:^|\s)Screenshot path:\s*(.+?)\s*$", payload)
        return [match.group(1)] if match else []
    if not isinstance(payload, dict):
        return []
    if payload.get("success") is False and tool_name != "browser_vision":
        return []

    refs: list[str] = []
    # Multimodal producers expose the exact image reference the main model saw.
    # Register it without retaining the inline bytes (canonicalization hashes
    # data URLs). This covers browser_vision and computer_use captures.
    for block in payload.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "image_url":
            continue
        image_url = block.get("image_url")
        if isinstance(image_url, dict):
            image_url = image_url.get("url")
        if isinstance(image_url, str):
            refs.append(image_url)
    if tool_name == "image_generate":
        refs.extend(
            value for key in ("image", "host_image", "agent_visible_image")
            if isinstance((value := payload.get(key)), str)
        )
    elif tool_name == "browser_get_images":
        for item in payload.get("images") or []:
            if isinstance(item, dict) and isinstance(item.get("src"), str):
                refs.append(item["src"])
    elif tool_name == "browser_vision":
        for value in (
            payload.get("screenshot_path"),
            (payload.get("meta") or {}).get("screenshot_path")
            if isinstance(payload.get("meta"), dict) else None,
        ):
            if isinstance(value, str):
                refs.append(value)
    return refs


def register_trusted_tool_result(
    session_id: Any,
    tool_name: str,
    result: Any,
) -> int:
    """Register media emitted by a registry-designated trusted producer."""
    try:
        from tools.registry import registry

        entry = registry.get_entry(tool_name)
        if entry is None or not entry.produces_trusted_media:
            return 0
    except Exception:
        return 0
    return register_trusted_media(
        session_id,
        _trusted_tool_references(tool_name, result),
        origin="trusted_tool",
        producer=tool_name,
    )
