"""Opt-in PA photo-pair classification helpers for sprucing workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping


DEFAULT_PAIR_WINDOW_SECONDS = 5 * 60
_MEDIA_KEYS = ("file_id", "file_url", "getFile_url", "url")
_JOB_TOKEN_RE = re.compile(r"\b[A-Z]{1,8}/JOB/\d{4}/\d{2,8}\b", re.IGNORECASE)
_LABELLED_TOKEN_RE = re.compile(
    r"\b(?:job(?:\s*(?:no|number|id))?|case(?:\s*(?:id|no|number))?)"
    r"\s*[:#-]?\s*([A-Z0-9][A-Z0-9/_-]{2,})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PhotoMessage:
    media_ref: Mapping[str, Any]
    timestamp: datetime
    text: str = ""
    message_id: str | None = None


@dataclass(frozen=True)
class PhotoPairClassification:
    before: PhotoMessage
    after: PhotoMessage
    confidence: float
    classified_at: str
    shared_context_tokens: tuple[str, ...] = ()

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "before": _payload_media_ref(self.before.media_ref),
            "after": _payload_media_ref(self.after.media_ref),
            "confidence": self.confidence,
            "classified_at": self.classified_at,
        }


def classify_photo_pair(
    event: Any,
    history: Iterable[Mapping[str, Any]] | None,
    *,
    cached_photos: Iterable[PhotoMessage] | None = None,
    current_text: str | None = None,
    now: datetime | None = None,
    window_seconds: int = DEFAULT_PAIR_WINDOW_SECONDS,
) -> PhotoPairClassification | None:
    """Classify the current photo as the after side of a recent before/after pair."""
    current = photo_message_from_event(event, current_text=current_text, now=now)
    if current is None:
        return None

    candidates = list(cached_photos or [])
    candidates.extend(photo_messages_from_history(history or []))
    candidates = _dedupe_photo_messages(candidates)

    best: tuple[float, PhotoMessage] | None = None
    for candidate in candidates:
        if current.message_id and candidate.message_id == current.message_id:
            continue
        delta_seconds = (current.timestamp - candidate.timestamp).total_seconds()
        if delta_seconds <= 0 or delta_seconds > window_seconds:
            continue
        if best is None or delta_seconds < best[0]:
            best = (delta_seconds, candidate)

    if best is None:
        return None

    delta_seconds, before = best
    shared_tokens = tuple(sorted(_context_tokens(before.text) & _context_tokens(current.text)))
    confidence = _confidence(delta_seconds, window_seconds, bool(shared_tokens))
    classified_at = _format_timestamp(now or datetime.now(timezone.utc))
    return PhotoPairClassification(
        before=before,
        after=current,
        confidence=confidence,
        classified_at=classified_at,
        shared_context_tokens=shared_tokens,
    )


def photo_message_from_event(
    event: Any,
    *,
    current_text: str | None = None,
    now: datetime | None = None,
) -> PhotoMessage | None:
    media_ref = _first_media_ref(getattr(event, "media_refs", None))
    if media_ref is None:
        return None
    timestamp = _parse_timestamp(getattr(event, "timestamp", None)) or _to_naive_utc(
        now or datetime.now(timezone.utc)
    )
    text = getattr(event, "text", "") if current_text is None else current_text
    return PhotoMessage(
        media_ref=media_ref,
        timestamp=timestamp,
        text=str(text or ""),
        message_id=str(getattr(event, "message_id", "") or "") or None,
    )


def remember_photo_message(
    cache: Iterable[PhotoMessage] | None,
    photo: PhotoMessage,
    *,
    window_seconds: int = DEFAULT_PAIR_WINDOW_SECONDS,
    max_entries: int = 50,
) -> list[PhotoMessage]:
    cutoff = photo.timestamp - timedelta(seconds=window_seconds)
    retained = [
        existing
        for existing in (cache or [])
        if existing.timestamp >= cutoff and not _same_photo(existing, photo)
    ]
    retained.append(photo)
    return retained[-max_entries:]


def photo_messages_from_history(
    history: Iterable[Mapping[str, Any]],
) -> list[PhotoMessage]:
    photos: list[PhotoMessage] = []
    for entry in history:
        if not isinstance(entry, Mapping) or entry.get("role") != "user":
            continue
        timestamp = _parse_timestamp(
            entry.get("timestamp") or entry.get("created_at") or entry.get("ts")
        )
        if timestamp is None:
            continue
        text = _content_text(entry.get("content"))
        media_ref = _first_media_ref_from_text(text)
        if media_ref is None:
            continue
        message_id = str(entry.get("message_id", "") or "") or None
        photos.append(
            PhotoMessage(
                media_ref=media_ref,
                timestamp=timestamp,
                text=text,
                message_id=message_id,
            )
        )
    return photos


def _first_media_ref(media_refs: Any) -> Mapping[str, Any] | None:
    if not isinstance(media_refs, list):
        return None
    for ref in media_refs:
        if not isinstance(ref, Mapping):
            continue
        if any(ref.get(key) for key in _MEDIA_KEYS):
            return dict(ref)
    return None


def _first_media_ref_from_text(text: str) -> Mapping[str, str] | None:
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[media#"):
            current = {}
            continue
        if current is None:
            continue
        if line == "]":
            if any(current.get(key) for key in _MEDIA_KEYS):
                return current
            current = None
            continue
        key, sep, value = line.partition("=")
        if sep and key in _MEDIA_KEYS:
            current[key] = value.strip()
    return None


def _payload_media_ref(ref: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        "file_id": _str_or_none(ref.get("file_id")),
        "getFile_url": _str_or_none(
            ref.get("getFile_url") or ref.get("file_url") or ref.get("url")
        ),
    }


def _confidence(delta_seconds: float, window_seconds: int, has_text_context: bool) -> float:
    proximity = max(0.0, min(1.0, 1.0 - (delta_seconds / max(window_seconds, 1))))
    score = 0.55 + (0.35 * proximity)
    if has_text_context:
        score += 0.10
    return round(min(1.0, score), 3)


def _context_tokens(text: str) -> set[str]:
    stripped = _strip_media_blocks(text)
    tokens = {match.group(0).upper() for match in _JOB_TOKEN_RE.finditer(stripped)}
    tokens.update(
        match.group(1).upper()
        for match in _LABELLED_TOKEN_RE.finditer(stripped)
        if any(ch.isdigit() for ch in match.group(1))
    )
    return tokens


def _strip_media_blocks(text: str) -> str:
    lines: list[str] = []
    in_media = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[media#"):
            in_media = True
            continue
        if in_media and line == "]":
            in_media = False
            continue
        if not in_media:
            lines.append(raw_line)
    return "\n".join(lines)


def _dedupe_photo_messages(messages: Iterable[PhotoMessage]) -> list[PhotoMessage]:
    deduped: list[PhotoMessage] = []
    seen: set[tuple[str, str, str]] = set()
    for message in messages:
        ref = message.media_ref
        key = (
            message.message_id or "",
            str(ref.get("file_id") or ""),
            _format_timestamp(message.timestamp),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(message)
    return deduped


def _same_photo(left: PhotoMessage, right: PhotoMessage) -> bool:
    if left.message_id and right.message_id and left.message_id == right.message_id:
        return True
    left_file = left.media_ref.get("file_id")
    right_file = right.media_ref.get("file_id")
    return bool(left_file and right_file and left_file == right_file)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                value = item.get("text") or item.get("content")
                if value:
                    parts.append(str(value))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    return "" if content is None else str(content)


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_naive_utc(value)
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return _parse_timestamp(float(raw))
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return _to_naive_utc(parsed)
        except ValueError:
            return None
    return None


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
