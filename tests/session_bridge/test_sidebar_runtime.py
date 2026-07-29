from __future__ import annotations

import inspect

from session_bridge.sidebar_runtime import sidebar_registration_app_server_args


def test_sidebar_registration_app_server_args_disable_mcp_and_plugins() -> None:
    assert sidebar_registration_app_server_args() == [
        "-c",
        "mcp_servers={}",
        "--disable",
        "plugins",
    ]


def test_sidebar_registration_app_server_args_returns_a_fresh_list() -> None:
    first = sidebar_registration_app_server_args()
    first.append("--unexpected")

    assert sidebar_registration_app_server_args() == [
        "-c",
        "mcp_servers={}",
        "--disable",
        "plugins",
    ]


def test_sidebar_registration_app_server_args_accepts_no_caller_overrides() -> None:
    assert list(
        inspect.signature(sidebar_registration_app_server_args).parameters
    ) == []
