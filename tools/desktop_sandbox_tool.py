"""Register the task-scoped desktop sandbox tool during builtin discovery."""

from tools.environments.desktop_sandbox_tool import (
    DESKTOP_SANDBOX_SCHEMA,
    handle_desktop_sandbox,
)

__all__ = ["DESKTOP_SANDBOX_SCHEMA", "handle_desktop_sandbox"]
