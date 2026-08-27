"""编码任务的子智能体模型路由。

只在用户明确配置了 agent.coding_route 时生效，避免悄悄改变现有模型和费用。
"""

from __future__ import annotations

from typing import Any, Optional


_ALLOWED_KEYS = {"provider", "model", "base_url", "api_key", "api_mode", "max_output_tokens"}


def resolve_coding_route(
    *,
    platform: Optional[str],
    cwd: Optional[str],
    model: Optional[str],
    config: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """返回编码子智能体路由；未启用、非编码场景或配置不完整时返回 None。"""
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
