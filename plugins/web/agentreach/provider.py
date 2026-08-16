"""Agent Reach (free tier) — plugin form.

Subclasses :class:`agent.web_search_provider.WebSearchProvider` (the
plugin-facing ABC). Free web search via multi-backend fallback chain
(Exa via mcporter → GitHub CLI → Jina Reader) and content extraction
via Jina Reader (always free, no API key).

Config keys this provider responds to::

    web:
      search_backend: "agentreach"     # explicit per-capability
      extract_backend: "agentreach"    # explicit per-capability
      backend: "agentreach"            # shared fallback for both

No environment variables required — Jina Reader and GitHub CLI are
zero-config. Exa search via mcporter needs ``mcporter`` installed
(optional, used only if available).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List, Optional

import httpx

from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

_JINA_ENDPOINT = "https://r.jina.ai/"
_UA = "Mozilla/5.0 (compatible; hermes-agent/2.0; +https://github.com/NousResearch/hermes-agent)"
_MAX_JINA_BYTES = 5 * 1024 * 1024


class AgentReachWebSearchProvider(WebSearchProvider):
    """Free web search + extraction via Agent Reach.

    Search backends (fallback chain):
        1. Exa via mcporter (if installed)
        2. GitHub search via gh CLI (code/repo search)
        3. Jina Reader (broad web search fallback)

    Extract backend:
        - Jina Reader (always free, zero-config)
    """

    @property
    def name(self) -> str:
        return "agentreach"

    @property
    def display_name(self) -> str:
        return "Agent Reach (Free)"

    def is_available(self) -> bool:
        """Jina Reader is always available (public HTTP endpoint)."""
        return True

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def _mcporter_available(self) -> bool:
        return shutil.which("mcporter") is not None

    def _gh_available(self) -> bool:
        return shutil.which("gh") is not None

    def _exa_via_mcporter(self, query: str, limit: int = 5) -> Optional[Dict[str, Any]]:
        """Try Exa search via mcporter (free tier via Exa MCP)."""
        if not self._mcporter_available():
            return None
        try:
            result = subprocess.run(
                [
                    "mcporter", "call", "exa.web_search_exa",
                    f"query={query}", f"numResults={limit}",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            # mcporter returns JSON with results array
            results = data if isinstance(data, list) else data.get("results", data.get("data", []))
            web_results = []
            for i, r in enumerate(results[:limit]):
                if isinstance(r, dict):
                    web_results.append({
                        "title": str(r.get("title", r.get("name", ""))),
                        "url": str(r.get("url", r.get("link", ""))),
                        "description": str(r.get("description", r.get("snippet", r.get("text", "")))),
                        "position": i + 1,
                    })
            if web_results:
                return {"success": True, "data": {"web": web_results}}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            logger.debug("Exa via mcporter failed: %s", exc)
        return None

    def _github_search(self, query: str, limit: int = 5) -> Optional[Dict[str, Any]]:
        """Try GitHub search via gh CLI (free, no key needed for public repos)."""
        if not self._gh_available():
            return None
        try:
            result = subprocess.run(
                ["gh", "search", "repos", query, "--sort", "stars",
                 "--limit", str(limit), "--json", "fullName,description,url,stargazersCount"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return None
            repos = json.loads(result.stdout) if result.stdout.strip() else []
            web_results = []
            for i, r in enumerate(repos[:limit]):
                web_results.append({
                    "title": str(r.get("fullName", "")),
                    "url": str(r.get("url", "")),
                    "description": str(r.get("description", f"\u2b50 {r.get('stargazersCount', 0)} stars")),
                    "position": i + 1,
                })
            if web_results:
                return {"success": True, "data": {"web": web_results}}
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            logger.debug("GitHub search failed: %s", exc)
        return None

    def _jina_search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Fallback: use Jina Reader to fetch a search results page.

        Jina doesn't have a native search endpoint, so we use it to read
        a search engine results page (DuckDuckGo HTML) and extract results.
        """
        try:
            # Use DDG HTML search as the source, read via Jina
            ddg_url = f"https://html.duckduckgo.com/html/?q={query}"
            resp = httpx.get(
                f"{_JINA_ENDPOINT}{ddg_url}",
                headers={"User-Agent": _UA, "Accept": "text/plain"},
                timeout=30,
                follow_redirects=True,
            )
            resp.raise_for_status()
            text = resp.text[:20000]  # limit parsing

            # Parse Jina Reader's markdown output
            # Format: ## [Title](URL) followed by snippet
            import re
            results = []
            lines = text.split("\n")
            i = 0
            while i < len(lines) and len(results) < limit:
                line = lines[i].strip()
                # Match: ## [Title](URL)
                match = re.match(r'^## \[(.+?)\]\((.+?)\)$', line)
                if match:
                    title = match.group(1)
                    url = match.group(2)
                    # Skip DDG self-links
                    if "duckduckgo.com" in url and "/html/" in url:
                        i += 1
                        continue
                    # Skip image links
                    if title.startswith("Image"):
                        i += 1
                        continue
                    # Next non-empty line is the snippet
                    snippet = ""
                    j = i + 1
                    while j < len(lines):
                        next_line = lines[j].strip()
                        if next_line and not next_line.startswith("[") and not next_line.startswith("!"):
                            snippet = next_line
                            break
                        j += 1
                    results.append({
                        "title": title,
                        "url": url,
                        "description": snippet,
                        "position": len(results) + 1,
                    })
                i += 1

            if results:
                return {"success": True, "data": {"web": results}}
            return {"success": False, "error": "Jina search returned no parseable results"}

        except Exception as exc:
            logger.warning("Jina search failed: %s", exc)
            return {"success": False, "error": f"Jina search failed: {exc}"}

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        """Execute a free web search via fallback chain.

        Returns ``{"success": True, "data": {"web": [results]}}`` on success,
        or ``{"success": False, "error": str}`` on failure.
        """
        # Try Exa via mcporter first
        result = self._exa_via_mcporter(query, limit)
        if result:
            logger.info("Agent Reach search '%s': %d results (via Exa/mcporter)", query, len(result["data"]["web"]))
            return result

        # Try GitHub search for code/repo queries
        if any(kw in query.lower() for kw in ["repo", "library", "framework", "code", "github", "package", "npm", "pypi"]):
            result = self._github_search(query, limit)
            if result:
                logger.info("Agent Reach search '%s': %d results (via GitHub)", query, len(result["data"]["web"]))
                return result

        # Fallback: Jina Reader + DDG
        result = self._jina_search(query, limit)
        if result.get("success"):
            logger.info("Agent Reach search '%s': %d results (via Jina Reader)", query, len(result["data"]["web"]))
            return result

        return {"success": False, "error": "All Agent Reach search backends failed"}

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        """Extract content from URLs via Jina Reader (always free).

        Returns list of result dicts matching the WebSearchProvider contract.
        """
        results = []
        for url in urls:
            try:
                # Validate URL
                if not url.startswith(("http://", "https://")):
                    results.append({
                        "url": url, "title": "", "content": "",
                        "raw_content": "", "error": "Invalid URL (must be http/https)",
                    })
                    continue

                resp = httpx.get(
                    f"{_JINA_ENDPOINT}{url}",
                    headers={"User-Agent": _UA, "Accept": "text/plain"},
                    timeout=30,
                    follow_redirects=True,
                )
                resp.raise_for_status()
                body = resp.text[:_MAX_JINA_BYTES]

                # Extract title from markdown (first heading)
                title = ""
                for line in body.split("\n"):
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break

                results.append({
                    "url": url,
                    "title": title,
                    "content": body,
                    "raw_content": body,
                    "metadata": {"source": "jina-reader", "bytes": len(body)},
                })

            except httpx.HTTPStatusError as exc:
                results.append({
                    "url": url, "title": "", "content": "", "raw_content": "",
                    "error": f"HTTP {exc.response.status_code}",
                })
            except httpx.RequestError as exc:
                results.append({
                    "url": url, "title": "", "content": "", "raw_content": "",
                    "error": f"Request failed: {exc}",
                })
            except Exception as exc:
                results.append({
                    "url": url, "title": "", "content": "", "raw_content": "",
                    "error": str(exc),
                })

        return results

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Agent Reach (Free)",
            "badge": "free",
            "tag": "Zero-cost web search + extraction. No API keys required.",
            "env_vars": [],
        }
