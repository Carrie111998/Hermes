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


def _inject_hermes_history_to_acp(agent, hermes_session_id, provider, cwd, mapper):
    """Best-effort: carry this Hermes session's history into a fresh ACP session.

    Renders the Hermes conversation (read from the session DB) as a Claude
    Code JSONL transcript at ``~/.claude/projects/<sanitized-cwd>/<id>.jsonl``
    and pre-binds a new ACP session id to it, so the first ``ensure_started()``
    resumes into a session that already holds the conversation context. This
    is what makes a Native -> ACP runtime switch seamless.

    Never raises: a failure logs at debug and the caller proceeds with a plain
    (un-injected) ``session/new``. ``redact=False`` is intentional -- the
    transcript is written to a local file for context continuity, not uploaded.
    """
    import json
    import re
    import uuid as _uuid
    from pathlib import Path

    from agent.trace_upload import build_trace_jsonl, load_session_messages
    from agent.transports.acp_session_mapping import ACPSessionBinding

    if not cwd:
        cwd = os.getcwd()

    # Export this session's history. A brand-new session has no messages yet,
    # so there is nothing to carry over -- bail out without binding.
    messages, meta = load_session_messages(hermes_session_id)
    if not messages:
        return

    model = getattr(agent, "model", None) or meta.get("model") or ""

    # New ACP session id that resume will target. The transcript file is named
    # <id>.jsonl and the embedded sessionId field must equal <id> -- Claude
    # Code keys both off the same value (verified against real transcripts), so
    # a mismatch would make resume fail to load the history.
    acp_session_id = str(_uuid.uuid4())
    jsonl = build_trace_jsonl(
        messages,
        session_id=acp_session_id,
        model=model,
        cwd=cwd,
        redact=False,  # local file write for context continuity, not upload
    )
    if not jsonl.strip():
        return

    # Claude Code stores transcripts under ~/.claude/projects/<sanitized-cwd>/,
    # where EVERY non-alphanumeric char in the absolute cwd becomes '-'
    # (per-char, not collapsed: /home/nbot/.hermes -> -home-nbot--hermes, so
    # both '/' and '.' map to '-' independently). Matching this exactly is
    # required for resume to locate the file.
    sanitized = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    project_dir = Path.home() / ".claude" / "projects" / sanitized
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = project_dir / f"{acp_session_id}.jsonl"
    transcript_path.write_text(jsonl + "\n", encoding="utf-8")

    # Defensive: verify file is non-empty and sessionId matches filename.
    # Tolerates a malformed first line (unexpected shapes) -- only raises
    # when a real sessionId is present and disagrees with the file stem.
    if transcript_path.stat().st_size == 0:
        raise ValueError(f"JSONL file is empty: {transcript_path}")
    try:
        _first_line = json.loads(
            transcript_path.read_text(encoding="utf-8").split("\n", 1)[0]
        )
    except ValueError:
        _first_line = None
    embedded = (
        _first_line.get("sessionId") if isinstance(_first_line, dict) else None
    )
    if embedded is not None and embedded != acp_session_id:
        raise ValueError(
            f"sessionId mismatch: file stem={acp_session_id}, embedded={embedded}"
        )

    mapper.bind(ACPSessionBinding(
        hermes_session_id=hermes_session_id,
        acp_session_id=acp_session_id,
        provider=provider,
        cwd=cwd,
        model=model or None,
        status="active",
    ))
    logger.debug(
        "Injected Hermes history into ACP session %s (%d messages, cwd=%s)",
        acp_session_id[:8],
        len(messages),
        cwd,
    )


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

    # cwd priority: agent.session_cwd > HERMES_ACP_SESSION_CWD env > os.getcwd()
    # HERMES_ACP_SESSION_CWD lets operators point the ACP session at a per-agent
    # sandbox directory (containing CLAUDE.md + .claude/settings.local.json) on
    # the gateway launch env without requiring a new config key in the provider
    # resolver chain.  Production Janet and janet_test run as separate processes
    # with distinct HERMES_HOME, so the env var is scoped to the right gateway.
    # Computed up here, before session creation, because the creation block
    # also needs it for the Native->ACP history injection below.
    cwd = (
        getattr(agent, "session_cwd", None)
        or os.environ.get("HERMES_ACP_SESSION_CWD", "").strip()
        or os.getcwd()
    )

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

        # Session binding + Native->ACP history injection: when this Hermes
        # session has an id and a backing DB, give the ACPClientSession a
        # mapper so ensure_started() can resume a previously-bound ACP session,
        # and pre-seed a Claude Code transcript from the existing Hermes
        # conversation so a runtime switch keeps its context. Best-effort --
        # any failure leaves the session to start fresh via session/new.
        hermes_session_id = getattr(agent, "session_id", "") or ""
        provider = command
        mapper = None
        if hermes_session_id:
            try:
                from agent.transports.acp_session_mapping import (
                    SQLiteACPSessionMapper,
                )
                mapper = SQLiteACPSessionMapper()
            except Exception:
                logger.debug(
                    "ACP session mapper init failed",
                    exc_info=True,
                )
                mapper = None

        agent._acp_session = ACPClientSession(
            command=command,
            args=list(args),
            model=model,
            mcp_servers=mcp_servers,
            session_meta=session_meta,
            on_delta=on_delta,
            approval_callback=approval_callback,
            auto_approve_permissions=auto_approve_permissions,
            mapper=mapper,
            hermes_session_id=hermes_session_id,
            provider=provider,
        )

        # Inject existing Hermes history into the fresh ACP session (first
        # creation only). Skipped when a binding already exists -- the resume
        # path in ensure_started() handles that -- or when there is no backing
        # session DB to export from. Never blocks session creation.
        if (
            mapper
            and hermes_session_id
            and getattr(agent, "_session_db", None) is not None
        ):
            try:
                if not mapper.lookup(hermes_session_id, provider):
                    _inject_hermes_history_to_acp(
                        agent, hermes_session_id, provider, cwd, mapper
                    )
            except Exception:
                logger.debug(
                    "ACP history injection failed",
                    exc_info=True,
                )

    # NOTE: the user message is ALREADY appended to messages by the
    # standard run_conversation() flow before the early return reaches us.
    # Do NOT append again — that would duplicate.

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

    # Defensive: warn on potential silent resume failure
    if turn and not turn.final_text and not turn.error:
        logger.warning(
            "ACP session may have silently failed to load context: "
            "empty response with no error (session=%s)",
            getattr(agent._acp_session, '_session_id', '?'),
        )

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

    # ACP runtime manages its own context; review fork inherits session_id
    # and collides on the (hermes_session_id, provider) binding in
    # SQLiteACPSessionMapper. Skip to avoid binding collision.
    if (
        turn.final_text
        and not turn.interrupted
        and (should_review_memory or should_review_skills)
    ):
        logger.debug(
            "background review skipped in ACP client mode "
            "(session binding collision)"
        )

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
