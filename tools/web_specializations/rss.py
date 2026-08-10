"""RSS/Atom post-processing specialization for :func:`tools.web_tools.web_extract_tool`.

Pure parsing and normalization only — no network I/O. Runs after the existing
provider fetch/validation path returns bounded content.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MAX_FEED_BYTES = 2 * 1024 * 1024
_MAX_ENTRIES = 200
_MAX_SUMMARY_CHARS = 2000
_SNIFF_SAMPLE_CHARS = 512
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")

_FEED_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/rdf+xml",
    "application/xml",
    "text/xml",
}
_FEED_SNIFF_MARKERS = (
    b"<?xml",
    b"<rss",
    b"<feed",
    b"<rdf:rdf",
    b"<rdf ",
)

try:
    from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
    from defusedxml.common import EntitiesForbidden  # type: ignore[import-untyped]

    _HAS_DEFUSEDXML = True
except ImportError:
    import xml.etree.ElementTree as ET

    _HAS_DEFUSEDXML = False
    EntitiesForbidden = ()  # type: ignore[misc,assignment]


def _local_name(tag: Any) -> str:
    value = str(tag or "")
    return value.rsplit("}", 1)[-1].lower()


def _text(element: Any) -> str:
    if element is None:
        return ""
    value = " ".join(part.strip() for part in element.itertext() if part.strip())
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _clean_summary(value: str) -> str:
    value = html.unescape(value or "")
    value = _STRIP_TAGS_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    if len(value) > _MAX_SUMMARY_CHARS:
        value = value[: _MAX_SUMMARY_CHARS].rstrip() + "…"
    return value


def _child(element: Any, *names: str) -> Any:
    wanted = {name.lower() for name in names}
    for candidate in list(element):
        if _local_name(candidate.tag) in wanted:
            return candidate
    return None


def _entry_link(entry: Any) -> str:
    fallback = ""
    for element in list(entry):
        if _local_name(element.tag) != "link":
            continue
        href = (element.attrib.get("href") or _text(element)).strip()
        if not href:
            continue
        rel = (element.attrib.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def _reject_unsafe_xml_text(xml_bytes: bytes) -> bool:
    """Return True when stdlib parsing must be rejected (XXE / entity expansion)."""
    if _HAS_DEFUSEDXML:
        return False
    try:
        sample = xml_bytes[:8192].decode("utf-8", errors="ignore").upper()
    except Exception:
        return True
    return "<!DOCTYPE" in sample or "<!ENTITY" in sample


def _safe_fromstring(xml_bytes: bytes) -> Optional[Any]:
    if _reject_unsafe_xml_text(xml_bytes):
        return None
    try:
        return ET.fromstring(xml_bytes)
    except (ET.ParseError, EntitiesForbidden):
        return None


def _content_bytes(result: Dict[str, Any]) -> bytes:
    raw = result.get("raw_content")
    if raw is None:
        raw = result.get("content") or ""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return b""


def _looks_like_feed_content(result: Dict[str, Any], xml_bytes: bytes) -> bool:
    metadata = result.get("metadata") or {}
    content_type = (
        metadata.get("contentType")
        or metadata.get("content_type")
        or metadata.get("Content-Type")
        or ""
    )
    if isinstance(content_type, str) and content_type.strip():
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type in _FEED_CONTENT_TYPES:
            return True

    sample = xml_bytes[:_SNIFF_SAMPLE_CHARS].lower()
    return any(marker in sample for marker in _FEED_SNIFF_MARKERS)


def parse_feed(xml_bytes: bytes, source_url: str) -> Optional[Dict[str, Any]]:
    """Parse RSS 2.0, Atom, or RDF/RSS bytes into a web-extract result.

    Returns ``None`` when XML is malformed, rejected as unsafe, or valid but
    not a recognized feed. Never raises.
    """
    if not xml_bytes or len(xml_bytes) > _MAX_FEED_BYTES:
        return None

    root = _safe_fromstring(xml_bytes)
    if root is None:
        return None

    root_name = _local_name(root.tag)
    if root_name not in {"rss", "feed", "rdf"}:
        return None

    channel = _child(root, "channel") if root_name in {"rss", "rdf"} else root
    if channel is None:
        channel = root

    feed_title = _text(_child(channel, "title")) or "Feed"
    feed_description = _clean_summary(_text(_child(channel, "description", "subtitle")))
    feed_updated = _text(_child(channel, "lastbuilddate", "updated", "pubdate"))

    entries = []
    seen = set()
    for candidate in root.iter():
        if _local_name(candidate.tag) not in {"item", "entry"}:
            continue
        if len(entries) >= _MAX_ENTRIES:
            break

        title = _text(_child(candidate, "title")) or "Untitled"
        link = _entry_link(candidate)
        identifier = _text(_child(candidate, "guid", "id")) or link or title
        if identifier in seen:
            continue
        seen.add(identifier)

        author_element = _child(candidate, "author", "creator")
        author = _text(_child(author_element, "name")) if author_element is not None else ""
        author = author or _text(author_element)
        published = _text(_child(candidate, "pubdate", "published", "updated", "date"))
        summary = _clean_summary(
            _text(_child(candidate, "description", "summary", "content", "encoded"))
        )
        entries.append(
            {
                "title": title,
                "link": link,
                "author": author,
                "published": published,
                "summary": summary,
            }
        )

    lines = [f"# {feed_title}", "", f"- Source: {source_url}"]
    if feed_updated:
        lines.append(f"- Updated: {feed_updated}")
    if feed_description:
        lines.extend(["", feed_description])
    lines.extend(["", "## Entries"])

    if not entries:
        lines.extend(["", "No entries found."])
    for entry in entries:
        heading = f"[{entry['title']}]({entry['link']})" if entry["link"] else entry["title"]
        lines.extend(["", f"### {heading}"])
        if entry["published"]:
            lines.append(f"- Published: {entry['published']}")
        if entry["author"]:
            lines.append(f"- Author: {entry['author']}")
        if entry["summary"]:
            lines.extend(["", entry["summary"]])

    content = "\n".join(lines).strip()
    return {
        "url": source_url,
        "title": feed_title,
        "content": content,
        "raw_content": content,
        "metadata": {
            "sourceURL": source_url,
            "title": feed_title,
            "contentType": "feed",
            "entryCount": len(entries),
        },
    }


def apply_rss_specialization(result: Dict[str, Any]) -> None:
    """Replace *result* content with parsed feed markdown when applicable.

    Mutates *result* in place on success. On any failure or non-feed content,
    leaves *result* unchanged. Never raises.
    """
    try:
        xml_bytes = _content_bytes(result)
        if not xml_bytes or len(xml_bytes) > _MAX_FEED_BYTES:
            return
        if not _looks_like_feed_content(result, xml_bytes):
            return

        source_url = result.get("url") or ""
        parsed = parse_feed(xml_bytes, source_url)
        if parsed is None:
            return

        result.update(parsed)
    except Exception as exc:
        logger.debug("RSS/Atom specialization skipped: %s", exc)
