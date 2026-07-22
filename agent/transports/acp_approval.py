"""ACP permission approval bridge — generic core bridge.

This module provides ``make_acp_approval_callback()``, a factory that returns
a callback suitable for ``ACPClientSession.approval_callback``.  It bridges
the ACP agent's ``session/request_permission`` to Hermes' standard approval
gate.

**Core bridge only — no kind classification.**

The generic bridge connects ACP permission requests to the best available
approval channel:

1. **CLI thread-local callback** — if ``tools.terminal_tool._get_approval_callback()``
   returns one, it is passed through directly (or in a minimal wrapper).  The
   callback itself decides approve/deny; no kind-based routing is done here.
2. **Gateway context** — if ``_is_gateway_approval_context()`` is True and a
   gateway notify callback is registered, escalate **all** permission requests
   to ``request_tool_approval()`` (human must approve).  No kind-based
   classification — every request gets the same treatment.
3. **Neither** — return a fail-closed callback that always returns ``"deny"``.

Kind-aware strategies (read → once, execute → guard, write → escalate) are
**plugin-owned**.  Plugins that need kind routing (e.g. claude-code-acp)
implement their own approval callback factory and do not use this generic
bridge for kind routing.  See ``claude_code_acp/approval.py`` for the
Claude-specific implementation.

**Dynamic approval-bypass wrapper.**  Whichever channel is resolved above,
``make_acp_approval_callback()`` wraps the returned callback in a bypass-aware
wrapper that checks ``is_approval_bypass_active()`` (yolo / ``approvals.mode:
off``) on **every** invocation — before any gateway round-trip or fail-closed
deny.  When bypass is active the wrapper returns ``"once"`` immediately and
skips the underlying channel.  This closes the gap where the fail-closed paths
(gateway-without-notify and neither-channel) would deny even though the user
had enabled yolo/bypass: those paths never reach ``_run_approval_gate``, which
was previously the only place the bypass was checked.  The check is dynamic
(re-evaluated per call, so a ``/yolo`` toggled after session creation is
honored) and fail-safe (if the check itself raises, the wrapper falls through
to the underlying callback so the approval flow is never broken by the check).

``auto_approve_permissions`` is NOT touched here — it stays driven solely by
``is_approval_bypass_active()`` (mode:off / yolo) in the caller.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def make_acp_approval_callback() -> Optional[Callable[..., str]]:
    """Return a callback for ``ACPClientSession.approval_callback``.

    The returned callable has the signature::

        (command_label: str, description: str, *, allow_permanent: bool, kind: str) -> str

    and returns one of ``"once"``, ``"session"``, ``"always"``, or ``"deny"``.

    This is the **generic** bridge — no kind-based routing.  All permission
    kinds are treated identically: the best available approval channel is used,
    or fail-closed if none is available.

    Resolution order:

    1. **CLI thread-local callback** — returned directly (or in a minimal
       wrapper that forwards the decision).
    2. **Gateway context with notify** — escalate to ``request_tool_approval()``.
    3. **Neither** — fail-closed ``"deny"``.

    Whichever channel is resolved, the returned callback is wrapped by
    ``_wrap_with_bypass_check()`` so that ``is_approval_bypass_active()``
    (yolo / ``approvals.mode: off``) is consulted on every invocation before
    any gateway round-trip or fail-closed deny.  When bypass is active the
    wrapper returns ``"once"`` immediately without invoking the underlying
    channel; if the bypass check itself raises, it falls through to the
    underlying channel (fail-safe).

    Returns ``None`` only when the core approval module itself is unavailable
    (import failure), so the caller falls back to the session's built-in
    fail-closed default.  In all other cases a non-None callback is returned.
    """
    # --- Resolve approval primitives early ---
    try:
        from tools.approval import (
            _is_gateway_approval_context,
            _gateway_notify_cbs,
            _lock,
            get_current_session_key,
            request_tool_approval,
        )
    except Exception:
        logger.debug(
            "ACP approval: tools.approval import failed; "
            "returning fail-closed callback"
        )
        return _wrap_with_bypass_check(
            _make_fail_closed_callback("approval module unavailable")
        )

    # --- 1. CLI thread-local callback (existing CLI UX) ---
    try:
        from tools.terminal_tool import _get_approval_callback

        cli_cb = _get_approval_callback()
    except Exception:
        cli_cb = None

    if cli_cb is not None:
        logger.debug("ACP approval: using CLI callback (passthrough)")
        return _wrap_with_bypass_check(cli_cb)

    # --- 2. Gateway context ---
    is_gateway = _is_gateway_approval_context()
    if is_gateway:
        # Check whether a notify callback is registered for this session.
        # If not, the gateway path in request_tool_approval would fall through
        # to submit_pending (non-blocking, returns approval_required — which
        # maps to approved=False → deny).  That is still correct: no listener
        # means no human can answer, so deny is the right outcome.
        session_key = get_current_session_key()
        with _lock:
            has_notify = session_key in _gateway_notify_cbs

        if has_notify:
            logger.debug(
                "ACP approval: gateway context with notify callback "
                "(session=%s) — using request_tool_approval bridge",
                session_key[:16] if session_key else "?",
            )
            return _wrap_with_bypass_check(
                _make_gateway_request_callback(
                    request_fn=request_tool_approval,
                )
            )
        else:
            logger.debug(
                "ACP approval: gateway context but no notify callback "
                "(session=%s) — fail-closed",
                session_key[:16] if session_key else "?",
            )
            return _wrap_with_bypass_check(
                _make_fail_closed_callback(
                    "gateway session has no approval notify channel"
                )
            )

    # --- 3. Neither CLI nor gateway ---
    logger.debug(
        "ACP approval: no CLI callback and not a gateway context — "
        "fail-closed"
    )
    return _wrap_with_bypass_check(
        _make_fail_closed_callback(
            "no interactive CLI or gateway approval channel available"
        )
    )


# --------------------------------------------------------------------------- #
# Internal: dynamic approval-bypass wrapper
# --------------------------------------------------------------------------- #


def _wrap_with_bypass_check(inner: Callable[..., str]) -> Callable[..., str]:
    """Wrap an approval callback with a dynamic approval-bypass check.

    On **every** invocation, before any gateway round-trip or fail-closed deny,
    check ``is_approval_bypass_active()`` (yolo / ``approvals.mode: off``).  If
    bypass is active, auto-approve with ``"once"`` and skip ``inner`` entirely.

    The check is:

    * **Dynamic** — re-evaluated on each call (not baked in at factory time),
      so a ``/yolo`` toggled after the session/callback was created is honored.
    * **Fail-safe** — if importing or calling ``is_approval_bypass_active``
      raises, fall through to ``inner`` so the bypass check can never break the
      approval flow.
    """

    def _bypass_aware_callback(
        command_label: str,
        description: str,
        *,
        allow_permanent: bool = False,
        kind: str = "",
        **kwargs,
    ) -> str:
        try:
            from tools.approval import is_approval_bypass_active

            if is_approval_bypass_active():
                logger.debug(
                    "ACP approval: bypass active (yolo/mode=off) — "
                    "auto-approve %r (kind=%r)",
                    command_label,
                    kind,
                )
                return "once"
        except Exception:
            logger.debug(
                "ACP approval: bypass check failed; falling through",
                exc_info=True,
            )
        return inner(
            command_label,
            description,
            allow_permanent=allow_permanent,
            kind=kind,
            **kwargs,
        )

    return _bypass_aware_callback


# --------------------------------------------------------------------------- #
# Internal: gateway request callback (generic, no kind routing)
# --------------------------------------------------------------------------- #


def _make_gateway_request_callback(
    request_fn: Callable,
) -> Callable[..., str]:
    """Build a generic gateway approval callback.

    Escalates **all** permission requests to ``request_tool_approval()``.
    No kind-based routing — the callback does not classify read/execute/write.
    Plugins that need kind routing implement their own callback factory.
    """

    def _callback(
        command_label: str,
        description: str,
        *,
        allow_permanent: bool = False,
        kind: str = "",
        **kwargs,
    ) -> str:
        tool_name = "acp_agent"
        reason = f"{command_label}: {description}" if description else command_label
        if kind:
            reason = f"[{kind}] {reason}"

        try:
            result = request_fn(
                tool_name=tool_name,
                reason=reason,
            )
        except Exception:
            logger.warning(
                "ACP approval: request_tool_approval raised — failing closed",
                exc_info=True,
            )
            return "deny"

        if isinstance(result, dict) and result.get("approved"):
            return "once"

        return "deny"

    return _callback


def _make_fail_closed_callback(reason: str) -> Callable[..., str]:
    """Return a callback that always returns ``"deny"`` with a debug log."""

    def _callback(
        command_label: str,
        description: str,
        *,
        allow_permanent: bool = False,
        kind: str = "",
        **kwargs,
    ) -> str:
        logger.debug(
            "ACP approval: fail-closed deny (%s) for %r (kind=%r)",
            reason,
            command_label,
            kind,
        )
        return "deny"

    return _callback


__all__ = ["make_acp_approval_callback"]
