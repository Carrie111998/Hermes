import shutil
import time
from typing import Any

from hermes_constants import get_hermes_home

from gateway.status import read_runtime_status
from tools.process_registry import process_registry


def resolve_runtime_model_info() -> dict[str, Any]:
    from agent.model_metadata import get_model_context_length
    from gateway.run import _resolve_gateway_model, _resolve_runtime_agent_kwargs

    model = _resolve_gateway_model()
    runtime = _resolve_runtime_agent_kwargs()
    provider = runtime.get("provider") or ""
    base_url = runtime.get("base_url") or ""
    api_key = runtime.get("api_key") or ""
    context_length = get_model_context_length(
        model,
        base_url=base_url,
        api_key=api_key,
        provider=provider,
    )
    return {
        "model": model,
        "model_short": model.rsplit("/", 1)[-1] if "/" in model else model,
        "provider": provider or "openrouter",
        "context_length": context_length,
    }


def collect_system_health() -> dict[str, Any]:
    runtime = read_runtime_status() or {}
    start_time = runtime.get("start_time")
    total, used, _free = shutil.disk_usage(get_hermes_home())
    return {
        "status": "ok",
        "platform": "hermes-agent",
        "cpu_percent": None,
        "memory_percent": None,
        "disk_percent": round((used / total) * 100, 1) if total else None,
        "uptime": int(time.time() - start_time) if start_time else None,
        "load_avg": None,
        "gateway_state": runtime.get("gateway_state"),
        "platforms": runtime.get("platforms", {}),
    }


def build_session_usage(session_id: str, snapshots: dict[str, dict[str, Any]]) -> dict[str, Any]:
    snapshot = snapshots.get(session_id, {})
    return {
        "session_id": session_id,
        "prompt_tokens": int(snapshot.get("prompt_tokens", 0)),
        "completion_tokens": int(snapshot.get("completion_tokens", 0)),
        "total_tokens": int(snapshot.get("total_tokens", 0)),
        "last_prompt_tokens": int(snapshot.get("last_prompt_tokens", 0)),
    }


def list_process_rows() -> dict[str, Any]:
    rows = []
    for item in process_registry.list_sessions():
        rows.append(
            {
                "session_id": item["session_id"],
                "name": item["command"],
                "pid": item["pid"],
                "running": item["status"] == "running",
                "cpu": None,
                "mem": None,
                "uptime": item["uptime_seconds"],
            }
        )
    return {"processes": rows}
