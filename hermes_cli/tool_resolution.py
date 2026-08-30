"""Classic CLI startup orchestration for canonical tool definitions."""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class ToolResolutionRequest:
    """Immutable inputs to the canonical model-tool resolver."""

    enabled_toolsets: Optional[tuple[str, ...]]
    disabled_toolsets: tuple[str, ...]

    @classmethod
    def from_lists(
        cls,
        enabled_toolsets: Optional[Iterable[str]],
        disabled_toolsets: Optional[Iterable[str]],
    ) -> "ToolResolutionRequest":
        return cls(
            None if enabled_toolsets is None else tuple(enabled_toolsets),
            tuple(disabled_toolsets or ()),
        )


def _submit_daemon(work: Callable[[], T]) -> Future[T]:
    future: Future[T] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(work())
        except BaseException as exc:
            future.set_exception(exc)

    threading.Thread(target=run, name="tool-surface-resolution", daemon=True).start()
    return future


def start_tool_surface_resolution(
    enabled_toolsets: Optional[Iterable[str]],
    disabled_toolsets: Optional[Iterable[str]],
) -> Optional[tuple[ToolResolutionRequest, Future[list[dict]]]]:
    """Start classic-CLI definition resolution for a known policy."""
    if os.environ.get("HERMES_DEFER_AGENT_STARTUP") == "1":
        return None

    request = ToolResolutionRequest.from_lists(enabled_toolsets, disabled_toolsets)
    if not request.enabled_toolsets and request.enabled_toolsets is not None:
        completed: Future[list[dict]] = Future()
        completed.set_result([])
        return request, completed
    future = _submit_daemon(
        lambda: get_cli_tool_definitions(
            enabled_toolsets=request.enabled_toolsets,
            disabled_toolsets=request.disabled_toolsets,
            quiet_mode=True,
        )
    )
    return request, future


def resolve_cli_toolsets(toolsets, config: dict) -> list[str]:
    """Resolve command input and configured defaults into one CLI policy."""
    if toolsets is not None:
        values = (toolsets,) if isinstance(toolsets, str) else toolsets
        return [
            part.strip()
            for value in values
            for part in (
                value.split(",") if isinstance(value, str) else (str(value),)
            )
            if part.strip()
        ]

    from hermes_cli.config import platform_toolsets_explicitly_empty

    if platform_toolsets_explicitly_empty(config, "cli"):
        return []

    try:
        from agent.coding_context import coding_selection

        coding = coding_selection(platform="cli", config=config)
    except Exception:
        coding = None
    if coding is not None:
        return coding

    from hermes_cli.tools_config import _get_platform_tools

    return sorted(_get_platform_tools(config, "cli"))


def get_cli_tool_definitions(
    enabled_toolsets: Optional[Iterable[str]] = None,
    disabled_toolsets: Optional[Iterable[str]] = None,
    quiet_mode: bool = False,
    skip_tool_search_assembly: bool = False,
) -> list[dict]:
    """Resolve classic-CLI definitions after MCP startup is settled."""
    if enabled_toolsets is not None:
        enabled_toolsets = list(enabled_toolsets)
        if not enabled_toolsets:
            return []
    if disabled_toolsets is not None:
        disabled_toolsets = list(disabled_toolsets)

    from hermes_cli.mcp_startup import wait_for_mcp_discovery
    from model_tools import get_tool_definitions

    wait_for_mcp_discovery()
    return get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=quiet_mode,
        skip_tool_search_assembly=skip_tool_search_assembly,
    )


def start_cli_tool_resolution(
    toolsets, config: Optional[dict] = None
) -> Optional[tuple[ToolResolutionRequest, Future[list[dict]]]]:
    """Resolve CLI policy and start definition resolution."""
    if toolsets is not None and not toolsets:
        return start_tool_surface_resolution([], [])
    if config is None:
        from hermes_cli.config import load_config

        resolved_config = load_config()
    else:
        resolved_config = config
    selection = resolve_cli_toolsets(toolsets, resolved_config)
    if not selection:
        return start_tool_surface_resolution([], [])

    from agent.skill_utils import parse_config_string_list

    return start_tool_surface_resolution(
        selection,
        parse_config_string_list(
            (resolved_config.get("agent") or {}).get("disabled_toolsets")
        ),
    )
