"""ACP permission approval bridge — generic core bridge.

This module provides ``make_acp_approval_callback()``, a factory that returns
a callback suitable for ``ACPClientSession.approval_callback``.  It bridges
the ACP agent's ``session/request_permission`` to Hermes' standard approval
gate.

The bridge connects ACP permission requests to the best available approval
channel:

1. **CLI thread-local callback** — if ``tools.terminal_tool._get_approval_callback()``
   returns one, it is used as the underlying channel.
2. **Gateway context** — if ``_is_gateway_approval_context()`` is True and a
   gateway notify callback is registered, escalate permission requests to
   ``request_tool_approval()`` (human must approve).
3. **Neither** — return a fail-closed callback that always returns ``"deny"``.

**Execute routing.**  One kind gets special treatment on every channel:
``kind="execute"`` requests carrying a non-empty command are routed
through ``tools.approval.check_all_command_guards()`` — the exact pipeline
the native terminal tool uses (hardline blocks, user deny rules, yolo /
mode=off, permanent allowlist, tirith + dangerous-pattern detection, smart
approval, and interactive prompting when a channel is available).  The
command is taken from the ``command`` kwarg when supplied (the plugin-side
format adapter unwraps the ACP ``toolCall.title`` from ``Tool(command)``
form); otherwise ``command_label`` is used as a fallback (older plugins may
still pass the title verbatim).  The guard decision is authoritative
(approved → ``"once"``, otherwise ``"deny"``); the underlying channel is
not consulted for those requests, so the user is prompted at most once.
All other kinds (read/edit/write/…) keep the channel behavior above: CLI
passthrough, gateway ``request_tool_approval``, or fail-closed deny.

**Dynamic approval-bypass wrapper.**  Whichever channel is resolved above,
``make_acp_approval_callback()`` wraps the returned callback in a bypass-aware
wrapper that checks ``is_approval_bypass_active()`` (yolo / ``approvals.mode:
off``) on **every** invocation — before the execute command guards, any
gateway round-trip, or fail-closed deny.  The bypass wrapper stays
outermost so yolo / mode=off short-circuits everything.  When bypass is
active the wrapper returns ``"once"`` immediately and
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
import os
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def make_acp_approval_callback() -> Optional[Callable[..., str]]:
    """Return a callback for ``ACPClientSession.approval_callback``.

    The returned callable has the signature::

        (command_label: str, description: str, *, allow_permanent: bool, kind: str) -> str

    and returns one of ``"once"``, ``"session"``, ``"always"``, or ``"deny"``.

    The best available approval channel is used, or fail-closed if none is
    available.  ``kind="execute"`` requests carrying a non-empty command are
    routed through the native command-guard pipeline
    (``tools.approval.check_all_command_guards()``) instead of the channel —
    safe commands auto-approve and dangerous ones go through the same
    detection / smart-approval / prompt flow as the native terminal tool.
    All other kinds are escalated to the channel unchanged.

    Resolution order:

    1. **CLI thread-local callback** — used as the underlying channel.
    2. **Gateway context with notify** — escalate to ``request_tool_approval()``.
    3. **Neither** — fail-closed ``"deny"``.

    Whichever channel is resolved, the returned callback is wrapped by
    ``_wrap_channel()``: ``_wrap_with_execute_command_guards()`` sits directly
    on the channel (execute → guards, other kinds → channel), and
    ``_wrap_with_bypass_check()`` stays outermost so that
    ``is_approval_bypass_active()`` (yolo / ``approvals.mode: off``) is
    consulted on every invocation before the guards, any gateway round-trip,
    or fail-closed deny.  When bypass is active the wrapper returns ``"once"``
    immediately without invoking anything underneath; if the bypass check
    itself raises, it falls through to the underlying stack (fail-safe).

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
        return _wrap_channel(
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
        return _wrap_channel(cli_cb)

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
            return _wrap_channel(
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
            return _wrap_channel(
                _make_fail_closed_callback(
                    "gateway session has no approval notify channel"
                )
            )

    # --- 3. Neither CLI nor gateway ---
    logger.debug(
        "ACP approval: no CLI callback and not a gateway context — "
        "fail-closed"
    )
    return _wrap_channel(
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
# Internal: execute-kind command guards
# --------------------------------------------------------------------------- #


def _wrap_with_execute_command_guards(
    inner: Callable[..., str],
) -> Callable[..., str]:
    """Route ``kind="execute"`` through the native command-guard pipeline.

    For execute requests carrying a non-empty command, run
    ``tools.approval.check_all_command_guards()`` — the same pipeline the
    native terminal tool uses (hardline blocks, user deny rules, yolo /
    mode=off, permanent allowlist, tirith + dangerous-pattern detection,
    smart approval, and interactive prompting when a channel is available).

    The command is taken from the ``command`` kwarg when supplied (the
    plugin-side format adapter unwraps the ACP ``toolCall.title`` from
    ``Tool(command)`` form); otherwise ``command_label`` is used as a
    fallback for plugins that still pass the title verbatim.

    The guard decision is authoritative: approved → ``"once"``, otherwise
    ``"deny"``.  ``inner`` is NOT consulted for those requests, so the user
    is never prompted twice for the same command.

    Everything else falls through to ``inner`` unchanged:

    * non-execute kinds (read / edit / write / …) keep the channel behavior
      (CLI passthrough, gateway ``request_tool_approval``, fail-closed deny);
    * execute requests with neither a ``command`` kwarg nor a non-empty
      ``command_label`` (nothing to guard);
    * unexpected guard failures (logged; the resolved channel still gets to
      decide, preserving fail-closed semantics on fail-closed channels).
    """

    def _execute_guard_callback(
        command_label: str,
        description: str,
        *,
        allow_permanent: bool = False,
        kind: str = "",
        **kwargs,
    ) -> str:
        cmd_kwarg = kwargs.get("command")
        has_command = bool((cmd_kwarg and cmd_kwarg.strip()) or (command_label and command_label.strip()))
        if (kind or "").lower() == "execute" and has_command:
            try:
                from tools.approval import (
                    check_all_command_guards,
                )

                # Pass the CLI approval callback (if one is registered on this
                # thread) so a nested interactive prompt works exactly like the
                # native terminal tool.  Absent on headless / ACP threads.
                approval_cb = None
                try:
                    from tools.terminal_tool import _get_approval_callback

                    approval_cb = _get_approval_callback()
                except Exception:
                    approval_cb = None

                # Prefer an explicitly-unwrapped ``command`` kwarg (supplied
                # by the plugin's format adapter) over ``command_label``;
                # the label may still be in ``Tool(command)`` form when no
                # adapter is wired in, in which case we fall back to it.
                cmd = kwargs.get("command") or command_label.strip()
                result = check_all_command_guards(
                    cmd,
                    os.getenv("TERMINAL_ENV", "local"),
                    approval_callback=approval_cb,
                )
                if isinstance(result, dict) and result.get("approved"):
                    return "once"
                return "deny"
            except Exception:
                logger.warning(
                    "ACP approval: execute command guards failed; "
                    "falling through to channel",
                    exc_info=True,
                )
        return inner(
            command_label,
            description,
            allow_permanent=allow_permanent,
            kind=kind,
            **kwargs,
        )

    return _execute_guard_callback


def _wrap_channel(inner: Callable[..., str]) -> Callable[..., str]:
    """Stack the standard wrappers onto a resolved approval channel.

    Order matters: ``_wrap_with_execute_command_guards()`` sits directly on
    the channel (execute → native command guards, other kinds → channel), and
    ``_wrap_with_bypass_check()`` stays outermost so yolo / ``approvals.mode:
    off`` short-circuits everything — including the command guards.
    """
    return _wrap_with_bypass_check(_wrap_with_execute_command_guards(inner))


# --------------------------------------------------------------------------- #
# Internal: gateway request callback (non-execute kinds)
# --------------------------------------------------------------------------- #


def _make_gateway_request_callback(
    request_fn: Callable,
) -> Callable[..., str]:
    """Build a generic gateway approval callback.

    Escalates permission requests to ``request_tool_approval()`` without
    classifying them.  ``kind="execute"`` requests carrying a command never
    reach this callback — they are decided by the outer
    ``_wrap_with_execute_command_guards()`` wrapper; everything else
    (read/edit/write/…) is escalated to a human here.
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
        # Show the meaningful content (tool title / unwrapped command) inside
        # the approval prompt's fenced code block instead of a synthetic
        # "<acp_agent> (plugin approval rule)" placeholder. ``command_label``
        # is already normalized by the caller (ACP title, Codex apply_patch
        # summary, …) and carries no raw arguments/secrets, so it is safe to
        # display. An explicitly-unwrapped ``command`` kwarg wins for execute
        # kinds that slip through here without a command to guard.
        display_target = (kwargs.get("command") or command_label or "").strip()
        # Keep `reason` a short summary — the detail now lives in the fenced
        # block via display_target, so we only need the kind + description.
        if description:
            reason = f"[{kind}] {description}" if kind else description
        else:
            reason = f"[{kind}]" if kind else (command_label or "ACP permission request")

        try:
            result = request_fn(
                tool_name=tool_name,
                reason=reason,
                display_target=display_target or None,
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
