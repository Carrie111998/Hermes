#!/usr/bin/env python3
"""MCP stdio/transport helpers and safe-environment builders.

Re-exported from :mod:`tools.mcp_tool` (the canonical package namespace) so
that ``from tools.mcp_tool.utils import _build_safe_env`` continues to work
after the module-to-subpackage refactor.
"""

from tools.mcp_tool import (  # noqa: F401
    _build_safe_env,
    _check_logging_callback_support,
    _check_message_handler_support,
    _env_ref_name,
    _get_mcp_stderr_log,
    _interpolate_env_vars,
    _jittered,
    _paginate_full_list,
    _prepend_path,
    _resolve_client_cert,
    _resolve_stdio_command,
    _safe_numeric,
    _scan_mcp_description,
    _validate_remote_mcp_url,
    _warn_hidden_whitespace,
    _wrap_command_with_watchdog,
    _write_stderr_log_header,
)

__all__ = [
    "_build_safe_env",
    "_check_logging_callback_support",
    "_check_message_handler_support",
    "_env_ref_name",
    "_get_mcp_stderr_log",
    "_interpolate_env_vars",
    "_jittered",
    "_paginate_full_list",
    "_prepend_path",
    "_resolve_client_cert",
    "_resolve_stdio_command",
    "_safe_numeric",
    "_scan_mcp_description",
    "_validate_remote_mcp_url",
    "_warn_hidden_whitespace",
    "_wrap_command_with_watchdog",
    "_write_stderr_log_header",
]
