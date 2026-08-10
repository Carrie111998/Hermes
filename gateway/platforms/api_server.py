"""
OpenAI-compatible API server platform adapter.

Exposes an HTTP server with endpoints:
- POST /v1/chat/completions        â€” OpenAI Chat Completions format (stateless; opt-in session continuity via X-Hermes-Session-Id header; opt-in long-term memory scoping via X-Hermes-Session-Key header)
- POST /v1/responses               â€” OpenAI Responses API format (stateful via previous_response_id; X-Hermes-Session-Key supported)
- GET  /v1/responses/{response_id} â€” Retrieve a stored response
- DELETE /v1/responses/{response_id} â€” Delete a stored response
- GET  /v1/models                  â€” lists hermes-agent and any configured model_routes aliases
- GET  /v1/capabilities            â€” machine-readable API capabilities for external UIs
- GET  /api/sessions               â€” list client-visible Hermes sessions
- POST /api/sessions               â€” create an empty Hermes session
- GET/PATCH/DELETE /api/sessions/{session_id} â€” read/update/delete a session
- GET  /api/sessions/{session_id}/messages â€” read session message history
- POST /api/sessions/{session_id}/fork â€” branch a session using SessionDB lineage
- POST /api/sessions/{session_id}/chat[/stream] â€” chat with a persisted session
- POST /v1/runs                    â€” start a run, returns run_id immediately (202)
- GET  /v1/runs/{run_id}           â€” retrieve current run status
- GET  /v1/runs/{run_id}/events    â€” SSE stream of structured lifecycle events
- POST /v1/runs/{run_id}/approval â€” resolve a pending run approval
- POST /v1/runs/{run_id}/stop       â€” interrupt a running agent
- GET  /health                     â€” health check
- GET  /health/detailed            â€” rich status for cross-container dashboard probing

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat,
AnythingLLM, NextChat, ChatBox, etc.) can connect to hermes-agent
through this adapter by pointing at http://localhost:8642/v1 and
authenticating with API_SERVER_KEY.

When ``gateway.multiplex_profiles`` is on, the default profile owns this
listener and secondary profiles are reached via a URL prefix â€” same contract
as the webhook adapter:

    GET  /p/<profile>/v1/models
    POST /p/<profile>/v1/chat/completions
    ...

Requires:
- aiohttp (already available in the gateway)
"""

import asyncio
import errno
import hashlib
import hmac
import itertools
import json
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Sentinel returned by _resolve_request_profile when a /p/<profile>/ prefix
# names a profile this gateway does not serve (â†’ 404). Distinct from None
# (no prefix / multiplexing off â†’ handle as the default profile).
_PROFILE_REJECTED = object()

# Profile selected by the /p/<profile>/ URL prefix for the current request.
# Set by the profile-prefix middleware; read by handlers / _run_agent.
_api_request_profile: ContextVar[Optional[str]] = ContextVar(
    "api_server_request_profile", default=None
)

def _approval_event_choices(*, smart_denied: bool, allow_permanent: bool) -> list[str]:
    if smart_denied:
        return ["once", "deny"]
    return ["once", "session", "always", "deny"] if allow_permanent else ["once", "session", "deny"]


try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    MEDIA_TAG_CLEANUP_RE,
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
    validate_media_delivery_path,
)
from agent.redact import redact_sensitive_text
from agent.interrupt_compat import request_hard_interrupt
from gateway.readiness import collect_runtime_readiness

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Secondary profiles construct their adapters under a profile secret
    scope -- the scope is authoritative and a scoped miss returns ``default``
    (no cross-profile borrow from ``os.environ``, which may hold another
    profile's value). The DEFAULT profile's adapter constructs and sends
    *unscoped* under multiplexing, where a bare ``get_secret`` would raise
    ``UnscopedSecretError`` and crash this path; there ``os.environ`` is that
    profile's own value, so fall back to it. Same pattern as the Slack
    ``SLACK_APP_TOKEN`` read (#59739) and
    ``gateway/platforms/whatsapp_common.py::_get_wsecret``.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


logger = logging.getLogger(__name__)


def _hermes_version() -> str:
    """Return the canonical Hermes Agent version string.

    ``hermes_cli.__version__`` is the runtime source of truth used by the CLI,
    dashboard, portal tags, and release script. Prefer it over installed
    distribution metadata because editable/source checkouts can retain stale
    ``hermes_agent-*.dist-info`` after a source update until the environment is
    reinstalled. Never raises â€” a version probe must not be able to break the
    health endpoint.
    """
    try:
        from hermes_cli import __version__

        return __version__
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version("hermes-agent")
    except Exception:
        return "dev"


# Default settings
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
MAX_STORED_RESPONSES = 100
MAX_REQUEST_BYTES = 10_000_000  # 10 MB â€” accommodates long agent conversations with tool calls
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 64 KB cap for normalized content parts
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array
RESPONSES_AUTO_TRUNCATION_HISTORY_LIMIT = 100
_COMPRESSED_SUMMARY_METADATA_KEY = "_compressed_summary"


class ThreadSafeAsyncQueue(asyncio.Queue):
    """An ``asyncio.Queue`` that a non-loop thread can push into safely.

    The SSE writers' streaming loops used to bridge a plain ``queue.Queue``
    into the event loop via ``await loop.run_in_executor(None, lambda:
    stream_q.get(timeout=0.5))`` inside a ``while True`` poll â€” a thread-pool
    round trip on every 0.5s tick even when idle, plus up to 500ms of tail
    latency between a delta landing in the queue and it reaching the
    response. ``run_conversation`` itself runs on a worker thread (via
    ``loop.run_in_executor``), so its ``stream_delta_callback`` closures
    (``_on_delta`` etc.) call ``put_threadsafe`` from off the loop thread;
    the consumer side just does a plain ``await queue.get()``/
    ``asyncio.wait_for(queue.get(), timeout=...)``, woken immediately by
    ``call_soon_threadsafe`` instead of polling.
    """

    def put_threadsafe(self, item, *, loop: asyncio.AbstractEventLoop = None) -> None:
        (loop or self._loop_ref).call_soon_threadsafe(self.put_nowait, item)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Always constructed inside a running async handler (the SSE
        # request handlers below), so get_running_loop() is safe here.
        self._loop_ref = asyncio.get_running_loop()


def _sse_frame(data: Any, *, event: str = None, ensure_ascii: bool = True) -> bytes:
    """Encode one SSE frame: optional ``event:`` line, then ``data: <json>\n\n``.

    The single source of truth for SSE frame serialization across every
    streaming writer in this module â€” ``_write_sse_chat_completion`` (the
    five call sites it was first extracted from), ``_write_sse_responses``'s
    inner ``_write_event`` closure, and the ``/v1/runs`` event stream.  All
    three used the identical ``json.dumps(data)`` / ``json.dumps(...,
    ensure_ascii=False)`` + ``"\\ndata: ...\\n\\n"`` shape; routing them all
    through here keeps the on-the-wire format in exactly one place.

    ``ensure_ascii`` defaults to ``True``, byte-identical to a bare
    ``json.dumps(data)``.  Callers that must preserve raw non-ASCII bytes on
    the wire (the Responses-API writer historically used
    ``ensure_ascii=False``) pass ``ensure_ascii=False`` explicitly â€” the
    option exists so every writer shares one helper without changing any
    existing byte stream.
    """
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data, ensure_ascii=ensure_ascii)}\n\n".encode()


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """Parse a listen port without letting malformed env/config values crash startup."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like API payload values.

    External clients should send real JSON booleans, but some OpenAI-compatible
    frontends and middleware serialize flags like ``stream`` as strings.  Using
    Python truthiness on those values misroutes requests because ``"false"`` is
    still truthy.  Treat only explicit bool-ish scalars as booleans; everything
    else falls back to the caller's default.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


_REQUEST_OPTION_MISSING = object()
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
_RUNTIME_AGENT_OVERRIDE_KEYS = (
    "api_key",
    "base_url",
    "provider",
    "api_mode",
    "command",
    "args",
    "credential_pool",
    "max_tokens",
)


def _clean_request_string(value: Any) -> Optional[str]:
    """Return a stripped request string, or None for absent/non-string values."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _request_reasoning_config(model_options: Any) -> Optional[Dict[str, Any]]:
    """Translate browser/API model_options into AIAgent reasoning_config.

    The browser extension sends both a structured ``reasoning`` object and a
    compatibility ``reasoning_effort`` scalar.  Keep this parser permissive so
    older clients can send either shape, but ignore unknown effort values rather
    than raising on a chat request.
    """
    if not isinstance(model_options, dict):
        return None

    reasoning = model_options.get("reasoning")
    enabled: Any = None
    effort: Any = model_options.get("reasoning_effort")
    if isinstance(reasoning, dict):
        enabled = reasoning.get("enabled")
        effort = reasoning.get("effort", effort)

    effort_norm = str(effort).strip().lower() if effort is not None else ""
    if enabled is False or effort_norm == "none":
        return {"enabled": False}
    if effort_norm in _REASONING_EFFORTS and effort_norm != "none":
        return {"enabled": True, "effort": effort_norm}
    if enabled is True:
        return {"enabled": True}
    return None


def _request_service_tier(model_options: Any) -> Any:
    """Return a per-request service_tier override or _REQUEST_OPTION_MISSING."""
    if not isinstance(model_options, dict):
        return _REQUEST_OPTION_MISSING
    if "service_tier" in model_options:
        raw_tier = model_options.get("service_tier")
        if raw_tier is None:
            return None
        if isinstance(raw_tier, str):
            return raw_tier.strip() or None
        return raw_tier
    if "fast" in model_options:
        return "priority" if _coerce_request_bool(model_options.get("fast"), default=False) else None
    return _REQUEST_OPTION_MISSING


def _apply_runtime_agent_overrides(
    runtime_kwargs: Dict[str, Any], overrides: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Merge resolved provider/runtime fields into ``runtime_kwargs`` in place."""
    if not isinstance(overrides, dict):
        return runtime_kwargs
    for key in _RUNTIME_AGENT_OVERRIDE_KEYS:
        if key not in overrides:
            continue
        value = overrides.get(key)
        if value is None:
            continue
        runtime_kwargs[key] = list(value) if key == "args" and isinstance(value, (list, tuple)) else value
    return runtime_kwargs


def _resolve_request_runtime_agent_kwargs(provider: str, target_model: Optional[str] = None) -> Dict[str, Any]:
    """Resolve runtime kwargs for a one-request provider override.

    This mirrors gateway.run._resolve_runtime_agent_kwargs(), but accepts an
    explicit provider/model so an API caller can use the same authenticated
    provider catalog as the TUI without mutating config.yaml.
    """
    from hermes_cli.runtime_provider import resolve_runtime_provider, format_runtime_provider_error, _get_model_config

    try:
        runtime = resolve_runtime_provider(requested=provider, target_model=target_model)
    except Exception as exc:
        raise RuntimeError(format_runtime_provider_error(exc)) from exc

    model_cfg = _get_model_config()
    max_tokens = None
    env_max_tokens = os.environ.get("HERMES_MAX_TOKENS")
    if env_max_tokens:
        try:
            max_tokens = int(env_max_tokens)
        except (ValueError, TypeError):
            max_tokens = None
    elif isinstance(model_cfg, dict):
        cfg_max_tokens = model_cfg.get("max_tokens")
        if isinstance(cfg_max_tokens, int):
            max_tokens = cfg_max_tokens
    if max_tokens is None:
        runtime_max_tokens = runtime.get("max_output_tokens")
        if isinstance(runtime_max_tokens, int) and runtime_max_tokens > 0:
            max_tokens = runtime_max_tokens

    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
        "credential_pool": runtime.get("credential_pool"),
        "max_tokens": max_tokens,
    }


def _request_agent_overrides(
    body: Any,
    *,
    virtual_model: Optional[str] = None,
    allow_bare_model: bool = True,
) -> Dict[str, Any]:
    """Extract per-request model/pro×M¶ïÛh‘éì¶»§q«^t\WÜÙ\™\—Ü[—ÜİÜ‚ˆ
B‚ˆ™]\›ˆÙX‹šœÛÛ—Ü™\ÜÛœÙJÈœ[—ÚYˆ[—ÚYœİ]\ÈˆœİÜ[™ÈŸJB‚ˆ\Ş[˜ÈYˆÜİÙY\ÛÜœ[™YÜ[œÊÙ[ŠHOˆ›Û™N‚ˆˆˆ”\š[ÙXØ[H^\™H˜[œÜÜY™™\œÈ[™\›Z[˜[İ]\È™XÛÜ™Ëˆˆˆ‚ˆÚ[HYN‚ˆ]ØZ]\Ş[˜Ú[ËœÛY\
Œ
BˆÙ[‹—ÜİÙY\ÛÜœ[™YÜ[œ×ÛÛ˜ÙJ[YK[YJ
JB‚ˆYˆÜİÙY\ÛÜœ[™YÜ[œ×ÛÛ˜ÙJÙ[‹›İÎˆÜ[Û˜[Ù›Ø]HH›Û™JHOˆ›Û™N‚ˆˆˆ‘^\™HÛÔÑHY™™\œÈÚ]İ]™X][™È˜[œÜÜYÙH\È[ˆYÙKˆˆˆ‚ˆYˆ›İÈ\È›Û™N‚ˆ›İÈH[YK[YJ
Bˆİ[HHÂˆ[—ÚYˆ›Üˆ[—ÚYÜ™X]YØ][ˆ\İ
Ù[‹—Ü[—Üİ™X[\×ØÜ™X]Yš][\Ê
JBˆYˆ›İÈHÜ™X]YØ]ˆÙ[‹—Ô•S—ÔÕ‘PSWÕˆ[™[—ÚY›İ[ˆÙ[‹—Ü[—Üİ™X[WÜİXœØÜšX™\œÂˆBˆ›Üˆ[—ÚY[ˆİ[N‚ˆÙÙÙ\‹™XYÊ–Ø\WÜÙ\™\—HİÙY\[™È^\™Y[ˆ˜[œÜÜ	\È‹[—ÚY
Bˆ\ÚÈHÙ[‹—ØXİ]™WÜ[—İ\ÚÜË™Ù]
[—ÚY
Bˆ\Ú×ÙÛ™HH\ÚÈ\È›Û™HÜˆ\ÚË™Û™J
BˆYˆ\Ú×ÙÛ™N‚ˆN‚ˆœ›ÛHÛÛË˜\›İ˜[[\Ü[œ™YÚ\İ\—ÙØ]]Ø^WÛ›İYB‚ˆ\›İ˜[ÜÙ\ÜÚ[Û—ÚÙ^HHÙ[‹—Ü[—Ø\›İ˜[ÜÙ\ÜÚ[ÛœË™Ù]
[—ÚY
BˆYˆ\›İ˜[ÜÙ\ÜÚ[Û—ÚÙ^N‚ˆ[œ™YÚ\İ\—ÙØ]]Ø^WÛ›İYJ\›İ˜[ÜÙ\ÜÚ[Û—ÚÙ^JBˆ^Ù\^Ù\[Û‚ˆ\ÜÂˆÈH˜[œÜÜ[Ø^\È›İ[™ÈY™™\š[™Ëˆ]™HÛÛ›Ûİ]H\ÂˆÈ[™\[™[[™İ\š]™\È[[H^Xİ]Ü‹X˜XÚÙY\ÚÈ™]\›œË‚ˆÙ[‹—Ü[—Üİ™X[\ËœÜ
[—ÚY›Û™JBˆÙ[‹—Ü[—Üİ™X[\×ØÜ™X]YœÜ
[—ÚY›Û™JBˆYˆ\Ú×ÙÛ™N‚ˆÙ[‹—ØXİ]™WÜ[—ØYÙ[ËœÜ
[—ÚY›Û™JBˆÙ[‹—ØXİ]™WÜ[—İ\ÚÜËœÜ
[—ÚY›Û™JBˆÙ[‹—Ü[—Ø\›İ˜[ÜÙ\ÜÚ[ÛœËœÜ
[—ÚY›Û™JBˆÙ[‹—ÜİÜ[™×Ü[—ÚYË™\ØØ\™
[—ÚY
B‚ˆİ[WÜİ]\Ù\ÈHÂˆ[—ÚYˆ›Üˆ[—ÚYİ]\È[ˆ\İ
Ù[‹—Ü[—Üİ]\Ù\Ëš][\Ê
JBˆYˆİ]\Ë™Ù]
œİ]\ÈŠH[ˆÈ˜ÛÛ\]Y‹™˜Z[Y‹˜Ø[˜Ù[YŸBˆ[™›İÈH›Ø]
İ]\Ë™Ù]
\]YØ]‹
HÜˆ
HˆÙ[‹—Ô•S—ÔÕUT×ÕˆBˆ›Üˆ[—ÚY[ˆİ[WÜİ]\Ù\Î‚ˆÙ[‹—Ü[—Üİ]\Ù\ËœÜ
[—ÚY›Û™JB‚ˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKBˆÈ˜\ÙT]›Ü›PY\\ˆ[\™˜XÙBˆÈKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKB‚ˆYˆØ\WÚÙ^WÜ\ÜÙ\×Üİ\\ÙİX\™
Ù[ŠHOˆ›ÛÛ‚ˆˆˆ”™]\›ˆYHÚ[ˆTWÔÑT•‘T—ÒÑVH\È™\Ù[[™İ›Û™È[›İYÚÈİ\ˆˆˆ‚ˆYˆ›İÙ[‹—Ø\WÚÙ^N‚ˆÙÙÙ\‹™\œ›ÜŠˆ–É\×H™Y\Ú[™ÈÈİ\ˆTWÔÑT•‘T—ÒÑVH\È™\]Z\™Y›ÜˆHTHÙ\™\‹‚ˆš[˜ÛY[™ÈÛÜ˜XÚË[Û›Hš[™ÈÛˆ	\Ëˆ‹ˆÙ[‹›˜[YKÙ[‹—ÚÜİˆ
Bˆ™]\›ˆ˜[ÙB‚ˆN‚ˆœ›ÛH\›Y\×ØÛK˜]][\Ü\×İ\ØX›WÜÙXÜ™]ˆ^Ù\^Ù\[Ûˆ\È^Î‚ˆÈ˜Z[ÓÔÑQˆ\ÈİX\™\ÈHÛ›H[™È™]ÙY[ˆHİY\ÜØX›BˆÈÙ^H[™H\›Z[˜[XØ\X›H[™Ú[ÛÈHÚXÚÈÛİ[›İ™BˆÈ[ˆˆ]\İ›İ™\ÛÛ™HÈœİ\[]Ø^Hˆ8 %HØ[YHÜİ\™BˆÈÛÛËØÜ™Y[X[Ùš[\ËœHZÙ\ÈÚ[ˆ]È[K[\İØ[››İ™BˆÈÛÛœİ[Y‚ˆÙÙÙ\‹™\œ›ÜŠˆ–É\×H™Y\Ú[™ÈÈİ\ˆTWÔÑT•‘T—ÒÑVHİ™[™İÛİ[›İ™H‚ˆ™\šYšYY
	\Îˆ	\ÊK[™\È[™Ú[\Ü]Ú\È‚ˆ\›Z[˜[XØ\X›HYÙ[ÛÜšËˆ™\Z\ˆH[œİ[][Ûˆ™Y›Ü™H‚ˆœİ\[™ÈHTHÙ\™\ˆÛˆ	\Ëˆ‹ˆÙ[‹›˜[YK\J^ÊK—×Û˜[YW×Ë^ËÙ[‹—ÚÜİˆ
Bˆ™]\›ˆ˜[ÙB‚ˆYˆ›İ\×İ\ØX›WÜÙXÜ™]
Ù[‹—Ø\WÚÙ^KZ[—Û[™İLMŠN‚ˆÙÙÙ\‹™\œ›ÜŠˆ–É\×H™Y\Ú[™ÈÈİ\ˆTWÔÑT•‘T—ÒÑVH\ÈH‚ˆœXÙZÛ\ˆÜˆÛÈÚÜ
MˆÚ\œÊKˆ\È[™Ú[‚ˆ™\Ü]Ú\È\›Z[˜[XØ\X›HYÙ[ÛÜšÈ8 %HİY\ÜØX›H‚ˆšÙ^H\È™[[İHÛÙH^Xİ][Û‹ˆÙ[™\˜]HHİ›Û™ÈÙXÜ™]‚ˆŠK™ËˆÜ[œÜÛ˜[™Z^Ì˜
H[™Ù]TWÔÑT•‘T—ÒÑVH‚ˆ˜™Y›Ü™Hİ\[™ÈHTHÙ\™\ˆÛˆ	\Ëˆ‹ˆÙ[‹›˜[YKÙ[‹—ÚÜİˆ
Bˆ™]\›ˆ˜[ÙBˆ™]\›ˆYB‚ˆ\Ş[˜ÈYˆÛÛ›™Xİ
Ù[‹
‹\×Ü™XÛÛ›™Xİˆ›ÛÛH˜[ÙJHOˆ›ÛÛ‚ˆˆˆ”İ\HZ[ÚÙXˆÙ\™\‹ˆˆˆ‚ˆYˆ›İRSÒĞURSP“N‚ˆÙÙÙ\‹Ø\›š[™Ê–É\×HZ[Ú›İ[œİ[Y‹Ù[‹›˜[YJBˆ™]\›ˆ˜[ÙB‚ˆYˆ›İÙ[‹—Ø\WÚÙ^WÜ\ÜÙ\×Üİ\\ÙİX\™

N‚ˆÈH™Z™XİYTWÔÑT•‘T—ÒÑVH\ÈHÛÛ™šYİ\˜][Ûˆ\œ›Ü‹›İBˆÈ˜[œÚY[›\8 %HÙ^HÚ[›İ™XÛÛYH˜[YÛˆ]ÈİÛ‹ˆBˆÈ˜\™H™]\›ˆ˜[ÙXXZÙ\ÈH™XÛÛ›™XİØ]Ú\ˆ[‚ˆÈØ]]Ø^Kœ[ˆ™X]]\È™]XX›H[™ÛÜ›Ü™]™\ˆ]BˆÈ˜XÚÛÙ™ˆØ\™KZ[œİ[X][™ÈHY\\ˆ
[™]ÂˆÈ™\ÜÛœÙTİÜ™HÜ[]HÛÛ›™Xİ[ÛŠH]™\H™]H
ÌÎÎˆLBˆÈXZÙYÛÛ›™Xİ[ÛœÈÈLˆ™Èİ™\ˆ‹H^\È[[SQ’SHÛÚÂˆÈHÚÛHØ]]Ø^HİÛŠKˆ›Û‹\™]XX›H›ÜÈ]œ›ÛHBˆÈ™XÛÛ›™Xİ]Y]YH8 %Ø[YH™X]Y[\ÈHÜXÛÛ™›XİİX\™ˆÈ
\WÜÙ\™\—ÜÜÚ[—İ\ÙJKˆHİX\™[™XYHÙÙÙYBˆÈÜXÚYšXÈ™Z™Xİ[Ûˆ™X\ÛÛˆ\İX›İ™K‚ˆÙ[‹—ÜÙ]Ù˜][Ù\œ›ÜŠˆ˜\WÜÙ\™\—ÚÙ^WÚ[˜[Y‹ˆTWÔÑT•‘T—ÒÑVHØ\È™Z™XİYHHİ\\İX\™
Z\ÜÚ[™Ë‚ˆœXÙZÛ\‹İÛÈÚÜÜˆİ™[™İ[™\šYšXX›H8 %ÙYHH‚ˆ™\œ›ÜˆÙÙÙYX›İ™JKˆÙ[™\˜]HHİ›Û™ÈÙXÜ™]
K™Ëˆ‚ˆ˜Ü[œÜÛ˜[™Z^Ì˜
KÙ]TWÔÑT•‘T—ÒÑVK[ˆ‚ˆ˜Ü]›Ü›H™\İ[YH\WÜÙ\™\˜ˆ‹ˆ™]XX›OQ˜[ÙKˆ
Bˆ™]\›ˆ˜[ÙB‚ˆN‚ˆ]ÜÈHÂˆ]Âˆ›Üˆ]È[ˆ
ˆÙ[‹—ÛXZÙWÜ›Ùš[WÜ™Yš^ÛZY]Ø\™J
KˆÛÜœ×ÛZY]Ø\™Kˆ›ÙWÛ[Z]ÛZY]Ø\™KˆÙXİ\š]WÚXY\œ×ÛZY]Ø\™Kˆ
BˆYˆ]È\È›İ›Û™BˆBˆÙ[‹—Ø\HÙX‹\XØ][ÛŠZY]Ø\™\Ï[]ÜËÛY[ÛX^ÜÚ^™OSPVÔ‘TUQTÕĞ–UTÊBˆ\ÜÙ\Ù[‹—Ø\\È›İ›Û™BˆÈ˜]]™H›İ]\È
È][\^ÜÏ›Ùš[O‹ø )ˆZ\œ›ÜœËˆØ[YH[™\œÎÂˆÈH›Ùš[K\™Yš^ZY]Ø\™H˜[Y]\ÈH™Yš^[™ØÛÜ\ÂˆÈÛÛ™šYËØÜ™Y[X[ÈÈ]›Ùš[HÚ[ˆ][\^[™È\ÈÛ‹‚ˆ›ÜˆY]Ù][™\ˆ[ˆÙ[‹—ÚÜ›İ]WİX›J
N‚ˆÙ[‹—Ø\œ›İ]\‹˜YÜ›İ]JY]Ù][™\ŠBˆÙ[‹—Ø\œ›İ]\‹˜YÜ›İ]JY]Ùˆ‹ÜŞŞÜ›Ùš[__^Ü]H‹[™\ŠBˆÈİÜ™HHY\\ˆY\ˆ˜]]™H›İ]\È\™H™YÚ\İ\™YˆØØ[\›Y\ËT™[^BˆÈ›Ûİİ˜\Ú[\È\ÙH\ÈÙ^H\ÈH™X]\™KY]Xİ[ÛˆÛÚÎÈ™YÚ\İ\š[™ÂˆÈ˜]]™H›İ]\Èš\œİ]ÈÜÙHÚ[\È›Ë[Ü[œİXYÙˆÚYİÚ[™ÈBˆÈ\İ™X[HÙ\ÜÚ[Û‹XÛÛ›Û[™\œË‚ˆÙ[‹—Ø\È˜\WÜÙ\™\—ØY\\ˆ—HHÙ[‚ˆYˆÙ[‹™Ø]]Ø^WÜ[›™\ˆ\È›İ›Û™N‚ˆÙ[‹—Ø\È™Ø]]Ø^WÜ[›™\ˆ—HHÙ[‹™Ø]]Ø^WÜ[›™\‚‚ˆÈİ\˜XÚÙÜ›İ[™İÙY\ÈÛX[ˆ\Üœ[™Y
[˜ÛÛœİ[YY
H[ˆİ™X[\ÂˆİÙY\İ\ÚÈH\Ş[˜Ú[Ë˜Ü™X]Wİ\ÚÊÙ[‹—ÜİÙY\ÛÜœ[™YÜ[œÊ
JBˆN‚ˆÙ[‹—Ø˜XÚÙÜ›İ[™İ\ÚÜË˜Y
İÙY\İ\ÚÊBˆ^Ù\\Q\œ›Ü‚ˆ\ÜÂˆYˆ\Ø]ŠİÙY\İ\ÚË˜YÙÛ™WØØ[˜XÚÈŠN‚ˆİÙY\İ\ÚË˜YÙÛ™WØØ[˜XÚÊÙ[‹—Ø˜XÚÙÜ›İ[™İ\ÚÜË™\ØØ\™
B‚ˆÈİYØ\›š[™ÈÚ[ˆH™]ÛÜšËXXØÙ\ÜÚX›HTHÙ\™\ˆ[œÈYØZ[œİ[‚ˆÈ[œØ[™›ŞYØØ[\›Z[˜[˜XÚÙ[™ˆHTHÙ\™\ˆØ[ˆš]™HBˆÈYÙ[	ÜÈ\›Z[˜[Ùš[HÛÛÈ\ÈHÜİ\Ù\ÈÛˆHX›XÈš[™ˆÈ]\ÈH^Xİİ\™˜XÙHH\›Y\ËL^HØ[\ZYÛˆX\ÙYÈÜš]BˆÈ‹Ëš\›Y\ËØÛÛ™šYËX[[[™[\œÚ\İ[˜ÙKˆØ[™›Ş[™È
ØÚÙ\ˆÂˆÈ™[[İH˜XÚÙ[™
HÛÛZ[œÈH›\İ˜Y]\ËˆØ\›‹Û‰İ™Y\ÙH8 %ˆÈHÜ\˜]ÜˆX^H]™H[ˆ^\›˜[š\™]Ø[Èİ›Û™ÈÙ^K‚ˆYˆ\×Û™]ÛÜš×ØXØÙ\ÜÚX›JÙ[‹—ÚÜİ
N‚ˆN‚ˆœ›ÛH\›Y\×ØÛK˜ÛÛ™šYÈ[\ÜØYØÛÛ™šYÈ\ÈÛØYØÙ™ÂˆØ˜XÚÙ[™H
ˆ

ÛØYØÙ™Ê
HÜˆßJK™Ù]
\›Z[˜[ŠHÜˆßJK™Ù]
ˆ˜˜XÚÙ[™‹›ØØ[‚ˆ
Bˆ
Bˆ^Ù\^Ù\[Û‚ˆØ˜XÚÙ[™H›ØØ[‚ˆYˆİŠØ˜XÚÙ[™
K›İÙ\Š
HOH›ØØ[‚ˆÙÙÙ\‹Ø\›š[™Êˆ–É\×HTHÙ\™\ˆ\È™]ÛÜšËXXØÙ\ÜÚX›H
	\ÊHS‘H‚ˆ\›Z[˜[˜XÚÙ[™\È	ÛØØ[	È
[œØ[™›ŞY
KˆYÙ[ÛÜšÈ‚ˆ™\Ü]ÚY›İYÚ\È[™Ú[[œÈ\ÈHÜİ\Ù\ˆ‚ˆÚ][\›Z[˜[Ùš[HXØÙ\ÜËˆİ›Û™ÛHÛÛœÚY\ˆH‚ˆœØ[™›ŞY˜XÚÙ[™
\›Z[˜[˜˜XÚÙ[™ˆØÚÙ\ŠH[™‚ˆ™š\™]Ø[[™È\ÈÜÈ\İY™]ÛÜšÜÈÛ›Kˆ‹ˆÙ[‹›˜[YKÙ[‹—ÚÜİˆ
B‚ˆÙ[‹—Ü[›™\ˆHÙX‹\[›™\ŠÙ[‹—Ø\
Bˆ]ØZ]Ù[‹—Ü[›™\‹œÙ]\

BˆÈš[™\™XİH[œİXYÙˆ›Øš[™ÈLËŒŒŒHš\œİ8 %HÛˆÈÚ[™ÛKY˜[Z[H™K\›Ø™H˜XÙYH™X[š[™[™™\ÜYBˆÈSQWÕĞRUÛØÚÙ]\Èš[ˆ\ÙHˆ
ÌLMÊK˜Z[[™ÈØ]]Ø^BˆÈ™\İ\È›Üˆ\ÈŒË‚ˆÂˆÈÓ×Ô‘UTÑPQˆ\È]›Ü›KY\[™[
Ø[YH˜][Û˜[H\ÈBˆÈÙXšÛÚÈY\\‹ÍMŠN‚ˆÈHXXÓÔÈ
”ÑÙ[X[XÜÊNˆÛÈÛØÚÙ]ÈÚ]Ó×Ô‘UTÑPQˆØ[‚ˆÈÚ[[HÜ]˜Y™šXÈÚ[H›İ™\ÜİXØÙ\ÜÈ8 %\ØX›K‚ˆÈH[^ˆÓ×Ô‘UTÑPQˆÛ›H\›Z]È™Xš[™[™È\İSQWÕĞRUˆÈ
HÙXÛÛ™]™H\İ[™\ˆ™YYÈÓ×Ô‘UTÑTÔ•™]™\ˆÙ]
KÛÂˆÈÙY\HY˜][
[˜X›Y
H›Üˆ[œİ[™\İ\™Xš[™Ë‚ˆÙ[‹—ÜÚ]HHÙX‹•ÔÚ]JˆÙ[‹—Ü[›™\‹ˆÙ[‹—ÚÜİˆÙ[‹—ÜÜˆ™]\ÙWØY™\ÜÏQ˜[ÙHYˆŞ\Ëœ]›Ü›HOH™\Ú[ˆˆ[ÙH›Û™Kˆ
BˆN‚ˆ]ØZ]Ù[‹—ÜÚ]Kœİ\

Bˆ^Ù\ÔÑ\œ›Üˆ\È^Î‚ˆ]ØZ]Ù[‹—Ü[›™\‹˜ÛX[\

BˆÙ[‹—Ü[›™\ˆH›Û™BˆÙ[‹—ÜÚ]HH›Û™BˆYˆÙ]]Š^Ë™\œ››È‹›Û™JHOH\œ››Ë‘PQ’S•TÑN‚ˆÈHÜÛÛ™›Xİ\ÈHÛÛ™šYİ\˜][Ûˆ\œ›Ü‹›İBˆÈ˜[œÚY[›\8 %[›İ\ˆ›ØÙ\ÜÈÛÈHÜ›Ü‚ˆÈ]ÈY™][YKˆH˜\™H™]\›ˆ˜[ÙXXZÙ\ÈBˆÈ™XÛÛ›™XİØ]Ú\ˆ[ˆØ]]Ø^Kœ[ˆ™X]]\È™]XX›BˆÈ[™ÛÜ›Ü™]™\ˆ]H˜XÚÛÙ™ˆØ\
ØœÙ\™YˆMM
ÂˆÈ™]šY\Èİ™\ˆH^\ÈXÜ›ÜÜÈ][K\›Ùš[HÙ]\È[ˆÈY˜][[™ÈÈHØ[YHÜÍLŒLÌŠKš[[™ÂˆÈ\œ›ÜœË›ÙÈ[™XZÚ[™ÈHY\\‰ÜÈ™\ÜÛœÙTİÜ™BˆÈ™ÈXXÚ™]Kˆ›Û‹\™]XX›H›ÜÈ]œ›ÛHBˆÈ™XÛÛ›™Xİ]Y]YNÈHÜ\˜]Üˆ™XÛİ™\œÈÚ]ˆÈÜ]›Ü›H™\İ[YH\WÜÙ\™\˜Y\ˆÚ[™Ú[™ÈHÜ‚ˆÙ[‹—ÜÙ]Ù˜][Ù\œ›ÜŠˆ˜\WÜÙ\™\—ÜÜÚ[—İ\ÙH‹ˆˆ”ÜÜÙ[‹—ÜÜH[™XYH[ˆ\ÙKˆÙ]‚ˆˆœ]›Ü›\Ë˜\WÜÙ\™\‹œÜ[ˆÛÛ™šYËX[[ÈH‚ˆˆ™Y™™\™[˜[YK[ˆÜ]›Ü›H™\İ[YH\WÜÙ\™\˜ˆ‹ˆ™]XX›OQ˜[ÙKˆ
BˆÙÙÙ\‹™\œ›ÜŠˆ–É\×HÛİ[›İš[™	\Î‰Yˆ	\ËˆÙ]HY™™\™[Ü[ˆ‚ˆ˜ÛÛ™šYËX[[ˆ]›Ü›\Ë˜\WÜÙ\™\‹œÜ‹ˆÙ[‹›˜[YKÙ[‹—ÚÜİÙ[‹—ÜÜ^Ëˆ
Bˆ™]\›ˆ˜[ÙB‚ˆÙ[‹—ÛX\š×ØÛÛ›™XİY

BˆÙÙÙ\‹š[™›Êˆ–É\×HTHÙ\™\ˆ\İ[š[™ÈÛˆ‹ËÉ\Î‰Y
[Ù[ˆ	\ÊH‹ˆÙ[‹›˜[YKÙ[‹—ÚÜİÙ[‹—ÜÜÙ[‹—Û[Ù[Û˜[YKˆ
Bˆ™]\›ˆYB‚ˆ^Ù\^Ù\[Ûˆ\ÈN‚ˆÙÙÙ\‹™\œ›ÜŠ–É\×H˜Z[YÈİ\THÙ\™\ˆ	\È‹Ù[‹›˜[YKJBˆ™]\›ˆ˜[ÙB‚ˆ\Ş[˜ÈYˆ\ØÛÛ›™Xİ
Ù[ŠHOˆ›Û™N‚ˆˆˆ”İÜHZ[ÚÙXˆÙ\™\ˆ[™™[X\ÙH[İÛ™Y™\Ûİ\˜Ù\Ë‚‚ˆÛÜÙ\ÈH™\ÜÛœÙTİÜ™HÔS]HÛÛ›™Xİ[Ûˆ[ˆY][ÛˆÈİÜ[™ÂˆHZ[ÚÙXˆÙ\™\‹ˆÚ]İ]\Ë]™\HY\\ˆ[œİ[˜ÙHXZÜÂˆˆš[H\ØÜš\ÜœÈ
H]X˜\ÙHš[H[™]ÈĞSÚYXØ\ŠH8 %Bˆ™XÛÛ›™XİÛÜ[ˆØ]]Ø^Kœ[˜ÛÛœİXİÈHœ™\ÚY\\ˆÛ‚ˆ]™\H™]KÛÈˆ™ËÜ™]H0åÈÌÈ˜XÚÛÙ™ˆØ\8¢bLˆ™ËÚİ\‹ÚXÚˆ^]\İÈHY˜][MŒ™[Z]Y\ˆŒLšÙˆ˜Z[Y™XÛÛ›™XİÂˆ[™\›œÈHÚÛHØ]]Ø^H[ÈH›ÛXšYBˆ
ÔÑ\œ›ÜˆÑ\œ››ÈHÛÈX[HÜ[ˆš[\ËÌÍÌLJK‚ˆˆˆ‚ˆÙ[‹—ÛX\š×Ù\ØÛÛ›™XİY

BˆÙ\ÜÚ[Û—ÙœÈHÙ]]ŠÙ[‹—ÜÙ\ÜÚ[Û—ÙœÈ‹›Û™JBˆYˆÙ\ÜÚ[Û—ÙœÎ‚ˆÛÜÙYÚYÈHÙ]

Bˆ›Üˆˆ[ˆ\JÙ\ÜÚ[Û—ÙœË˜[Y\Ê
JN‚ˆYˆˆ\È›Û™HÜˆY
ŠH[ˆÛÜÙYÚYÎ‚ˆÛÛ[YBˆÛÜÙYÚYË˜Y
Y
ŠJBˆN‚ˆ‹˜ÛÜÙJ
Bˆ^Ù\^Ù\[Û‚ˆÙÙÙ\‹™XYÊˆ‘˜Z[YÈÛÜÙHØXÚYÙ\ÜÚ[Ûˆˆ›Üˆ	\È‹ˆÙ[‹›˜[YKˆ^×Ú[™›ÏUYKˆ
BˆÙ\ÜÚ[Û—ÙœË˜ÛX\Š
BˆYˆÙ[‹—Ü™\ÜÛœÙWÜİÜ™H\È›İ›Û™N‚ˆN‚ˆÙ[‹—Ü™\ÜÛœÙWÜİÜ™K˜ÛÜÙJ
Bˆ^Ù\^Ù\[Û‚ˆÙÙÙ\‹™XYÊˆ‘˜Z[YÈÛÜÙH™\ÜÛœÙHİÜ™H›Üˆ	\È‹Ù[‹›˜[YK^×Ú[™›ÏUYKˆ
BˆYˆÙ[‹—ÜÚ]N‚ˆ]ØZ]Ù[‹—ÜÚ]KœİÜ

BˆÙ[‹—ÜÚ]HH›Û™BˆYˆÙ[‹—Ü[›™\‚ˆ]ØZ]Ù[‹—Ü[›™\‹˜ÛX[\

BˆÙ[‹—Ü[›™\ˆH›Û™BˆÙ[‹—Ø\H›Û™BˆÙÙÙ\‹š[™›Ê–É\×HTHÙ\™\ˆİÜY‹Ù[‹›˜[YJB‚ˆ\Ş[˜ÈYˆÙ[™
ˆÙ[‹ˆÚ]ÚYˆİ‹ˆÛÛ[ˆİ‹ˆ™\WİÎˆÜ[Û˜[Üİ—HH›Û™KˆY]Y]NˆÜ[Û˜[ÑXİÜİ‹[WWHH›Û™Kˆ
HOˆÙ[™™\İ[‚ˆˆˆ‚ˆ›İ\ÙY8 %™\]Y\İÜ™\ÜÛœÙHŞXÛH[™\È[]™\H\™XİK‚ˆˆˆ‚ˆ™]\›ˆÙ[™™\İ[
İXØÙ\ÜÏQ˜[ÙK\œ›ÜHTHÙ\™\ˆ\Ù\È™\]Y\İÜ™\ÜÛœÙK›İÙ[™

HŠB‚ˆ\Ş[˜ÈYˆÙ]ØÚ]Ú[™›ÊÙ[‹Ú]ÚYˆİŠHOˆXİÜİ‹[WN‚ˆˆˆ”™]\›ˆ˜\ÚXÈ[™›ÈX›İ]HTHÙ\™\‹ˆˆˆ‚ˆ™]\›ˆÂˆ›˜[YHˆTHÙ\™\ˆ‹ˆ\Hˆ˜\H‹ˆšÜİˆÙ[‹—ÚÜİˆœÜˆÙ[‹—ÜÜˆB