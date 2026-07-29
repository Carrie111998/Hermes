import logging

from capability.decision import check_capability
from capability.mapping import resolve_capability


logger = logging.getLogger(__name__)


def observe_tool_call(
    *,
    function_name: str,
    runtime: str = "hermes",
):
    capability = resolve_capability(function_name)

    try:
        from agent.file_safety import _resolve_active_profile_name

        profile = _resolve_active_profile_name()
    except Exception:
        profile = "unknown"

    decision = check_capability(
        profile=profile,
        capability=capability,
    )

    logger.info(
        "CAPABILITY_SHADOW runtime=%s profile=%s tool=%s capability=%s decision=%s",
        runtime,
        profile,
        function_name,
        capability,
        decision.value,
    )
    return decision
