import logging
from src.registry import SubagentHandle, registry

logger = logging.getLogger(__name__)


def _on_subagent_start(**kwargs: object) -> None:
    child_subagent_id = kwargs.get("child_subagent_id")
    child_session_id = kwargs.get("child_session_id")
    child_goal = kwargs.get("child_goal")
    parent_subagent_id = kwargs.get("parent_subagent_id")

    child_role = kwargs.get("child_role")

    if not child_subagent_id or not child_session_id or child_goal is None:
        logger.debug("subagent_start missing required kwargs, skipping")
        return

    try:
        handle = SubagentHandle(
            subagent_id=str(child_subagent_id),
            session_id=str(child_session_id),
            goal=str(child_goal),
            parent_subagent_id=str(parent_subagent_id) if parent_subagent_id else None,
            role=str(child_role) if child_role else "",
        )
        registry.register(handle)
    except ValueError:
        # Duplicate — already registered; keep existing handle.
        pass
    except Exception:
        logger.debug("subagent_start registry registration failed", exc_info=True)


def _on_subagent_stop(**kwargs: object) -> None:
    child_session_id = kwargs.get("child_session_id")
    if not child_session_id:
        logger.debug("subagent_stop missing child_session_id, skipping")
        return

    try:
        target = str(child_session_id)
        for handle in registry:
            if handle.session_id == target:
                registry.set_state(handle.subagent_id, "done")
                break
    except Exception:
        logger.debug("subagent_stop registry update failed", exc_info=True)


def register(ctx) -> None:
    ctx.register_plugin("subagent-handles", registry)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    # Wire the subagent_send / cancel_subagent tools so the agent can
    # actually call them at runtime. They share the module-level registry.
    try:
        from src.sender import register_tools as _register_tools

        _register_tools(ctx)
    except Exception:
        logger.debug("subagent_send/cancel tool registration failed", exc_info=True)
