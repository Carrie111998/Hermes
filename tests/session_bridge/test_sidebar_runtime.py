from __future__ import annotations

import inspect

import pytest

from session_bridge.sidebar_runtime import (
    configured_mcp_server_names,
    sidebar_registration_app_server_args,
)


def test_sidebar_registration_app_server_args_disable_configured_mcp_apps_and_plugins() -> None:
    assert sidebar_registration_app_server_args(
        ("session_bridge", "gbrain")
    ) == [
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "-c",
        "mcp_servers.gbrain.enabled=false",
        "-c",
        "mcp_servers.session_bridge.enabled=false",
    ]


def test_sidebar_registration_app_server_args_returns_a_fresh_list() -> None:
    first = sidebar_registration_app_server_args(("gbrain",))
    first.append("--unexpected")

    assert sidebar_registration_app_server_args(("gbrain",)) == [
        "--disable",
        "apps",
        "--disable",
        "plugins",
        "-c",
        "mcp_servers.gbrain.enabled=false",
    ]


def test_sidebar_registration_app_server_args_accepts_only_server_names() -> None:
    assert list(
        inspect.signature(sidebar_registration_app_server_args).parameters
    ) == ["mcp_server_names"]

    with pytest.raises(ValueError, match="server name"):
        sidebar_registration_app_server_args(("gbrain\nmalformed",))
    with pytest.raises(ValueError, match="server name"):
        sidebar_registration_app_server_args(("quoted.or.nested",))


def test_configured_mcp_server_names_reads_only_exact_config_map_keys() -> None:
    assert configured_mcp_server_names({
        "config": {
            "mcp_servers": {
                "session_bridge": {"url": "http://127.0.0.1"},
                "gbrain": {"url": "http://127.0.0.1"},
            }
        }
    }) == ("gbrain", "session_bridge")

    with pytest.raises(ValueError, match="config/read"):
        configured_mcp_server_names({"config": {"mcp_servers": []}})
