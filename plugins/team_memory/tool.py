"""Agent-facing team-memory search tool."""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from hermes_cli.config import cfg_get, load_config_readonly

from .storage import (
    get_agent_variant,
    get_db_path,
    log_query_metric,
    search_memory,
    validate_workspace_id,
)


def _config() -> dict:
    try:
        value = load_config_readonly()
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _enabled_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def is_feature_enabled(config: Optional[dict] = None) -> bool:
    """Use the new flag, with the original Stage 1 flag as compatibility."""
    cfg = _config() if config is None else config
    team_cfg = cfg_get(cfg, "team_memory", default={})
    if isinstance(team_cfg, dict) and "enabled" in team_cfg:
        return _enabled_value(team_cfg.get("enabled"))
    return _enabled_value(cfg_get(cfg, "features", "team_memory", default=False))


def check_team_memory_requirements() -> bool:
    """Gate schema exposure before a conversation's tool snapshot is built."""
    cfg = _config()
    if not is_feature_enabled(cfg):
        return False
    workspace = cfg_get(cfg, "team_memory", "workspace_id", default=None)
    if workspace is None:
        workspace = cfg_get(cfg, "team_memory", "workspace", default=None)
    try:
        validate_workspace_id(str(workspace or ""))
    except ValueError:
        return False
    return True


TEAM_MEMORY_SEARCH_SCHEMA = {
    "name": "team_memory_search",
    "description": (
        "Search reviewed shared team memory for architecture decisions, API "
        "contracts, and best practices. Results are limited to the configured "
        "workspace and do not write memory. Use exact project or API terms when "
        "possible."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords or a concise question to search",
            },
            "category": {
                "type": "string",
                "description": "Optional category filter; use all when unsure",
                "enum": ["all", "architecture_decision", "api_contract", "best_practice"],
                "default": "all",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return str(get_active_profile_name() or "")
    except Exception:
        return ""


def handle_team_memory_search(args: dict, **kwargs: Any) -> str:
    """Registry handler; context is passed by the existing tool dispatcher."""
    args = args if isinstance(args, dict) else {}
    return team_memory_search(
        query=str(args.get("query", "")),
        category=str(args.get("category", "all")),
        agent_type=kwargs.get("agent_type"),
        task_id=kwargs.get("task_id"),
        session_id=kwargs.get("session_id"),
    )


def team_memory_search(
    query: str,
    category: str = "all",
    agent_type: Optional[str] = None,
    *,
    task_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Search scoped memory and return a bounded JSON result envelope."""
    cfg = _config()
    if not is_feature_enabled(cfg):
        return json.dumps(
            {
                "success": False,
                "code": "feature_disabled",
                "message": "Team memory is disabled for this Hermes process.",
            },
            ensure_ascii=False,
        )

    raw_workspace = cfg_get(
        cfg,
        "team_memory",
        "workspace_id",
        default=cfg_get(cfg, "team_memory", "workspace", default=""),
    )
    try:
        workspace = validate_workspace_id(str(raw_workspace or ""))
    except ValueError:
        return json.dumps(
            {
                "success": False,
                "code": "invalid_configuration",
                "message": "team_memory.workspace_id is missing or invalid.",
            },
            ensure_ascii=False,
        )

    db_path = get_db_path(cfg)
    if not db_path.exists():
        return json.dumps(
            {
                "success": False,
                "code": "not_initialized",
                "message": "Team memory is not initialized for this profile.",
                "hint": "Run: hermes team-memory init --workspace <id>",
            },
            ensure_ascii=False,
        )

    category_filter = None if category in {"", "all", None} else category
    start = time.monotonic()
    try:
        results = search_memory(
            query,
            category=category_filter,
            workspace_id=workspace,
            project_id=cfg_get(cfg, "team_memory", "project_id", default=None),
            limit=int(cfg_get(cfg, "team_memory", "result_limit", default=5) or 5),
            max_content_chars=int(
                cfg_get(cfg, "team_memory", "max_content_chars", default=4000) or 4000
            ),
            max_total_chars=int(
                cfg_get(
                    cfg,
                    "team_memory",
                    "max_total_chars",
                    default=24000,
                )
                or 24000
            ),
        )
        latency_ms = (time.monotonic() - start) * 1000
        try:
            log_query_metric(
                query=query,
                category=category_filter,
                results_count=len(results),
                latency_ms=latency_ms,
                workspace_id=workspace,
                agent_variant=agent_type or get_agent_variant(cfg),
                profile_name=_profile_name(),
                session_id=session_id,
                task_id=task_id,
            )
        except Exception:
            # Metrics are evidence, not a dependency of the agent task.
            pass
        return json.dumps(
            {
                "success": True,
                "workspace_id": workspace,
                "query": query,
                "category": category or "all",
                "count": len(results),
                "latency_ms": round(latency_ms, 2),
                "results": results,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {
                "success": False,
                "code": "search_failed",
                "message": f"Team memory search failed: {type(exc).__name__}",
            },
            ensure_ascii=False,
        )
