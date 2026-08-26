"""Deterministic tool restrictions for bounded automatic fallbacks.

Fallback entries may opt into ``safety_mode: read_only``. While that entry is
active, only a small allowlist of inspection-only tools/actions may execute.
Unknown tools fail closed. Fallback entries without ``safety_mode`` retain the
legacy unrestricted behavior for compatibility.
"""

from __future__ import annotations

from typing import Any


_READ_ONLY_TOOLS = frozenset(
    {
        "clarify",
        "page_info",
        "read_file",
        "read_preview",
        "read_terminal",
        "read_window_below",
        "search_files",
        "session_search",
        "skill_view",
        "skills_list",
        "vision_analyze",
        "web_extract",
        "web_search",
    }
)

_READ_ONLY_ACTIONS = {
    "computer_use": frozenset(
        {"capture", "list_apps", "list_windows", "cua_browser_state"}
    ),
    "cronjob": frozenset({"list"}),
    "process": frozenset({"list", "poll", "log", "wait"}),
}

_READ_ONLY_MODES = frozenset({"bounded", "read-only", "read_only", "readonly"})
_UNRESTRICTED_MODES = frozenset({"full", "none", "off", "unrestricted"})
# Runtimes that execute tools inside a model-driven subprocess that never
# passes through Hermes tool dispatch. ``codex_app_server`` is the embedded
# Codex app-server runtime (detected via api_mode). The copilot-acp provider
# resolves the same way through its *provider name*: CopilotACPClient keeps
# api_mode=chat_completions but services ACP fs/write_text_file requests
# directly, so a read_only fallback on that provider could still mutate files.
_EMBEDDED_EXECUTION_RUNTIMES = frozenset({"codex_app_server"})
_EMBEDDED_EXECUTION_PROVIDERS = frozenset({"copilot-acp"})
# Mirrors the copilot-acp entries in auxiliary_client._PROVIDER_ALIASES.
_EMBEDDED_PROVIDER_ALIASES = {
    "github-copilot-acp": "copilot-acp",
    "copilot-acp-agent": "copilot-acp",
}


def _embedded_runtime_name(api_mode: str, provider: str | None) -> str | None:
    """Return the embedded-execution runtime name when the entry resolves to one.

    A bounded fallback entry is rejected when either its api_mode names an
    embedded runtime or its provider resolves (after alias normalization) to
    a provider whose client executes outside Hermes tool dispatch.
    """
    mode = str(api_mode or "").strip().lower()
    if mode in _EMBEDDED_EXECUTION_RUNTIMES:
        return mode
    name = str(provider or "").strip().lower()
    if not name:
        return None
    name = _EMBEDDED_PROVIDER_ALIASES.get(name, name)
    if name in _EMBEDDED_EXECUTION_PROVIDERS:
        return name
    return None


def fallback_runtime_block_reason(
    entry: dict[str, Any], api_mode: str
) -> str | None:
    """Reject embedded-execution runtimes for bounded fallback entries."""
    raw_mode = entry.get("safety_mode") if isinstance(entry, dict) else None
    if raw_mode is None or not str(raw_mode).strip():
        return None
    mode = str(raw_mode).strip().lower()
    if mode in _UNRESTRICTED_MODES:
        return None
    runtime = _embedded_runtime_name(
        api_mode, entry.get("provider") if isinstance(entry, dict) else None
    )
    if runtime is None:
        return None
    if mode not in _READ_ONLY_MODES:
        mode = "read_only"
    return (
        f"Fallback safety policy ({mode}) rejected runtime '{runtime}' "
        "because it executes tools outside Hermes tool dispatch."
    )


def _active_fallback_entry(agent: Any) -> dict[str, Any] | None:
    snapshot = getattr(agent, "_active_fallback_entry", None)
    if isinstance(snapshot, dict):
        return snapshot
    chain = getattr(agent, "_fallback_chain", None)
    index = getattr(agent, "_fallback_index", None)
    if not isinstance(chain, list) or not isinstance(index, int):
        return None
    active_index = index - 1
    if active_index < 0 or active_index >= len(chain):
        return None
    entry = chain[active_index]
    return entry if isinstance(entry, dict) else None


def _is_bounded_read(agent: Any, tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in _READ_ONLY_TOOLS:
        return True
    allowed_actions = _READ_ONLY_ACTIONS.get(tool_name)
    if allowed_actions is None:
        return False
    action = str(args.get("action") or "").strip().lower()
    return action in allowed_actions


def fallback_tool_block_reason(
    agent: Any,
    tool_name: str,
    args: dict[str, Any] | None,
) -> str | None:
    """Return a deterministic denial reason, or ``None`` when execution is allowed."""
    if getattr(agent, "_fallback_activated", False) is not True:
        return None

    entry = _active_fallback_entry(agent)
    if entry is None:
        mode = "read_only"
        provider = str(getattr(agent, "provider", "") or "unknown fallback")
        model = str(getattr(agent, "model", "") or "unknown model")
        identity = f"{provider}:{model} (unknown fallback entry)"
    else:
        raw_mode = entry.get("safety_mode")
        if raw_mode is None or not str(raw_mode).strip():
            return None
        mode = str(raw_mode).strip().lower()
        if mode in _UNRESTRICTED_MODES:
            return None
        # Unknown modes fail closed to the bounded read-only policy.
        if mode not in _READ_ONLY_MODES:
            mode = "read_only"
        provider = str(entry.get("provider") or getattr(agent, "provider", "") or "unknown fallback")
        model = str(entry.get("model") or getattr(agent, "model", "") or "unknown model")
        identity = f"{provider}:{model}"

    normalized_args = args if isinstance(args, dict) else {}
    if _is_bounded_read(agent, tool_name, normalized_args):
        return None

    return (
        f"Fallback safety policy ({mode}) blocked tool '{tool_name}' while "
        f"automatic fallback {identity} is active. Only bounded read-only "
        "inspection is permitted; retry after the primary provider recovers "
        "or explicitly switch to a trusted model."
    )
