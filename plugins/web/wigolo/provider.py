"""Wigolo search — plugin form (via the local `wigolo` CLI).

Subclasses the plugin-facing :class:`agent.web_search_provider.WebSearchProvider`.

Why this provider exists (2026-08-01): the Firecrawl backend needs either an
API key or the Nous subscriber gateway; Wigolo runs entirely on this machine —
multi-engine search fused and reranked locally, no key, no per-query cost.
Measured on a 20-query finance exam (zh+en) before adoption: median 1.6s,
15/20 on-point. Known failure mode: when its engine pool degrades to
bing-only the quality collapses (homepage/junk hits) — the pool state is the
thing to check first when results look off (`npx wigolo doctor`).

Search-only by design, like the ddgs provider: Wigolo's own fetch/crawl exist
but extraction here stays with whatever extract provider the user paired —
one job per provider, and the search leg is the one we benchmarked.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

# Hard wall-clock cap for one search. Wigolo's own engine timeouts usually
# return in ~2s; the cap only exists so a wedged browser engine cannot block
# the (single, shared) agent loop. subprocess.run(timeout=...) kills the child
# on expiry — unlike a thread, nothing keeps running behind our back.
_SEARCH_TIMEOUT_SECS = 45

# Results ceiling mirrors the CLI's own --max-results maximum.
_MAX_RESULTS_CAP = 20


def _wigolo_argv() -> list[str] | None:
    """Resolve how to invoke wigolo, or None when it is not installed.

    Prefers a real `wigolo` binary on PATH (global npm install); falls back to
    `npx -y wigolo`, which is how `npx wigolo init` leaves the package cached.
    Must stay cheap and network-free — called at tool-registration time.
    """
    direct = shutil.which("wigolo")
    if direct:
        return [direct]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "wigolo"]
    return None


def _initialized() -> bool:
    """True once `npx wigolo init` has provisioned the runtime (~/.wigolo).

    The data dir is created by init (browser engine, embeddings, reranker);
    without it every search would fail slowly. Checking the dir keeps
    `is_available()` honest without spawning Node at registration time.
    """
    return (Path.home() / ".wigolo").is_dir()


def _run_wigolo_search(query: str, safe_limit: int) -> Dict[str, Any]:
    """Run one CLI search and return the parsed JSON document.

    Module-level (not a closure) so tests can patch it directly. The CLI
    prints structured logs before the JSON payload; the document is recovered
    from the first ``{`` — measured shape (wigolo 2026-08-01):
    ``{"results": [{"title", "url", "snippet", ...}], "engines_used": [...]}``.
    """
    argv = _wigolo_argv()
    if argv is None:  # pragma: no cover — guarded by is_available()
        raise RuntimeError("wigolo CLI not resolvable")
    proc = subprocess.run(
        [*argv, "search", query, f"--max-results={safe_limit}", "--no-content", "--json"],
        capture_output=True,
        text=True,
        timeout=_SEARCH_TIMEOUT_SECS,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
        raise RuntimeError(f"wigolo exited {proc.returncode}: {tail}")
    raw = proc.stdout or ""
    start = raw.find("{")
    if start < 0:
        raise ValueError("wigolo returned no JSON document")
    return json.loads(raw[start:])


class WigoloWebSearchProvider(WebSearchProvider):
    """Local-first multi-engine search. No key; requires one-time init."""

    @property
    def name(self) -> str:
        return "wigolo"

    @property
    def display_name(self) -> str:
        return "Wigolo (local)"

    def is_available(self) -> bool:
        """CLI resolvable AND the runtime has been provisioned.

        No network I/O and no Node process spawn — this runs at
        tool-registration time and on every ``hermes tools`` paint.
        """
        return _wigolo_argv() is not None and _initialized()

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a Wigolo search and return normalized results."""
        if _wigolo_argv() is None:
            return {
                "success": False,
                "error": "wigolo CLI not found — install Node and run `npx wigolo init`",
            }
        if not _initialized():
            return {
                "success": False,
                "error": (
                    "wigolo runtime not provisioned — run `npx wigolo init` once "
                    "(~1.5GB: browser engine + on-device models)"
                ),
            }
        safe_limit = min(max(1, int(limit)), _MAX_RESULTS_CAP)
        try:
            doc = _run_wigolo_search(query, safe_limit)
        except subprocess.TimeoutExpired:
            logger.warning(
                "wigolo search timed out after %ds for query: %r",
                _SEARCH_TIMEOUT_SECS, query,
            )
            return {
                "success": False,
                "error": (
                    f"wigolo search timed out after {_SEARCH_TIMEOUT_SECS}s — "
                    "check `npx wigolo doctor` (browser engine may be wedged)"
                ),
            }
        except Exception as exc:  # noqa: BLE001 — CLI/JSON errors surface as data
            logger.warning("wigolo search error: %s", exc)
            return {"success": False, "error": f"wigolo search failed: {exc}"}

        web_results: List[Dict[str, Any]] = []
        for i, hit in enumerate(doc.get("results") or []):
            if i >= safe_limit:
                break
            web_results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": str(hit.get("url", "")),
                    "description": str(hit.get("snippet", "")),
                    "position": i + 1,
                }
            )
        engines = doc.get("engines_used") or []
        # The one measured failure mode: pool degraded to bing-only produced
        # junk on 2/20 exam questions. Not an error — but say it out loud so a
        # bad answer is diagnosable from the log line alone.
        if engines == ["bing"]:
            logger.warning(
                "wigolo engine pool degraded to bing-only for %r — "
                "result quality may suffer (see `npx wigolo doctor`)", query,
            )
        logger.info(
            "wigolo search '%s': %d results (limit %d, engines %s)",
            query, len(web_results), limit, ",".join(map(str, engines)) or "?",
        )
        return {"success": True, "data": {"web": web_results}}

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Wigolo (local)",
            "badge": "free · local · no key · search only",
            "tag": (
                "Local multi-engine search with on-device rerank — run "
                "`npx wigolo init` once (~1.5GB), pair with any extract provider"
            ),
            "env_vars": [],
        }
