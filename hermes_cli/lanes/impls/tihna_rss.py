"""Hardened RSS/Atom fetch and normalization for Tihna."""

from __future__ import annotations

import calendar
import hashlib
import html
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import bleach
import feedparser
import requests

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 10
USER_AGENT = "Hermes-Tihna/1.0 (Adrian Stroe research)"
CACHE_SECONDS = 4 * 60 * 60

_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _emit(metric: Callable[[str, float], None] | None, name: str) -> None:
    if metric is not None:
        metric(name, 1.0)


def sanitize_text(value: Any, *, limit: int) -> str:
    raw = bleach.clean(
        str(value or ""),
        tags=[],
        attributes={},
        strip=True,
    )
    normalized = re.sub(r"\s+", " ", html.unescape(raw)).strip()
    if len(normalized) <= limit:
        return normalized
    if limit <= 1:
        return "…"[:limit]
    return normalized[: limit - 1].rstrip() + "…"


def external_id_for(feed_url: str, entry: Any) -> str:
    identity = (
        entry.get("id")
        or entry.get("guid")
        or entry.get("link")
        or entry.get("title")
        or ""
    )
    value = f"{feed_url}||{identity}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


def _published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        return datetime.fromtimestamp(
            calendar.timegm(parsed),
            tz=timezone.utc,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_link(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_feed_from_bytes(
    payload: bytes,
    *,
    feed_url: str,
    category: str = "other",
    now: datetime | None = None,
    metric: Callable[[str, float], None] | None = None,
) -> list[dict[str, Any]]:
    if len(payload) > MAX_RESPONSE_BYTES:
        _emit(metric, "feed_fetch_error")
        return []
    parsed = feedparser.parse(payload)
    if parsed.bozo:
        _emit(metric, "feed_fetch_error")
        return []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current - timedelta(days=8)
    entries: list[dict[str, Any]] = []
    for entry in parsed.entries:
        title = sanitize_text(entry.get("title"), limit=300)
        summary_source = (
            entry.get("summary")
            or entry.get("description")
            or entry.get("content", [{}])[0].get("value")
        )
        summary = sanitize_text(summary_source, limit=4000)
        published = _published_at(entry)
        link = str(entry.get("link") or "").strip()
        if (
            not title
            or not summary
            or len(summary) < 100
            or published is None
            or published < cutoff
            or published > current + timedelta(hours=24)
            or not _valid_link(link)
        ):
            continue
        entries.append(
            {
                "external_id": external_id_for(feed_url, entry),
                "feed_url": feed_url,
                "title": title,
                "link": link,
                "pub_date": published.isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z"),
                "summary": summary,
                "author": sanitize_text(entry.get("author"), limit=200),
                "tags": [
                    sanitize_text(tag.get("term"), limit=100)
                    for tag in entry.get("tags", [])
                    if tag.get("term")
                ],
                "category": category,
            }
        )
    return entries


def fetch_feed(
    feed_url: str,
    *,
    category: str = "other",
    now: datetime | None = None,
    metric: Callable[[str, float], None] | None = None,
    http_get: Callable[..., Any] = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[dict[str, Any]]:
    cached = _CACHE.get(feed_url)
    clock = monotonic()
    if cached is not None and clock - cached[0] < CACHE_SECONDS:
        return [dict(item) for item in cached[1]]
    for attempt in range(2):
        try:
            response = http_get(
                feed_url,
                timeout=TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            content_length = int(response.headers.get("Content-Length") or 0)
            if content_length > MAX_RESPONSE_BYTES:
                raise ValueError("RSS response exceeds 5MB")
            payload = bytes(response.content)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("RSS response exceeds 5MB")
            entries = fetch_feed_from_bytes(
                payload,
                feed_url=feed_url,
                category=category,
                now=now,
                metric=metric,
            )
            _CACHE[feed_url] = (clock, [dict(item) for item in entries])
            return entries
        except (requests.RequestException, ValueError):
            if attempt == 0:
                sleep(2.0)
                continue
    _emit(metric, "feed_fetch_error")
    return []


def _reset_cache_for_tests() -> None:
    _CACHE.clear()


__all__ = [
    "CACHE_SECONDS",
    "MAX_RESPONSE_BYTES",
    "TIMEOUT_SECONDS",
    "USER_AGENT",
    "_reset_cache_for_tests",
    "external_id_for",
    "fetch_feed",
    "fetch_feed_from_bytes",
    "sanitize_text",
]
