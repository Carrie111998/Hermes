"""Narrow, read-only proof of an authenticated inbound gateway message.

``TrustedToolInvocationContext`` is built exclusively from the gateway's own
task-local ``ContextVar``-bound session state (see
:mod:`gateway.session_context`) — never from model tool-call arguments, a
plain mapping, or an environment variable. A plugin that must discover a
sender's identity for a security-sensitive action (e.g. picking a message
delivery target) should accept this typed object as a tool-handler kwarg and
reject anything else with a nominal ``isinstance`` check, so a model can never
forge one through tool-call arguments or a duck-typed lookalike.

This is the seam a standalone plugin imports as
``gateway.tool_context.TrustedToolInvocationContext``; core has no plugin-
specific branch here — the type and its builder are generic to any tool
handler that needs proof of the authenticated sender behind the current turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gateway.session import Platform, SessionSource

# Platforms this seam currently vouches for. Extend deliberately: every entry
# here is a platform whose adapter has been audited to set `is_bot` correctly
# and to bind session vars only after the inbound message is authenticated.
_TRUSTED_PLATFORMS = frozenset({Platform.FEISHU.value})


@dataclass(frozen=True)
class TrustedToolInvocationContext:
    """Typed, post-auth proof of who sent the message driving this tool call.

    ``source`` carries the authenticated sender's platform/user_id/is_bot.
    ``context_id`` and ``anchor_id`` tie the proof to one authenticated
    inbound turn — a consumer keys any state it derives on both so a stale or
    cross-session context can't be replayed.
    """

    source: SessionSource
    context_id: str
    anchor_id: str
    authenticated: bool = True

    def require_valid(self) -> "TrustedToolInvocationContext":
        """Raise if this is not a usable, authenticated proof.

        Defense in depth: :func:`build_trusted_tool_invocation_context`
        already refuses to construct an instance unless every field below is
        present, so this should never raise for a builder-constructed
        context. It exists so a consumer's own ``value.require_valid()``
        check has real validation behind it rather than a no-op.
        """
        if not self.authenticated:
            raise PermissionError("trusted tool invocation context is not authenticated")
        if not self.anchor_id:
            raise ValueError("trusted tool invocation context is missing an inbound message anchor")
        if not self.context_id:
            raise ValueError("trusted tool invocation context is missing a context id")
        if self.source.is_bot:
            raise PermissionError("trusted tool invocation context sender is a bot")
        return self


def build_trusted_tool_invocation_context() -> Optional[TrustedToolInvocationContext]:
    """Build a context strictly from the current task's bound session state.

    Returns ``None`` (fail-closed) unless ALL of the following hold for the
    CURRENT task-local session:

    - the platform is one of ``_TRUSTED_PLATFORMS`` (Feishu today);
    - the sender is not a bot/webhook;
    - the inbound message id (the reply/callback anchor) is present;
    - the sender's user id and a stable context id (session key, falling
      back to session id) are present.

    Reads ONLY task-local ContextVars via
    ``gateway.session_context.get_session_var_strict`` / ``session_is_bot`` —
    never model tool-call arguments, a plain mapping, or ``os.environ`` (that
    fallback exists in ``get_session_env`` for CLI/cron compatibility and is
    deliberately not used here, since it would let a same-process CLI/cron/
    test env var masquerade as a real inbound gateway message). CLI, the API
    server, cron, and any other surface that never called
    ``set_session_vars`` for this task always resolve to ``None`` here.
    """
    from gateway.session_context import get_session_var_strict, session_is_bot

    if session_is_bot():
        return None

    platform = (get_session_var_strict("HERMES_SESSION_PLATFORM") or "").strip().lower()
    if platform not in _TRUSTED_PLATFORMS:
        return None

    user_id = (get_session_var_strict("HERMES_SESSION_USER_ID") or "").strip()
    anchor_id = (get_session_var_strict("HERMES_SESSION_MESSAGE_ID") or "").strip()
    context_id = (
        (get_session_var_strict("HERMES_SESSION_KEY") or "").strip()
        or (get_session_var_strict("HERMES_SESSION_ID") or "").strip()
    )
    if not user_id or not anchor_id or not context_id:
        return None

    source = SessionSource(
        platform=Platform(platform),
        chat_id=(get_session_var_strict("HERMES_SESSION_CHAT_ID") or "").strip(),
        user_id=user_id,
        message_id=anchor_id,
        is_bot=False,
    )
    return TrustedToolInvocationContext(
        source=source,
        context_id=context_id,
        anchor_id=anchor_id,
    )
