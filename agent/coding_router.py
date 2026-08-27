"""编码任务的子智能体模型路由。

只在用户明确配置了 agent.coding_route 时生效，避免悄悄改变现有模型和费用。
"""

from __future__ import annotations

import re
from typing import Any, Optional


# Recognized keys that may flow from agent.coding_route into the runtime
# credential resolver. Anything outside this set is dropped without warning
# to keep the router a pure data shaper.
_ALLOWED_KEYS = {"provider", "model", "base_url", "api_key", "api_mode", "max_output_tokens"}

# Only recognise explicit code actions so general chat like "summarise this
# README" or "look up the weather" is never routed to an external coding CLI.
_CODING_TASK_RE = re.compile(
    r"(?:修复|改(?:代码|功能|bug)|实现|开发|重构|新增(?:接口|功能|测试)|"
    r"写(?:代码|测试|接口)|排查(?:代码|bug)|运行(?:测试|构建)|提交(?:代码|PR)|"
    r"fix|implement|refactor|debug|add\s+(?:test|feature|endpoint)|"
    r"write\s+(?:code|test)|run\s+tests?)",
    re.IGNORECASE,
)


def is_explicit_coding_task(task: Any) -> bool:
    """Return True only for user messages that describe code work."""
    return isinstance(task, str) and bool(_CODING_TASK_RE.search(task))


def resolve_coding_route(
    *,
    platform: Optional[str],
    cwd: Optional[str],
    model: Optional[str],
    config: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return the coding subagent route, or None when it should not apply."""
    cfg = config if isinstance(config, dict) else {}
    agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    route = agent_cfg.get("coding_route")
    if not isinstance(route, dict) or route.get("enabled") is not True:
        return None
    route_cfg = route

    try:
        from agent.coding_context import resolve_runtime_mode

        mode = resolve_runtime_mode(
            platform=platform,
            cwd=cwd,
            config=cfg,
            model=model,
        )
    except Exception:
        return None
    if not mode.is_coding:
        return None

    provider = str(route_cfg.get("provider") or "").strip()
    target_model = str(route_cfg.get("model") or "").strip()
    if not provider and not route_cfg.get("base_url"):
        return None
    if not target_model and not route_cfg.get("base_url"):
        return None

    result: dict[str, Any] = {"provider": provider or None, "model": target_model or None}
    for key in _ALLOWED_KEYS - {"provider", "model"}:
        value = route_cfg.get(key)
        if value not in (None, ""):
            result[key] = value
    return result
