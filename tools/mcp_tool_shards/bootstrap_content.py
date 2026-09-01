"""Executable source shard for the legacy MCP tool seam.

The source is compiled with the original module namespace so public
imports and monkeypatch targets remain tools.mcp_tool-compatible.
"""
import linecache
from pathlib import Path

_SOURCE = r'''

#!/usr/bin/env python3
"""
MCP (Model Context Protocol) Client Support

Connects to external MCP servers via stdio, HTTP/StreamableHTTP, or SSE
transport, discovers their tools, and registers them into the hermes-agent
tool registry so the agent can call them like any built-in tool.

Configuration is read from ~/.hermes/config.yaml under the ``mcp_servers`` key.
The ``mcp`` Python package is optional -- if not installed, this module is a
no-op and logs a debug message.

Example config::

    mcp_servers:
      filesystem:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        env: {}
        timeout: 120         # per-tool-call timeout in seconds (default: 300)
        connect_timeout: 60  # initial connection timeout (default: 60)
        keepalive_interval: 10  # liveness ping cadence in seconds (default:
                                # 180). Set below the server's session TTL for
                                # servers that GC idle sessions quickly (e.g.
                                # Unreal Engine editor MCP, ~15s). Floored at 5s.
        idle_timeout_seconds: 3600      # optional stdio recycle after idle
        max_lifetime_seconds: 86400     # optional stdio recycle after age
        # The recycle settings may also live under lifecycle: {...}.
        # Use 0 to disable either recycle limit.
      github:
        command: "npx"
        args: ["-y", "@modelcontextprotocol/server-github"]
        env:
          GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_..."
        supports_parallel_tool_calls: true  # tools from this server may run concurrently
      remote_api:
        url: "https://my-mcp-server.example.com/mcp"
        headers:
          Authorization: "Bearer sk-..."
        identity_header:       # optional per-user identity header attached
          name: "X-User-Id"    # to this server's HTTP/SSE requests
          value_from: "static" # "static" (default) or "profile"
          value: "alice"       # required for static; profile mode uses the
                               # active Hermes profile name
        timeout: 180
        skip_preflight: true  # bypass the content-type probe for a valid
                              # Streamable HTTP endpoint that answers HEAD/GET
                              # with a non-MCP content type but serves real
                              # MCP over POST. Default: false.
      searxng:
        url: "http://localhost:8000/sse"
        transport: sse       # use SSE transport instead of Streamable HTTP
        timeout: 180
        connect_timeout: 10
        command: "npx"
        args: ["-y", "analysis-server"]
        sampling:                    # server-initiated LLM requests
          enabled: true              # default: true
          model: "gemini-3-flash"    # override model (optional)
          max_tokens_cap: 4096       # max tokens per request
          timeout: 30                # LLM call timeout (seconds)
          max_rpm: 10                # max requests per minute
          allowed_models: []         # model whitelist (empty = all)
          max_tool_rounds: 5         # tool loop limit (0 = disable)
          log_level: "info"          # audit verbosity

Features:
    - Stdio transport (command + args) and HTTP/StreamableHTTP transport (url)
    - SSE transport (transport: sse) for MCP servers using the SSE protocol
    - Automatic reconnection with exponential backoff (up to 5 retries)
    - Environment variable filtering for stdio subprocesses (security)
    - Credential stripping in error messages returned to the LLM
    - Configurable per-server timeouts for tool calls and connections
    - Thread-safe architecture with dedicated background event loop
    - Sampling support: MCP servers can request LLM completions via
      sampling/createMessage (text and tool-use responses)
    - Parallel tool call opt-in: per-server ``supports_parallel_tool_calls``
      flag allows concurrent execution of tools from the same server

Architecture:
    A dedicated background event loop (_mcp_loop) runs in a daemon thread.
    Each MCP server runs as a long-lived asyncio Task on this loop, keeping
    its transport context alive. Tool call coroutines are scheduled onto the
    loop via ``run_coroutine_threadsafe()``.

    On shutdown, each server Task is signalled to exit its ``async with``
    block, ensuring the anyio cancel-scope cleanup happens in the *same*
    Task that opened the connection (required by anyio).

Thread safety:
    _servers and _mcp_loop/_mcp_thread are accessed from both the MCP
    background thread and caller threads.  All mutations are protected by
    _lock so the code is safe regardless of GIL presence (e.g. Python 3.13+
    free-threading).
"""

import asyncio
import contextvars
import concurrent.futures
import errno
import fnmatch
import inspect
import json
import logging
import math
import os
import random
import re
import shutil
import sys
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Callable
from datetime import datetime
from typing import Any, Coroutine, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from tools.registry import tool_error
from tools.ansi_strip import strip_unicode_tags

logger = logging.getLogger(__name__)


# Hard allocation ceiling for a single MCP text payload (chars). This is the
# FIRST line of defense against a buggy or malicious MCP server returning
# multi-megabyte text: without it the full payload is allocated, JSON-encoded
# and handed downstream before the budget/spillover layer ever sees it
# (#56059). It deliberately sits far ABOVE the budget layer's 50K MCP
# spillover threshold (tools/budget_config.py) so ordinary large results
# reach spillover INTACT — spilled to disk in full, preview in context —
# while only pathological multi-MB floods are lossy-truncated here.
#
# Distilled from #56060 (Stoltemberg), #56072 (AlexFucuson9) and #56511
# (Tranquil-Flow), which capped at get_max_bytes() (50K) — correct
# protection, but at that level it would truncate before spillover could
# preserve the data. The 40% head / 60% tail split is #56511's shape.
_MCP_HARD_RESULT_CAP_CHARS = 2_000_000


def _truncate_mcp_text_result(text: str, max_chars: int = _MCP_HARD_RESULT_CAP_CHARS) -> str:
    """Bound pathological MCP text before it propagates (#56059).

    Results at or under ``max_chars`` pass through unchanged; oversized text
    keeps a 40% head / 60% tail split with an omission notice in between.
    """
    if len(text) <= max_chars:
        return text
    head_chars = int(max_chars * 0.4)
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars
    return (
        text[:head_chars]
        + f"\n\n... [MCP RESULT TRUNCATED - {omitted:,} chars omitted "
          f"out of {len(text):,} total] ...\n\n"
        + text[-tail_chars:]
    )

# Upper bound for the OSV malware preflight during stdio MCP startup. The
# check makes a blocking urllib HTTPS call whose own timeout can fail to
# interrupt a stalled SSL handshake, which froze the asyncio event loop and
# blew past the gateway's 15s startup budget (#29184). We run it off the loop
# AND bound it here; the check is fail-open, so a timeout lets startup proceed.
# Set just ABOVE osv_check._TIMEOUT (10s) so the inner socket timeout fires
# first in the normal case; this outer bound only bites when a stalled SSL
# handshake defeats the inner timeout (the #29184 failure mode).
_OSV_MALWARE_CHECK_TIMEOUT_S = 12.0


# ---------------------------------------------------------------------------
# Stdio subprocess stderr redirection
# ---------------------------------------------------------------------------
#
# The MCP SDK's ``stdio_client(server, errlog=sys.stderr)`` defaults the
# subprocess stderr stream to the parent process's real stderr, i.e. the
# user's TTY.  That means any MCP server we spawn at startup (FastMCP
# banners, slack-mcp-server JSON startup logs, etc.) writes directly onto
# the terminal while prompt_toolkit / Rich is rendering the TUI — which
# corrupts the display and can hang the session.
#
# Instead we redirect every stdio MCP subprocess's stderr into a shared
# per-profile log file (~/.hermes/logs/mcp-stderr.log), tagged with the
# server name so individual servers remain debuggable.
#
# Fallback is os.devnull if opening the log file fails for any reason.

_mcp_stderr_log_fh: Optional[Any] = None
_mcp_stderr_log_lock = threading.Lock()


def _get_mcp_stderr_log() -> Any:
    """Return a shared append-mode file handle for MCP subprocess stderr.

    Opened once per process and reused for every stdio server.  Must have a
    real OS-level file descriptor (``fileno()``) because asyncio's subprocess
    machinery wires the child's stderr directly to that fd.  Falls back to
    ``/dev/null`` if opening the log file fails.
    """
    global _mcp_stderr_log_fh
    with _mcp_stderr_log_lock:
        if _mcp_stderr_log_fh is not None:
            return _mcp_stderr_log_fh
        try:
            from hermes_constants import get_hermes_home
            log_dir = get_hermes_home() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "mcp-stderr.log"
            # Line-buffered so server output lands on disk promptly; errors=
            # "replace" tolerates garbled binary output from misbehaving
            # servers.
            fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            # Sanity-check: confirm a real fd is available before we commit.
            fh.fileno()
            _mcp_stderr_log_fh = fh
        except Exception as exc:  # pragma: no cover — best-effort fallback
            logger.debug("Failed to open MCP stderr log, using devnull: %s", exc)
            try:
                _mcp_stderr_log_fh = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                # Last resort: the real stderr.  Not ideal for TUI users but
                # it matches pre-fix behavior.
                _mcp_stderr_log_fh = sys.stderr
        return _mcp_stderr_log_fh


def _write_stderr_log_header(server_name: str) -> None:
    """Write a human-readable session marker before launching a server.

    Gives operators a way to find each server's output in the shared
    ``mcp-stderr.log`` file without needing per-line prefixes (which would
    require a pipe + reader thread and complicate shutdown).
    """
    fh = _get_mcp_stderr_log()
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== [{ts}] starting MCP server '{server_name}' =====\n")
        fh.flush()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Graceful import -- MCP SDK is an optional dependency
# ---------------------------------------------------------------------------

_MCP_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False
_MCP_NEW_HTTP = False
_MCP_LEGACY_HTTP = False
_MCP_SAMPLING_TYPES = False
_MCP_NOTIFICATION_TYPES = False
_MCP_ELICITATION_TYPES = False
_MCP_MESSAGE_HANDLER_SUPPORTED = False
_MCP_LOGGING_CALLBACK_SUPPORTED = False
_MCP_NEW_HTTP = False
sse_client = None
# Conservative fallback for SDK builds that don't export LATEST_PROTOCOL_VERSION.
# Streamable HTTP was introduced by 2025-03-26, so this remains valid for the
# HTTP transport path even on older-but-supported SDK versions.
LATEST_PROTOCOL_VERSION = "2025-03-26"
# The newest revision reachable through `ClientSession.initialize()`, which is
# NOT the newest revision the SDK knows about: from 2026-07-28 onward the
# handshake is replaced by a per-request envelope, so `initialize()` keeps
# sending `LATEST_HANDSHAKE_VERSION`. Seeding the MCP-Protocol-Version header
# from LATEST_PROTOCOL_VERSION would advertise a revision the body does not
# speak. Defaults to the handshake fallback for SDKs predating the split.
LATEST_HANDSHAKE_VERSION = LATEST_PROTOCOL_VERSION

# The heavy SDK import is LAZY (see _ensure_mcp_sdk): importing `mcp` costs
# ~260ms (mcp.types alone is ~60ms of pydantic model construction), which used
# to be paid at tool-discovery time on EVERY CLI startup even with zero MCP
# servers configured. Availability is decided here with a metadata-only
# find_spec probe (~1ms, no module execution) so every existing
# `if not _MCP_AVAILABLE` gate, test patch, and skipif keeps its exact
# semantics; the symbol import itself happens on first real SDK use.
try:
    import importlib.util as _importlib_util
    _MCP_AVAILABLE = _importlib_util.find_spec("mcp") is not None
except Exception:
    _MCP_AVAILABLE = False
if not _MCP_AVAILABLE:
    logger.debug("mcp package not installed -- MCP tool support disabled")

ClientSession: Any = None
_MCP_SDK_IMPORT_ATTEMPTED = False
_MCP_SDK_IMPORT_LOCK = threading.Lock()

# SDK symbols that _ensure_mcp_sdk() binds on first use. Module-level
# __getattr__ (PEP 562) below resolves external access to any of these by
# importing the SDK first — so tests doing mock.patch("tools.mcp_tool.
# stdio_client", ...) trigger the import when patch() saves the original,
# and the subsequent mock is never clobbered (_ensure is idempotent).
_MCP_SDK_LAZY_SYMBOLS = frozenset({
    "StdioServerParameters", "stdio_client",
    "streamablehttp_client", "streamable_http_client",
    "CreateMessageResult", "CreateMessageResultWithTools", "ErrorData",
    "SamplingCapability", "SamplingToolsCapability", "TextContent",
    "ToolUseContent", "ElicitRequestParams", "ElicitResult",
    "ServerNotification", "ToolListChangedNotification",
    "PromptListChangedNotification", "ResourceListChangedNotification",
})


def __getattr__(name: str):
    if name in _MCP_SDK_LAZY_SYMBOLS:
        _ensure_mcp_sdk()
        try:
            return globals()[name]
        except KeyError:
            pass  # SDK missing or symbol absent on this SDK build
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _ensure_mcp_sdk() -> bool:
    """Import the optional ``mcp`` SDK on first use. Returns availability.

    Idempotent and thread-safe. Sets the module-level ``_MCP_*`` flags and
    SDK symbol globals exactly as the old import-time block did. Honors a
    test-patched ``_MCP_AVAILABLE=False`` (returns False without importing)
    and test-installed mock symbols (``ClientSession`` already set → no
    re-import, so mocks are never clobbered).
    """
    global _MCP_SDK_IMPORT_ATTEMPTED, _MCP_AVAILABLE, _MCP_HTTP_AVAILABLE
    global _MCP_SAMPLING_TYPES, _MCP_NOTIFICATION_TYPES, _MCP_ELICITATION_TYPES
    global _MCP_MESSAGE_HANDLER_SUPPORTED, _MCP_LOGGING_CALLBACK_SUPPORTED
    global _MCP_NEW_HTTP, _MCP_LEGACY_HTTP, LATEST_PROTOCOL_VERSION, LATEST_HANDSHAKE_VERSION, sse_client
    global ClientSession, StdioServerParameters, stdio_client
    global streamablehttp_client, streamable_http_client
    global CreateMessageResult, CreateMessageResultWithTools, ErrorData
    global SamplingCapability, SamplingToolsCapability, TextContent, ToolUseContent
    global ElicitRequestParams, ElicitResult
    global ServerNotification, ToolListChangedNotification
    global PromptListChangedNotification, ResourceListChangedNotification

    if not _MCP_AVAILABLE:
        return False
    if _MCP_SDK_IMPORT_ATTEMPTED or ClientSession is not None:
        return _MCP_AVAILABLE
    with _MCP_SDK_IMPORT_LOCK:
        if _MCP_SDK_IMPORT_ATTEMPTED or ClientSession is not None:
            return _MCP_AVAILABLE
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            _MCP_AVAILABLE = True
            # Prefer the non-deprecated API (mcp >= 1.24.0); fall back to the
            # deprecated wrapper for older SDK versions.
            try:
                from mcp.client.streamable_http import streamable_http_client
                _MCP_NEW_HTTP = True
            except ImportError:
                _MCP_NEW_HTTP = False
            try:
                from mcp.client.streamable_http import streamablehttp_client
                _MCP_LEGACY_HTTP = True
            except ImportError:
                _MCP_LEGACY_HTTP = False
            # HTTP support requires EITHER entry point. mcp 2.0 dropped the
            # deprecated `streamablehttp_client` alias, so gating on that name
            # alone made _run_http raise ImportError for every HTTP and SSE
            # server on 2.x before it could reach the `streamable_http_client`
            # path.
            #
            # Reaching it was necessary and not sufficient: that path also
            # unpacked the transport as a fixed 3-tuple, which is 1.x's shape.
            # On 2.x it raised "not enough values to unpack (expected 3, got
            # 2)" and every HTTP/SSE server parked after its retry ladder.
            # Only stdio servers kept working, which is why this survived
            # review - the common configs are all stdio.
            _MCP_HTTP_AVAILABLE = _MCP_NEW_HTTP or _MCP_LEGACY_HTTP
            try:
                from mcp.types import LATEST_PROTOCOL_VERSION
            except ImportError:
                logger.debug("mcp.types.LATEST_PROTOCOL_VERSION not available -- using fallback protocol version")
            try:
                from mcp.client.session import LATEST_HANDSHAKE_VERSION
            except ImportError:
                # Pre-2.x SDKs make no distinction: the newest revision IS the
                # newest handshake revision, so the header and the body agree
                # either way.
                LATEST_HANDSHAKE_VERSION = LATEST_PROTOCOL_VERSION
            # SSE transport client (for MCP servers using SSE transport instead of Streamable HTTP)
            try:
                from mcp.client.sse import sse_client
            except ImportError:
                sse_client = None
                logger.debug("mcp.client.sse.sse_client not available -- SSE transport disabled")
            # Sampling types -- separated so older SDK versions don't break MCP support
            try:
                from mcp.types import (
                    CreateMessageResult,
                    CreateMessageResultWithTools,
                    ErrorData,
                    SamplingCapability,
                    SamplingToolsCapability,
                    TextContent,
                    ToolUseContent,
                )
                _MCP_SAMPLING_TYPES = True
            except ImportError:
                logger.debug("MCP sampling types not available -- sampling disabled")
            # Elicitation types -- gated separately for the same reason as sampling.
            # Added in mcp Python SDK 1.11.0 (Jul 2025); servers use elicitation to
            # ask the client for structured input mid-tool-call (e.g. payment
            # authorization). Missing types just disable the feature; everything
            # else keeps working.
            try:
                from mcp.types import ElicitRequestParams, ElicitResult
                _MCP_ELICITATION_TYPES = True
            except ImportError:
                logger.debug("MCP elicitation types not available -- elicitation disabled")
            # Notification types for dynamic tool discovery (tools/list_changed)
            try:
                from mcp.types import (
                    ServerNotification,
                    ToolListChangedNotification,
                    PromptListChangedNotification,
                    ResourceListChangedNotification,
                )
                _MCP_NOTIFICATION_TYPES = True
            except ImportError:
                logger.debug("MCP notification types not available -- dynamic tool discovery disabled")
        except ImportError:
            logger.debug("mcp package not installed -- MCP tool support disabled")

        if _MCP_AVAILABLE:
            try:
                from mcp.types import METHOD_NOT_FOUND as _mnf
                global _JSONRPC_METHOD_NOT_FOUND
                _JSONRPC_METHOD_NOT_FOUND = _mnf
            except Exception:  # pragma: no cover — SDK without the constant
                pass

        _MCP_MESSAGE_HANDLER_SUPPORTED = _check_message_handler_support()
        if _MCP_AVAILABLE and not _MCP_MESSAGE_HANDLER_SUPPORTED:
            logger.debug("MCP SDK does not support message_handler -- dynamic tool discovery disabled")
        _MCP_LOGGING_CALLBACK_SUPPORTED = _check_logging_callback_support()
        _MCP_SDK_IMPORT_ATTEMPTED = True
        return _MCP_AVAILABLE


_SDK_HTTPX_MOD = None


def sdk_httpx():
    """Return the httpx module the *installed* MCP SDK is built against.

    mcp 2.0 moved its HTTP transports and OAuth stack from ``httpx`` to
    ``httpx2`` — a separate distribution with the same public API, importable
    side by side with Hermes' own pinned ``httpx``. Every object that crosses
    the SDK boundary has to come from the module the SDK itself imports:
    the ``AsyncClient`` handed to ``streamable_http_client``, the client the
    ``sse_client`` factory returns, the ``Request`` built by the SDK's OAuth
    metadata helpers, and the exception classes those raise. Mixing the two
    fails at the transport layer rather than at import, so resolve it from the
    SDK's own transport module instead of inferring it from a version number.

    Returns ``None`` only when neither module is importable, which also means
    the SDK import above failed and no caller here can run.
    """
    global _SDK_HTTPX_MOD
    if _SDK_HTTPX_MOD is not None:
        return _SDK_HTTPX_MOD
    try:
        from mcp.client import streamable_http as _transport
        _SDK_HTTPX_MOD = getattr(_transport, "httpx2", None) or getattr(
            _transport, "httpx", None
        )
    except ImportError:
        _SDK_HTTPX_MOD = None
    if _SDK_HTTPX_MOD is None:
        # SDK transport module unavailable (or it stopped importing the
        # module under a predictable name). Fall back to whichever is
        # present, newest first.
        try:
            import httpx2 as _fallback
        except ImportError:
            try:
                import httpx as _fallback  # type: ignore[no-redef]
            except ImportError:
                return None
        _SDK_HTTPX_MOD = _fallback
    return _SDK_HTTPX_MOD


_MISSING = object()


def mcp_field(obj, snake: str, camel: str, default=None):
    """Read an MCP model field across the 1.x -> 2.x field rename.

    mcp 2.0 renamed every model field to snake_case and kept the camelCase
    spelling only as a *serialization* alias — pydantic aliases do not apply
    to attribute access, so ``getattr(result, "isError", False)`` returns the
    default on 2.x rather than raising. That turns a rename into silent wrong
    behaviour: failed tool calls read as successful, tool schemas read as
    empty, paginated lists stop after page one. Asking for both spellings
    keeps the read correct on either SDK generation, which matters because
    ``mcp`` is an optional extra users can install at their own version.
    """
    value = getattr(obj, snake, _MISSING)
    if value is not _MISSING:
        return value
    value = getattr(obj, camel, _MISSING)
    return default if value is _MISSING else value


def _check_message_handler_support() -> bool:
    """Check if ClientSession accepts ``message_handler`` kwarg.

    Inspects the constructor signature for backward compatibility with older
    MCP SDK versions that don't support notification handlers.
    """
    if not _MCP_AVAILABLE:
        return False
    try:
        return "message_handler" in inspect.signature(ClientSession).parameters
    except (TypeError, ValueError):
        return False


def _check_logging_callback_support() -> bool:
    """Check if ClientSession accepts the ``logging_callback`` kwarg.

    Mirrors ``_check_message_handler_support`` for backward compatibility
    with older MCP SDK versions.  Without a logging_callback, the SDK's
    default handler silently discards every ``notifications/message`` a
    server emits, so server-side diagnostics never reach Hermes' logs.
    """
    if not _MCP_AVAILABLE:
        return False
    try:
        return "logging_callback" in inspect.signature(ClientSession).parameters
    except (TypeError, ValueError):
        return False


# MCP logging levels (RFC 5424 syslog severities) -> Python logging levels.
# Port of anomalyco/opencode#34529's serverLog mapping.
_MCP_LOG_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.ERROR,
    "alert": logging.ERROR,
    "emergency": logging.ERROR,
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_TIMEOUT = 300      # seconds for tool calls


def _resolve_tool_timeout(config: dict) -> float:
    """Per-server tool-call timeout with unified-layer resolution (#85125 2g).

    Precedence: per-server ``mcp_servers.<name>.timeout`` (most specific,
    always wins) > ``timeouts.mcp.tool_call`` in config.yaml > the historical
    default. Values are platform-clamped by ``resolve_timeout`` either way.
    Defaults are unchanged: with neither key set this returns 300, exactly
    as before.
    """
    per_server = config.get("timeout")
    if per_server is not None:
        return per_server
    try:
        from agent.deadline import resolve_timeout

        resolved = resolve_timeout("mcp.tool_call", default=_DEFAULT_TOOL_TIMEOUT)
        if resolved is not None:
            return resolved
    except Exception:
        logger.debug("mcp.tool_call timeout resolution failed", exc_info=True)
    return _DEFAULT_TOOL_TIMEOUT


_DEFAULT_CONNECT_TIMEOUT = 60    # seconds for initial connection per server
_MAX_RECONNECT_RETRIES = 5
_MAX_INITIAL_CONNECT_RETRIES = 3 # retries for the very first connection attempt
_MAX_BACKOFF_SECONDS = 60
# While parked (reconnect budget exhausted, tools deregistered) the run task
# wakes on this cadence and attempts one revival probe. Without it a parked
# server is unrevivable: its tools are out of the registry, so no tool call
# can ever reach the circuit-breaker half-open probe or _signal_reconnect.
_PARKED_RETRY_INTERVAL = 300     # seconds between parked self-probes
_RECYCLED_RECONNECT_TIMEOUT = 15.0
# Jitter applied to reconnect backoff sleeps. Without it, every server that
# lost the same backend retries in lockstep (thundering herd) and log lines
# from N servers land in synchronized bursts.
_BACKOFF_JITTER = 0.2            # +/-20%


def _jittered(seconds: float) -> float:
    """Return ``seconds`` with +/-20% uniform jitter, floored at 0."""
    return max(0.0, seconds * random.uniform(1.0 - _BACKOFF_JITTER,
                                             1.0 + _BACKOFF_JITTER))

# Keepalive cadence for HTTP/SSE sessions. The MCP spec lets a server expire
# idle sessions on any TTL it chooses (Streamable HTTP "Session Management"),
# so a client that wants a session to survive idle periods MUST refresh faster
# than that TTL. The default suits long LB/NAT idle windows (commonly
# 300-600s); servers with short session TTLs (e.g. Unreal Engine's editor MCP,
# ~15s) need a smaller ``keepalive_interval`` in their config or every idle
# tool call lands on a dead session and pays the full reconnect path. The floor
# stops a misconfigured tiny interval from busy-looping the keepalive.
_DEFAULT_KEEPALIVE_INTERVAL = 180  # seconds between liveness pings
_MIN_KEEPALIVE_INTERVAL = 5        # clamp floor for configured intervals

# Final shutdown gives pending MCP-loop tasks one bounded cancellation cycle
# before closing their owning loop. Cooperative parked/reconnect waiters finish
# immediately; cancellation-resistant tasks must not hang process exit.
_MCP_LOOP_DRAIN_TIMEOUT = 3.0

# Environment variables that are safe to pass to stdio subprocesses
_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR",
})

_SAFE_ENV_KEYS_CASE_INSENSITIVE = frozenset({
    # Windows process/location vars. These are needed by launcher-style tools
    # such as Docker Desktop's MCP plugin discovery, and do not carry secrets.
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "COMPUTERNAME",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PUBLIC",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
})

# Regex for credential patterns to strip from error messages
_CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"ghp_[A-Za-z0-9_]{1,255}"           # GitHub PAT
    r"|sk-[A-Za-z0-9_]{1,255}"           # OpenAI-style key
    r"|Bearer\s+\S+"                      # Bearer token
    r"|token=[^\s&,;\"']{1,255}"         # token=...
    r"|key=[^\s&,;\"']{1,255}"           # key=...
    r"|API_KEY=[^\s&,;\"']{1,255}"       # API_KEY=...
    r"|password=[^\s&,;\"']{1,255}"      # password=...
    r"|secret=[^\s&,;\"']{1,255}"        # secret=...
    r")",
    re.IGNORECASE,
)

# Pre-compiled pattern for ${VAR_NAME} style env-var interpolation.
# Supports any non-} characters in the variable name (hyphens, dots, etc.)
# so providers like MY-VAR or my.var work correctly.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _env_ref_name(ref: str) -> str:
    """Normalize a ``${...}`` reference body into an env-var name.

    Accepts Cursor-style ``${env:VAR}`` in addition to plain ``${VAR}`` by
    stripping a leading ``env:`` prefix. The result is the bare variable name
    to look up in the secret scope / ``os.environ``.
    """
    ref = ref.strip()
    if ref.startswith("env:"):
        ref = ref[len("env:"):].strip()
    return ref


def _workspace_folder() -> str:
    """Best-effort absolute workspace root for ``${workspaceFolder}``.

    Resolution order:

      1. ``tools.file_tools._authoritative_workspace_root()`` — the session's
         recorded terminal cwd, a registered task/session cwd override, or a
         sentinel-free absolute ``$TERMINAL_CWD`` (in that order).
      2. ``os.getcwd()`` as the final fallback when no session anchor exists.
    """
    try:
        from tools.file_tools import _authoritative_workspace_root

        root = _authoritative_workspace_root()
        if root:
            return root
    except Exception:
        pass
    return os.getcwd()


def _context_var_value(ref: str) -> Optional[str]:
    """Resolve Cursor-style context variables in ``${...}`` references.

    Supports the case-sensitive names Cursor's ``mcp.json`` interpolation
    understands beyond env vars: ``${userHome}``, ``${workspaceFolder}``,
    ``${workspaceFolderBasename}``, ``${pathSeparator}`` and its ``${/}``
    shorthand. Returns ``None`` for anything else so unknown references keep
    the existing env-var lookup semantics.
    """
    if ref == "userHome":
        return os.path.expanduser("~")
    if ref == "workspaceFolder":
        return _workspace_folder()
    if ref == "workspaceFolderBasename":
        root = _workspace_folder()
        return os.path.basename(root.rstrip("/\\")) or root
    if ref in ("pathSeparator", "/"):
        return os.sep
    return None


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _build_safe_env(user_env: Optional[dict]) -> dict:
    """Build a filtered environment dict for stdio subprocesses.

    Only passes through safe baseline variables (PATH, HOME, etc.) and XDG_*
    variables from the current process environment, secrets injected by an
    external secret source (Bitwarden, 1Password, plugin backends) that
    Hermes explicitly tagged during dotenv loading, plus any variables
    explicitly specified by the user in the server config.

    This prevents accidentally leaking secrets like API keys, tokens, or
    credentials to MCP server subprocesses.  Secret-source-injected vars are
    an exception: users configured that backend specifically so Hermes and
    its subprocesses can consume those credentials without duplicating them
    in every MCP server's ``env:`` block.
    """
    try:
        from hermes_cli.env_loader import get_secret_source
    except Exception:  # pragma: no cover — early bootstrap/import fallback
        get_secret_source = None
    env = {}
    for key, value in os.environ.items():
        if (
            key in _SAFE_ENV_KEYS
            or key.upper() in _SAFE_ENV_KEYS_CASE_INSENSITIVE
            or key.startswith("XDG_")
            or (get_secret_source is not None and get_secret_source(key))
        ):
            env[key] = value
    if user_env:
        env.update(user_env)
    return env


_MCP_OPAQUE_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_])(?P<key>[A-Za-z][A-Za-z0-9_.-]{0,127})"
    r"\s*[:=]\s*)(?P<quote>[\"']?)(?P<value>[^\"'\s,;\]}]+)(?P=quote)",
    re.IGNORECASE,
)


def _sanitize_opaque_mcp_assignments(text: str) -> str:
    """Redact opaque credential assignments embedded in MCP prose."""
    from agent.redact import _key_has_secret_keyword

    def _replace(match: re.Match) -> str:
        if not _key_has_secret_keyword(match.group("key")):
            return match.group(0)
        return f"{match.group('prefix')}{match.group('quote')}***{match.group('quote')}"

    return _MCP_OPAQUE_ASSIGNMENT_RE.sub(_replace, text)


_MCP_AUTHORIZATION_HEADER_RE = re.compile(
    r"((?:Proxy-)?Authorization:\s*)"
    r"(?:(\S+)\s+)?([^\s\"'`,;\]}]+)",
    re.IGNORECASE,
)


def _sanitize_mcp_authorization_headers(text: str) -> str:
    """Redact opaque Authorization credentials before generic text passes.

    The generic redactor also recognizes YAML-style ``key: value`` text.  If
    that pass runs first, it can consume the scheme word (``Bearer``) and
    expose the actual opaque credential to the later header matcher.  Handle
    the composed header form both before and after the shared policy so the
    passes cannot interfere with one another.
    """
    def _replace(match: re.Match) -> str:
        scheme = match.group(2)
        scheme_text = f"{scheme} " if scheme else ""
        return f"{match.group(1)}{scheme_text}***"

    return _MCP_AUTHORIZATION_HEADER_RE.sub(_replace, text)


_MCP_MASKED_VALUE_RE = re.compile(r"(?<!\S)\*{3}(?!\S)")

# Structured MCP objects use a mixture of protocol metadata and prose-bearing
# fields.  Keep this list deliberately semantic rather than tied to one
# server's object names: servers commonly nest ``description``/``content``
# below resource, prompt, result, or vendor-specific containers.
_MCP_FREE_TEXT_FIELD_NAMES = frozenset({
    "text", "description", "content", "message", "title", "summary",
    "detail", "label", "reason", "error", "value",
})

# Generic text redaction cannot identify a random opaque credential in prose.
# At an MCP egress boundary, a credential noun followed by a value is an
# unambiguous credential disclosure even when the value has no vendor prefix.
# Preserve the explanatory words and replace only the value so diagnostics
# remain useful.
_MCP_OPAQUE_CREDENTIAL_PROSE_RE = re.compile(
    r"(?i)(^|[^A-Za-z0-9_])((?:the )?(?:credential|secret|password|token|api[ ]*key|"
    r"access[ ]+token|refresh[ ]+token|client[ ]+secret|authorization)"
    r"(?:[ ]+(?:presented|provided|received|returned|supplied|sent|given|"
    r"shown|reported|is|was|equals|value|values|credentialed))*[ ]+)"
    r"([A-Za-z0-9][A-Za-z0-9_./:+~@%=-]{11,})"
)
_MCP_OPAQUE_PROSE_STOPWORDS = frozenset({
    "authenticated", "authorized", "available", "configured", "expired",
    "invalid", "missing", "required", "unknown", "valid", "provided",
})


def _sanitize_opaque_mcp_prose(text: str) -> str:
    """Redact opaque values named by credential-bearing prose."""
    def _replace(match: re.Match) -> str:
        candidate = match.group(3)
        if candidate.lower().rstrip(".,;:!?)]}") in _MCP_OPAQUE_PROSE_STOPWORDS:
            return match.group(0)
        trailing = ""
        while candidate and candidate[-1] in ".,;:!?)]}":
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]
        if len(candidate) < 12:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}***{trailing}"

    return _MCP_OPAQUE_CREDENTIAL_PROSE_RE.sub(_replace, text)


def _is_mcp_free_text_field(field_name: str | None) -> bool:
    """Return whether an MCP field is a model-facing prose position."""
    if not field_name:
        return False
    return field_name.rsplit(".", 1)[-1].lower() in _MCP_FREE_TEXT_FIELD_NAMES


def _is_opaque_mcp_text_candidate(text: str) -> bool:
    """Identify scalar opaque values without masking ordinary prose.

    A whitespace-free, non-URL scalar at this boundary has no useful
    diagnostic structure to preserve and is commonly how custom MCP servers
    carry opaque credentials. Short names remain intact; longer candidates are
    replaced with the same non-reusable sentinel used for credential keys.
    """
    candidate = text.strip()
    if len(candidate) < 16 or any(char.isspace() for char in candidate):
        return False
    if "://" in candidate or candidate.startswith(("MEDIA:", "data:")):
        return False
    return True


def _sanitize_opaque_mcp_tokens(text: str) -> str:
    """Redact long opaque tokens in forced model-facing prose."""
    def _replace(match: re.Match) -> str:
        original = match.group(0)
        token = original.lstrip("([{")
        trailing = token[len(token.rstrip(".,;:!?)]}")):]
        candidate = token[:-len(trailing)] if trailing else token
        if not _is_opaque_mcp_text_candidate(candidate):
            return original
        leading = original[:len(original) - len(token)]
        return f"{leading}***{trailing}"

    return re.sub(r"\S+", _replace, text)


def _sanitize_mcp_text_leaf(
    value: str,
    field_name: str | None = None,
    *,
    _redact_url_credentials: bool = True,
) -> str:
    """Apply the forced MCP policy to one model-facing text leaf."""
    from agent.redact import _key_has_secret_keyword, redact_sensitive_text

    secret_field = (
        field_name
        if field_name and _key_has_secret_keyword(field_name)
        else None
    )
    if secret_field:
        # Probe a synthetic assignment to obtain the established opaque-value
        # sentinel without ever exposing a suffix of the actual value.
        prefix = f"{secret_field}="
        redacted_probe = redact_sensitive_text(
            f"{prefix}placeholder", force=True,
        )
        if redacted_probe.startswith(prefix):
            return redacted_probe[len(prefix):]

    redacted = _sanitize_mcp_authorization_headers(value)
    redacted = redact_sensitive_text(
        redacted,
        force=True,
        redact_url_credentials=_redact_url_credentials,
    )
    redacted = _sanitize_opaque_mcp_assignments(redacted)
    redacted = _sanitize_opaque_mcp_prose(redacted)
    redacted = _sanitize_mcp_authorization_headers(redacted)
    if _is_mcp_free_text_field(field_name):
        redacted = _sanitize_opaque_mcp_tokens(redacted)
    if (
        _is_mcp_free_text_field(field_name)
        and redacted == value
        and _is_opaque_mcp_text_candidate(value)
    ):
        return "***"
    return redacted


def _sanitize_error(text: str) -> str:
    """Strip credential-like and URL credentials from model-facing errors.

    Error text is an explicit egress boundary just like successful result
    content. Use the shared forced policy so opaque key/value credentials,
    authorization headers, credential-bearing URLs, and credential-bearing
    prose receive the same protection as structured results.
    """
    from agent.redact import redact_sensitive_text

    text = _sanitize_mcp_authorization_headers(text)
    text = _CREDENTIAL_PATTERN.sub("[REDACTED]", text)
    text = redact_sensitive_text(
        text,
        force=True,
        redact_url_credentials=True,
    )
    text = _sanitize_opaque_mcp_assignments(text)
    text = _sanitize_opaque_mcp_prose(text)
    text = _sanitize_mcp_authorization_headers(text)
    text = _sanitize_opaque_mcp_tokens(text)
    return _MCP_MASKED_VALUE_RE.sub("[REDACTED]", text)


def _exc_str(exc: BaseException) -> str:
    """Return a non-empty human-readable string for *exc*.

    Some exception classes (e.g. ``anyio.ClosedResourceError``) are raised
    without a message argument, so ``str(exc)`` is ``""``.  This helper
    falls back to ``repr(exc)`` so that error messages shown to the user
    and logged to disk always carry *some* diagnostic information.
    """
    text = str(exc).strip()
    return text if text else repr(exc)


# JSON-RPC "method not found" — the error a server returns when it does not
# implement a requested method (e.g. a tool-capable server that never wired up
# the optional ``ping`` utility). -32601 is the JSON-RPC 2.0 spec constant;
# _ensure_mcp_sdk() overrides it from mcp.types when the SDK is loaded (kept
# lazy so this module never triggers the ~260ms `mcp` import at import time).
_JSONRPC_METHOD_NOT_FOUND = -32601

# 2026-07-28 stateless servers answering a legacy ``initialize`` reject it
# with one of these: UnsupportedProtocolVersion (-32022, spec-reserved range)
# or plain method-not-found when the handshake methods are gone entirely.
# Structural codes only — checked via _handshake_rejected_as_modern().
_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION = -32022


def _handshake_rejected_as_modern(exc: BaseException) -> bool:
    """True when a failed ``initialize`` signals a 2026-07-28-only server.

    Mirrors :func:`_is_method_not_found_error`'s structural-then-substring
    shape (never ``isinstance`` on SDK exception types — the SDK wraps
    task-group errors in ``ExceptionGroup`` and symbols drift across
    generations; see references/sdk-exceptiongroup-wrapping.md).
    """
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None) or getattr(exc, "code", None)
    if code in (_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION, _JSONRPC_METHOD_NOT_FOUND):
        return True
    msg = str(exc).lower()
    if not msg:
        return False
    return (
        "unsupported protocol version" in msg
        or str(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION) in msg
        or _is_method_not_found_error(exc)
    )


def _is_method_not_found_error(exc: BaseException) -> bool:
    """Return True if *exc* is a JSON-RPC ``method not found`` (-32601).

    ``ping`` is an *optional* MCP utility (spec: "optional ping mechanism").
    A server that doesn't implement it answers a ping with -32601 rather than
    an empty result. Structurally inspect ``MCPError.error.code`` first, then
    fall back to a substring match so detection survives SDK version drift and
    servers that surface the condition as a plain message.

    The substring fallback matters when a server reports method-not-found
    without a structural ``-32601`` code (e.g. surfaced as a plain exception
    string). Besides the canonical "method not found", many JSON-RPC
    implementations phrase it as "Unknown method: <name>" — agentmemory's MCP
    server is one such case (#50028). Without matching that phrasing the
    ping→list_tools fallback never latches and the keepalive reconnect-loops.
    """
    # Structural: mcp.shared.exceptions.MCPError carries ErrorData.code.
    err = getattr(exc, "error", None)
    code = getattr(err, "code", None)
    if code == _JSONRPC_METHOD_NOT_FOUND:
        return True
    msg = str(exc).lower()
    if not msg:
        return False
    return (
        str(_JSONRPC_METHOD_NOT_FOUND) in msg
        or "method not found" in msg
        or "unknown method" in msg
        or "not found: ping" in msg
    )


# ---------------------------------------------------------------------------
# MCP tool description content scanning
# ---------------------------------------------------------------------------

# Patterns that indicate potential prompt injection in MCP tool descriptions.
# These are WARNING-level — we log but don't block, since false positives
# would break legitimate MCP servers.
_MCP_INJECTION_PATTERNS = [
    (re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
     "prompt override attempt ('ignore previous instructions')"),
    (re.compile(r"you\s+are\s+now\s+a", re.I),
     "identity override attempt ('you are now a...')"),
    (re.compile(r"your\s+new\s+(task|role|instructions?)\s+(is|are)", re.I),
     "task override attempt"),
    (re.compile(r"system\s*:\s*", re.I),
     "system prompt injection attempt"),
    (re.compile(r"<\s*(system|human|assistant)\s*>", re.I),
     "role tag injection attempt"),
    (re.compile(r"do\s+not\s+(tell|inform|mention|reveal)", re.I),
     "concealment instruction"),
    (re.compile(r"(curl|wget|fetch)\s+https?://", re.I),
     "network command in description"),
    (re.compile(r"base64\.(b64decode|decodebytes)", re.I),
     "base64 decode reference"),
    (re.compile(r"exec\s*\(|eval\s*\(", re.I),
     "code execution reference"),
    (re.compile(r"import\s+(subprocess|os|shutil|socket)", re.I),
     "dangerous import reference"),
]


def _scan_mcp_description(server_name: str, tool_name: str, description: str) -> List[str]:
    """Scan an MCP tool description for prompt injection patterns.

    Returns a list of finding strings (empty = clean).
    """
    findings = []
    if not description:
        return findings
    for pattern, reason in _MCP_INJECTION_PATTERNS:
        if pattern.search(description):
            findings.append(reason)
    if findings:
        logger.warning(
            "MCP server '%s' tool '%s': suspicious description content — %s. "
            "Description: %.200s",
            server_name, tool_name, "; ".join(findings),
            description,
        )
    return findings


def _prepend_path(env: dict, directory: str) -> dict:
    """Prepend *directory* to env PATH if it is not already present."""
    updated = dict(env or {})
    if not directory:
        return updated

    existing = updated.get("PATH", "")
    parts = [part for part in existing.split(os.pathsep) if part]
    if directory not in parts:
        parts = [directory, *parts]
    updated["PATH"] = os.pathsep.join(parts) if parts else directory
    return updated


# Safety cap on nextCursor pagination loops so a misbehaving server that
# returns a cursor forever cannot spin discovery indefinitely. 50 pages at
# the common 50-100 items/page covers thousands of tools/resources/prompts.
_MCP_LIST_MAX_PAGES = 50


async def _paginate_full_list(list_method, items_attr: str, server_name: str,
                              cache_meta_out: Optional[dict] = None):
    """Drain a paginated MCP ``list_*`` call by following ``nextCursor``.

    The MCP spec allows servers to paginate ``tools/list``,
    ``resources/list``, and ``prompts/list`` responses via an opaque
    ``nextCursor`` token. The Python SDK's ``ClientSession.list_*`` methods
    fetch exactly one page per call, so a client that never passes the
    cursor back silently sees only the first page — on a paginated server
    every tool/resource/prompt past page 1 would be invisible to the agent.

    Args:
        list_method: Bound ``session.list_tools`` / ``list_resources`` /
            ``list_prompts`` coroutine function.
        items_attr: Result attribute holding the page's items
            (``"tools"``, ``"resources"``, or ``"prompts"``).
        server_name: For log messages.
        cache_meta_out: Optional dict that receives the first page's
            SEP-2549 cache hints (``ttl_ms``, ``cache_scope``) when the
            server provides them (2026-07-28 servers MUST; earlier ones
            won't). Callers use ``ttl_ms`` to bound the schema cache.

    Returns:
        Combined list of items across all pages. Callers must hold the
        server's ``_rpc_lock`` for the duration so pages come from a
        consistent snapshot.
    """
    items: list = []
    cursor = None
    for _ in range(_MCP_LIST_MAX_PAGES):
        if not cursor:
            result = await list_method()
        else:
            # Cursor continuation differs by SDK generation: mcp 1.x
            # accepts ``cursor=``, mcp 2.0 takes ``params=`` (a
            # PaginatedRequestParams). Try modern first, fall back.
            try:
                _params_cls = getattr(_mcp_types(), "PaginatedRequestParams", None)
                if _params_cls is not None:
                    result = await list_method(params=_params_cls(cursor=cursor))
                else:
                    result = await list_method(cursor=cursor)
            except TypeError:
                result = await list_method(cursor=cursor)
        if cache_meta_out is not None and not items:
            _ttl = mcp_field(result, "ttl_ms", "ttlMs")
            _scope = mcp_field(result, "cache_scope", "cacheScope")
            if _ttl is not None:
                cache_meta_out["ttl_ms"] = _ttl
            if _scope is not None:
                cache_meta_out["cache_scope"] = _scope
        items.extend(getattr(result, items_attr, None) or [])
        cursor = mcp_field(result, "next_cursor", "nextCursor")
        # Per the MCP spec the cursor is an opaque string; anything else
        # (including mock objects in tests) means "no more pages".
        if not isinstance(cursor, str) or not cursor:
            break
    else:
        logger.warning(
            "MCP server '%s': %s pagination exceeded %d pages; "
            "truncating at %d items",
            server_name, items_attr, _MCP_LIST_MAX_PAGES, len(items),
        )
    return items


def _mcp_types():
    """Late import of ``mcp.types`` (module keeps the SDK import lazy)."""
    import mcp.types as _t
    return _t


def _resolve_stdio_command(command: str, env: dict) -> tuple[str, dict]:
    """Resolve a stdio MCP command against the exact subprocess environment.

    This primarily exists to make bare ``npx``/``npm``/``node`` commands work
    reliably even when MCP subprocesses run under a filtered PATH.
    """
    resolved_command = os.path.expanduser(str(command).strip())
    resolved_env = dict(env or {})

    if os.sep not in resolved_command:
        path_arg = resolved_env["PATH"] if "PATH" in resolved_env else None
        which_hit = shutil.which(resolved_command, path=path_arg)
        if which_hit is None and sys.platform == "win32" and resolved_env:
            # shutil.which(..., path=...) resolves extensions from the PARENT
            # process PATHEXT, not the MCP subprocess env — so a config that
            # supplies both PATH and PATHEXT can fail to resolve a command
            # its own env can find (#56536). Retry with the config's PATHEXT
            # (any key casing: PATHEXT / Pathext / pathext) applied.
            cfg_pathext = next(
                (v for k, v in resolved_env.items()
                 if k.upper() == "PATHEXT" and isinstance(v, str) and v.strip()),
                None,
            )
            if cfg_pathext and cfg_pathext != os.environ.get("PATHEXT"):
                _saved = os.environ.get("PATHEXT")
                try:
                    os.environ["PATHEXT"] = cfg_pathext
                    which_hit = shutil.which(resolved_command, path=path_arg)
                finally:
                    if _saved is None:
                        os.environ.pop("PATHEXT", None)
                    else:
                        os.environ["PATHEXT"] = _saved
        if which_hit:
            resolved_command = which_hit
        elif resolved_command in {"npx", "npm", "node"}:
            hermes_home = os.path.expanduser(
                os.getenv(
                    "HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes")
                )
            )
            candidates = [
                os.path.join(hermes_home, "node", "bin", resolved_command),
                os.path.join(os.path.expanduser("~"), ".local", "bin", resolved_command),
                # /usr/local/bin is the canonical install location for Node on
                # Linux from-source builds, the upstream node:bookworm-slim
                # image (which the Hermes Docker image copies node + npm +
                # corepack from since #4977), and macOS Homebrew on Intel.
                # Without this candidate, any MCP server configured with an
                # env.PATH that omits /usr/local/bin (a common pattern when
                # users hand-author PATH for sandboxing) fails with ENOENT
                # at execvp, and a naive symlink workaround into the user's
                # PATH only fails one layer deeper because npx's shebang
                # re-execs /usr/bin/env node which needs the same directory.
                os.path.join(os.sep, "usr", "local", "bin", resolved_command),
            ]
            for candidate in candidates:
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    resolved_command = candidate
                    break

    command_dir = os.path.dirname(resolved_command)
    if command_dir:
        resolved_env = _prepend_path(resolved_env, command_dir)

    return resolved_command, resolved_env


def _wrap_command_with_watchdog(command: str, args: list) -> tuple[str, list]:
    """Wrap a stdio MCP server command in the parent-death watchdog supervisor.

    On POSIX, the watchdog records this process's PID and later detects parent
    death directly through ``getppid()``. Returns the (command, args) unchanged
    on non-POSIX platforms or if the PID cannot be read.
    """
    if os.name != "posix":
        # Relies on process groups (os.getpgid/os.killpg); no POSIX
        # equivalent wired up here yet, matching the existing killpg-based
        # orphan cleanup's platform scope (Windows falls back to plain
        # os.kill there too).
        return command, args
    try:
        my_pid = os.getpid()
    except Exception:
        # Never let watchdog bookkeeping failure block a real MCP connection.
        return command, args
    watchdog_args = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_stdio_watchdog.py"),
        "--ppid", str(my_pid),
        "--",
        command,
        *args,
    ]
    return sys.executable, watchdog_args


# ---------------------------------------------------------------------------
# MCP ImageContent block → Hermes MEDIA tag
# ---------------------------------------------------------------------------


def _is_reserved_mcp_meta_key(key: str) -> bool:
    """Return True if an MCP ``_meta`` key uses a protocol-reserved prefix.

    Per the MCP spec's key-name rules, a prefix is reserved when a
    ``modelcontextprotocol`` or ``mcp`` label is followed by at least one
    more label (``modelcontextprotocol.io/...``, ``tools.mcp.com/...``).
    A trailing reserved word (``com.example.mcp/...``) is a legitimate
    vendor namespace and passes through. Ported from
    MoonshotAI/kimi-code#2600.
    """
    slash = key.find("/")
    if slash <= 0:
        return False
    labels = key[:slash].split(".")
    return any(
        label in ("modelcontextprotocol", "mcp") and i < len(labels) - 1
        for i, label in enumerate(labels)
    )


def _strip_reserved_meta_keys(meta) -> "Optional[Dict[str, Any]]":
    """Drop protocol-reserved keys from a tool result's ``_meta`` mapping.

    Returns the filtered dict, or ``None`` when there is nothing
    model-facing left (or the input wasn't a mapping).
    """
    if not isinstance(meta, dict):
        return None
    out = {k: v for k, v in meta.items()
           if isinstance(k, str) and not _is_reserved_mcp_meta_key(k)}
    return out or None


_MCP_MAX_RESULT_NESTING = 64
_MCP_UNSUPPORTED = object()


def _sanitize_mcp_result_value(
    value: Any,
    field_name: str | None = None,
    *,
    _redact_url_credentials: bool = True,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    """Recursively apply the forced MCP redaction boundary."""
    from agent.redact import _key_has_secret_keyword

    # A root structured scalar/container is model-facing free text. Passing the
    # semantic field through containers preserves that policy for descendants.
    field_name = field_name or "text"
    if _depth > _MCP_MAX_RESULT_NESTING:
        return None
    if isinstance(value, str):
        return _sanitize_mcp_text_leaf(
            value, field_name, _redact_url_credentials=_redact_url_credentials
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if not isinstance(value, (dict, list, tuple)):
        return _MCP_UNSUPPORTED

    secret_field = field_name if _key_has_secret_keyword(field_name) else None
    if _seen is None:
        _seen = set()
    value_id = id(value)
    if value_id in _seen:
        return None
    _seen.add(value_id)
    try:
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                if not isinstance(key, (str, int, float, bool)) and key is not None:
                    continue
                child_field = (
                    "resource.text" if field_name == "resource" and isinstance(key, str) and key.lower() == "text"
                    else key if isinstance(key, str) and _is_mcp_free_text_field(key)
                    else key if isinstance(key, str) and _key_has_secret_keyword(key)
                    else "resource" if isinstance(key, str) and key.lower() == "resource" and secret_field is None
                    else field_name if _is_mcp_free_text_field(field_name)
                    else secret_field
                )
                child = _sanitize_mcp_result_value(
                    item, child_field,
                    _redact_url_credentials=_redact_url_credentials,
                    _seen=_seen, _depth=_depth + 1,
                )
                if child is not _MCP_UNSUPPORTED:
                    sanitized[key] = child
            return sanitized

        sequence_field = field_name if _is_mcp_free_text_field(field_name) else secret_field
        sanitized_items = []
        for item in value:
            child = _sanitize_mcp_result_value(
                item, sequence_field,
                _redact_url_credentials=_redact_url_credentials,
                _seen=_seen, _depth=_depth + 1,
            )
            if child is not _MCP_UNSUPPORTED:
                sanitized_items.append(child)
        return sanitized_items if isinstance(value, list) else tuple(sanitized_items)
    finally:
        _seen.remove(value_id)


def _mcp_image_extension_for_mime_type(mime_type: str) -> str:
    """Return a reasonable file extension for an MCP image MIME type."""
    import mimetypes
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    return mimetypes.guess_extension(normalized) or ".png"


def _cache_mcp_image_block(block) -> str:
    """Cache an MCP ``ImageContent`` block to the shared image cache and
    return a ``MEDIA:<path>`` tag that Hermes gateways know how to render.

    Returns an empty string when *block* is not an image, when the base64
    payload is malformed, or when the cache helper rejects the bytes (e.g.
    non-image MIME masquerading as an image). Errors are logged, not raised:
    a single bad block shouldn't kill the tool result, and the caller will
    fall through to any text blocks that did parse.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = mcp_field(block, "mime_type", "mimeType")
    normalized_mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if data is None or not normalized_mime.startswith("image/"):
        return ""

    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP image block decode failed (%s): %s", normalized_mime, exc)
        return ""

    try:
        from gateway.platforms.base import cache_image_from_bytes

        image_path = cache_image_from_bytes(
            raw_bytes,
            ext=_mcp_image_extension_for_mime_type(normalized_mime),
        )
    except ImportError:
        # gateway.platforms.base not importable in this process (e.g. cron
        # without gateway deps). Fall back to silently dropping — callers
        # get any text blocks that did parse.
        logger.debug("MCP image caching skipped — gateway.platforms.base unavailable")
        return ""
    except Exception as exc:
        logger.warning("MCP image block cache failed: %s", exc)
        return ""

    return f"MEDIA:{image_path}"


# ---------------------------------------------------------------------------
# MCP resource blocks (ResourceLink / EmbeddedResource / AudioContent)
# ---------------------------------------------------------------------------

# Hard cap on decoded resource bytes materialized from an MCP tool result.
# Prevents a misbehaving server from filling the cache disk via one block.
_MCP_RESOURCE_MAX_BYTES = 50 * 1024 * 1024

# Base64 expands raw bytes by ~4/3; reject oversized payloads before decoding
# so a multi-GB blob string is never transiently doubled in memory.
_MCP_RESOURCE_MAX_B64_CHARS = _MCP_RESOURCE_MAX_BYTES * 4 // 3 + 4


def _mcp_resource_filename(uri: str, mime_type: str) -> str:
    """Derive a safe display filename for an MCP resource.

    Only the last path segment of the URI is considered, and only as a
    *name hint* — `cache_document_from_bytes` re-sanitizes and prefixes it,
    so remote path components can't influence the cache location.
    """
    import mimetypes
    import re as _re
    from pathlib import Path
    from urllib.parse import urlparse, unquote

    name = ""
    if uri:
        try:
            name = Path(unquote(urlparse(str(uri)).path or "")).name
        except (ValueError, TypeError):
            name = ""
    # Strip control characters (newlines/ANSI escapes from hostile URIs would
    # otherwise land in the filename and the transcript marker) and cap the
    # length, preserving the extension.
    name = _re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 12:
            name = stem[: 150 - len(ext) - 1] + "." + ext
        else:
            name = name[:150]
    if not name or name in {".", ".."}:
        normalized = (mime_type or "").split(";", 1)[0].strip().lower()
        ext = mimetypes.guess_extension(normalized) or ".bin"
        name = f"resource{ext}"
    return name


def _cache_mcp_audio_block(block) -> str:
    """Cache an MCP ``AudioContent`` block and return a ``MEDIA:`` tag.

    Returns an empty string when *block* is not audio or on any failure —
    same fail-open contract as ``_cache_mcp_image_block``.
    """
    import base64

    data = getattr(block, "data", None)
    mime_type = str(mcp_field(block, "mime_type", "mimeType") or "").split(";", 1)[0].strip().lower()
    if data is None or not mime_type.startswith("audio/"):
        return ""
    if len(data) > _MCP_RESOURCE_MAX_B64_CHARS:
        return f"[MCP audio resource too large to cache: ~{len(data) * 3 // 4} bytes]"
    try:
        raw_bytes = base64.b64decode(data)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP audio block decode failed (%s): %s", mime_type, exc)
        return ""
    if len(raw_bytes) > _MCP_RESOURCE_MAX_BYTES:
        return f"[MCP audio resource too large to cache: {len(raw_bytes)} bytes]"
    try:
        from gateway.platforms.base import cache_audio_from_bytes
        import mimetypes

        ext = (
            {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav"}.get(mime_type)
            or mimetypes.guess_extension(mime_type)
            or ".ogg"
        )
        audio_path = cache_audio_from_bytes(raw_bytes, ext=ext)
    except ImportError:
        logger.debug("MCP audio caching skipped — gateway.platforms.base unavailable")
        return ""
    except Exception as exc:
        logger.warning("MCP audio block cache failed: %s", exc)
        return ""
    return f"MEDIA:{audio_path}"


def _render_mcp_resource_block(block, server_name: str = "") -> str:
    """Render an MCP ``ResourceLink`` or ``EmbeddedResource`` block as text.

    - ``EmbeddedResource`` with text contents → the text itself.
    - ``EmbeddedResource`` with blob contents → bytes are decoded (size-capped)
      and materialized into the Hermes document cache; returns a marker with
      the local path so file/terminal tools can consume it.
    - ``ResourceLink`` → the URI plus a pointer at the server's read_resource
      tool. No network fetch happens here; the link is only readable through
      the originating MCP session.

    Returns an empty string for non-resource blocks. Failures are logged and
    reported inline rather than silently dropping the block.
    """
    block_type = getattr(block, "type", "")

    if block_type == "resource_link" or (
        hasattr(block, "uri") and not hasattr(block, "resource") and block_type != "text"
    ):
        uri = getattr(block, "uri", None)
        if not uri:
            return ""
        name = getattr(block, "name", "") or ""
        mime = mcp_field(block, "mime_type", "mimeType", "") or ""
        details = f"uri={uri}"
        if name:
            details += f", name={name}"
        if mime:
            details += f", mimeType={mime}"
        reader = (
            mcp_prefixed_tool_name(server_name, "read_resource")
            if server_name
            else "the MCP server's read_resource tool"
        )
        return f"[MCP resource link: {details} — fetch it with {reader}]"

    resource = getattr(block, "resource", None)
    if resource is None:
        return ""

    text = getattr(resource, "text", None)
    if text is not None:
        return _sanitize_mcp_text_leaf(
            strip_unicode_tags(str(text)),
            "resource.text",
        )

    blob = getattr(resource, "blob", None)
    if blob is None:
        return ""

    import base64

    uri = str(getattr(resource, "uri", "") or "")
    mime = str(mcp_field(resource, "mime_type", "mimeType", "") or "")
    if len(blob) > _MCP_RESOURCE_MAX_B64_CHARS:
        return f"[MCP embedded resource too large to cache: ~{len(blob) * 3 // 4} bytes, uri={uri}]"
    try:
        raw_bytes = base64.b64decode(blob)
    except (TypeError, ValueError) as exc:
        logger.warning("MCP embedded resource decode failed (%s): %s", mime or uri, exc)
        return f"[MCP embedded resource could not be decoded: {mime or uri}]"
    if len(raw_bytes) > _MCP_RESOURCE_MAX_BYTES:
        return f"[MCP embedded resource too large to cache: {len(raw_bytes)} bytes, uri={uri}]"
    try:
        from gateway.platforms.base import cache_document_from_bytes

        path = cache_document_from_bytes(raw_bytes, _mcp_resource_filename(uri, mime))
    except ImportError:
        logger.debug("MCP resource caching skipped — gateway.platforms.base unavailable")
        return f"[MCP embedded resource received ({len(raw_bytes)} bytes, {mime or 'unknown type'}) but document cache unavailable in this process]"
    except Exception as exc:
        logger.warning("MCP embedded resource cache failed: %s", exc)
        return f"[MCP embedded resource could not be cached: {mime or uri}]"
    detail = mime or "unknown type"
    return f"[MCP resource saved to {path} ({detail}, {len(raw_bytes)} bytes) — read it with read_file or terminal tools]"


# ---------------------------------------------------------------------------
# Remote MCP URL validation
# ---------------------------------------------------------------------------


class InvalidMcpUrlError(ValueError):
    """Raised when a remote MCP server's ``url`` cannot be parsed as http(s)://.

    Validated once at startup so we fail fast with a clear message instead of
    burning through the reconnect-backoff loop on every attempt.  (Ported from
    anomalyco/opencode#25019.)
    """


class NonMcpEndpointError(ConnectionError):
    """Raised when an HTTP MCP URL serves a non-MCP response.

    A genuine MCP Streamable-HTTP endpoint answers with ``application/json``
    or ``text/event-stream``.  Anything else on a 2xx response (typically
    ``text/html`` from a web-app root) means the configured ``url`` points at
    the wrong place.  This is non-retryable: every attempt returns the same
    page, so the reconnect-backoff loop is skipped and the server is reported
    failed immediately with an actionable message.

    Subclasses :class:`ConnectionError` so callers that only catch the broad
    class still treat it as a connection problem.
    """


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Extract the root-cause exception from anyio TaskGroup wrappers.

    The MCP SDK uses anyio task groups, which wrap errors in
    ``BaseExceptionGroup`` / ``ExceptionGroup``. Their ``str()`` is opaque —
    "unhandled errors in a TaskGroup (1 sub-exception)" — so log sites must
    unwrap to surface the real cause (e.g. ``BrokenPipeError`` on a dead
    stdio pipe, "401 Unauthorized" on an auth failure).

    Adapted from :func:`hermes_cli.mcp_config._unwrap_exception_group` with
    two extra behaviours needed on the runtime path:

    - **Fatal leaves re-raise.** A ``KeyboardInterrupt`` / ``SystemExit``
      anywhere in the (possibly nested) group must propagate to the
      interpreter, never be flattened into a loggable error.
    - **Prefer non-cancellation leaves.** When a group carries both a real
      error and the ``CancelledError``s that anyio cancellation sprays across
      sibling tasks, the real error is the root cause worth logging.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        fatal, _rest = exc.split((KeyboardInterrupt, SystemExit))
        if fatal is not None:
            # Surface the fatal signal itself, not the wrapper.
            leaf: BaseException = fatal
            while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
                leaf = leaf.exceptions[0]
            raise leaf
        # Prefer a non-cancellation leaf when one exists: cancellation
        # noise from sibling tasks should not mask the real error.
        chosen = exc.exceptions[0]
        for sub in exc.exceptions:
            if not _contains_only_cancellation(sub):
                chosen = sub
                break
        exc = chosen
    return exc


def _contains_only_cancellation(exc: BaseException) -> bool:
    """True if ``exc`` is (or a group containing only) CancelledError."""
    if isinstance(exc, BaseExceptionGroup):
        return all(_contains_only_cancellation(sub) for sub in exc.exceptions)
    return isinstance(exc, asyncio.CancelledError)


def _classify_mcp_failure(exc: BaseException) -> str:
    """Classify an MCP connection failure as ``'permanent'`` or ``'transient'``.

    Permanent failures are deterministic — every retry hits the same wall, so
    burning the retry ladder (and log lines) on them is pure noise; ``run()``
    parks them immediately:

    - auth failures (401/403) — need new credentials, not a retry;
    - :class:`NonMcpEndpointError` — the URL serves a web page, not MCP;
    - :class:`InvalidMcpUrlError` — unusable config;
    - ``FileNotFoundError`` / ``ENOENT`` — the stdio command doesn't exist.

    Everything else (network blips, EOF, ``ClosedResourceError``, transport
    TaskGroup drops, timeouts) is transient and keeps the normal
    retry-with-backoff ladder.
    """
    root = _unwrap_exception_group(exc)
    if _is_auth_error(root):
        return "permanent"
    if isinstance(root, (NonMcpEndpointError, InvalidMcpUrlError)):
        return "permanent"
    # Stdio command missing: FileNotFoundError, or an OSError carrying ENOENT.
    if isinstance(root, FileNotFoundError):
        return "permanent"
    if isinstance(root, OSError) and getattr(root, "errno", None) == errno.ENOENT:
        return "permanent"
    # httpx.HTTPStatusError with 401/403 that _is_auth_error's type-gate
    # missed (e.g. auth types not importable in this environment).
    status = getattr(getattr(root, "response", None), "status_code", None)
    if status in (401, 403):
        return "permanent"
    return "transient"


def _validate_remote_mcp_url(server_name: str, url: Any) -> str:
    """Return the URL as a string if it's a valid http(s) remote MCP URL.

    Raises :class:`InvalidMcpUrlError` otherwise with a message naming the
    offending server, so users can spot the bad entry in their config.

    Accepts:
    - ``http://host`` / ``https://host`` with optional port, path, query
    - IPv4, IPv6 (bracketed), DNS hostnames

    Rejects:
    - Non-string values (``None``, dicts, ints)
    - Missing scheme (``example.com/mcp``)
    - Non-http(s) schemes (``file://``, ``ws://``, ``stdio:`` — stdio servers
      use the ``command`` key, not ``url``)
    - Empty host (``http://``, ``https:///path``)
    """
    if not isinstance(url, str):
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': expected a string, got "
            f"{type(url).__name__}"
        )
    stripped = url.strip()
    if not stripped:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': empty url"
        )
    try:
        parsed = urlparse(stripped)
    except Exception as exc:  # urlparse is very permissive — belt and braces
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': {stripped!r} ({exc})"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': scheme must be http or "
            f"https, got {parsed.scheme!r} ({stripped!r})"
        )
    if not parsed.netloc:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing host ({stripped!r})"
        )
    # ``urlparse`` accepts ``http://:8080`` (empty host, explicit port).
    # Reject that — we need a real host.
    if not parsed.hostname:
        raise InvalidMcpUrlError(
            f"Invalid MCP URL for '{server_name}': missing hostname "
            f"({stripped!r})"
        )
    return stripped


def _resolve_client_cert(server_name: str, config: dict):
    """Resolve the ``client_cert`` / ``client_key`` config for mTLS.

    Returns whatever ``httpx``'s ``cert=`` parameter accepts, or ``None`` when
    no client certificate is configured:

      - ``None`` if neither ``client_cert`` nor ``client_key`` is set.
      - A single absolute path string if ``client_cert`` is a string and
        ``client_key`` is unset (PEM file with cert + key combined).
      - A ``(cert_path, key_path)`` tuple when both are set, or when
        ``client_cert`` is a 2-element list/tuple.
      - A ``(cert_path, key_path, password)`` tuple when ``client_cert`` is
        a 3-element list/tuple — the third element is the key passphrase.

    User paths support ``~`` expansion. Missing files raise ``FileNotFoundError``
    with a server-scoped message so the failure surfaces as a clear setup
    error rather than an opaque TLS handshake error.
    """
    raw_cert = config.get("client_cert")
    raw_key = config.get("client_key")

    if raw_cert is None and raw_key is None:
        return None

    def _expand(path: Any, label: str) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"MCP server '{server_name}': {label} must be a non-empty "
                f"string path (got {type(path).__name__})"
            )
        expanded = os.path.expanduser(path.strip())
        if not os.path.isfile(expanded):
            raise FileNotFoundError(
                f"MCP server '{server_name}': {label} not found at "
                f"{expanded!r}"
            )
        return expanded

    # Tuple/list form for client_cert — (cert, key) or (cert, key, password).
    if isinstance(raw_cert, (list, tuple)):
        if raw_key is not None:
            raise ValueError(
                f"MCP server '{server_name}': specify either client_cert as "
                f"a list [cert, key] OR client_cert + client_key, not both"
            )
        if len(raw_cert) == 2:
            cert_path = _expand(raw_cert[0], "client_cert[0]")
            key_path = _expand(raw_cert[1], "client_cert[1]")
            return (cert_path, key_path)
        if len(raw_cert) == 3:
            cert_path = _expand(raw_cert[0], "client_cert[0]")
            key_path = _expand(raw_cert[1], "client_cert[1]")
            password = raw_cert[2]
            if not isinstance(password, str):
                raise ValueError(
                    f"MCP server '{server_name}': client_cert[2] (key "
                    f"passphrase) must be a string"
                )
            return (cert_path, key_path, password)
        raise ValueError(
            f"MCP server '{server_name}': client_cert list form must have 2 "
            f"or 3 elements (got {len(raw_cert)})"
        )

    # String form for client_cert.
    cert_path = _expand(raw_cert, "client_cert")
    if raw_key is not None:
        key_path = _expand(raw_key, "client_key")
        return (cert_path, key_path)
    # Single combined PEM file (cert + key in one file).
    return cert_path


def _resolve_identity_header(server_name: str, config: dict):
    """Resolve the optional per-server ``identity_header`` config.

    Config shape (in the server's ``mcp_servers`` entry)::

        identity_header:
          name: "X-User-Id"
          value_from: "static"   # or "profile"; default: static
          value: "alice"         # required when value_from is static

    Returns a ``(header_name, header_value)`` tuple, or ``None`` when the
    key is unset or invalid. Invalid configs warn and are ignored — an
    identity header must never break the server connection. ``profile``
    mode resolves the value to the active Hermes profile name once at
    connect time; there is no per-call mutation.
    """
    raw = config.get("identity_header")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "MCP server '%s': identity_header must be a mapping with "
            "'name' and 'value'/'value_from' keys (got %s) — ignoring",
            server_name, type(raw).__name__,
        )
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning(
            "MCP server '%s': identity_header requires a non-empty "
            "'name' — ignoring", server_name,
        )
        return None
    value_from = (raw.get("value_from") or "static").strip().lower()
    if value_from == "static":
        value = raw.get("value")
        if not isinstance(value, str) or not value.strip():
            logger.warning(
                "MCP server '%s': identity_header with value_from: static "
                "requires a non-empty string 'value' — ignoring",
                server_name,
            )
            return None
        return (name.strip(), value)
    if value_from == "profile":
        from hermes_cli.profiles import get_active_profile_name
        return (name.strip(), get_active_profile_name())
    logger.warning(
        "MCP server '%s': identity_header value_from must be 'static' or "
        "'profile' (got %r) — ignoring", server_name, value_from,
    )
    return None


def _apply_identity_header(server_name: str, config: dict, headers: dict) -> dict:
    """Merge the resolved identity header into ``headers`` (in place).

    An explicit per-server ``headers`` entry with the same name (any
    casing) wins — the identity header never silently overrides user
    config.
    """
    resolved = _resolve_identity_header(server_name, config)
    if resolved is None:
        return headers
    name, value = resolved
    if any(key.lower() == name.lower() for key in headers):
        logger.debug(
            "MCP server '%s': identity_header '%s' already set via explicit "
            "headers config — keeping the explicit value", server_name, name,
        )
        return headers
    headers[name] = value
    return headers


def _make_redirect_header_stripper(
    original_url,
    *,
    strict: bool = False,
    configured_header_names: "set[str] | frozenset[str]" = frozenset(),
):
    """Build an httpx response hook that guards cross-origin redirects.

    Always strips ``Authorization`` when a redirect leaves the original
    origin. When *strict* is true (portable Agent Plugins v1 packages with
    ``strict_redirect_headers``), every *configured* header (lowercase names
    in *configured_header_names*) is stripped as well — the v1 spec forbids
    forwarding package-configured headers to a different origin without
    explicit user authorization.
    """

    async def _strip_on_cross_origin_redirect(response):
        if response.is_redirect and response.next_request:
            target = response.next_request.url
            if (target.scheme, target.host, target.port) != (
                original_url.scheme, original_url.host, original_url.port,
            ):
                response.next_request.headers.pop("authorization", None)
                response.next_request.headers.pop("Authorization", None)
                if strict:
                    for _name in configured_header_names:
                        while _name in response.next_request.headers:
                            del response.next_request.headers[_name]

    return _strip_on_cross_origin_redirect
'''

EXPORTED_NAMES = ('asyncio', 'contextvars', 'concurrent', 'errno', 'fnmatch', 'inspect', 'json', 'logging', 'math', 'os', 'random', 're', 'shutil', 'sys', 'threading', 'time', 'asynccontextmanager', 'SimpleNamespace', 'Callable', 'datetime', 'Any', 'Coroutine', 'Dict', 'List', 'Optional', 'Set', 'Tuple', 'urlparse', 'tool_error', 'strip_unicode_tags', 'logger', '_MCP_HARD_RESULT_CAP_CHARS', '_truncate_mcp_text_result', '_OSV_MALWARE_CHECK_TIMEOUT_S', '_mcp_stderr_log_fh', '_mcp_stderr_log_lock', '_get_mcp_stderr_log', '_write_stderr_log_header', '_MCP_AVAILABLE', '_MCP_HTTP_AVAILABLE', '_MCP_NEW_HTTP', '_MCP_LEGACY_HTTP', '_MCP_SAMPLING_TYPES', '_MCP_NOTIFICATION_TYPES', '_MCP_ELICITATION_TYPES', '_MCP_MESSAGE_HANDLER_SUPPORTED', '_MCP_LOGGING_CALLBACK_SUPPORTED', 'sse_client', 'LATEST_PROTOCOL_VERSION', 'LATEST_HANDSHAKE_VERSION', 'ClientSession', '_MCP_SDK_IMPORT_ATTEMPTED', '_MCP_SDK_IMPORT_LOCK', '_MCP_SDK_LAZY_SYMBOLS', '__getattr__', '_ensure_mcp_sdk', '_SDK_HTTPX_MOD', 'sdk_httpx', '_MISSING', 'mcp_field', '_check_message_handler_support', '_check_logging_callback_support', '_MCP_LOG_LEVEL_MAP', '_DEFAULT_TOOL_TIMEOUT', '_resolve_tool_timeout', '_DEFAULT_CONNECT_TIMEOUT', '_MAX_RECONNECT_RETRIES', '_MAX_INITIAL_CONNECT_RETRIES', '_MAX_BACKOFF_SECONDS', '_PARKED_RETRY_INTERVAL', '_RECYCLED_RECONNECT_TIMEOUT', '_BACKOFF_JITTER', '_jittered', '_DEFAULT_KEEPALIVE_INTERVAL', '_MIN_KEEPALIVE_INTERVAL', '_MCP_LOOP_DRAIN_TIMEOUT', '_SAFE_ENV_KEYS', '_SAFE_ENV_KEYS_CASE_INSENSITIVE', '_CREDENTIAL_PATTERN', '_ENV_VAR_PATTERN', '_env_ref_name', '_workspace_folder', '_context_var_value', '_build_safe_env', '_MCP_OPAQUE_ASSIGNMENT_RE', '_sanitize_opaque_mcp_assignments', '_sanitize_error', '_exc_str', '_JSONRPC_METHOD_NOT_FOUND', '_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION', '_handshake_rejected_as_modern', '_is_method_not_found_error', '_MCP_INJECTION_PATTERNS', '_scan_mcp_description', '_prepend_path', '_MCP_LIST_MAX_PAGES', '_paginate_full_list', '_mcp_types', '_resolve_stdio_command', '_wrap_command_with_watchdog', '_is_reserved_mcp_meta_key', '_strip_reserved_meta_keys', '_sanitize_mcp_result_value', '_mcp_image_extension_for_mime_type', '_cache_mcp_image_block', '_MCP_RESOURCE_MAX_BYTES', '_MCP_RESOURCE_MAX_B64_CHARS', '_mcp_resource_filename', '_cache_mcp_audio_block', '_render_mcp_resource_block', 'InvalidMcpUrlError', 'NonMcpEndpointError', '_unwrap_exception_group', '_contains_only_cancellation', '_classify_mcp_failure', '_validate_remote_mcp_url', '_resolve_client_cert', '_resolve_identity_header', '_apply_identity_header', '_make_redirect_header_stripper')
SOURCE_PATH = Path(__file__)

def install(namespace: dict[str, object]) -> None:
    filename = str(SOURCE_PATH)
    linecache.cache[filename] = (
        len(_SOURCE), None, _SOURCE.splitlines(True), filename
    )
    exec(compile(_SOURCE, filename, "exec"), namespace, namespace)
