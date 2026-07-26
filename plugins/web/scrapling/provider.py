"""Scrapling web content extraction — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider`. Scrapling is
a self-hosted scraper (no API key, no third-party reader): pages are fetched
directly from this machine.

Two-tier fetch strategy per URL:

1. **Fast path** — :class:`scrapling.AsyncFetcher` issues an HTTP request with
   browser TLS-fingerprint impersonation. No browser process; covers the
   large majority of pages.
2. **Stealth fallback** — on HTTP >=400, a fetch error, or an obvious
   anti-bot interstitial, retry with :class:`scrapling.StealthyFetcher`
   (real browser + Cloudflare Turnstile solving). Requires the stealth
   browser, installed via ``scrapling install``; when it's missing we return
   a precise "run scrapling install" error rather than crashing.

HTML is converted to Markdown with ``markdownify`` (same library Scrapling's
own AI/shell extras use), matching the clean-markdown ``content`` the other
extract providers return.

Scrapling is search-only-NO: it fetches/parses pages, it is not a SERP
engine, so ``supports_search()`` is False — pair it with any search backend
(exa / brave-free / ddgs / searxng / …).

Config keys::

    web:
      extract_backend: "scrapling"   # or backend: "scrapling"

No env vars. Lazy dep: ``search.scrapling`` (see :mod:`tools.lazy_deps`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Per-URL wall-clock cap for the fast HTTP path. Scrapling's own ``timeout``
# bounds the transfer; this is a belt-and-suspenders overall cap so a stalled
# fetch can't hang the shared agent loop.
_FAST_TIMEOUT_SECS = 30
# The stealth browser path (Cloudflare solving) needs a longer budget —
# Scrapling docs require >=60s when solve_cloudflare is on.
_STEALTH_TIMEOUT_SECS = 90


def _package_installed() -> bool:
    """True when scrapling + its fetchers extra (curl_cffi) are importable.

    Uses ``find_spec`` (cheap, no import side effects) so this stays safe to
    call at tool-registration time and on every ``hermes tools`` paint —
    importing ``scrapling.fetchers`` would pull in playwright at module load.
    """
    try:
        return (
            importlib.util.find_spec("scrapling") is not None
            and importlib.util.find_spec("curl_cffi") is not None
        )
    except (ImportError, ValueError):
        return False


def _ensure_installed() -> None:
    """Lazy-install the scrapling deps on first extract. Best-effort."""
    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.scrapling", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        raise ImportError(str(exc))


def _to_markdown(page: Any) -> str:
    """Convert a Scrapling page to clean Markdown.

    Strips script/style/noscript/svg noise first (matching Scrapling's own
    shell converter), then ``markdownify``. Falls back to plain text when
    markdownify isn't installed.
    """
    try:
        for el in page.css("script, style, noscript, svg"):
            try:
                el._root.drop_tree()
            except Exception:  # noqa: BLE001 — best-effort noise strip
                pass
    except Exception:  # noqa: BLE001 — selector optional; degrade to raw html
        pass
    html = page.html_content
    try:
        from markdownify import markdownify

        return markdownify(str(html)).strip()
    except ImportError:
        return page.get_all_text(strip=True) or ""


def _looks_challenged(page: Any) -> bool:
    """Heuristic: does this page look like an anti-bot interstitial?

    Cheap substring probe on a short prefix so we don't scan megabytes.
    """
    try:
        head = str(page.html_content)[:4000].lower()
    except Exception:  # noqa: BLE001
        return False
    return (
        "just a moment" in head
        or "cf-challenge" in head
        or "checking your browser" in head
        or "enable javascript and cookies" in head
    )


class ScraplingWebSearchProvider(WebSearchProvider):
    """Scrapling extract provider (keyless, self-hosted). Extract-only."""

    @property
    def name(self) -> str:
        return "scrapling"

    @property
    def display_name(self) -> str:
        return "Scrapling"

    def is_available(self) -> bool:
        """True when the scrapling package + fetchers extra are importable."""
        return _package_installed()

    def supports_search(self) -> bool:
        return False

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Fetch + convert each URL to markdown. Returns the standard list.

        The dispatcher (:func:`tools.web_tools.web_extract_tool`) has already
        blocked secret-bearing URLs and SSRF-unsafe hosts before calling us;
        we still run the per-host website-policy gate and re-check SSRF on the
        post-redirect final URL (a fetch can be redirected to a private host).
        """
        try:
            _ensure_installed()
        except ImportError as exc:
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "error": (
                        f"Scrapling is not installed: {exc}. Install with "
                        "`pip install 'scrapling[fetchers]' markdownify` "
                        "then `scrapling install`."
                    ),
                }
                for u in urls
            ]

        from tools.interrupt import is_interrupted as _is_interrupted
        from tools.url_safety import is_safe_url
        from tools.website_policy import check_website_access

        results: List[Dict[str, Any]] = []
        for url in urls:
            if _is_interrupted():
                results.append({"url": url, "title": "", "content": "", "error": "Interrupted"})
                continue

            blocked = check_website_access(url)
            if blocked:
                logger.info("Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"])
                results.append(
                    {
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": blocked["message"],
                        "blocked_by_policy": {
                            "host": blocked["host"],
                            "rule": blocked["rule"],
                            "source": blocked["source"],
                        },
                    }
                )
                continue

            results.append(await self._extract_one(url, is_safe_url))
        return results

    async def _extract_one(self, url: str, is_safe_url) -> Dict[str, Any]:
        """Fetch a single URL: fast path, then stealth fallback."""
        page = None
        fast_err: str | None = None
        try:
            from scrapling.fetchers import AsyncFetcher

            page = await asyncio.wait_for(
                AsyncFetcher.get(url, stealthy_headers=True, timeout=_FAST_TIMEOUT_SECS, retries=2),
                timeout=_FAST_TIMEOUT_SECS + 5,
            )
        except asyncio.TimeoutError:
            fast_err = f"fast fetch timed out after {_FAST_TIMEOUT_SECS}s"
        except Exception as exc:  # noqa: BLE001 — curl_cffi/network errors
            fast_err = f"fast fetch failed: {exc}"

        needs_stealth = (
            page is None
            or getattr(page, "status", 0) >= 400
            or _looks_challenged(page)
        )
        if needs_stealth:
            stealth = await self._fetch_stealth(url)
            if isinstance(stealth, str):
                # Stealth unavailable/failed — return the more useful error.
                if page is not None and getattr(page, "status", 0) < 400:
                    pass  # fast page is usable after all; fall through to convert
                else:
                    return {"url": url, "title": "", "content": "", "error": fast_err or stealth}
            else:
                page = stealth

        # Re-check SSRF on the final (possibly redirected) URL.
        final_url = getattr(page, "url", url) or url
        if not is_safe_url(str(final_url)):
            logger.info("Blocked redirected web_extract for unsafe final URL: %s", final_url)
            return {
                "url": str(final_url),
                "title": "",
                "content": "",
                "error": "Blocked: redirected to a private or internal network address",
            }

        title = ""
        try:
            title = (page.css("title::text").get() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        content = _to_markdown(page)
        return {
            "url": str(final_url),
            "title": title,
            "content": content,
            "raw_content": content,
            "metadata": {"status": getattr(page, "status", None)},
        }

    async def _fetch_stealth(self, url: str):
        """Stealth-browser fetch. Returns a page, or an error string.

        A missing stealth browser (never ran ``scrapling install``) surfaces
        as an actionable error string rather than an exception.
        """
        try:
            from scrapling.fetchers import StealthyFetcher

            return await asyncio.wait_for(
                # StealthyFetcher.timeout is in milliseconds; solve_cloudflare
                # requires >=60s. Outer wait_for is the hard ceiling.
                StealthyFetcher.async_fetch(
                    url,
                    headless=True,
                    solve_cloudflare=True,
                    network_idle=True,
                    timeout=(_STEALTH_TIMEOUT_SECS - 10) * 1000,
                ),
                timeout=_STEALTH_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            return f"stealth fetch timed out after {_STEALTH_TIMEOUT_SECS}s"
        except Exception as exc:  # noqa: BLE001 — browser-missing, launch errors
            msg = str(exc).lower()
            if "executable" in msg or "install" in msg or "browser" in msg or "camoufox" in msg:
                return (
                    "stealth browser not installed — run `scrapling install` to "
                    "enable anti-bot/Cloudflare fallback"
                )
            return f"stealth fetch failed: {exc}"

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Scrapling",
            "badge": "free · no key · self-hosted · extract only",
            "tag": "Self-hosted scraper — TLS impersonation + stealth-browser fallback (pair with any search provider)",
            "env_vars": [],
            # Installs scrapling[fetchers] + markdownify and downloads the
            # stealth browser (`scrapling install`) on first selection.
            "post_setup": "scrapling",
        }
