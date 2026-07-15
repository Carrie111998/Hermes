"""ACP client runtime — one turn through an ACP-compliant agent subprocess.

Extracted from AIAgent to keep the agent loop file focused.
Takes the parent AIAgent as its first argument (``agent``).
AIAgent keeps thin forwarder methods for backward compatibility.

``run_acp_client_turn`` — drives one turn through an
``ACPClientSession`` subprocess (used when api_mode == "acp_client").
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def run_acp_client_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
) -> Dict[str, Any]:
    """ACP client runtime path. Hands the entire turn to an ACP-compliant
    agent subprocess and projects its streaming events back into Hermes'
    messages list so memory/skill review keep working.

    Called from run_conversation() when agent.api_mode == "acp_client".
    Returns the same dict shape as the chat_completions path.

    Lazy session: one ACPClientSession per AIAgent instance.
    Spawned on first turn, reused across turns; kernel reclaims stdin on
    process exit — no explicit teardown hook on AIAgent.
    """
    from agent.transports.acp_client_session import ACPClientSession

    if not hasattr(agent, "_acp_session") or agent._acp_session is None:
        command = getattr(agent, "acp_command", None) or "acp-agent"
        args = getattr(agent, "acp_args", None) or []

        # on_delta: bridge streaming text deltas to Hermes' live-output path.
        # _fire_stream_delta is the same hook the chat_completions path uses
        # (see conversation_loop.py). Falls back to None in contexts that
        # don't have streaming hooked up (cron, batch).
        on_delta = getattr(agent, "_fire_stream_delta", None)

        # model: read from agent.model so the ACP server uses the same model
        # Hermes is configured for, rather than its own default (Fix 1).
        # Falls back to None when not set -- ACPClientSession skips the
        # session/set_config_option call in that case.
        model = getattr(agent, "model", None) or None

        # mcp_servers: pre-translated at agent init; forwarded into session/new
        # so the ACP agent can connect to the user's MCP tools.  Empty list
        # when none configured (current default).
        mcp_servers = getattr(agent, "acp_mcp_servers", None) or []

        # session_meta: opaque vendor _meta dict read from the agent so a
        # coding-tool plugin (or future trusted config seam) can inject
        # vendor-specific session/new _meta without the core knowing about any
        # particular ACP server.  Defaults to None when not set.
        session_meta = getattr(agent, "acp_session_meta", None)

        # Approval callback: use the shared bridge that connects ACP
        # permission requests to Hermes' standard approval gate — CLI
        # interactive prompt, Gateway blocking approval (Matrix/Telegram/etc.),
        # or fail-closed when neither is available.  This fixes the silent-
        # reject bug where Gateway sessions with approvals.mode: smart/manual
        # had no callback and the core's fail-closed default kicked in.
        try:
            from agent.transports.acp_approval import make_acp_approval_callback
            approval_callback = make_acp_approval_callback()
        except Exception:
            logger.debug(
                "ACP client: make_acp_approval_callback failed; "
                "falling back to bare CLI callback lookup",
                exc_info=True,
            )
            try:
                from tools.terminal_tool import _get_approval_callback
                approval_callback = _get_approval_callback()
            except Exception:
                approval_callback = None

        # Approval bypass: when the user has opted out of Hermes approvals
        # (--yolo / HERMES_YOLO_MODE / approvals.mode:off / /yolo session
        # toggle), honor that so the ACP agent's own permission profile is
        # the policy gate instead of double-gating with a missing UI.
        # Mirrors codex_runtime.py's auto_approve_requests pattern.
        auto_approve_permissions = False
        try:
            from tools.approval import is_approval_bypass_active
            auto_approve_permissions = is_approval_bypass_active()
        except Exception:
            logger.debug(
                "ACP client: approval-bypass lookup failed; "
                "keeping fail-closed default",
                exc_info=True,
            )

        agent._acp_session = ACPClientSession(
            command=command,
            args=list(args),
            model=model,
            mcp_servers=mcp_servers,
            session_meta=session_meta,
            on_delta=on_delta,
            approval_callback=approval_callback,
            auto_approve_permissions=auto_approve_permissions,
        )

    # NOTE: the user message is ALREADY appended to messages by the
    # standard run_conversation() flow before the early return reaches us.
    # Do NOT append again — that would duplicate.

    # cwd priority: agent.session_cwd > HERMES_ACP_SESSION_CWD env > os.getcwd()
    # HERMES_ACP_SESSION_CWD lets operators point the ACP session at a per-agent
    # sandbox directory (containing CLAUDE.md + .claude/settings.local.json) on
    # the gateway launch env without requiring a new config key in the provider
    # resolver chain.  Production Janet and janet_test run as separate processes
    # with distinct HERMES_HOME, so the env var is scoped to the right gateway.
    cwd = (
        getattr(agent, "session_cwd", None)
        or os.environ.get("HERMES_ACP_SESSION_CWD", "").strip()
        or os.getcwd()
    )

    try:
        turn = agent._acp_session.run_turn(user_input=user_message, cwd=cwd)
    except Exception as exc:
        logger.exception("ACP client turn failed")
        # Crash → unconditionally drop the session so the next turn
        # respawns from scratch instead of reusing a dead client.
        try:
            agent._acp_session.close()
        except Exception:
            pass
        agent._acp_session = None
        return {
            "final_response": (
                f"ACP client turn failed: {exc}. "
                f"Check acp_command/acp_args in your config."
            ),
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "error": str(exc),
            # Early-return path: the inbound user turn was already flushed
            # at turn start (turn_context._persist_session). Report
            # agent_persisted=True so the gateway does NOT re-INSERT it
            # (append_message has no dedup → #860/#42039 duplicate write).
            # Mirrors codex_runtime.py's crash-return contract.
            "agent_persisted": True,
        }

    # If the turn signalled the underlying client is wedged (deadline
    # blown, subprocess exited, protocol error), retire the session so
    # the next turn respawns the agent from scratch.
    if getattr(turn, "should_retire", False):
        logger.warning(
            "ACP client session retired (turn error: %s)",
            turn.error,
        )
        try:
            agent._acp_session.close()
        except Exception:
            pass
        agent._acp_session = None

    # Splice projected messages into the conversation. The session emits
    # standard {role, content} entries, which is what curator.py / sessions
    # DB expect.
    if turn.projected_messages:
        messages.extend(turn.projected_messages)

        # Persist the newly-projected assistant messages ourselves.
        # This path is an early return that bypasses conversation_loop, whose
        # normal per-step _persist_session() calls would otherwise flush them.
        # The inbound user turn was already flushed at turn start
        # (turn_context.py _persist_session), and _flush_messages_to_session_db
        # is idempotent via the intrinsic _DB_PERSISTED_MARKER — so this writes
        # ONLY the new projected rows and does NOT re-write the user turn.
        # Keeping the agent as the sole persister lets us return
        # agent_persisted=True below, so the gateway skips its own DB write and
        # we avoid the #860/#42039 duplicate user-message write. Mirrors
        # codex_runtime.py.
        if getattr(agent, "_session_db", None) is not None:
            try:
                agent._flush_messages_to_session_db(messages)
            except Exception:
                logger.debug(
                    "ACP client projected-message flush failed",
                    exc_info=True,
                )

    # Counter ticks for the agent-improvement loop.
    # _turns_since_memory and _user_turn_count are ALREADY incremented
    # in the run_conversation() pre-loop block before the early return,
    # so do NOT touch them here — that would double-count.
    # Only _iters_since_skill needs explicit increment, since the
    # chat_completions loop bumps it per tool iteration and that loop is
    # bypassed on this path.
    agent._iters_since_skill = (
        getattr(agent, "_iters_since_skill", 0) + turn.tool_iterations
    )

    # Check the skill nudge AFTER iters were incremented — same pattern
    # as the chat_completions path.
    should_review_skills = False
    if (
        agent._skill_nudge_interval > 0
        and agent._iters_since_skill >= agent._skill_nudge_interval
        and "skill_manage" in agent.valid_tool_names
    ):
        should_review_skills = True
        agent._iters_since_skill = 0

    # External memory provider sync — skip on interrupt/error to avoid
    # feeding partial transcripts to memory.
    if not turn.interrupted and turn.error is None:
        try:
            agent._sync_external_memory_for_turn(
                original_user_message=original_user_message,
                final_response=turn.final_text,
                interrupted=False,
                messages=messages,
            )
        except Exception:
            logger.debug("external memory sync raised", exc_info=True)

    # Background review fork — same cadence + signature as the default path.
    if (
        turn.final_text
        and not turn.interrupted
        and (should_review_memory or should_review_skills)
    ):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=should_review_memory,
                review_skills=should_review_skills,
            )
        except Exception:
            logger.debug("background review spawn raised", exc_info=True)

    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,  # one ACP session/prompt call maps to one logical API call
        "completed": not turn.interrupted and turn.error is None,
        "partial": turn.interrupted or turn.error is not None,
        "error": turn.error,
        # The ACP client runtime IS an early-return path that bypasses
        # conversation_loop, but we flush the projected assistant messages
        # ourselves above (see the _flush_messages_to_session_db call after
        # messages.extend). The inbound user turn was already flushed at turn
        # start (turn_context._persist_session) and the flush dedups via
        # _DB_PERSISTED_MARKER, so state.db ends up with each real message
        # exactly once. Report agent_persisted=True so the gateway skips its
        # own append_to_transcript DB write — writing again there would
        # re-INSERT the already-flushed user turn (append_message has no dedup),
        # reintroducing the #860 / #42039 duplicate-write bug. Mirrors
        # codex_runtime.py.
        "agent_persisted": True,
    }


__all__ = ["run_acp_client_turn"]
