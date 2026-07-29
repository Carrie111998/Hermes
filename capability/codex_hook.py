import logging

from capability.decision import check_capability
from capability.source import resolve_source


logger = logging.getLogger(__name__)


def observe_codex_tool(*, tool: str):
    from agent.file_safety import _resolve_active_profile_name

    profile = _resolve_active_profile_name()
    capability = resolve_source("codex", tool)
    decision = check_capability(
        profile=profile,
        capability=capability,
    )

    logger.info(
        "CAPABILITY_SHADOW runtime=codex profile=%s tool=%s capability=%s decision=%s",
        profile,
        tool,
        capability,
        decision.value,
    )
    return decision
