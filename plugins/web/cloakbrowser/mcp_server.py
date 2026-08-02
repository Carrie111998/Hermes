"""CloakBrowser MCP server module.

Exposes CloakBrowser stealth browser capability (web_search, web_extract, navigate)
as a Model Context Protocol (MCP) server over stdio.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List, Union

from plugins.web.cloakbrowser.session import (
    _ensure_cloakbrowser,
    extract_urls_sync,
    search_duckduckgo_sync,
)

logger = logging.getLogger("hermes.plugins.web.cloakbrowser.mcp_server")


def search_tool(query: str, limit: int = 5) -> Dict[str, Any]:
    """Perform stealth web search via CloakBrowser (DuckDuckGo HTML)."""
    try:
        _ensure_cloakbrowser()
    except ImportError as exc:
        return {"success": False, "error": str(exc)}
    try:
        results = search_duckduckgo_sync(query, limit=max(1, limit))
        return {"success": True, "query": query, "web": results}
    except Exception as exc:  # noqa: BLE001
        logger.warning("CloakBrowser MCP search failed: %s", exc)
        return {"success": False, "error": f"Search failed: {exc}"}


def extract_tool(urls: Union[List[str], str], format: str = "text") -> List[Dict[str, Any]]:
    """Extract content from web URLs using CloakBrowser stealth browser."""
    if isinstance(urls, str):
        url_list = [urls]
    else:
        url_list = list(urls)
    try:
        _ensure_cloakbrowser()
    except ImportError as exc:
        return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in url_list]
    try:
        return extract_urls_sync(url_list, format=format)
    except Exception as exc:  # noqa: BLE001
        logger.warning("CloakBrowser MCP extract failed: %s", exc)
        return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in url_list]


def navigate_tool(url: str, wait_for: str = "domcontentloaded") -> Dict[str, Any]:
    """Navigate to a single URL via CloakBrowser stealth browser and return text content."""
    res = extract_tool([url], format="text")
    if res and len(res) > 0:
        return res[0]
    return {"url": url, "title": "", "content": "", "error": "No response returned"}


def run_stdio_jsonrpc() -> None:
    """Fallback JSON-RPC stdio loop for MCP clients when mcp SDK is unavailable."""
    logger.info("Starting CloakBrowser MCP server in JSON-RPC stdio fallback mode...")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "cloakbrowser-mcp", "version": "1.0.0"},
                    },
                }
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [
                            {
                                "name": "cloakbrowser_search",
                                "description": "Search the web via CloakBrowser stealth Chromium (DuckDuckGo).",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "query": {"type": "string", "description": "Search query"},
                                        "limit": {"type": "integer", "default": 5, "description": "Number of results"},
                                    },
                                    "required": ["query"],
                                },
                            },
                            {
                                "name": "cloakbrowser_extract",
                                "description": "Extract content from web pages using CloakBrowser stealth browser.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "urls": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "List of URLs to extract",
                                        },
                                        "format": {"type": "string", "default": "text", "description": "'text' or 'html'"},
                                    },
                                    "required": ["urls"],
                                },
                            },
                            {
                                "name": "cloakbrowser_navigate",
                                "description": "Navigate to a URL using CloakBrowser stealth browser and return text.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "url": {"type": "string", "description": "Target URL to navigate to"},
                                    },
                                    "required": ["url"],
                                },
                            },
                        ]
                    },
                }
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                if tool_name == "cloakbrowser_search":
                    data = search_tool(arguments.get("query", ""), arguments.get("limit", 5))
                    out_text = json.dumps(data, ensure_ascii=False, indent=2)
                elif tool_name == "cloakbrowser_extract":
                    data = extract_tool(arguments.get("urls", []), arguments.get("format", "text"))
                    out_text = json.dumps(data, ensure_ascii=False, indent=2)
                elif tool_name == "cloakbrowser_navigate":
                    data = navigate_tool(arguments.get("url", ""))
                    out_text = json.dumps(data, ensure_ascii=False, indent=2)
                else:
                    out_text = json.dumps({"error": f"Unknown tool: {tool_name}"})

                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": out_text,
                            }
                        ]
                    },
                }
            else:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }

            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            logger.error("Error handling MCP request: %s", exc)


def main() -> None:
    """Run the CloakBrowser MCP server."""
    logging.basicConfig(level=logging.INFO)

    try:
        from mcp.server.fastmcp import FastMCP

        mcp = FastMCP("cloakbrowser")

        @mcp.tool(name="cloakbrowser_search", description="Search the web via CloakBrowser stealth Chromium.")
        def _mcp_search(query: str, limit: int = 5) -> str:
            res = search_tool(query, limit)
            return json.dumps(res, ensure_ascii=False, indent=2)

        @mcp.tool(name="cloakbrowser_extract", description="Extract web pages content using CloakBrowser.")
        def _mcp_extract(urls: List[str], format: str = "text") -> str:
            res = extract_tool(urls, format=format)
            return json.dumps(res, ensure_ascii=False, indent=2)

        @mcp.tool(name="cloakbrowser_navigate", description="Navigate to a single URL via CloakBrowser.")
        def _mcp_navigate(url: str) -> str:
            res = navigate_tool(url)
            return json.dumps(res, ensure_ascii=False, indent=2)

        mcp.run(transport="stdio")
    except ImportError:
        run_stdio_jsonrpc()


if __name__ == "__main__":
    main()
