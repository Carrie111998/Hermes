"""Persistent-goal orchestration for stateless API-server turns.

Messaging adapters let :class:`gateway.run.GatewayRunner` enqueue another
message after a goal verdict.  The API server has no persistent outbound
channel or adapter FIFO, so its direct agent entry points need a small bridge:
run canonical continuation prompts in the same request until GoalManager says
done, paused, or parked.  All state, judging, quality gates, wait barriers, and
budget accounting remain owned by :mod:`hermes_cli.goals`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Optional


GoalStatusCallback = Callable[[dict[str, Any]], None]
RunTurn = Callable[[str, Optional[list[dict[str, Any]]]], dict[str, Any]]
StopPredicate = Callable[[], bool]


def _goal_command(text: str) -> tuple[str, str] | None:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    if lowered in {"/goal", "/goal status"}:
        return ("status", "")
    for action in ("pause", "resume", "clear"):
        if lowered == f"/goal {action}":
            return (action, "")
    if lowered == "/goal unwait":
        return ("unwait", "")
    if lowered.startswith("/goal wait "):
        return ("wait", stripped[len("/goal wait ") :].strip())
    if lowered.startswith("/goal "):
        return ("set", stripped[len("/goal ") :].strip())
    return None


def run_goal_aware_turn(
    *,
    session_id: str,
    user_message: str,
    conversation_history: Optional[list[dict[str, Any]]],
    run_turn: RunTurn,
    default_max_turns: int | None = None,
    status_callback: GoalStatusCallback | None = None,
    should_stop: StopPredicate | None = None,
) -> dict[str, Any]:
    """Run one API turn plus any native persistent-goal continuations.

    Control-plane commands return without calling ``run_turn``. A judge
    ``wait`` verdict returns immediately with the persisted barrier intact.
    A later request for the same raw session re-enters this function; when the
    process/session/time barrier has cleared, that request is evaluated against
    the same persisted goal.
    """
    from hermes_cli.goals import GoalManager, gather_background_processes

    if default_max_turns is None:
        try:
            from hermes_cli.config import load_config

            goals_config = (load_config() or {}).get("goals") or {}
            default_max_turns = int(goals_config.get("max_turns", 20) or 20)
        except Exception:
            default_max_turns = 20

    manager = GoalManager(
        session_id=session_id,
        default_max_turns=default_max_turns,
    )

    def emit(payload: dict[str, Any]) -> None:
        if status_callback is not None:
            status_callback(payload)

    command = _goal_command(user_message)
    current_message = user_message
    current_history = conversation_history
    user_initiated = True

    if command is not None:
        action, value = command
        if action == "status":
            return {
                "final_response": manager.status_line(),
                "completed": True,
                "goal_control": True,
            }
        if action == "pause":
            manager.pause()
            return {
                "final_response": manager.status_line(),
                "completed": True,
                "goal_control": True,
            }
        if action == "clear":
            manager.clear()
            return {
                "final_response": "Goal cleared.",
                "completed": True,
                "goal_control": True,
            }
        if action == "unwait":
            cleared = manager.stop_waiting()
            return {
                "final_response": (
                    "Goal wait cleared." if cleared else "Goal has no active wait."
                ),
                "completed": True,
                "goal_control": True,
            }
        if action == "wait":
            pid_text, _, reason = value.partition(" ")
            try:
                manager.wait_on(int(pid_text), reason=reason)
            except (RuntimeError, TypeError, ValueError) as exc:
                return {
                    "final_response": f"Could not park goal: {exc}",
                    "completed": True,
                    "goal_control": True,
                }
            return {
                "final_response": manager.status_line(),
                "completed": True,
                "goal_control": True,
            }
        if action == "resume":
            state = manager.resume(reset_budget=True)
            if state is None:
                return {
                    "final_response": "No goal to resume.",
                    "completed": True,
                    "goal_control": True,
                }
            current_message = manager.next_continuation_prompt() or ""
            current_history = None
            user_initiated = False
            emit({
                "event": "goal.status",
                "status": "active",
                "message": manager.status_line(),
            })
        elif action == "set":
            if not value:
                return {
                    "final_response": "Usage: /goal <objective>",
                    "completed": True,
                    "goal_control": True,
                }
            state = manager.set(value)
            current_message = value
            emit({
                "event": "goal.started",
                "status": "active",
                "goal": state.goal,
                "max_turns": state.max_turns,
                "message": f"⊙ Goal set ({state.max_turns}-turn budget): {state.goal}",
            })

    goal_identity = (
        (manager.state.created_at, manager.state.goal)
        if manager.state is not None
        else None
    )
    result = run_turn(current_message, current_history)

    while manager.is_active() and not bool(result.get("failed")):
        if should_stop is not None and should_stop():
            break
        # Control-plane requests may pause/clear/replace the goal while this
        # model turn is in flight. Reload persisted state before judging so an
        # old turn cannot resurrect a paused goal or judge a replacement goal
        # against the old objective's response.
        manager = GoalManager(
            session_id=session_id,
            default_max_turns=default_max_turns,
        )
        current_identity = (
            (manager.state.created_at, manager.state.goal)
            if manager.state is not None
            else None
        )
        if not manager.is_active() or current_identity != goal_identity:
            break
        decision = manager.evaluate_after_turn(
            str(result.get("final_response") or ""),
            user_initiated=user_initiated,
            background_processes=gather_background_processes(),
        )
        emit({
            "event": "goal.status",
            "status": decision.get("status"),
            "verdict": decision.get("verdict"),
            "reason": decision.get("reason"),
            "message": decision.get("message"),
            "turns_used": manager.state.turns_used if manager.state else None,
            "max_turns": manager.state.max_turns if manager.state else None,
        })
        if not decision.get("should_continue"):
            break
        current_message = str(decision.get("continuation_prompt") or "")
        if not current_message:
            break
        if should_stop is not None and should_stop():
            break
        result = run_turn(current_message, None)
        user_initiated = False

    return result


__all__ = ["run_goal_aware_turn"]
