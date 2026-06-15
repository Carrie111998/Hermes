"""call_kernel() — stdlib-only Streamable HTTP MCP client for the Fridai kernel.

Phase 2.5 (#278): the kernel's plain-HTTP /call shim was deleted with the SSE
transport (clean break). This speaks the real MCP Streamable HTTP protocol to
/mcp instead — initialize handshake, session header, tools/call — still with
nothing beyond the stdlib, so the plugin stays dependency-free in the Hermes
container.

Env:
  FRIDAY_MCP_URL  — kernel endpoint (default http://mcp:8643/mcp)
  FRIDAY_API_KEY  — shared Bearer key (the kernel is fail-closed; required)
"""

import json
import os
import threading
import urllib.error
import urllib.request

KERNEL_MCP_URL = os.getenv("FRIDAY_MCP_URL", "http://mcp:8643/mcp")
PROTOCOL_VERSION = "2025-06-18"

_lock = threading.Lock()
_session_id: str | None = None
_next_id = 0


def _headers(session_id: str | None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    key = os.getenv("FRIDAY_API_KEY", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


def _post(body: dict, session_id: str | None, timeout: float):
    """POST one JSON-RPC message. Returns (parsed-or-None, mcp-session-id)."""
    req = urllib.request.Request(
        KERNEL_MCP_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=_headers(session_id),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sid = resp.headers.get("mcp-session-id")
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read().decode("utf-8", "ignore")
    if "text/event-stream" in ctype:
        # Single-response stream: the JSON-RPC reply is the last data: line.
        data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        raw = data_lines[-1] if data_lines else ""
    return (json.loads(raw) if raw.strip() else None), sid


def _rpc_id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def _handshake(timeout: float) -> str:
    """initialize + notifications/initialized; returns the session id."""
    body, sid = _post(
        {
            "jsonrpc": "2.0",
            "id": _rpc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "fridai-memory", "version": "2.0"},
            },
        },
        session_id=None,
        timeout=timeout,
    )
    if body and body.get("error"):
        raise RuntimeError(f"kernel MCP initialize error: {body['error']}")
    _post(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        session_id=sid,
        timeout=timeout,
    )
    return sid or ""


def _tools_call(tool: str, args: dict, session_id: str, timeout: float):
    return _post(
        {
            "jsonrpc": "2.0",
            "id": _rpc_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        session_id=session_id,
        timeout=timeout,
    )[0]


def call_kernel(tool: str, timeout: float = 30.0, **args):
    """Call a Fridai kernel tool over MCP Streamable HTTP. Returns the tool's
    parsed result (same contract the old /call shim had).

    Raises RuntimeError on any kernel-side error.
    """
    global _session_id
    try:
        with _lock:
            if _session_id is None:
                _session_id = _handshake(timeout)
            try:
                body = _tools_call(tool, args, _session_id, timeout)
            except urllib.error.HTTPError as e:
                if e.code in (400, 404):
                    # Session expired/unknown — re-handshake once and retry.
                    _session_id = _handshake(timeout)
                    body = _tools_call(tool, args, _session_id, timeout)
                else:
                    raise
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"kernel {tool} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}"
        )
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"kernel {tool} call failed: {e}")

    if not body:
        raise RuntimeError(f"kernel {tool}: empty MCP response")
    if body.get("error"):
        raise RuntimeError(f"kernel {tool} error: {body['error']}")

    result = body.get("result") or {}
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    if result.get("isError"):
        raise RuntimeError(f"kernel {tool} error: {text}")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
