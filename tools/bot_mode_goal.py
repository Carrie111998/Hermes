"""Bot Mode bridge to Hermes' native persistent GoalManager.

``goal_manage`` exists for one reason: an agent in its canonical Bot Chat may
need to turn a natural-language standing objective into the same native
``GoalManager`` state that a human reaches through ``/goal`` and ``/subgoal``.

This module deliberately does NOT implement a second goal loop. It is only a
thin session-scoped tool wrapper around :mod:`hermes_cli.goals`.

Containment mirrors ``message_agent``:
- injected only into a Bot-Mode-managed canonical ``Bot Chat``;
- not registered in the global tool registry/toolsets;
- execution re-checks the Bot Chat gate;
- the calling session id is supplied server-side by the tool executor and is
  never model-controlled.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

GOAL_MANAGE_TOOL_NAME = "goal_manage"


def goal_manage_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": GOAL_MANAGE_TOOL_NAME,
            "description": (
                "Manage the CURRENT Bot Chat's real persistent Hermes goal using the native "
                "GoalManager. Use this when a natural-language objective should persist across "
                "turns. This does not simulate a goal and cannot target another session. "
                "For a new standing objective use action='set' with a concrete completion "
                "contract. Use action='add_subgoal' only for a newly discovered required "
                "criterion. This tool cannot pause, clear, resume, or replace an existing "
                "goal; those remain user/host controls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "status", "add_subgoal"],
                    },
                    "goal": {
                        "type": "string",
                        "description": "Standing goal text for action=set.",
                    },
                    "max_turns": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "Optional native goal turn budget for action=set.",
                    },
                    "outcome": {
                        "type": "string",
                        "description": "Optional GoalContract outcome.",
                    },
                    "verification": {
                        "type": "string",
                        "description": "Optional GoalContract verification evidence required for completion.",
                    },
                    "constraints": {
                        "type": "string",
                        "description": "Optional GoalContract constraints.",
                    },
                    "boundaries": {
                        "type": "string",
                        "description": "Optional GoalContract scope/authority boundaries.",
                    },
                    "stop_when": {
                        "type": "string",
                        "description": "Optional GoalContract blocked/stop conditions.",
                    },
                    "criterion": {
                        "type": "string",
                        "description": "Required new criterion for action=add_subgoal.",
                    },
                },
                "required": ["action"],
            },
        },
    }


def ensure_goal_manage_tool(agent: Any) -> bool:
    """Inject ``goal_manage`` only into a managed canonical Bot Chat."""
    try:
        if not getattr(agent, "_bot_mode_protocol", True):
            return False
        tools = getattr(agent, "tools", None)
        if tools:
            for tool in tools:
                if (
                    isinstance(tool, dict)
                    and tool.get("function", {}).get("name") == GOAL_MANAGE_TOOL_NAME
                ):
                    return True

        from tools.bot_mode_dm import _agent_home, _session_title
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        if _session_title(agent) != BOT_CHAT_TITLE:
            return False
        # Match message_agent's managed-install gate rather than the rendered
        # protocol section. Older Bot Mode builds appended that protocol text
        # into SOUL.md; current prompt generation correctly deduplicates it to
        # an empty section, but the install is still managed and must retain
        # native Bot Chat tools after upgrade.
        if not is_bot_mode_managed(_agent_home(agent)):
            return False
        if agent.tools is None:
            agent.tools = []
        agent.tools.append(goal_manage_tool_schema())
        valid = getattr(agent, "valid_tool_names", None)
        if isinstance(valid, set):
            valid.add(GOAL_MANAGE_TOOL_NAME)
        return True
    except Exception:  # pragma: no cover - tool injection must never break a turn
        logger.debug("ensure_goal_manage_tool failed", exc_info=True)
        return False


def _result(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _state_payload(manager: Any) -> dict:
    state = manager.state
    if state is None:
        return {"status": "none", "status_line": manager.status_line()}
    contract = getattr(state, "contract", None)
    return {
        "status": state.status,
        "goal": state.goal,
        "turns_used": state.turns_used,
        "max_turns": state.max_turns,
        "subgoals": list(state.subgoals),
        "contract": contract.to_dict() if contract is not None else {},
        "status_line": manager.status_line(),
    }


def goal_manage_tool(
    *,
    action: str,
    session_id: str,
    agent: Any,
    goal: str = "",
    max_turns: Optional[int] = None,
    outcome: str = "",
    verification: str = "",
    constraints: str = "",
    boundaries: str = "",
    stop_when: str = "",
    criterion: str = "",
) -> str:
    """Execute one native GoalManager mutation for the calling Bot Chat."""
    from tools.bot_mode_dm import _agent_home, _session_title
    from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

    if _session_title(agent) != BOT_CHAT_TITLE:
        return _result({"success": False, "error": "goal_manage is only available in a Bot Mode 'Bot Chat' session."})
    if not is_bot_mode_managed(_agent_home(agent)):
        return _result({"success": False, "error": "goal_manage is unavailable because this profile is not Bot-Mode managed."})

    sid = str(session_id or "").strip()
    if not sid:
        return _result({"success": False, "error": "No active session id is available for goal_manage."})

    try:
        from hermes_cli.goals import GoalContract, GoalManager, mark_goal_state_dirty

        manager = GoalManager(session_id=sid)
        act = str(action or "").strip().lower()

        if act == "status":
            return _result({"success": True, **_state_payload(manager)})

        if act == "set":
            text = str(goal or "").strip()
            if not text:
                return _result({"success": False, "error": "goal is required for action=set."})
            if manager.has_goal():
                return _result({
                    "success": False,
                    "error": "An active or paused goal already exists. goal_manage cannot replace it; use the existing goal or let the user change/clear it.",
                    **_state_payload(manager),
                })
            budget = None
            if max_turns is not None:
                budget = int(max_turns)
                if budget < 1 or budget > 500:
                    return _result({"success": False, "error": "max_turns must be between 1 and 500."})
            contract = GoalContract(
                outcome=str(outcome or "").strip(),
                verification=str(verification or "").strip(),
                constraints=str(constraints or "").strip(),
                boundaries=str(boundaries or "").strip(),
                stop_when=str(stop_when or "").strip(),
            )
            manager.set(text, max_turns=budget, contract=contract)
            mark_goal_state_dirty(sid)
            return _result({"success": True, "action": "set", **_state_payload(manager)})

        if act == "add_subgoal":
            text = str(criterion or "").strip()
            if not text:
                return _result({"success": False, "error": "criterion is required for action=add_subgoal."})
            manager.add_subgoal(text)
            mark_goal_state_dirty(sid)
            return _result({"success": True, "action": "add_subgoal", **_state_payload(manager)})

        return _result({"success": False, "error": f"Unknown goal_manage action: {action!r}."})
    except Exception as exc:
        logger.error("goal_manage failed: %s", exc, exc_info=True)
        return _result({"success": False, "error": f"goal_manage failed: {exc}"})
