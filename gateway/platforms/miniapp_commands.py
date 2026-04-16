from __future__ import annotations

import uuid
from typing import Any

import gateway.platforms.miniapp_status as miniapp_status

MINIAPP_COMMANDS: tuple[dict[str, Any], ...] = (
    {
        "name": "help",
        "description": "Show this miniapp help",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "",
    },
    {
        "name": "commands",
        "description": "List supported miniapp commands",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "",
    },
    {
        "name": "new",
        "description": "Start a fresh miniapp session",
        "category": "Mini App",
        "aliases": ("reset",),
        "args_hint": "",
    },
    {
        "name": "model",
        "description": "Show the current model",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "",
    },
    {
        "name": "usage",
        "description": "Show token usage for this miniapp session",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "",
    },
    {
        "name": "status",
        "description": "Show gateway health, usage, and model info",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "",
    },
    {
        "name": "cron",
        "description": "List cron jobs or show cron guidance",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "[list]",
    },
    {
        "name": "stop",
        "description": "Show guidance for stopping background processes",
        "category": "Mini App",
        "aliases": (),
        "args_hint": "",
    },
)


def command_catalog() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cmd in MINIAPP_COMMANDS:
        rows.append(
            {
                "name": cmd["name"],
                "description": cmd["description"],
                "category": cmd["category"],
                "aliases": list(cmd["aliases"]),
                "args_hint": cmd["args_hint"],
                "cli_only": False,
                "gateway_only": False,
            }
        )
    return rows


def _format_help() -> str:
    lines = ["Telegram Mini App commands:"]
    lines.extend(f"/{row['name']} - {row['description']}" for row in command_catalog())
    lines.append("Use the Hermes CLI window for commands that are not listed here.")
    return "\n".join(lines)


def _format_jobs(adapter) -> str:
    if adapter is None or not getattr(adapter, "_CRON_AVAILABLE", False):
        return "Cron is unavailable in the Telegram Mini App."

    jobs = adapter._cron_list(include_disabled=True)
    if not jobs:
        return "No cron jobs configured."
    return "\n".join(
        f"- {job['id']}  {job['name']}  {job.get('schedule_display') or job.get('schedule', 'unknown schedule')}  ({'enabled' if job.get('enabled', True) else 'paused'})"
        for job in jobs
    )


def _build_model_output() -> str:
    info = miniapp_status.resolve_runtime_model_info()
    return (
        f"Model: {info['model']}\n"
        f"Provider: {info['provider']}\n"
        f"Context: {info['context_length']:,} tokens"
    )


def _get_session_snapshots(adapter) -> dict[str, dict[str, Any]]:
    if adapter is None:
        return {}
    snapshots = getattr(adapter, "_miniapp_sessions", {})
    return snapshots if isinstance(snapshots, dict) else {}


def _build_usage_output(adapter, session_id: str) -> str:
    usage = miniapp_status.build_session_usage(session_id, _get_session_snapshots(adapter))
    return (
        f"Prompt: {usage['prompt_tokens']:,}\n"
        f"Completion: {usage['completion_tokens']:,}\n"
        f"Total: {usage['total_tokens']:,}"
    )


def _build_status_output(adapter, session_id: str) -> str:
    health = miniapp_status.collect_system_health()
    info = miniapp_status.resolve_runtime_model_info()
    usage = miniapp_status.build_session_usage(session_id, _get_session_snapshots(adapter))
    return (
        f"Gateway: {health['status']}\n"
        f"Model: {info['model_short']} via {info['provider']}\n"
        f"Session prompt tokens: {usage['prompt_tokens']:,} (max {info['context_length']:,})\n"
        f"Total tokens: {usage['total_tokens']:,}\n"
        f"Disk: {health['disk_percent']}%\n"
        f"Uptime: {health['uptime']}s"
    )


def execute_command(adapter, *, command: str, args: str, session_id: str | None) -> dict[str, Any]:
    name = command.lstrip("/").strip().lower()
    if name in {"new", "reset"}:
        return {"output": "Started a new miniapp session.", "session_id": str(uuid.uuid4())}
    if name in {"help", "commands"}:
        return {"output": _format_help(), "session_id": session_id}
    if name == "model":
        return {"output": _build_model_output(), "session_id": session_id}
    if name == "usage":
        return {"output": _build_usage_output(adapter, session_id or ""), "session_id": session_id}
    if name == "status":
        return {"output": _build_status_output(adapter, session_id or ""), "session_id": session_id}
    if name == "cron":
        subcommand = (args or "list").strip().lower()
        if subcommand == "list":
            return {"output": _format_jobs(adapter), "session_id": session_id}
        return {
            "output": "Use the Cron tab for create/edit/pause/resume/run/delete actions.",
            "session_id": session_id,
        }
    if name == "stop":
        return {
            "output": "Stopping background processes is disabled in the Telegram Mini App. Use the Hermes CLI window for /stop.",
            "session_id": session_id,
        }
    return {
        "output": f"/{name} is not available in the Telegram Mini App yet. Use the Hermes CLI window for that command.",
        "session_id": session_id,
    }
