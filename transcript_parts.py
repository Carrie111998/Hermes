"""Bounded ordered transcript-part admission.

The model-facing message shape is intentionally left alone.  This module owns
the small, presentation-safe envelope persisted alongside a message and sent
on the gateway display/event paths.  Older rows have no envelope and are
projected to one text part at the edge.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit


PARTS_VERSION = 1
MAX_PARTS = 64
MAX_NODES = 256
MAX_DEPTH = 8
MAX_SCALARS = 65_536
MAX_BYTES = 256 * 1024
MAX_STRING_SCALARS = 16_384
MAX_REFERENCE_SCALARS = 4_096
MAX_ID_SCALARS = 256

_TEXT_TYPES = frozenset({"text", "input_text", "output_text"})
_REASONING_TYPES = frozenset({"reasoning", "thinking", "reasoning_text"})
_IMAGE_TYPES = frozenset({"image", "image_url", "input_image"})
_AUDIO_TYPES = frozenset({"audio", "input_audio"})
_FILE_TYPES = frozenset({"file", "input_file", "file_url"})


class _Budget:
    __slots__ = ("nodes", "scalars", "bytes", "clipped")

    def __init__(self) -> None:
        self.nodes = 0
        self.scalars = 0
        self.bytes = 0
        self.clipped = False

    def node(self) -> bool:
        if self.nodes >= MAX_NODES:
            self.clipped = True
            return False
        self.nodes += 1
        return True

    def scalar(self) -> bool:
        if self.scalars >= MAX_SCALARS:
            self.clipped = True
            return False
        self.scalars += 1
        return True

    def text(self, value: Any, maximum: int = MAX_STRING_SCALARS) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        elif not isinstance(value, str):
            try:
                value = str(value)
            except (TypeError, ValueError, OverflowError):
                # Python itself can refuse hostile integer/string coercions
                # (for example the max-int-digit guard). Admission must stay
                # fail-closed rather than taking down persistence or a live
                # gateway event.
                self.clipped = True
                value = f"<{type(value).__name__}>"
        # Slice before any encoding so an attacker cannot force an unbounded
        # temporary string through a hostile scalar.
        raw = value[: maximum + 1]
        clipped = len(value) > maximum
        available_scalars = max(0, MAX_SCALARS - self.scalars)
        if len(raw) > available_scalars:
            raw = raw[:available_scalars]
            clipped = True
        available_bytes = max(0, MAX_BYTES - self.bytes)
        encoded = raw.encode("utf-8", "replace")
        if len(encoded) > available_bytes:
            # Bound in linear time. Repeatedly slicing one scalar and
            # re-encoding makes a hostile multibyte string quadratic.
            raw = encoded[:available_bytes].decode("utf-8", "ignore")
            encoded = raw.encode("utf-8")
            clipped = True
        else:
            # Replace lone surrogates now so later JSON encoding cannot fail.
            normalized = encoded.decode("utf-8")
            if normalized != raw:
                clipped = True
            raw = normalized
        if clipped:
            self.clipped = True
        self.scalars += len(raw)
        self.bytes += len(encoded)
        return raw


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0 or value > 1_000_000_000_000:
        return None
    return value


def _identity(value: Any, budget: _Budget) -> str | None:
    if value is None:
        return None
    text = budget.text(value, MAX_ID_SCALARS)
    # Control characters and bidi controls are not safe durable identities.
    safe = "".join(
        ch for ch in text
        if ch.isprintable() and not (0x202A <= ord(ch) <= 0x202E)
        and not (0x2066 <= ord(ch) <= 0x2069)
    )
    if safe != text:
        budget.clipped = True
    return safe or None


def _copy_timing(source: Mapping[str, Any], part: dict[str, Any], budget: _Budget) -> None:
    timestamp = _finite_number(source.get("timestamp", source.get("started_at")))
    completed = _finite_number(
        source.get("completed_at", source.get("completedAt", source.get("ended_at")))
    )
    if timestamp is not None:
        part["timestamp"] = timestamp
    if completed is not None:
        part["completed_at"] = completed
    # Keep the admission accounting explicit even though numbers do not consume
    # scalar/byte text budget. This prevents a future metadata field from
    # bypassing the node cap by accident.
    if timestamp is not None or completed is not None:
        budget.node()


def _safe_media_ref(value: Any, budget: _Budget) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if isinstance(value, Mapping):
        value = (
            value.get("url")
            or value.get("ref")
            or value.get("file_id")
            or value.get("file_ref")
            or value.get("path")
        )
    if not isinstance(value, str):
        return None, True
    raw = budget.text(value, MAX_REFERENCE_SCALARS).strip()
    if not raw:
        return None, False
    if raw.startswith("data:"):
        # Inline bytes are content, not a safe durable reference. Keep only the
        # media header so an event/row cannot leak image/audio bytes or a token.
        header = raw.split(",", 1)[0]
        return budget.text(header, MAX_REFERENCE_SCALARS), True
    if raw.startswith("@image:"):
        # The gateway uses this marker for local attachments.  Persist only a
        # harmless basename so a display event/row cannot disclose the
        # operator's absolute filesystem path.
        marker_path = raw[len("@image:"):].replace("\\", "/")
        basename = marker_path.rsplit("/", 1)[-1]
        if not basename or basename in {".", ".."}:
            basename = "attachment"
        safe = "@image:" + basename
        return budget.text(safe, MAX_REFERENCE_SCALARS), safe != raw
    if raw.startswith("@"):
        return budget.text("@attachment", MAX_REFERENCE_SCALARS), True
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None, True
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        # Strip userinfo, query and fragment. Signed URLs are credentials, and
        # the display contract carries a ref rather than a fetch authority.
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            return "media:invalid", True
        if port:
            host = f"{host}:{port}"
        safe = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        return budget.text(safe, MAX_REFERENCE_SCALARS), safe != raw
    if parsed.scheme == "file":
        return "file:local", True
    if parsed.scheme or raw.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw):
        return "file:local", True
    # Relative refs are useful for a gateway-managed artifact; arbitrary
    # opaque strings are retained only as bounded inert references.
    if ":" not in raw:
        # Relative references are inert, but avoid carrying path traversal or
        # an accidental absolute path from a foreign platform.
        normalized = raw.replace("\\", "/")
        if normalized.startswith("/") or "/../" in f"/{normalized}/":
            basename = normalized.rsplit("/", 1)[-1] or "attachment"
            return budget.text("attachment:" + basename, MAX_REFERENCE_SCALARS), True
        return raw, False
    return None, True


def _source_type(source: Mapping[str, Any]) -> str:
    value = source.get("kind", source.get("type"))
    return value if isinstance(value, str) else ""


def _json_value(value: Any, budget: _Budget, depth: int = 0) -> Any:
    """Copy JSON-like tool evidence without walking an attacker-sized tree."""
    if depth > MAX_DEPTH:
        budget.clipped = True
        return {"clipped": True, "reason": "depth"}
    if not budget.node():
        return {"clipped": True, "reason": "nodes"}
    if value is None or isinstance(value, (bool, int, float)):
        if not budget.scalar():
            return {"clipped": True, "reason": "scalars"}
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 1_000_000_000_000:
            budget.clipped = True
            return {"clipped": True, "reason": "number"}
        if isinstance(value, float) and (not math.isfinite(value) or abs(value) > 1_000_000_000_000):
            budget.clipped = True
            return {"clipped": True, "reason": "number"}
        return value
    if isinstance(value, str):
        return budget.text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_NODES:
                budget.clipped = True
                break
            name = budget.text(key, MAX_ID_SCALARS)
            if not name:
                budget.clipped = True
                continue
            result[name] = _json_value(item, budget, depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = []
        for item in value:
            if len(result) >= MAX_PARTS or budget.nodes >= MAX_NODES:
                budget.clipped = True
                break
            result.append(_json_value(item, budget, depth + 1))
        return result
    budget.clipped = True
    return {"clipped": True, "reason": "unsupported"}


def _arguments(source: Mapping[str, Any], budget: _Budget) -> Any:
    value = source.get("arguments", source.get("args", source.get("input")))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {"text": budget.text(value), "malformed": True}
    if value is None:
        return None
    return _json_value(value, budget)


def _base_part(source: Mapping[str, Any], kind: str, budget: _Budget) -> dict[str, Any]:
    part: dict[str, Any] = {"kind": kind}
    identity = _identity(
        source.get("part_id", source.get("id", source.get("tool_call_id", source.get("tool_id")))),
        budget,
    )
    if identity:
        part["id"] = identity
    _copy_timing(source, part, budget)
    return part


def _map_part(
    source: Any,
    budget: _Budget,
    *,
    tool_result_id: str | None = None,
    tool_result_name: str | None = None,
) -> dict[str, Any] | None:
    if isinstance(source, str):
        source = {"type": "text", "text": source}
    if not isinstance(source, Mapping):
        part = {"kind": "malformed", "evidence": budget.text(type(source).__name__, MAX_ID_SCALARS)}
        part["clipped"] = True
        budget.clipped = True
        return part
    raw_kind = _source_type(source)
    if raw_kind in _TEXT_TYPES:
        kind = "text"
    elif raw_kind in _REASONING_TYPES:
        kind = "reasoning"
    elif raw_kind in _IMAGE_TYPES:
        kind = "image"
    elif raw_kind in _AUDIO_TYPES:
        kind = "audio"
    elif raw_kind in _FILE_TYPES:
        kind = "file"
    elif raw_kind in {"tool", "tool_call", "tool-call", "invocation"}:
        kind = "tool-call"
    elif raw_kind in {"tool_result", "tool-result", "result"}:
        kind = "tool-result"
    elif "text" in source or "content" in source:
        kind = "text"
    else:
        kind = "unknown"

    if tool_result_id is not None:
        result = _base_part(source, "tool-result", budget)
        result["id"] = tool_result_id
        if tool_result_name:
            result["name"] = tool_result_name
        result["content_kind"] = kind
    else:
        result = _base_part(source, kind, budget)

    if kind in {"text", "reasoning"}:
        value = source.get("text", source.get("content", ""))
        result["text"] = budget.text(value)
    elif kind in {"image", "audio", "file"}:
        candidate = source.get("url", source.get("ref"))
        if candidate is None:
            for nested_key in ("image_url", "input_image", "audio", "input_audio", "file", "input_file"):
                if nested_key in source:
                    candidate = source.get(nested_key)
                    break
        ref, clipped = _safe_media_ref(candidate, budget)
        if ref:
            result["ref"] = ref
        mime = source.get("mime_type", source.get("mime", source.get("format")))
        if isinstance(mime, str) and mime:
            result["mime_type"] = budget.text(mime, MAX_ID_SCALARS)
        if clipped or not ref:
            result["clipped"] = True
            budget.clipped = True
    elif kind == "tool-call":
        name = _identity(source.get("name", source.get("tool_name")), budget)
        if name:
            result["name"] = name
        arguments = _arguments(source, budget)
        if arguments is not None:
            result["arguments"] = arguments
    elif kind == "tool-result":
        # A canonical tool-result can be admitted more than once (DB write,
        # DB read, resume projection, wire copy). Preserve its evidence on
        # every pass instead of treating only the provider-native shape as
        # authoritative.
        name = _identity(source.get("name", source.get("tool_name")), budget)
        if name:
            result["name"] = name
        content_kind = source.get("content_kind")
        if isinstance(content_kind, str) and content_kind:
            result["content_kind"] = budget.text(content_kind, MAX_ID_SCALARS)
        if content_kind in {"image", "audio", "file"} and source.get("ref") is not None:
            ref, clipped = _safe_media_ref(source.get("ref"), budget)
            if ref:
                result["ref"] = ref
            mime = source.get("mime_type")
            if isinstance(mime, str) and mime:
                result["mime_type"] = budget.text(mime, MAX_ID_SCALARS)
            if clipped or not ref:
                result["clipped"] = True
                budget.clipped = True
        else:
            value = source.get(
                "value",
                source.get("text", source.get("content", source.get("result", ""))),
            )
            if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
                result["value"] = _json_value(value, budget)
            else:
                result["text"] = budget.text(value)
    else:
        result["source_type"] = budget.text(raw_kind or "unknown", MAX_ID_SCALARS)
        evidence = source.get("text", source.get("content", source.get("value")))
        if evidence is not None and not isinstance(evidence, (Mapping, Sequence)):
            result["evidence"] = budget.text(evidence)
        result["clipped"] = True
        budget.clipped = True
    return result


def _canonical_envelope(value: Any, budget: _Budget) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    raw_parts = value.get("parts", value.get("items"))
    if not isinstance(raw_parts, list):
        return None
    parts: list[dict[str, Any]] = []
    for raw in raw_parts:
        if len(parts) >= MAX_PARTS:
            budget.clipped = True
            break
        # Clipping is envelope evidence, not a second content item.  Drop an
        # already-materialized marker while normalizing concatenated message
        # runs; _finish puts one marker at the end so later tool results keep
        # their original order.
        if isinstance(raw, Mapping) and raw.get("kind") == "clipped":
            budget.clipped = True
            continue
        mapped = _map_part(raw, budget)
        if mapped is not None:
            parts.append(mapped)
    clipped = bool(value.get("clipped") or value.get("parts_clipped") or budget.clipped)
    return _finish(parts, clipped, budget)


def _finish(parts: list[dict[str, Any]], clipped: bool, budget: _Budget) -> dict[str, Any]:
    clipped = bool(clipped or budget.clipped)
    envelope: dict[str, Any] = {"version": PARTS_VERSION, "parts": parts, "clipped": clipped}
    marker = {"kind": "clipped", "clipped": True, "reason": "budget"}
    if clipped:
        # The marker itself consumes one part slot. Replace the last admitted
        # item at the hard boundary rather than returning MAX_PARTS + 1.
        if len(parts) >= MAX_PARTS:
            del parts[MAX_PARTS - 1:]
        parts.append(marker)
        envelope["clipped"] = True
    while _encoded_size(envelope) > MAX_BYTES and len(parts) > 1:
        # Keep explicit clipping evidence at the tail while dropping the
        # newest content item atomically.
        marker_at_tail = parts[-1].get("kind") == "clipped"
        del parts[-2 if marker_at_tail else -1]
        envelope["clipped"] = True
    if len(parts) > MAX_PARTS:
        del parts[MAX_PARTS:]
        envelope["clipped"] = True
    return envelope


def _encoded_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return MAX_BYTES + 1


def message_parts(message: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a bounded canonical envelope for one message."""
    if not isinstance(message, Mapping):
        return {"version": PARTS_VERSION, "parts": [], "clipped": True}
    budget = _Budget()
    supplied = message.get("parts")
    if isinstance(supplied, Mapping):
        envelope = _canonical_envelope(supplied, budget)
        if envelope is not None:
            return envelope
    elif isinstance(supplied, list):
        envelope = _canonical_envelope({"parts": supplied}, budget)
        if envelope is not None:
            return envelope

    role = message.get("role")
    parts: list[dict[str, Any]] = []
    if role == "assistant":
        reasoning = message.get("reasoning") or message.get("reasoning_content")
        if reasoning:
            mapped = _map_part({"type": "reasoning", "text": reasoning}, budget)
            if mapped:
                parts.append(mapped)

    content = message.get("content")
    if isinstance(content, Mapping) and content.get("_multimodal") is True:
        content = content.get("content")
    if isinstance(content, list):
        for raw in content:
            if len(parts) >= MAX_PARTS:
                budget.clipped = True
                break
            mapped = _map_part(
                raw,
                budget,
                tool_result_id=_identity(message.get("tool_call_id"), budget) if role == "tool" else None,
                tool_result_name=_identity(message.get("tool_name", message.get("name")), budget) if role == "tool" else None,
            )
            if mapped:
                parts.append(mapped)
    elif content is not None:
        if role == "tool" and isinstance(content, (Mapping, Sequence)) and not isinstance(content, (str, bytes, bytearray)):
            source = {"type": "tool-result", "result": content}
        else:
            source = {"type": "text", "text": content}
        mapped = _map_part(
            source,
            budget,
            tool_result_id=_identity(message.get("tool_call_id"), budget) if role == "tool" else None,
            tool_result_name=_identity(message.get("tool_name", message.get("name")), budget) if role == "tool" else None,
        )
        if mapped and (mapped.get("text") or mapped.get("kind") not in {"text", "reasoning"}):
            parts.append(mapped)

    if role == "assistant" and isinstance(message.get("tool_calls"), list):
        for raw_call in message["tool_calls"][:MAX_PARTS]:
            if len(parts) >= MAX_PARTS:
                budget.clipped = True
                break
            if not isinstance(raw_call, Mapping):
                budget.clipped = True
                continue
            function = raw_call.get("function")
            source = dict(function) if isinstance(function, Mapping) else dict(raw_call)
            source.setdefault("type", "tool-call")
            source.setdefault("id", raw_call.get("id", raw_call.get("tool_call_id")))
            mapped = _map_part(source, budget)
            if mapped:
                parts.append(mapped)
    return _finish(parts, budget.clipped, budget)


def wire_fields(envelope: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the additive fields used by display/live gateway payloads."""
    normalized = message_parts({"parts": envelope}) if envelope is not None else message_parts(None)
    return {
        "parts": copy.deepcopy(normalized["parts"]),
        "parts_version": PARTS_VERSION,
        "parts_clipped": bool(normalized.get("clipped")),
    }


def stream_text_part(text: Any, *, part_id: str = "assistant-stream", timestamp: Any = None) -> dict[str, Any]:
    source: dict[str, Any] = {"type": "text", "text": text, "id": part_id}
    if timestamp is not None:
        source["timestamp"] = timestamp
    envelope = message_parts({"role": "assistant", "parts": [source]})
    return envelope["parts"][0] if envelope["parts"] else {"kind": "clipped", "clipped": True}


__all__ = [
    "MAX_BYTES",
    "MAX_DEPTH",
    "MAX_NODES",
    "MAX_PARTS",
    "MAX_SCALARS",
    "MAX_STRING_SCALARS",
    "PARTS_VERSION",
    "message_parts",
    "stream_text_part",
    "wire_fields",
]
