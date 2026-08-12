"""Bing search + page extraction via the local agent-browser CLI.

Subclasses :class:`agent.web_search_provider.WebSearchProvider` (the
plugin-facing ABC) and registers under the explicit opt-in name
``bing-browser``. No API key, no credentials, no network in
``is_available()`` — availability is the cheap combination of *explicit
web config naming this provider* and *the agent-browser CLI resolving*
(``tools.browser_tool._find_agent_browser(validate=False)``), so the
provider never auto-activates and never runs a subprocess at
registration/paint time.

Search flow:

1. Build a Bing SERP URL (``https://www.bing.com/search?q=...``).
2. ``tools.browser_tool.browser_navigate(url, task_id=<unique>)`` and
   parse its JSON response; prefer the auto snapshot embedded in the
   response.
3. When the navigate snapshot yields no organic results (including a
   nonempty header-only snapshot), fall back to
   ``tools.browser_tool.browser_snapshot(full=True, task_id=...)``.
4. Parse result entries from the accessibility snapshot, filter out
   bing.com-internal and non-http(s) URLs, dedupe preserving order, cap
   at the requested limit (hard cap 20), and return the legacy
   ``{"title", "url", "description", "position"}`` shape.

Bing organic results are ``listitem`` accessibility blocks: the result
URL appears as a child StaticText of the result link (truncated at
breadcrumb ``›`` / ellipsis ``…``/``...`` markers), the title is the
block's level=2 heading, the description is the paragraph's StaticText.
Annotated ``[url=...]`` link lines are still parsed first, and a
bare-URL fallback parser applies ONLY to the compact snapshot returned
by ``browser_navigate`` — a full ``browser_snapshot`` accessibility tree
is full of plain-text URL noise, so the full-snapshot path is parsed
strictly (annotated links + listitem blocks only, no bare scanning). No
Bing DOM selectors are used.

Extract flow: navigate each URL (max 5) in a fresh session with a unique
task id, take the navigate snapshot (or the ``full=True`` snapshot
fallback), strip ``[ref=...]``/``[url=...]`` annotations, and emit
``{"url", "title", "content", "raw_content", "metadata"}`` entries in
input order. Per-URL failures become ``{"url": ..., "error": ...}``
entries and the batch continues. Every task id is cleaned up in a
``finally`` block, even on parse/navigation failure.

``search`` never follows result URLs — ``web_search`` and ``web_extract``
stay separate contracts; the caller decides which results to extract.

Config keys this provider responds to (explicit opt-in)::

    web:
      search_backend: "bing-browser"   # per-capability override
      extract_backend: "bing-browser"  # per-capability override
      backend: "bing-browser"          # shared fallback
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional
from uuid import uuid4

from agent.web_search_provider import WebSearchProvider
from tools.browser_tool import (
    _find_agent_browser,
    browser_navigate,
    browser_snapshot,
    cleanup_browser,
)

logger = logging.getLogger(__name__)

_BING_SEARCH_URL = "https://www.bing.com/search"

# Hard cap on parsed search results (mirrors the brave-free provider's
# min(limit, 20) API cap — the approved contract for this provider even
# though web_search_tool clamps limit to [1, 100]).
_MAX_RESULTS = 20

# Max URLs processed per extract() call (mirrors web_extract's 5-URL cap).
_MAX_EXTRACT_URLS = 5

# Match a snapshot link line, capturing the anchor text and the trailing
# attribute block, e.g.:
#   - link "Example Domain" [ref=@e1] [url=https://example.com]
#   link "Docs" [url=https://docs.example.com]
_LINK_LINE_RE = re.compile(
    r"(?:^|\n)\s*[-*]?\s*link\s+\"((?:[^\"\\\\]|\\\\.)*)\"\s*"
    r"((?:\[[^\]\n]*\][ \t]*)*)",
    re.IGNORECASE,
)

# Extract the [url=...] annotation from a link line's attribute block.
_URL_ATTR_RE = re.compile(r"\[url=([^\]]+)\]", re.IGNORECASE)

# Bing organic results are ``listitem`` accessibility blocks: the result
# URL appears as a child StaticText of the result link, the title is the
# block's level=2 heading, the description is the paragraph's StaticText.
_LISTITEM_LINE_RE = re.compile(r"^\s*(?:-\s*)?listitem\b", re.IGNORECASE)
_STATICTEXT_LINE_RE = re.compile(
    r'^\s*(?:-\s*)?StaticText\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE
)
_LINK_START_RE = re.compile(r'^\s*(?:-\s*)?link\s+"', re.IGNORECASE)
_HEADING_LINE_RE = re.compile(
    r'^\s*(?:-\s*)?heading\s+"((?:[^"\\]|\\.)*)"\s*(\[[^\]]*\])?',
    re.IGNORECASE,
)
_PARAGRAPH_LINE_RE = re.compile(r"^\s*(?:-\s*)?paragraph\b", re.IGNORECASE)
_LEVEL2_ATTR_RE = re.compile(r"level=2\b", re.IGNORECASE)

# Bare absolute URLs anywhere in snapshot text (fallback only). The
# lookbehind skips URLs already captured inside [url=...] annotations.
_BARE_URL_RE = re.compile(r"(?<!\[url=)(https?://[^\s\"'<>\]\)]+)", re.IGNORECASE)

# Annotations stripped from extract content — refs and URLs only; other
# a11y attributes ([level=N], etc.) are preserved.
_ANNOTATION_RE = re.compile(r"\[(?:ref|url)=[^\]]*\]", re.IGNORECASE)

_MISSING_BROWSER_ERROR = (
    "agent-browser CLI is not installed. Install it via `hermes tools` "
    "(Browser setup) or run `npm install` in the repo root, then retry."
)


# ---------------------------------------------------------------------------
# Availability helpers (cheap — no network, no subprocess)
# ---------------------------------------------------------------------------


def _is_explicitly_configured() -> bool:
    """Return True when web config explicitly names ``bing-browser``.

    Checks ``web.search_backend`` / ``web.extract_backend`` / ``web.backend``
    in config.yaml. This is what makes the provider an explicit opt-in: it
    never auto-activates just because the agent-browser CLI exists.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:  # noqa: BLE001 — config layer optional here
        cfg = {}
    web = cfg.get("web") or {}
    for key in ("search_backend", "extract_backend", "backend"):
        if str(web.get(key) or "").strip().lower() == "bing-browser":
            return True
    return False


def _agent_browser_present() -> bool:
    """Return True when the agent-browser CLI resolves via a cheap probe.

    ``validate=False`` skips the exec-based runnable check, so this is a
    PATH/fs existence probe only — safe for tool-registration time and
    every ``hermes tools`` repaint.
    """
    try:
        _find_agent_browser(validate=False)
        return True
    except Exception:  # noqa: BLE001 — any probe failure means "absent"
        return False


# ---------------------------------------------------------------------------
# Snapshot parsing
# ---------------------------------------------------------------------------


def _is_acceptable_result_url(url: str) -> bool:
    """True for absolute http(s) URLs that are not bing.com-internal."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    host = urllib.parse.urlparse(url).netloc.lower()
    if host == "bing.com" or host.endswith(".bing.com"):
        return False
    return True


def _split_listitem_blocks(snapshot: str) -> List[List[str]]:
    """Split a snapshot into indentation-delimited ``listitem`` blocks.

    Each block is the ``- listitem`` marker line plus every deeper-indented
    child line, ending at the next line at or above the marker's indent.
    """
    blocks: List[List[str]] = []
    current: Optional[List[str]] = None
    current_indent = -1
    for line in (snapshot or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if _LISTITEM_LINE_RE.match(line):
            if current is not None:
                blocks.append(current)
            current = [line]
            current_indent = indent
        elif current is not None and indent <= current_indent:
            blocks.append(current)
            current = None
            current_indent = -1
        elif current is not None:
            current.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def _truncate_url_caption(caption: str) -> str:
    """Cut a Bing caption URL at the first breadcrumb/ellipsis marker.

    Bing renders result URLs as captions like
    ``https://blog.cloudflare.com › kitesurf`` or
    ``https://ai-revolution.co.jp › ... › what-is-cloudflare-kitesurf``.
    Only the exact absolute http(s) prefix before the first ``›`` /
    ``…`` / ``...`` marker is a real URL; the rest is breadcrumb noise.
    Bing ``/ck/a`` redirects are never followed or decoded.
    """
    cut = len(caption)
    for marker in ("›", "…", "..."):
        idx = caption.find(marker)
        if idx != -1 and idx < cut:
            cut = idx
    return caption[:cut].strip()


def _block_result_url(block: List[str]) -> Optional[str]:
    """Absolute http(s) URL from the result link's child StaticText lines.

    The first ``link`` line in a result block is the result link; its
    direct StaticText children carry the visible domain and the caption
    URL. Returns the first caption that is an absolute http(s) URL after
    breadcrumb/ellipsis truncation, or None (block skipped).
    """
    link_indent = -1
    seen_link = False
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if not seen_link:
            if _LINK_START_RE.match(line):
                seen_link = True
                link_indent = indent
            continue
        if indent <= link_indent:
            break
        match = _STATICTEXT_LINE_RE.match(line)
        if not match:
            continue
        caption = _truncate_url_caption(match.group(1))
        if caption.startswith(("http://", "https://")):
            return caption
    return None


def _block_heading_title(block: List[str]) -> Optional[str]:
    """Title text of the block's level=2 heading, or None."""
    for line in block:
        match = _HEADING_LINE_RE.match(line)
        if not match:
            continue
        attrs = match.group(2) or ""
        if _LEVEL2_ATTR_RE.search(attrs):
            return match.group(1)
    return None


def _block_description(block: List[str]) -> str:
    """Visible text of the block's paragraph ("" when absent or empty)."""
    parts: List[str] = []
    in_paragraph = False
    paragraph_indent = -1
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if _PARAGRAPH_LINE_RE.match(line):
            in_paragraph = True
            paragraph_indent = indent
            continue
        if in_paragraph:
            if indent <= paragraph_indent:
                in_paragraph = False
            else:
                match = _STATICTEXT_LINE_RE.match(line)
                if match:
                    parts.append(match.group(1))
    return " ".join(parts).strip()


def _parse_listitem_results(snapshot: str) -> List[Dict[str, str]]:
    """Parse Bing organic-result candidates from ``listitem`` blocks.

    Returns raw ``{"title", "url", "description"}`` candidates in tree
    order — no bing.com filtering, dedupe, or limit (the caller applies
    those). Blocks missing a level=2 heading or a caption URL are
    skipped, which also keeps header/filter-navigation listitems out of
    the results. Nested links inside headings/paragraphs are ignored —
    the block is the unit, so they cannot create duplicate results.
    """
    candidates: List[Dict[str, str]] = []
    for block in _split_listitem_blocks(snapshot):
        title = _block_heading_title(block)
        if title is None:
            continue
        url = _block_result_url(block)
        if url is None:
            continue
        candidates.append(
            {"title": title, "url": url, "description": _block_description(block)}
        )
    return candidates


def _parse_snapshot_results(
    snapshot: str,
    *,
    limit: int,
    allow_bare_urls: bool,
) -> List[Dict[str, Any]]:
    """Parse search results out of an accessibility snapshot.

    Three layered strategies, first nonempty wins:

    1. Annotated link lines: ``link "Title" ... [url=https://...]``.
    2. Bing organic-result ``listitem`` blocks: caption-URL StaticText,
       level=2 heading title, paragraph description.
    3. Bare absolute http(s) URLs anywhere in the text — ONLY when
       ``allow_bare_urls`` is set (navigate-embedded compact snapshot),
       never for full ``browser_snapshot`` trees (full of URL noise).

    Results are filtered (bing.com-internal, non-http schemes), deduped
    preserving first occurrence, capped at ``limit`` (hard cap 20), and
    returned in the legacy ``{"title", "url", "description", "position"}``
    shape. Bare-URL entries use the URL as the title.
    """
    candidates: List[Dict[str, str]] = []
    source: str = "annotated"

    for match in _LINK_LINE_RE.finditer(snapshot or ""):
        title = match.group(1)
        attrs = match.group(2) or ""
        url_match = _URL_ATTR_RE.search(attrs)
        if not url_match:
            continue
        candidates.append(
            {"title": title, "url": url_match.group(1).strip(), "description": ""}
        )

    if not candidates:
        candidates = _parse_listitem_results(snapshot or "")
        source = "listitem"

    if not candidates and allow_bare_urls:
        for url in _BARE_URL_RE.findall(snapshot or ""):
            candidates.append({"title": url, "url": url, "description": ""})
        source = "bare"

    seen: set = set()
    web_results: List[Dict[str, Any]] = []
    for candidate in candidates:
        url = candidate["url"]
        if not _is_acceptable_result_url(url):
            continue
        # Bing organic ``listitem`` blocks dedupe on (url, title): the SERP
        # legitimately repeats a host across distinct results (real artifacts
        # show same-host/multiple-title rows), so URL alone must not drop
        # them. Annotated ``[url=...]`` link lines and bare-URL fallbacks
        # keep URL-only dedupe (bare entries have title == url anyway).
        dedupe_key = (url, candidate["title"]) if source == "listitem" else url
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        web_results.append(
            {
                "title": candidate["title"],
                "url": url,
                "description": candidate.get("description", ""),
                "position": len(web_results) + 1,
            }
        )
        if len(web_results) >= max(1, min(int(limit), _MAX_RESULTS)):
            break
    return web_results


def _clean_snapshot_text(snapshot: str) -> str:
    """Strip ``[ref=...]`` / ``[url=...]`` annotations, keep the rest."""
    return _ANNOTATION_RE.sub("", snapshot or "").strip()


def _parse_browser_json(raw: Any) -> Dict[str, Any]:
    """Parse a browser-tool JSON string response; tolerate dict inputs."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        raise ValueError(f"browser tool returned non-JSON value: {type(raw).__name__}")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError("browser tool returned invalid JSON") from exc


def _build_search_url(query: str) -> str:
    """Build the Bing SERP URL for *query*."""
    return _BING_SEARCH_URL + "?" + urllib.parse.urlencode({"q": str(query)})


def _new_task_id(prefix: str) -> str:
    """Unique per-navigation task id for session isolation + cleanup."""
    return f"{prefix}-{uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BingBrowserWebSearchProvider(WebSearchProvider):
    """Bing search + page extraction via the local agent-browser CLI.

    Explicit opt-in (no credentials): available only when web config names
    ``bing-browser`` AND the agent-browser CLI resolves. Search and extract
    both run through :func:`tools.browser_tool.browser_navigate` /
    ``browser_snapshot`` / ``cleanup_browser`` with unique task ids; every
    task is cleaned up in a ``finally`` block.
    """

    @property
    def name(self) -> str:
        return "bing-browser"

    @property
    def display_name(self) -> str:
        return "Bing (browser)"

    def is_available(self) -> bool:
        """Explicit config opt-in AND agent-browser CLI present.

        Cheap existence checks only — no network, no subprocess, no exec
        (``_find_agent_browser(validate=False)``).
        """
        return _is_explicitly_configured() and _agent_browser_present()

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Search Bing through the browser and return legacy-shaped results.

        Returns ``{"success": True, "data": {"web": [{"title", "url",
        "description", "position"}, ...]}}`` or ``{"success": False,
        "error": str}``. The browser session is always cleaned up, even
        when navigation or parsing fails.
        """
        if not _agent_browser_present():
            return {"success": False, "error": _MISSING_BROWSER_ERROR}

        task_id = _new_task_id("bing-browser-search")
        try:
            raw = browser_navigate(_build_search_url(query), task_id=task_id)
            nav = _parse_browser_json(raw)
            if not nav.get("success"):
                return {
                    "success": False,
                    "error": nav.get("error") or "Bing navigation failed",
                }

            snapshot = nav.get("snapshot") or ""
            safe_limit = max(1, min(int(limit), _MAX_RESULTS))
            web_results = _parse_snapshot_results(
                snapshot, limit=safe_limit, allow_bare_urls=bool(snapshot)
            )
            if not web_results:
                # The navigate-embedded snapshot can be nonempty but
                # header-only (banner + filter nav, no organic result
                # blocks). Fall back to a full accessibility snapshot
                # and parse its listitem blocks.
                raw_snap = browser_snapshot(full=True, task_id=task_id)
                snap = _parse_browser_json(raw_snap)
                if not snap.get("success"):
                    return {
                        "success": False,
                        "error": snap.get("error") or "Bing snapshot failed",
                    }
                full_snapshot = snap.get("snapshot") or ""
                web_results = _parse_snapshot_results(
                    full_snapshot, limit=safe_limit, allow_bare_urls=False
                )
            logger.info(
                "Bing browser search '%s': %d results (limit %d)",
                query, len(web_results), limit,
            )
            return {"success": True, "data": {"web": web_results}}
        except Exception as exc:  # noqa: BLE001 — surface as a typed error
            logger.warning("Bing browser search failed: %s", exc)
            return {"success": False, "error": f"Bing browser search failed: {exc}"}
        finally:
            try:
                cleanup_browser(task_id)
            except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                logger.debug("cleanup_browser failed for %s: %s", task_id, exc)

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract readable content from up to 5 URLs via the browser.

        Each URL is processed sequentially in its own session (unique task
        id). Returns a list of ``{"url", "title", "content", "raw_content",
        "metadata"}`` entries preserving input order; per-URL failures
        become ``{"url": ..., "error": ...}`` entries and the batch
        continues. Every task is cleaned up, including on failure.
        """
        if not urls:
            return []
        safe_urls = [
            u for u in urls if isinstance(u, str) and u.strip()
        ][:_MAX_EXTRACT_URLS]

        results: List[Dict[str, Any]] = []
        for url in safe_urls:
            task_id = _new_task_id("bing-browser-extract")
            try:
                raw = browser_navigate(url, task_id=task_id)
                nav = _parse_browser_json(raw)
                if not nav.get("success"):
                    results.append(
                        {"url": url, "error": nav.get("error") or "Navigation failed"}
                    )
                    continue

                title = str(nav.get("title") or "")
                snapshot = nav.get("snapshot") or ""
                if not snapshot:
                    raw_snap = browser_snapshot(full=True, task_id=task_id)
                    snap = _parse_browser_json(raw_snap)
                    if not snap.get("success"):
                        results.append(
                            {"url": url, "error": snap.get("error") or "Snapshot failed"}
                        )
                        continue
                    snapshot = snap.get("snapshot") or ""

                content = _clean_snapshot_text(snapshot)
                results.append(
                    {
                        "url": url,
                        "title": title,
                        "content": content,
                        "raw_content": content,
                        "metadata": {"source": "bing-browser", "task_id": task_id},
                    }
                )
            except Exception as exc:  # noqa: BLE001 — per-URL failure, keep going
                logger.warning("Bing browser extract failed for %s: %s", url, exc)
                results.append({"url": url, "error": str(exc)})
            finally:
                try:
                    cleanup_browser(task_id)
                except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                    logger.debug("cleanup_browser failed for %s: %s", task_id, exc)
        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Bing (browser)",
            "badge": "no key · needs agent-browser CLI",
            "tag": (
                "Search + extract via the local agent-browser CLI — no API key. "
                "Explicit opt-in: set web.backend (or web.search_backend / "
                "web.extract_backend) to bing-browser."
            ),
            "env_vars": [],
        }
