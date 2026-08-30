"""Shared auxiliary client router for side tasks.

Provides a single resolution chain so every consumer (context compression,
session search, web extraction, vision analysis, browser vision) picks up
the best available backend without duplicating fallback logic.

Resolution order for text tasks (auto mode):
  1. User's main provider + main model (used regardless of provider type —
     aggregators, direct API-key providers, native Anthropic, Codex, etc.)
  2. OpenRouter  (OPENROUTER_API_KEY)
  3. Nous Portal (~/.hermes/auth.json active provider)
  4. Custom endpoint (config.yaml model.base_url + OPENAI_API_KEY)
  5. Native Anthropic
  6. Direct API-key providers (z.ai/GLM, Kimi/Moonshot, MiniMax, MiniMax-CN)
  7. None

OpenRouter fallback cost guard: ``auxiliary.free_only: true`` restricts the
step-2 fallback to ``:free`` SKUs; ``auxiliary.openrouter_model`` overrides
the default. A one-time WARNING is logged for non-``:free`` models.

Resolution order for vision/multimodal tasks (auto mode):
  1. Selected main provider, if it is one of the supported vision backends below
  2. OpenRouter
  3. Nous Portal
  4. Native Anthropic
  5. Custom endpoint (for local vision models: Qwen-VL, LLaVA, Pixtral, etc.)
  6. None

Codex OAuth (ChatGPT-account auth) is intentionally NOT in either
fallback chain: OpenAI gates this endpoint behind an undocumented,
shifting model allow-list, so "just try Codex with a hardcoded model"
rots on its own.  Codex is used only when the user's main provider *is*
openai-codex (Step 1 above) or when a caller explicitly requests it with
a model (auxiliary.<task>.provider + auxiliary.<task>.model).

Per-task overrides are configured in config.yaml under the ``auxiliary:`` section
(e.g. ``auxiliary.vision.provider``, ``auxiliary.compression.model``).
Default "auto" follows the chains above.

Payment / credit exhaustion fallback:
  When a resolved provider returns HTTP 402 or a credit-related error,
  call_llm() automatically retries with the next available provider in the
  auto-detection chain.  This handles the common case where a user depletes
  their OpenRouter balance but has Codex OAuth or another provider available.
"""

import contextlib
import contextvars
import copy
import functools
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path  # noqa: F401 — used by test mocks
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, TYPE_CHECKING
from urllib.parse import urlparse, parse_qs, urlunparse

# NOTE: `from openai import OpenAI` is deliberately NOT at module top — the
# openai SDK pulls a large type tree (~240 ms cold, including responses/*,
# graders/*). We expose `OpenAI` here as a thin proxy that imports the SDK on
# first call and forwards, so:
#   (a) the 15+ in-module `OpenAI(...)` construction sites work unchanged
#       (Python's function-scope name lookup resolves `OpenAI` to the proxy
#       object bound in module globals here, without triggering any import);
#   (b) external code can still do `auxiliary_client.OpenAI` or
#       `patch("agent.auxiliary_client.OpenAI", ...)` — tests see the proxy,
#       and patch replaces the module attribute as usual;
#   (c) `OpenAI` as a type annotation resolves at runtime to the proxy class
#       (which is harmless — annotations aren't type-checked at runtime).
# See tests/agent/test_auxiliary_client.py for patch patterns this supports.
if TYPE_CHECKING:
    from openai import OpenAI  # noqa: F401 — type hints only

_OPENAI_CLS_CACHE: Optional[type] = None


def _load_openai_cls() -> type:
    """Import and cache ``openai.OpenAI``."""
    global _OPENAI_CLS_CACHE
    if _OPENAI_CLS_CACHE is None:
        from openai import OpenAI as _cls
        _OPENAI_CLS_CACHE = _cls
    return _OPENAI_CLS_CACHE


class _OpenAIProxy:
    """Module-level proxy that looks like the ``openai.OpenAI`` class.

    Forwards ``OpenAI(...)`` calls and ``isinstance(x, OpenAI)`` checks to the
    real SDK class, importing the SDK lazily on first use.
    """

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return _load_openai_cls()(*args, **kwargs)

    def __instancecheck__(self, obj):
        return isinstance(obj, _load_openai_cls())

    def __repr__(self):
        return "<lazy openai.OpenAI proxy>"


OpenAI = _OpenAIProxy()  # module-level name, resolves lazily on call/isinstance


# ── Availability probe mode ───────────────────────────────────────────────
# check_fns (tool gating) only need to know whether a client is RESOLVABLE —
# credentials present, provider routable. Building a real SDK client for that
# answer forces the `openai` import (~0.3s) plus httpx/SSL-context setup on
# the CLI startup path, twice (vision + browser_vision), for an object that
# is immediately discarded. Inside `aux_probe_mode()` the client constructors
# return a lightweight stub instead; resolution POLICY (which provider wins,
# credential lookup, fallback order) is unchanged and stays single-owner.
# Stubs are never cached (see _store_cached_client), so runtime callers can
# never receive one.
_aux_probe_state = threading.local()


class _AuxProbeClientStub:
    """Non-functional placeholder returned while `aux_probe_mode` is active."""

    __slots__ = ("api_key", "base_url")

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url

    def __getattr__(self, name: str) -> Any:
        # Loud failure if a probe stub ever leaks into a runtime call path
        # (it must not — stubs are cache-excluded and probe-scoped).
        raise RuntimeError(
            f"_AuxProbeClientStub used as a real client (attribute {name!r}); "
            "aux_probe_mode is for availability checks only"
        )

    def __repr__(self) -> str:
        return "<aux availability-probe client stub>"


def _aux_probe_active() -> bool:
    return bool(getattr(_aux_probe_state, "active", False))


@contextlib.contextmanager
def aux_probe_mode():
    """Resolve provider availability without constructing real SDK clients."""
    prev = getattr(_aux_probe_state, "active", False)
    _aux_probe_state.active = True
    try:
        yield
    finally:
        _aux_probe_state.active = prev

from agent.credential_pool import load_pool
from agent.model_metadata import (
    MINIMUM_CONTEXT_LENGTH,
    get_model_context_length,
    strip_codex_context_variant_suffix as _strip_codex_ctx_variant,
)
from hermes_cli.config import get_hermes_home
from hermes_constants import OPENROUTER_BASE_URL
from utils import base_url_host_matches, base_url_hostname, env_float, is_truthy_value, model_forces_max_completion_tokens, normalize_proxy_env_vars
from agent.failure_scope import (
    is_connection_error as _is_connection_error,
    is_endpoint_unreachable_error as _is_endpoint_unreachable_error,
    is_timeout_error as _is_timeout_error,
    is_transient_transport_error as _failure_scope_is_transient_transport_error,
)

logger = logging.getLogger(__name__)


# ── resolve_provider_client fall-through dedup ───────────────────────────
# Both fall-through warning sites in resolve_provider_client (the "unknown
# provider" and "unhandled auth_type" branches) fire on every retry of a
# misconfigured provider, spamming the logs. Demote them to logger.debug with
# per-process dedup: the FIRST occurrence still surfaces (it carries real
# diagnostic value — a provider-name typo or PROVIDER_REGISTRY/auth_type
# drift), and identical repeats are suppressed for the lifetime of the
# process. Two independent sets keep each branch linear and let tests clear
# them independently.
_LOGGED_UNKNOWN_PROVIDER_KEYS: set = set()
_LOGGED_UNHANDLED_AUTHTYPE_KEYS: set = set()
# Same treatment for the two "registered provider, unsupported sub-branch"
# routing dead-ends — external-process and OAuth providers that fall through
# with no matching handler. Keyed by provider name.
_LOGGED_UNSUPPORTED_EXTPROC_KEYS: set = set()
_LOGGED_UNSUPPORTED_OAUTH_KEYS: set = set()


def _resolve_aux_verify(base_url: Optional[str]) -> Any:
    """Resolve httpx ``verify`` for an auxiliary-client base_url.

    Mirrors the main client's TLS resolution so auxiliary calls (compression,
    vision, title generation, etc.) honor per-provider
    ``ssl_ca_cert`` / ``ssl_verify`` config and the ``HERMES_CA_BUNDLE`` /
    ``SSL_CERT_FILE`` env conventions. Best-effort: any failure falls back to
    the httpx/certifi default (``True``).
    """
    try:
        from agent.ssl_verify import resolve_httpx_verify
        from hermes_cli.config import (
            get_custom_provider_tls_settings,
            load_config_readonly,
        )

        tls = get_custom_provider_tls_settings(
            str(base_url or ""), config=load_config_readonly()
        )
        return resolve_httpx_verify(
            ca_bundle=tls.get("ssl_ca_cert"),
            ssl_verify=tls.get("ssl_verify"),
            base_url=str(base_url or ""),
        )
    except Exception:
        return True


_WARNED_KEEPALIVE_IMPORT_SKEW = False


def _openai_http_client_kwargs(
    base_url: Optional[str],
    *,
    async_mode: bool = False,
) -> Dict[str, Any]:
    """Inject keepalive httpx client with env-only proxy (not macOS system proxy)."""
    try:
        from agent.process_bootstrap import build_keepalive_http_client
        client = build_keepalive_http_client(
            str(base_url or ""),
            async_mode=async_mode,
            verify=_resolve_aux_verify(base_url),
        )
    except (ImportError, AttributeError):
        # Version-skewed installs (#64333): a process whose sys.path resolves
        # an older agent/process_bootstrap.py without this helper — seen when
        # the Desktop app's bundled runtime lags a git-installed source tree
        # that newer callers (cron scheduler) were written against. Every cron
        # job died on this ImportError before any agent logic ran. Degrade
        # gracefully to the OpenAI SDK's default httpx client (respects macOS
        # system proxy, no pool-level keepalive expiry) instead of failing the
        # whole job, and say so once — silent version skew is how this bug
        # went unnoticed until jobs were already dead on arrival.
        global _WARNED_KEEPALIVE_IMPORT_SKEW
        if not _WARNED_KEEPALIVE_IMPORT_SKEW:
            _WARNED_KEEPALIVE_IMPORT_SKEW = True
            logger.warning(
                "agent.process_bootstrap.build_keepalive_http_client is "
                "unavailable — mixed/stale install detected (#64333). Falling "
                "back to the SDK default HTTP client. Run `hermes update` (or "
                "reinstall the Desktop app) to resync the runtime."
            )
        client = None

    if client is None:
        return {}
    return {"http_client": client}

def _create_openai_client(*, api_key: str, base_url: str, **kwargs: Any) -> Any:
    if _aux_probe_active():
        # Availability probe: credentials/base_url resolved — that is the
        # answer. Skip the openai import + httpx/SSL construction entirely.
        return _AuxProbeClientStub(api_key=api_key, base_url=base_url)
    kwargs = {**_openai_http_client_kwargs(base_url), **kwargs}
    # OpenCode Zen free tier: the keyless placeholder must never reach the
    # wire — the Zen relay serves free models anonymously but 401s any
    # unrecognized bearer. Override the SDK's Authorization header with an
    # empty value (single shared chokepoint for every aux client build).
    try:
        from hermes_cli.models import (
            OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER,
            opencode_zen_free_headers,
        )
        if api_key == OPENCODE_ZEN_FREE_KEYLESS_PLACEHOLDER:
            merged = dict(kwargs.get("default_headers") or {})
            merged.update(opencode_zen_free_headers())
            kwargs["default_headers"] = merged
    except Exception:
        pass
    _apply_required_codex_headers(kwargs, access_token=api_key, base_url=base_url)
    # Hermes owns auxiliary retry + provider/model fallback policy (the
    # same-provider transient retry in call_llm plus the except-chain
    # fallback). The OpenAI SDK's own default (max_retries=2 → up to 3
    # attempts) silently multiplies the effective wall time of every aux call
    # by 3× on a slow/hung endpoint, so a 120s timeout can stall ~360s before
    # Hermes sees a single failure (issue #54465). Disable SDK-internal retries
    # by default and let Hermes control the budget; explicit callers can still
    # override via kwargs.
    kwargs.setdefault("max_retries", 0)
    return OpenAI(api_key=api_key, base_url=base_url, **kwargs)


# ── Interrupt protection for atomic auxiliary tasks ──────────────────────
# Some auxiliary tasks must NOT be aborted mid-flight by a gateway interrupt
# (e.g. an incoming user message while the agent is busy). Context
# compression is the prime case: if the summary LLM call is interrupted
# part-way, compression falls back to a static "summary unavailable" marker
# and the real handoff is lost (#23975). A thread-local flag lets such a
# task mark its in-flight LLM call as interrupt-protected; the Codex
# Responses stream's cancellation check honors it. An explicit host cancel
# (CLI Ctrl+C or /stop) may install a cancel check that overrides protection;
# ordinary incoming-message interrupts remain protected. TIMEOUTS still fire
# (a hung call must die), and all OTHER aux tasks (vision, web_extract,
# title_generation, …) remain freely interruptible.
_aux_interrupt_protection = threading.local()


class AuxiliaryExplicitCancellation(BaseException):
    """Frozen signal that an auxiliary attempt was explicitly hard-cancelled.

    This deliberately follows ``asyncio.CancelledError`` and inherits directly
    from ``BaseException``: provider retry/fallback code catches ``Exception``
    broadly and must never reinterpret an explicit host stop as a transport
    failure. ``cause`` is immutable class data so downstream compression code
    does not re-query a mutable host Event after the transport has unwound.
    """

    cause = "explicit_host_cancel"

    def __init__(self) -> None:
        super().__init__("auxiliary request explicitly cancelled by host")


def _aux_interrupt_protected() -> bool:
    return bool(getattr(_aux_interrupt_protection, "active", False))


def _aux_interrupt_cancel_requested() -> bool:
    """Return whether an explicit host cancel overrides aux protection."""
    event = getattr(_aux_interrupt_protection, "cancel_event", None)
    if event is not None:
        try:
            return bool(event.is_set())
        except Exception:
            logger.debug("aux interrupt cancel event check failed", exc_info=True)
            return False
    check = getattr(_aux_interrupt_protection, "cancel_check", None)
    if not callable(check):
        return False
    try:
        return bool(check())
    except Exception:
        logger.debug("aux interrupt cancel check failed", exc_info=True)
        return False


@contextlib.contextmanager
def aux_interrupt_protection(
    active: bool = True,
    cancel_check=None,
    cancel_event=None,
):
    """Mark the current thread's auxiliary LLM call as interrupt-protected.

    Used by atomic aux tasks (compression) so a mid-flight gateway interrupt
    doesn't abort the call and trigger a degraded fallback. Re-entrant-safe:
    restores the previous value on exit. ``cancel_check`` lets the host retain
    an explicit hard-cancel path; ``cancel_event`` is preferred when the host
    already owns an Event. Nested protection scopes inherit both values.
    """
    prev = getattr(_aux_interrupt_protection, "active", False)
    prev_cancel_check = getattr(_aux_interrupt_protection, "cancel_check", None)
    prev_cancel_event = getattr(_aux_interrupt_protection, "cancel_event", None)
    _aux_interrupt_protection.active = active
    if callable(cancel_check):
        _aux_interrupt_protection.cancel_check = cancel_check
    if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)):
        _aux_interrupt_protection.cancel_event = cancel_event
    try:
        yield
    finally:
        _aux_interrupt_protection.active = prev
        _aux_interrupt_protection.cancel_check = prev_cancel_check
        _aux_interrupt_protection.cancel_event = prev_cancel_event


def _capture_aux_cancel_check() -> Optional[Callable[[], Any]]:
    """Capture the current explicit-cancel source on the owning request thread."""
    event = getattr(_aux_interrupt_protection, "cancel_event", None)
    is_set = getattr(event, "is_set", None)
    if callable(is_set):
        return is_set
    check = getattr(_aux_interrupt_protection, "cancel_check", None)
    if callable(check):
        # Preserve callable identity so attempt-local decision objects retain
        # methods such as begin_timeout_cleanup() when captured by adapters.
        return check
    return None


def _captured_aux_cancel_requested(cancel_check: Callable[[], Any]) -> bool:
    """Read a request-thread cancellation source without leaking its failures."""
    try:
        return bool(cancel_check())
    except Exception:
        logger.debug("captured aux cancel check failed", exc_info=True)
        return False


class _AuxiliaryCancellationDecision:
    """Atomically choose explicit cancellation or provider timeout per attempt."""

    def __init__(self, source_cancel_check: Callable[[], Any]) -> None:
        self._source_cancel_check = source_cancel_check
        self._lock = threading.Lock()
        self._outcome = "active"

    def __call__(self) -> bool:
        with self._lock:
            if self._outcome == "cancelled":
                return True
            if self._outcome == "timed_out":
                return False
            if _captured_aux_cancel_requested(self._source_cancel_check):
                self._outcome = "cancelled"
                return True
            return False

    def begin_timeout_cleanup(self) -> bool:
        """Return whether timeout won and destructive cleanup is permitted."""
        with self._lock:
            if self._outcome == "active":
                if _captured_aux_cancel_requested(self._source_cancel_check):
                    self._outcome = "cancelled"
                else:
                    self._outcome = "timed_out"
            return self._outcome == "timed_out"


# ── Forward-progress hook for streamed auxiliary calls ───────────────────
# Long auxiliary calls (context compression is the prime case) are watched by
# wall-clock deadlines in their hosts (gateway session hygiene). A fixed
# deadline punishes SLOW summary models exactly as hard as HUNG ones: a
# reasoning model happily streaming a large summary is killed mid-generation.
# This thread-local hook lets the host observe liveness instead: the wire
# consumers below tick it only for non-empty streamed payloads, and the host
# extends its deadline while tokens are moving (see gateway/run.py session
# hygiene + CompressionCommitFence.touch_progress). Thread-local matches the
# call topology — the aux call and its stream consumption run synchronously
# on the thread that installed the hook.
_aux_progress = threading.local()
_aux_dispatch = threading.local()
_aux_provider_response = threading.local()


def _notify_aux_progress() -> None:
    """Tick the installed forward-progress hook, if any. Never raises."""
    hook = getattr(_aux_progress, "hook", None)
    if hook is None:
        return
    try:
        hook()
    except Exception:
        logger.debug("aux progress hook failed", exc_info=True)


def _notify_aux_dispatch() -> None:
    """Record an actual provider dispatch without claiming response progress."""
    hook = getattr(_aux_dispatch, "hook", None)
    if hook is not None:
        try:
            hook()
        except Exception:
            logger.debug("aux dispatch hook failed", exc_info=True)


def _notify_aux_timing_response() -> None:
    """Record a provider response/chunk WITHOUT claiming forward progress.

    Same timing slot as :func:`_notify_aux_provider_response`, minus the
    forward-progress chain: used for content-free frames (keepalives,
    lifecycle events, typed-but-empty deltas) that must still count toward
    ``time_to_first_progress_ms`` telemetry but must not reset a compression
    inactivity fence.
    """
    hook = getattr(_aux_provider_response, "hook", None)
    if hook is not None:
        try:
            hook()
        except Exception:
            logger.debug("aux provider response hook failed", exc_info=True)


def _notify_aux_provider_response() -> None:
    """Record a provider response/chunk, then preserve the liveness signal."""
    _notify_aux_timing_response()
    _notify_aux_progress()


def _aux_progress_active() -> bool:
    return getattr(_aux_progress, "hook", None) is not None


def _event_field(event: Any, name: str) -> Any:
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def _anthropic_event_has_content(event: Any) -> bool:
    """Whether an Anthropic stream event carries a non-empty payload."""
    event_type = _event_field(event, "type")
    if event_type == "content_block_delta":
        delta = _event_field(event, "delta")
        return any(
            bool(_event_field(delta, field))
            for field in ("text", "thinking", "partial_json", "signature", "citation")
        )
    if event_type == "content_block_start":
        block = _event_field(event, "content_block")
        return _event_field(block, "type") == "tool_use" and any(
            bool(_event_field(block, field)) for field in ("id", "name")
        )
    return False


_CODEX_PROGRESS_DELTA_TYPES = frozenset(
    {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.text.delta",
        "response.audio.delta",
        "response.function_call_arguments.delta",
        "response.reasoning_text.delta",
    }
)


def _codex_event_has_content(event: Any) -> bool:
    """Whether a Codex Responses event carries a non-empty payload."""
    event_type = _event_field(event, "type")
    if event_type in _CODEX_PROGRESS_DELTA_TYPES:
        return bool(_event_field(event, "delta"))
    if event_type == "response.output_item.added":
        item = _event_field(event, "item")
        return "function_call" in str(_event_field(item, "type") or "") and any(
            bool(_event_field(item, field))
            for field in ("id", "call_id", "name", "arguments")
        )
    return False


@contextlib.contextmanager
def _aux_thread_local_hook(local: threading.local, hook):
    """Install one thread-local hook callback and restore its prior value.

    ``hook=None`` (or any non-callable) is a no-op passthrough so callers can
    wire it unconditionally. Re-entrant-safe: restores the previous hook on
    exit. Shared by the forward-progress hook and the content-free timing
    hooks — one save/restore implementation, three thread-local slots.
    """
    previous = getattr(local, "hook", None)
    local.hook = hook if callable(hook) else previous
    try:
        yield
    finally:
        local.hook = previous


@contextlib.contextmanager
def aux_progress_hook(hook):
    """Install *hook* as the current thread's aux forward-progress callback.

    ``hook=None`` is a no-op passthrough so callers can wire it
    unconditionally. Re-entrant-safe: restores the previous hook on exit.
    """
    with _aux_thread_local_hook(_aux_progress, hook):
        yield


# Back-compat alias — the timing hooks were introduced with this name.
_aux_timing_hook = _aux_thread_local_hook


def _run_protected_sync_provider_call(
    callback: Callable[[dict[str, Any]], Any],
    kwargs: dict[str, Any],
) -> Any:
    """Run one protected provider callback in an attempt-isolated daemon.

    A hard cancel must release the compression-owning thread promptly, but
    auxiliary clients are process-shared and cannot safely be closed or evicted
    to wake one request.  Only protected calls with a captured hard-cancel source
    use this seam.  Their provider callback (including stream aggregation) runs
    in a daemon worker while the owner polls cancellation.  On cancel the owner
    unwinds immediately; the worker is left to finish under the provider timeout
    already present in ``kwargs``.  It owns no transcript or compressor commit
    state and never holds the session lock.

    Ordinary auxiliary calls, and protected calls without a cancellation source,
    retain the historical direct synchronous path with no extra thread.
    """
    source_cancel_check = _capture_aux_cancel_check()
    if not _aux_interrupt_protected() or not callable(source_cancel_check):
        return callback(kwargs)

    # Freeze one linearized outcome for this isolated attempt. The host Event is
    # reused and cleared on a later turn, while the Codex timeout Timer may race
    # owner polling. Both paths must decide under the same attempt-local lock.
    cancel_check = _AuxiliaryCancellationDecision(source_cancel_check)

    if cancel_check():
        raise AuxiliaryExplicitCancellation()

    progress_hook = getattr(_aux_progress, "hook", None)
    # Timing hooks ride along with the progress hook: _create_with_progress
    # fires _notify_aux_dispatch/_notify_aux_provider_response from whichever
    # thread runs the provider callback, so an owner-thread-only install would
    # silently drop provider_dispatch_ms / time_to_first_progress_ms whenever
    # the protected daemon path is taken.
    dispatch_hook = getattr(_aux_dispatch, "hook", None)
    provider_response_hook = getattr(_aux_provider_response, "hook", None)
    provider_context = contextvars.copy_context()
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def _provider_worker() -> None:
        try:
            with (
                aux_progress_hook(progress_hook),
                _aux_thread_local_hook(_aux_dispatch, dispatch_hook),
                _aux_thread_local_hook(_aux_provider_response, provider_response_hook),
                aux_interrupt_protection(cancel_check=cancel_check),
            ):
                outcome["result"] = callback(kwargs)
        except BaseException as exc:
            outcome["exception"] = exc
        finally:
            done.set()

    threading.Thread(
        target=provider_context.run,
        args=(_provider_worker,),
        name="hermes-protected-aux-provider",
        daemon=True,
    ).start()

    while True:
        # Cancellation is checked before and after every completion wait so it
        # wins whenever result publication and the host Event become visible in
        # the same polling interval.
        if _captured_aux_cancel_requested(cancel_check):
            raise AuxiliaryExplicitCancellation()
        if not done.wait(0.02):
            continue
        if _captured_aux_cancel_requested(cancel_check):
            raise AuxiliaryExplicitCancellation()
        exception = outcome.get("exception")
        if exception is not None:
            raise exception
        return outcome.get("result")


def _safe_isinstance(obj: Any, maybe_type: Any) -> bool:
    """Return False instead of raising when a patched symbol is not a type."""
    try:
        return isinstance(obj, maybe_type)
    except TypeError:
        return False


def _extract_url_query_params(url: str):
    """Extract query params from URL, return (clean_url, default_query dict or None)."""
    parsed = urlparse(url)
    if parsed.query:
        clean = urlunparse(parsed._replace(query=""))
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return clean, params
    return url, None


# Module-level flag: only warn once per process about stale OPENAI_BASE_URL.
_stale_base_url_warned = False

_PROVIDER_ALIASES = {
    "google": "gemini",
    "google-gemini": "gemini",
    "google-ai-studio": "gemini",
    "x-ai": "xai",
    "x.ai": "xai",
    "grok": "xai",
    "glm": "zai",
    "z-ai": "zai",
    "z.ai": "zai",
    "zhipu": "zai",
    "kimi": "kimi-coding",
    "moonshot": "kimi-coding",
    "kimi-cn": "kimi-coding-cn",
    "moonshot-cn": "kimi-coding-cn",
    "gmi-cloud": "gmi",
    "gmicloud": "gmi",
    "actual-computer": "actual",
    "actualcomputer": "actual",
    "aci": "actual",
    "minimax-china": "minimax-cn",
    "minimax_cn": "minimax-cn",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "github": "copilot",
    "github-copilot": "copilot",
    "github-model": "copilot",
    "github-models": "copilot",
    "github-copilot-acp": "copilot-acp",
    "copilot-acp-agent": "copilot-acp",
    "tencent": "tencent-tokenhub",
    "tokenhub": "tencent-tokenhub",
    "tencent-cloud": "tencent-tokenhub",
    "tencentmaas": "tencent-tokenhub",
    "tokenplan": "tencent-tokenplan",
    "tencent-lkeap": "tencent-tokenplan",
}


def _normalize_aux_provider(provider: Optional[str]) -> str:
    normalized = (provider or "auto").strip().lower()
    if normalized.startswith("custom:"):
        suffix = normalized.split(":", 1)[1].strip()
        if not suffix:
            return "custom"
        normalized = suffix
    if normalized == "codex":
        return "openai-codex"
    if normalized == "main":
        # Resolve to the user's actual main provider so named custom providers
        # and non-aggregator providers (DeepSeek, Alibaba, etc.) work correctly.
        main_prov = (_read_main_provider() or "").strip().lower()
        if main_prov and main_prov not in {"auto", "main", ""}:
            normalized = main_prov
        else:
            return "custom"
    return _PROVIDER_ALIASES.get(normalized, normalized)


# Sentinel: when returned by _fixed_temperature_for_model(), callers must
# strip the ``temperature`` key from API kwargs entirely so the provider's
# server-side default applies.  Kimi/Moonshot models manage temperature
# internally — sending *any* value (even the "correct" one) can conflict
# with gateway-side mode selection (thinking → 1.0, non-thinking → 0.6).
OMIT_TEMPERATURE: object = object()


def _is_kimi_model(model: Optional[str]) -> bool:
    """True for any Kimi / Moonshot model that manages temperature server-side."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare.startswith("kimi-") or bare == "kimi"


def _is_arcee_trinity_thinking(model: Optional[str]) -> bool:
    """True for Arcee Trinity Large Thinking (direct or via OpenRouter)."""
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare == "trinity-large-thinking"


# Context window enforced by ChatGPT's Codex OAuth backend for the
# gpt-5.4 / gpt-5.5 / gpt-5.6 families. The raw OpenAI API and OpenRouter
# expose 1.05M for the same slugs, but the Codex backend hard-caps at 272K
# (verified live for 5.4/5.5: a ~330K-token request to
# chatgpt.com/backend-api/codex/responses is rejected with
# ``context_length_exceeded`` while ~250K succeeds; gpt-5.6 shares the same
# 272K Codex cap — see _CODEX_OAUTH_CONTEXT_FALLBACK in model_metadata.py).
# With a 272K ceiling the default 50% compaction trigger fires at ~136K —
# wasteful, since the model can hold far more raw context before
# summarization actually buys anything. We raise the trigger to 85% (~231K)
# on this exact route so Codex gpt-5.4 / gpt-5.5 / gpt-5.6 sessions use the
# window they actually have.
_CODEX_GPT54_GPT55_COMPACTION_THRESHOLD = 0.85

# gpt-5.3-codex-spark is Codex-OAuth-only (ChatGPT Pro entitlement) with a
# native 128K context window.  The default 50% compaction trigger fires at
# ~64K — wasting half the usable window, often before the session has enough
# turns to summarize meaningfully.  We raise the trigger to 70% (~90K) so
# spark sessions use more of the window before summarization, while still
# leaving ~38K headroom for the summary and continued conversation before
# the 128K hard limit.
_CODEX_SPARK_COMPACTION_THRESHOLD = 0.70


def _is_codex_gpt54_or_gpt55(model: Optional[str], provider: Optional[str] = None) -> bool:
    """True for gpt-5.4 / gpt-5.5 / gpt-5.6 on the ChatGPT Codex OAuth backend.

    Matches only the Codex OAuth route (provider ``openai-codex``), not the
    direct OpenAI API, OpenRouter, or GitHub Copilot paths — those expose a
    larger context window for the same slug and must keep the user's default
    compaction threshold. ``-pro`` variants and dated snapshots are matched
    via prefix so the override tracks every 272K-capped family (5.4, 5.5,
    5.6 sol/terra/luna incl. their ``-pro`` modes) without re-listing every
    variant. (Name kept for backward compatibility with the
    ``compression.codex_gpt55_autoraise`` config key.) The exact
    ``gpt-daybreak-blue-latest`` Codex slug is also a verified Sol-family
    alias and receives the same autoraise.

    ``-900k`` large-context picker variants are explicitly EXCLUDED: the
    85% autoraise exists to stop wasting a small 272K window, and a 900K
    window doesn't have that problem — those sessions keep the user's
    global ``compression.threshold`` (default 50%, ~450K).
    """
    prov = (provider or "").strip().lower()
    if prov != "openai-codex":
        return False
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    from agent.model_metadata import is_codex_context_variant
    if is_codex_context_variant(bare):
        return False
    return (
        bare == "gpt-5.4"
        or bare.startswith("gpt-5.4-")
        or bare.startswith("gpt-5.4.")
        or bare == "gpt-5.5"
        or bare.startswith("gpt-5.5-")
        or bare.startswith("gpt-5.5.")
        or bare == "gpt-5.6"
        or bare.startswith("gpt-5.6-")
        or bare.startswith("gpt-5.6.")
        or bare == "gpt-daybreak-blue-latest"
    )


def _is_codex_spark(model: Optional[str], provider: Optional[str] = None) -> bool:
    """True for ``gpt-5.3-codex-spark`` on the ChatGPT Codex OAuth backend.

    The model is Codex-OAuth-only (ChatGPT Pro entitlement) with a native
    128K context window.  Only the Codex OAuth route (provider
    ``openai-codex``) is matched — the slug is not available on other
    routes.
    """
    prov = (provider or "").strip().lower()
    if prov != "openai-codex":
        return False
    bare = (model or "").strip().lower().rsplit("/", 1)[-1]
    return bare == "gpt-5.3-codex-spark"


def _fixed_temperature_for_model(
    model: Optional[str],
    base_url: Optional[str] = None,
) -> "Optional[float] | object":
    """Return a temperature directive for models with strict contracts.

    Returns:
        ``OMIT_TEMPERATURE`` — caller must remove the ``temperature`` key so the
            provider chooses its own default.  Used for all Kimi / Moonshot
            models whose gateway selects temperature server-side.
        ``float`` — a specific value the caller must use (reserved for future
            models with fixed-temperature contracts).
        ``None`` — no override; caller should use its own default.
    """
    if _is_kimi_model(model):
        logger.debug("Omitting temperature for Kimi model %r (server-managed)", model)
        return OMIT_TEMPERATURE
    if _is_arcee_trinity_thinking(model):
        return 0.5
    return None


def _compression_threshold_for_model(
    model: Optional[str],
    provider: Optional[str] = None,
    *,
    allow_codex_gpt55_autoraise: bool = True,
) -> Optional[float]:
    """Return a context-compression threshold override for specific models.

    The threshold is the fraction of the model's context window that must be
    consumed before Hermes triggers summarization.  Higher values delay
    compression and preserve more raw context.

    Per-model/route overrides:
      - Arcee Trinity Large Thinking → 0.75 (preserve reasoning context).
      - gpt-5.4 / gpt-5.5 / gpt-5.6 and the exact Daybreak Sol alias on the
        Codex OAuth route → 0.85, because
        Codex caps all three families at 272K and the default 50% trigger
        would compact at ~136K. Gated by ``allow_codex_gpt55_autoraise``
        (historical config-key name kept for backward compatibility) so the
        user can opt back down to the global default (the caller passes the
        config flag through here).
      - gpt-5.3-codex-spark on the Codex OAuth route → 0.70, because the model
        has a native 128K window and the default 50% trigger would compact at
        ~64K — wasting half the usable context. Not gated by the gpt-5.5
        opt-out flag: 128K is the model's native window, so the raise is
        unambiguously correct.

    Returns a float in (0, 1] to override the global ``compression.threshold``
    config value, or ``None`` to leave the user's config value unchanged.
    """
    if _is_arcee_trinity_thinking(model):
        return 0.75
    if allow_codex_gpt55_autoraise and _is_codex_gpt54_or_gpt55(model, provider):
        return _CODEX_GPT54_GPT55_COMPACTION_THRESHOLD
    if _is_codex_spark(model, provider):
        return _CODEX_SPARK_COMPACTION_THRESHOLD
    return None

# Model-family priority for the auxiliary "fast tier", fastest first.
#
# Matched as substrings against the provider's LIVE /v1/models catalog rather
# than pinned as exact ids, because exact ids rot: a hardcoded
# "google/gemini-3-flash" kept 404ing here once Nous dropped it upstream, and
# every aux call paid a wasted round-trip before the retry net caught it.
# Families outlive their version numbers, so a new mini/flash/haiku release is
# picked up with no source edit.
#
# Rolling "-latest" aliases come first where a provider publishes them (Nous
# serves ~openai/gpt-mini-latest, ~google/gemini-flash-latest, …): they are the
# only ids that are structurally rot-proof.
#
# Order is measured, not guessed — p50 on a real titling prompt against the
# Nous catalog: gpt-mini-latest 1.40s, claude-haiku-latest 1.55s,
# gemini-flash-latest 2.13s, step-3.7-flash 7.84s, grok-4.1-fast 8.05s. So the
# first family a provider actually serves is also the fastest it can offer.
_FAST_MODEL_FAMILIES: tuple = (
    "gpt-mini-latest",
    "gpt-nano-latest",
    "claude-haiku-latest",
    "gemini-flash-latest",
    "gpt-5.4-nano",
    "gpt-5.4-mini",
    "gpt-5-mini",
    "haiku-4.5",
    "gemini-3.6-flash",
    "flash-lite",
    "-nano",
    "-mini",
    "-flash",
    "haiku",
)

# Substrings that disqualify an otherwise-matching id. Reasoning variants
# ("o3-mini", "gpt-5.4-mini-thinking") think before answering, which is the
# opposite of what a titler wants; ":batch" is an async queue, not a live
# endpoint; embedding models ("all-minilm") match "-mini" but aren't chat
# models at all; ":free" tiers are heavily rate-limited and measured slowest.
# The modality suffixes are the same trap as the embedders — a provider names
# its speech and image endpoints after the chat model they're paired with, so
# "gpt-4o-mini-tts" satisfies the "-mini" rung and cannot answer a prompt.
_FAST_MODEL_EXCLUDE: tuple = (
    "thinking", "reason", "-r1", "minilm", ":batch", ":free",
    "o1-", "o3-", "o4-", "codex", "audio", "-vl", "embed",
    "-tts", "-transcribe", "-realtime", "-image", "-search-preview",
)


_VERSION_CHUNK_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _model_recency_key(model_id: str) -> tuple:
    """Sort key that puts a family's newest release first (descending).

    The rungs at the bottom of ``_FAST_MODEL_FAMILIES`` are bare family names —
    ``-mini``, ``-flash``, ``haiku`` — and a provider serves every generation of
    those it hasn't retired. Compared as plain strings, the oldest wins:
    ``gpt-3.5-mini`` sorts before ``gpt-5.4-mini``, and ``claude-3-haiku`` before
    ``claude-haiku-4.5``. So the rung meant to keep us current on a provider's
    small tier was pinning us to its most obsolete member.

    Splitting digit runs out and comparing them as numbers fixes both the
    generation order and the 9-vs-10 cliff a string sort walks off.
    """
    chunks = []
    for index, part in enumerate(_VERSION_CHUNK_RE.split(model_id.lower())):
        if not part:
            continue
        # re.split with one capturing group alternates text, number, text, …
        chunks.append((1, float(part), "") if index % 2 else (0, 0.0, part))
    return tuple(chunks)


def _fast_model_from_catalog(provider_id: str) -> str:
    """Pick the fastest small model the provider ACTUALLY serves right now.

    Reads the provider's live (cached) ``/v1/models`` catalog and returns the
    newest ``_FAST_MODEL_FAMILIES`` match. Returns "" when the catalog is
    unavailable or holds no small model, so the caller falls through to the
    provider's curated default. Never raises and never blocks on a cold
    network path — the underlying fetch is memory+disk cached with a
    last-known-good fallback.
    """
    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials
        from hermes_cli.models import fetch_models_with_pricing
        from providers import get_provider_profile

        # The provider's own credentials, because most ``/v1/models`` endpoints
        # are authenticated: fetched anonymously they 401, and the caller reads
        # that as "this provider serves no small model" and quietly falls back
        # to the curated default forever.
        api_key, base_url = "", ""
        try:
            creds = resolve_api_key_provider_credentials(provider_id) or {}
            api_key = str(creds.get("api_key", "")).strip()
            base_url = str(creds.get("base_url", "")).strip()
        except Exception:
            # Not an API-key provider, or nothing configured yet. The anonymous
            # fetch below still works for the catalogs that allow it.
            logger.debug("No credentials for %s catalog", provider_id, exc_info=True)

        if not base_url:
            base_url = str(getattr(get_provider_profile(provider_id), "base_url", "") or "")
        base_url = base_url.rstrip("/")
        if not base_url:
            return ""
        # fetch_models_with_pricing appends its own /v1/models.
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        catalog = fetch_models_with_pricing(
            api_key=api_key or None, base_url=base_url, timeout=3.0
        ) or {}
    except Exception:
        logger.debug("Fast-model catalog lookup failed for %s", provider_id, exc_info=True)
        return ""

    ids = sorted((str(m) for m in catalog), key=_model_recency_key, reverse=True)
    for family in _FAST_MODEL_FAMILIES:
        for model_id in ids:
            lowered = model_id.lower()
            if family in lowered and not any(x in lowered for x in _FAST_MODEL_EXCLUDE):
                return model_id
    return ""


# Default auxiliary models for direct API-key providers (cheap/fast for side tasks)
def _get_aux_model_for_provider(provider_id: str, *, prefer_fast: bool = False) -> str:
    """Return the cheap auxiliary model for a provider.

    Resolution ladder, fastest-and-most-live first:

    1. ``prefer_fast`` only — a family match against the provider's LIVE
       ``/v1/models`` catalog, preferring rolling ``-latest`` aliases. This is
       both rot-proof and latency-ordered.
    2. ``prefer_fast`` only — the provider's own recommendation hook
       (``ProviderProfile.resolve_aux_model``). Live, but tuned for *quality*
       on long-context side tasks (Nous returns its compaction pick), so it
       ranks below the catalog match for latency-critical work.
    3. ``ProviderProfile.default_aux_model`` — curated, hardcoded, may rot.
    4. The legacy hardcoded dict, for providers predating the profiles system.

    ``prefer_fast`` is opt-in so this only changes latency-critical tasks
    (titling). Every other auxiliary caller keeps the existing static
    behaviour and its cache keys.
    """
    profile = None
    try:
        from providers import get_provider_profile
        profile = get_provider_profile(provider_id)
    except Exception:
        pass

    if prefer_fast:
        catalog_pick = _fast_model_from_catalog(provider_id)
        if catalog_pick:
            return catalog_pick
        if profile is not None:
            try:
                live = profile.resolve_aux_model()
                if live:
                    return live
            except Exception:
                logger.debug("resolve_aux_model failed for %s", provider_id, exc_info=True)

    if profile is not None and profile.default_aux_model:
        return profile.default_aux_model
    return _API_KEY_PROVIDER_AUX_MODELS_FALLBACK.get(provider_id, "")



# Fallback for providers not yet migrated to ProviderProfile.default_aux_model,
# plus providers we intentionally keep pinned here (e.g. Anthropic predates
# profiles). New providers should set default_aux_model on their profile instead.
_API_KEY_PROVIDER_AUX_MODELS_FALLBACK: Dict[str, str] = {
    "gemini": "gemini-3.6-flash",
    "zai": "glm-4.5-flash",
    "kimi-coding": "kimi-k2-turbo-preview",
    "stepfun": "step-3.5-flash",
    "kimi-coding-cn": "kimi-k2-turbo-preview",
    "gmi": "google/gemini-3.1-flash-lite-preview",
    "anthropic": "claude-haiku-4-5-20251001",
    "ai-gateway": "google/gemini-3-flash",
    "opencode-zen": "gemini-3-flash",
    "opencode-go": "glm-5",
    "kilocode": "google/gemini-3.6-flash",
    "ollama-cloud": "nemotron-3-nano:30b",
    "tencent-tokenhub": "hy4-preview",
    "tencent-tokenplan": "hy4-preview",
    # NB: no "deepinfra" entry — its aux model lives on the ProviderProfile
    # (plugins/model-providers/deepinfra: default_aux_model), which
    # _get_aux_model_for_provider() reads first. Duplicating it here would be
    # dead data that drifts when the profile's value is bumped.
}

# Legacy alias — callers that haven't been updated to _get_aux_model_for_provider()
# can still use this dict directly. Kept in sync with _FALLBACK above.
_API_KEY_PROVIDER_AUX_MODELS: Dict[str, str] = _API_KEY_PROVIDER_AUX_MODELS_FALLBACK

# Auxiliary tasks that may opt into the provider's fast/cheap model instead of
# the user's main chat model. The opt-in lives in
# ``auxiliary.<task>.prefer_fast_model`` so the default ``auto = main model``
# contract remains true on every settings surface.
_FAST_MODEL_TASKS: frozenset = frozenset({"title_generation"})


def _task_prefers_fast_model(task: Optional[str]) -> bool:
    """Return whether an eligible task explicitly opts into fast-model routing."""
    if task not in _FAST_MODEL_TASKS:
        return False
    task_config = _get_auxiliary_task_config(task)
    return is_truthy_value(task_config.get("prefer_fast_model"), default=False)


# Vision-specific model overrides for direct providers.
# When the user's main provider has a dedicated vision/multimodal model that
# differs from their main chat model, map it here.  The vision auto-detect
# "exotic provider" branch checks this before falling back to the main model.
_PROVIDER_VISION_MODELS: Dict[str, str] = {
    "xiaomi": "mimo-v2.5",
    "zai": "glm-5v-turbo",
}


def _resolve_provider_vision_default(provider: str) -> Optional[str]:
    """Return the provider's preferred default vision model id, or None.

    Static entries in :data:`_PROVIDER_VISION_MODELS` win first (xiaomi /
    zai have dedicated vision-only model names that don't live in any
    discoverable catalog). Otherwise the provider's :class:`ProviderProfile`
    gets a chance to supply one via its ``default_vision_model()`` hook —
    that's where catalog-backed providers (DeepInfra) resolve a live default,
    keeping the discovery logic inside their plugin instead of a name-check
    branch here.
    """
    static = _PROVIDER_VISION_MODELS.get(provider)
    if static:
        return static
    try:
        from providers import get_provider_profile
        profile = get_provider_profile(provider)
    except Exception:
        return None
    if profile is None:
        return None
    try:
        return profile.default_vision_model()
    except Exception:
        return None

# Providers whose endpoint does not accept image input, even though the
# provider's broader ecosystem has vision models available elsewhere.  When
# `auxiliary.vision.provider: auto` sees one of these as the main provider,
# it must skip straight to the aggregator chain instead of returning a client
# that will 404 on every vision request.
#
# kimi-coding / kimi-coding-cn: the Kimi Coding Plan routes through
# api.kimi.com/coding (Anthropic Messages wire) which Kimi's own docs
# describe as having no image_in capability. Vision lives on the separate
# Kimi Platform (api.moonshot.ai, OpenAI-wire, pay-as-you-go).  See #17076.
_PROVIDERS_WITHOUT_VISION: frozenset = frozenset({
    "kimi-coding",
    "kimi-coding-cn",
})

# OpenRouter app attribution headers (base — always sent).
# `X-Title` is the canonical attribution header OpenRouter's dashboard
# reads; the previous `X-OpenRouter-Title` label was not recognized there.
_OR_HEADERS_BASE = {
    "HTTP-Referer": "https://hermes-agent.nousresearch.com",
    "X-Title": "Hermes Agent",
    "X-OpenRouter-Categories": "productivity,cli-agent",
}

# Truthy values for boolean env-var parsing.
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _apply_user_default_headers(headers: dict | None) -> dict | None:
    """Merge user-configured ``model.default_headers`` onto resolved headers.

    User values take precedence over provider/SDK defaults, mirroring the main
    agent client (``AIAgent._apply_user_default_headers``). This lets a
    ``custom`` OpenAI-compatible endpoint behind a gateway/WAF that rejects the
    OpenAI SDK's identifying headers (``User-Agent: OpenAI/Python ...``,
    ``X-Stainless-*``) override them for auxiliary calls too — otherwise the
    main turn would succeed but title/compression/vision calls to the same
    endpoint would still fail. (#40033)

    Returns the merged dict, or the original ``headers`` (possibly ``None``)
    when nothing is configured. No allocation when there are no overrides.
    """
    try:
        from hermes_cli.config import cfg_get, load_config
        _cfg = load_config()
        user_headers = cfg_get(_cfg, "model", "default_headers")
        # ``model.extra_headers`` is an accepted alias (matches the
        # per-provider ``extra_headers`` key on providers/custom_providers
        # entries). When both are set they merge, with ``extra_headers``
        # winning. SECURITY: values may carry credentials — never log them.
        alias_headers = cfg_get(_cfg, "model", "extra_headers")
        if isinstance(alias_headers, dict) and alias_headers:
            merged_user: dict = {}
            if isinstance(user_headers, dict):
                merged_user.update(user_headers)
            merged_user.update(alias_headers)
            user_headers = merged_user
    except Exception:
        return headers
    if not isinstance(user_headers, dict) or not user_headers:
        return headers
    merged = dict(headers or {})
    for key, value in user_headers.items():
        if value is None:
            continue
        merged[str(key)] = str(value)
    return merged or headers


def build_or_headers(or_config: dict | None = None) -> dict:
    """Build OpenRouter headers, optionally including response-cache headers.

    Precedence for response cache: env var > config.yaml > default (enabled).

    Environment variables:
        ``HERMES_OPENROUTER_CACHE`` — truthy (``1``/``true``/``yes``/``on``)
            enables caching; ``0``/``false``/``no``/``off`` disables.
            Overrides ``openrouter.response_cache`` in config.yaml.
        ``HERMES_OPENROUTER_CACHE_TTL`` — integer seconds (1-86400).
            Overrides ``openrouter.response_cache_ttl`` in config.yaml.

    *or_config* is the ``openrouter`` section from config.yaml.  When *None*,
    falls back to reading config from disk via ``load_config_readonly()``.
    """
    headers = dict(_OR_HEADERS_BASE)

    # Resolve config from disk if not provided.
    if or_config is None:
        try:
            from hermes_cli.config import load_config_readonly
            or_config = load_config_readonly().get("openrouter", {})
        except Exception:
            or_config = {}

    # Determine cache enabled: env var overrides config.
    env_cache = os.environ.get("HERMES_OPENROUTER_CACHE", "").strip().lower()
    if env_cache:
        cache_enabled = env_cache in _TRUTHY_ENV_VALUES
    else:
        cache_enabled = or_config.get("response_cache", False)

    if not cache_enabled:
        return headers

    headers["X-OpenRouter-Cache"] = "true"

    # Determine TTL: env var overrides config.
    env_ttl = os.environ.get("HERMES_OPENROUTER_CACHE_TTL", "").strip()
    if env_ttl:
        if env_ttl.isdigit():
            ttl = int(env_ttl)
            if 1 <= ttl <= 86400:
                headers["X-OpenRouter-Cache-TTL"] = str(ttl)
    else:
        ttl = or_config.get("response_cache_ttl", 300)
        if isinstance(ttl, (int, float)) and 1 <= ttl <= 86400:
            headers["X-OpenRouter-Cache-TTL"] = str(int(ttl))

    return headers


# NVIDIA NIM cloud billing attribution.  Keep this host-gated because the
# nvidia provider also supports local/on-prem NIM endpoints via NVIDIA_BASE_URL.
_NVIDIA_NIM_CLOUD_HEADERS = {
    "X-BILLING-INVOKE-ORIGIN": "HermesAgent",
}


def build_nvidia_nim_headers(base_url: str | None) -> dict:
    """Return NVIDIA NIM cloud attribution headers for build.nvidia.com traffic."""
    if base_url_host_matches(str(base_url or ""), "integrate.api.nvidia.com"):
        return dict(_NVIDIA_NIM_CLOUD_HEADERS)
    return {}


# Vercel AI Gateway app attribution headers. HTTP-Referer maps to
# referrerUrl and X-Title maps to appName in the gateway's analytics.
from hermes_cli import __version__ as _HERMES_VERSION

_AI_GATEWAY_HEADERS = {
    "HTTP-Referer": "https://hermes-agent.nousresearch.com",
    "X-Title": "Hermes Agent",
    "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
}

# Nous Portal extra_body for product attribution.
# Callers should pass this as extra_body in chat.completions.create()
# when the auxiliary client is backed by Nous Portal.
#
# The tags are computed from agent.portal_tags so the client= marker stays
# in lockstep with hermes_cli.__version__ across every Portal call site
# (main loop, aux, compression, web_extract). Do not inline a literal here;
# see agent/portal_tags.py for the rationale.
from agent.portal_tags import nous_portal_tags as _nous_portal_tags


def _nous_extra_body() -> dict:
    """Return a fresh Nous Portal ``extra_body`` dict.

    Computed at call time so a hot-reloaded ``hermes_cli.__version__`` is
    reflected without restarting long-running processes.
    """
    return {"tags": _nous_portal_tags()}


# Backwards-compatible module attribute. Some callers (tests, third-party
# plugins) read ``NOUS_EXTRA_BODY`` directly; keep it as a snapshot of the
# current tags. Callers that need the freshest value should call
# ``_nous_extra_body()`` or import ``nous_portal_tags`` directly.
NOUS_EXTRA_BODY = _nous_extra_body()

# Set at resolve time — True if the auxiliary client points to Nous Portal
auxiliary_is_nous: bool = False

# Default auxiliary models per provider
_OPENROUTER_MODEL = "google/gemini-3.6-flash"
_NOUS_MODEL = "google/gemini-3.6-flash"
_NOUS_DEFAULT_BASE_URL = "https://inference-api.nousresearch.com/v1"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
_AUTH_JSON_PATH = get_hermes_home() / "auth.json"

# Codex OAuth endpoint used when a caller explicitly requests
# provider="openai-codex".  There is deliberately no hardcoded default
# model: the set of models OpenAI accepts on this endpoint for
# ChatGPT-account auth is an undocumented, shifting allow-list, and
# pinning one here has drifted silently twice (gpt-5.3-codex → gpt-5.2-codex
# → gpt-5.4 over 6 weeks in early 2026).  Callers must pass the model
# they want explicitly (from config.yaml model.model, auxiliary.<task>.model,
# or the user's active Codex model selection).
_CODEX_AUX_BASE_URL = "https://chatgpt.com/backend-api/codex"


def _is_official_codex_base_url(base_url: str) -> bool:
    """Identify OpenAI's Codex endpoint without matching custom proxies."""
    try:
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        return (
            parsed.scheme == "https"
            and parsed.hostname == "chatgpt.com"
            and parsed.port in (None, 443)
            and (path == "/backend-api/codex" or path.startswith("/backend-api/codex/"))
        )
    except (TypeError, ValueError):
        return False


def _codex_cloudflare_headers(
    access_token: str, *, base_url: str = _CODEX_AUX_BASE_URL,
) -> Dict[str, str]:
    """Identity and account headers for chatgpt.com/backend-api/codex.

    OpenAI requires third-party harnesses to identify themselves. Requests to
    the official endpoint always send Hermes' originator and version. Custom
    endpoints retain the existing compatibility identity. In either case,
    preserve ``ChatGPT-Account-ID`` from the OAuth JWT's
    ``chatgpt_account_id`` claim.

    Malformed tokens are tolerated — we drop the account-ID header rather than
    raise, so a bad token still surfaces as an auth error (401) instead of a
    crash at client construction.
    """
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Hermes Agent)",
        "originator": "codex_cli_rs",
    }
    if _is_official_codex_base_url(base_url):
        from hermes_cli import __version__

        headers.update({
            "User-Agent": f"HermesAgent/{__version__}",
            "originator": "hermes-agent",
        })
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    try:
        import base64
        parts = access_token.split(".")
        if len(parts) < 2:
            return headers
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        acct_id = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if isinstance(acct_id, str) and acct_id:
            headers["ChatGPT-Account-ID"] = acct_id
    except Exception:
        pass
    return headers


def _apply_required_codex_headers(
    client_kwargs: Dict[str, Any], *, access_token: str, base_url: str,
) -> None:
    """Keep required Codex identity after user/provider header overrides."""
    if not _is_official_codex_base_url(base_url):
        return
    required = _codex_cloudflare_headers(access_token, base_url=base_url)
    required_names = {name.lower() for name in required}
    existing = client_kwargs.get("default_headers") or {}
    client_kwargs["default_headers"] = {
        **{name: value for name, value in existing.items()
           if str(name).lower() not in required_names},
        **required,
    }


# Hosts that expose BOTH an Anthropic-style ``…/anthropic`` path and a sibling
# OpenAI-compatible ``…/v1`` (or vendor-specific OpenAI path). Unconditional
# ``/anthropic`` → ``/v1`` rewrites break Anthropic-only gateways such as
# Alibaba Bailian Token Plan (#83642).
#
# Matching is anchored to the URL *host* (exact domain or subdomain suffix /
# ``api.minimax.*`` prefix) — never a substring of the whole URL, so a path
# that merely contains ``api.minimax`` cannot false-positive.
_DUAL_SURFACE_ANTHROPIC_HOST_SUFFIXES = (
    "minimax.io",
    "minimax.chat",
    "minimaxi.com",
)
_DUAL_SURFACE_ANTHROPIC_HOST_PREFIXES = ("api.minimax.",)


def _is_dual_surface_anthropic_host(url: str) -> bool:
    """True when the URL's host is a known dual-surface (MiniMax-family) host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for suffix in _DUAL_SURFACE_ANTHROPIC_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return any(host.startswith(prefix) for prefix in _DUAL_SURFACE_ANTHROPIC_HOST_PREFIXES)


def _to_openai_base_url(base_url: str) -> str:
    """Normalize dual-surface Anthropic URLs to OpenAI-compatible format.

    MiniMax (and MiniMax-CN) expose an ``/anthropic`` endpoint for the Anthropic
    Messages API and a separate ``/v1`` endpoint for OpenAI chat completions.
    The auxiliary client often uses the OpenAI SDK, so those dual-surface hosts
    must hit ``/v1``.

    Anthropic-**only** custom gateways (path ends in ``/anthropic`` but has no
    sibling ``/v1``) must keep their path; rewriting them to ``/v1`` yields 404
    on compression/vision/title_generation (#83642).

    ZAI exposes its general API and Coding Plan on separate endpoints.  Its
    Anthropic-compatible Coding Plan endpoint maps to ``/api/coding/paas/v4``
    on the OpenAI wire, not the general ``/api/paas/v4`` endpoint.  Rewriting
    to the general endpoint changes the billing pool and can return a false
    insufficient-balance error for a valid Coding Plan key.
    """
    url = str(base_url or "").strip().rstrip("/")
    if url.endswith("/anthropic"):
        # ZAI uses /api/anthropic for the Coding Plan's Anthropic wire.  The
        # matching OpenAI-wire endpoint is /api/coding/paas/v4; /api/paas/v4
        # is the independently billed general API.
        if base_url_host_matches(url, "open.bigmodel.cn") or base_url_host_matches(url, "api.z.ai"):
            rewritten = url[: -len("/anthropic")] + "/coding/paas/v4"
            logger.debug("Auxiliary client: rewrote ZAI base URL %s → %s", url, rewritten)
            return rewritten
        if _is_dual_surface_anthropic_host(url):
            rewritten = url[: -len("/anthropic")] + "/v1"
            logger.debug("Auxiliary client: rewrote dual-surface base URL %s → %s", url, rewritten)
            return rewritten
        # Anthropic-only gateway: leave the /anthropic path alone.
        logger.debug(
            "Auxiliary client: keeping Anthropic-only base URL %s (no dual-surface host match)",
            url,
        )
        return url
    if base_url_host_matches(url, "api.kimi.com") and url.endswith("/coding"):
        # Kimi Code uses /coding/v1/messages for Anthropic SDK (appends /v1/messages)
        # but /coding/v1/chat/completions for OpenAI SDK (appends /chat/completions)
        # Without /v1 here, OpenAI SDK hits /coding/chat/completions — a 404.
        rewritten = url + "/v1"
        logger.debug("Auxiliary client: rewrote Kimi base URL %s → %s", url, rewritten)
        return rewritten
    return url


def _select_pool_entry(provider: str) -> Tuple[bool, Optional[Any]]:
    """Return (pool_exists_for_provider, selected_entry)."""
    try:
        pool = load_pool(provider)
    except Exception as exc:
        logger.debug("Auxiliary client: could not load pool for %s: %s", provider, exc)
        return False, None
    if not pool or not pool.has_credentials():
        return False, None
    try:
        return True, pool.select()
    except Exception as exc:
        logger.debug("Auxiliary client: could not select pool entry for %s: %s", provider, exc)
        return True, None


def _peek_pool_entry(provider: str) -> Optional[Any]:
    """Best-effort current/next pool entry without mutating selection order."""
    try:
        pool = load_pool(provider)
    except Exception as exc:
        logger.debug("Auxiliary client: could not load pool for %s (peek): %s", provider, exc)
        return None
    if not pool or not pool.has_credentials():
        return None
    try:
        current_fn = getattr(pool, "current", None)
        if callable(current_fn):
            current = current_fn()
            if current is not None:
                return current
        peek_fn = getattr(pool, "peek", None)
        if callable(peek_fn):
            return peek_fn()
    except Exception as exc:
        logger.debug("Auxiliary client: could not peek pool entry for %s: %s", provider, exc)
    return None


def _pool_runtime_api_key(entry: Any) -> str:
    if entry is None:
        return ""
    # Use the PooledCredential.runtime_api_key property which handles
    # provider-specific fallback (e.g. agent_key for nous).
    key = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", "")
    return str(key or "").strip()


def _pool_runtime_base_url(entry: Any, fallback: str = "") -> str:
    if entry is None:
        return str(fallback or "").strip().rstrip("/")
    if getattr(entry, "provider", None) == "nous":
        # Funnel through the canonical auth-layer reader so the env override
        # shares one normalization path with the rest of the NOUS resolution.
        from hermes_cli.auth import _nous_inference_env_override

        env_url = _nous_inference_env_override()
        if env_url:
            return env_url
    # runtime_base_url handles provider-specific logic (e.g. nous prefers inference_base_url).
    # Fall back through inference_base_url and base_url for non-PooledCredential entries.
    url = (
        getattr(entry, "runtime_base_url", None)
        or getattr(entry, "inference_base_url", None)
        or getattr(entry, "base_url", None)
        or fallback
    )
    return str(url or "").strip().rstrip("/")


# Hostnames (lowercase, exact) that the auxiliary Anthropic path is allowed to
# be pointed at via config.yaml model.base_url. Anything else falls back to the
# Anthropic default — operators routing main-session traffic through a
# non-Anthropic host (e.g. OpenRouter, OpenAI) with provider=anthropic in config
# must NOT have that foreign host leak into the auxiliary client. See #52608.
_ANTHROPIC_COMPATIBLE_HOSTS = frozenset({
    "api.anthropic.com",
})


def _is_anthropic_compatible_host(url: str) -> bool:
    """Return True if ``url`` is an Anthropic endpoint we trust for aux calls.

    Trust the native Anthropic hosts, plus Anthropic-compatible gateways that
    expose the native Messages protocol under a ``/anthropic`` path suffix
    (MiniMax, Zhipu GLM, LiteLLM-style relays, self-hosted proxies). That suffix
    is the same convention ``runtime_provider._detect_api_mode_for_url`` uses to
    route ``provider: anthropic`` on the primary path, and ``_wrap_if_needed``
    uses to pick the Anthropic wire transport — without this, ``_try_anthropic``
    discards a configured ``model.base_url`` for auxiliary and fallback calls and
    forces ``https://api.anthropic.com``, so those calls diverge from the main
    agent's endpoint (and fail when the gateway, not Anthropic, holds auth).

    A bare non-Anthropic base_url (e.g. a stale ``openrouter.ai/api/v1`` left on
    ``provider: anthropic``) still returns False — the guard #52608 added.
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if host in _ANTHROPIC_COMPATIBLE_HOSTS:
            return True
        path = (parsed.path or "").rstrip("/").lower()
        return path.endswith("/anthropic") or path.endswith("/anthropic/v1")
    except Exception:
        return False


def _nous_min_key_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("HERMES_NOUS_MIN_KEY_TTL_SECONDS", "1800")))
    except (TypeError, ValueError):
        return 1800


def _scoped_key_env(name: str) -> str:
    """Read a provider API key env var through the profile secret scope.

    Auxiliary-client resolution runs both inside agent turns (secret scope
    installed — its verdict is authoritative under multiplex, so a scoped
    miss must NOT borrow another profile's process-env key) and on unscoped
    startup/CLI probe paths, which keep the legacy ``os.environ`` read via
    the ``UnscopedSecretError`` fallback (Slack pattern, #59739).
    """
    if not name:
        return ""
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret

        try:
            return (get_secret(name) or "").strip()
        except UnscopedSecretError:
            pass
    except Exception:
        pass
    return (os.getenv(name) or "").strip()
