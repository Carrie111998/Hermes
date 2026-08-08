"""Factual summaries for transport-timeout errors with empty messages.

httpx timeout exceptions (``ReadTimeout``, ``ConnectTimeout``, ``PoolTimeout``,
``WriteTimeout``) stringify to an EMPTY string, and the OpenAI SDK's
``APITimeoutError`` stringifies to the generic "Request timed out.".  When one
of these survives the retry loop, ``AIAgent._summarize_api_error`` fell through
to its ``raw[:500]`` fallback and produced a blank (or near-blank) summary —
the user saw ``"API call failed after 6 retries: "`` with nothing after the
colon, and an operator could not tell a TLS abort from a reset connection from
a deterministic self-inflicted timeout fire.

This module is a pure classifier (no network, no agent instance) that turns
the exception *type* into a factual, phase-specific message with the
configured timeout value embedded and the exact config knob to raise:

=====================  =====================================================
Case                   Message
=====================  =====================================================
Connect-phase timeout  ``connect timeout: no connection established with the
                       provider``
Read/pool timeout      ``read timeout: no response received within <N>s —
                       consider raising providers.<provider>.
                       request_timeout_seconds in ~/.hermes/config.yaml``
Non-timeout            ``None`` (caller falls through to existing handling)
=====================  =====================================================

Ported from block/buzz#4959 (``timeout_message`` pure fn over
``{is_connect, llm_timeout, phase}`` in ``crates/buzz-agent/src/llm.rs``),
adapted to hermes-agent's exception-type landscape and per-provider
``request_timeout_seconds`` config.
"""

from __future__ import annotations

import os
from typing import Optional

# Exception type names that indicate the CONNECT phase timed out: no TCP/TLS
# connection was ever established.  Raising the read timeout will not help;
# the endpoint is unreachable or the connect window is too small.
_CONNECT_TIMEOUT_TYPES = frozenset({
    "ConnectTimeout",           # httpx
    "ConnectTimeoutError",      # urllib3
})

# Exception type names that indicate the connection was established but no
# (or no further) response bytes arrived within the read window.  This is the
# deterministic self-inflicted case: a slow model generation legitimately
# exceeding the configured timeout looks identical to a network fault unless
# we say which timer fired.
_READ_TIMEOUT_TYPES = frozenset({
    "ReadTimeout",              # httpx
    "WriteTimeout",             # httpx
    "PoolTimeout",              # httpx
    "TimeoutException",         # httpx base class
    "ReadTimeoutError",         # urllib3
    "APITimeoutError",          # openai SDK ("Request timed out.")
})


def _resolve_configured_timeout(
    provider: Optional[str], model: Optional[str]
) -> Optional[float]:
    """Best-effort lookup of the request timeout that governed the call.

    Mirrors the resolution order in ``agent/chat_completion_helpers.py``:
    per-model / per-provider ``request_timeout_seconds`` from config.yaml
    wins; otherwise the ``HERMES_API_TIMEOUT`` env default (1800s).  Returns
    ``None`` when nothing can be resolved (e.g. config unreadable in tests).
    """
    if provider:
        try:
            from hermes_cli.timeouts import get_provider_request_timeout
            cfg = get_provider_request_timeout(provider, model)
            if cfg is not None:
                return float(cfg)
        except Exception:
            pass
    raw = os.environ.get("HERMES_API_TIMEOUT")
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return None


def summarize_timeout_error(
    error: BaseException,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Return a factual timeout summary, or ``None`` for non-timeout errors.

    Pure over the exception's type name (the exceptions themselves carry no
    useful message — that is the bug).  ``provider``/``model`` are optional
    context used to embed the configured timeout value and name the exact
    config knob; when absent the message still names the knob generically.
    """
    name = type(error).__name__

    if name in _CONNECT_TIMEOUT_TYPES:
        return (
            "connect timeout: no connection established with the provider "
            "(endpoint unreachable or connect window too small — check the "
            "base_url and network path)"
        )

    if name not in _READ_TIMEOUT_TYPES:
        return None

    timeout_s = _resolve_configured_timeout(provider, model)
    if timeout_s is not None:
        within = f"within {timeout_s:g}s"
    else:
        within = "within the configured request timeout"

    if provider:
        knob = f"`providers.{provider}.request_timeout_seconds`"
    else:
        knob = "`providers.<provider>.request_timeout_seconds`"

    return (
        f"read timeout: no response received {within} — the generation may "
        f"legitimately need longer; consider raising {knob} in "
        "`~/.hermes/config.yaml`"
    )
