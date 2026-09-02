"""CLI coverage for ``hermes tools catalog``."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.subcommands.tools import build_tools_parser
from hermes_cli.tools_config import tools_catalog_command


def test_tools_catalog_process_does_not_create_profile_files(tmp_path):
    home = tmp_path / "fresh-hermes-home"
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.main import main; main()",
            "tools",
            "catalog",
            "--compact",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema_version"] == 1
    assert not home.exists() or not any(home.rglob("*"))


def test_tools_catalog_process_rejects_unknown_platform_without_writes(tmp_path):
    home = tmp_path / "fresh-hermes-home"
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.main import main; main()",
            "tools",
            "catalog",
            "--platform",
            "definitely-not-a-platform",
            "--compact",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 2
    assert "Unknown platform" in result.stdout
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
    assert not home.exists() or not any(home.rglob("*"))


def test_tools_catalog_process_skips_startup_maintenance_paths(tmp_path):
    home = tmp_path / "existing-hermes-home"
    home.mkdir()
    env = os.environ.copy()
    env.update({"HERMES_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"})
    script = """
import json
import sys

sys.argv = ["hermes", "tools", "catalog", "--compact"]
import hermes_cli.main as cli_main

import hermes_cli
from types import SimpleNamespace

called = []
cli_main._cleanup_quarantined_exes = lambda: called.append("quarantine")
cli_main._sweep_stale_bytecode_if_checkout_changed = lambda: called.append("bytecode")
original_platform = cli_main.sys.platform
cli_main.sys.platform = "win32"
hermes_cli._install_repair = SimpleNamespace(
    ensure_windows_bin_launchers=lambda _root: called.append("windows-launcher")
)
cli_main._repair_windows_launchers_if_needed()
cli_main.sys.platform = original_platform
cli_main.main()
if called:
    raise SystemExit("startup maintenance reached: " + ",".join(called))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema_version"] == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["hermes", "tools", "catalog", "--compact"], True),
        (["hermes", "--profile", "work", "tools", "catalog"], True),
        (["hermes", "--profile=work", "tools", "catalog"], True),
        (["hermes", "-pwork", "tools", "catalog"], False),
        (["hermes", "-mMODEL", "-tfile", "-sskill", "tools", "catalog"], True),
        (["hermes", "-z", "tools", "catalog"], False),
        (["hermes", "--oneshot=tools", "tools", "catalog"], False),
        (["hermes", "-rsession", "tools", "catalog"], False),
        (["hermes", "-cname", "tools", "catalog"], False),
        (["hermes", "--worktree", "tools", "catalog"], False),
        (["hermes", "chat", "-q", "tools", "catalog"], False),
    ],
)
def test_catalog_readonly_detection_matches_only_the_catalog_command(
    tmp_path, argv, expected
):
    home = tmp_path / "home"
    (home / "profiles" / "work").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_CATALOG_READONLY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEST_ARGV": json.dumps(argv),
        }
    )
    script = """
import json
import os
import sys
sys.argv = json.loads(os.environ["TEST_ARGV"])
import hermes_cli.main as cli_main
print(json.dumps({
    "readonly": cli_main._CATALOG_READONLY,
    "sentinel": os.environ.get("HERMES_CATALOG_READONLY"),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["readonly"] is expected
    assert payload["sentinel"] == ("1" if expected else None)


def test_tools_catalog_parser_is_noninteractive_and_probe_is_explicit():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    handler = lambda args: None
    build_tools_parser(subparsers, cmd_tools=handler)

    args = parser.parse_args([
        "tools",
        "catalog",
        "--platform",
        "discord",
        "--probe",
        "--include-plugins",
        "--compact",
    ])

    assert args.command == "tools"
    assert args.tools_action == "catalog"
    assert args.platform == "discord"
    assert args.probe is True
    assert args.include_plugins is True
    assert args.compact is True
    assert args.func is handler


def test_tools_catalog_command_marks_platform_exposure_without_values(capsys):
    catalog = {
        "schema_version": 1,
        "probe_performed": False,
        "toolsets": ["file", "network"],
        "tools": [
            {
                "name": "read_file",
                "toolset": "file",
                "requires_env": [],
                "origin": {"kind": "builtin", "id": "tools.file_tools"},
            },
            {
                "name": "remote_search",
                "toolset": "network",
                "requires_env": ["TEST_API_KEY"],
                "origin": {"kind": "plugin", "id": "example.plugin"},
            },
        ],
    }
    fake_registry = SimpleNamespace(get_capability_catalog=lambda *, probe: catalog)
    args = SimpleNamespace(
        platform="cli", probe=False, include_plugins=False, compact=True
    )
    with (
        patch("tools.registry.registry", fake_registry),
        patch("tools.registry.discover_builtin_tools") as discover_builtins,
        patch(
            "hermes_cli.tools_config.load_config",
            return_value={"platform_toolsets": {"cli": ["file"]}},
        ),
        patch(
            "hermes_cli.tools_config._get_platform_tools",
            return_value=({"file", "hunter2"}, {"unobserved-secret-server"}),
        ) as get_platform_tools,
    ):
        tools_catalog_command(args)

    discover_builtins.assert_called_once_with(read_only=True)
    get_platform_tools.assert_called_once_with(
        {"platform_toolsets": {"cli": ["file"]}},
        "cli",
        include_default_mcp_servers=True,
        include_plugin_toolsets=False,
        include_portable_plugin_mcp_servers=False,
        probe_credentials=False,
        return_selection_details=True,
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["platform"] == "cli"
    assert payload["enabled_toolsets"] == ["file"]
    assert "hunter2" not in json.dumps(payload)
    assert "unobserved-secret-server" not in json.dumps(payload)
    # The command did not request discovery, but a plugin contribution was
    # already present in the process-global registry. Report the observed
    # state rather than incorrectly claiming that plugins were skipped.
    assert payload["plugin_loading"] == "preloaded"
    assert payload["tools"][0]["selected_for_platform"] is True
    assert payload["tools"][1]["selected_for_platform"] is False
    assert all("enabled" not in tool for tool in payload["tools"])
    assert "TEST_API_KEY" in json.dumps(payload)


def test_tools_catalog_redacts_composed_credential_shaped_mcp_servers(capsys):
    from tools.registry import ToolRegistry

    temporary_access_key = "AS" + "IA" + "IOSFODNN7EXAMPLE"
    github_token = "gh" + "p_exampletoken"
    fake_registry = ToolRegistry()
    fake_registry.register(
        "temporary_server_tool",
        f"mcp-{temporary_access_key}",
        {"name": "temporary_server_tool", "parameters": {"type": "object"}},
        lambda: None,
    )
    fake_registry.register(
        "github_token_server_tool",
        f"mcp-{github_token}",
        {"name": "github_token_server_tool", "parameters": {"type": "object"}},
        lambda: None,
    )
    args = SimpleNamespace(
        platform="cli", probe=False, include_plugins=False, compact=True
    )
    with (
        patch("tools.registry.registry", fake_registry),
        patch("tools.registry.discover_builtin_tools"),
        patch("hermes_cli.tools_config.load_config", return_value={}),
        patch(
            "hermes_cli.tools_config._get_platform_tools",
            return_value=(set(), {temporary_access_key, github_token}),
        ),
    ):
        tools_catalog_command(args)

    payload = json.loads(capsys.readouterr().out)
    encoded = json.dumps(payload, sort_keys=True)
    assert temporary_access_key not in encoded
    assert github_token not in encoded
    assert all(
        toolset.startswith("redacted-") for toolset in payload["enabled_toolsets"]
    )
    assert all(tool["selected_for_platform"] for tool in payload["tools"])


def test_tools_catalog_uses_real_mcp_platform_resolution(capsys):
    catalog = {
        "schema_version": 1,
        "probe_performed": False,
        "toolsets": ["mcp-github"],
        "tools": [
            {
                "name": "github_search",
                "toolset": "mcp-github",
                "requires_env": [],
                "origin": {"kind": "mcp", "id": "github"},
            }
        ],
    }
    from tools.registry import registry

    args = SimpleNamespace(
        platform="cli", probe=False, include_plugins=False, compact=True
    )
    config = {
        "platform_toolsets": {"cli": []},
        "mcp_servers": {"github": {"command": "unused"}},
    }

    with (
        patch.object(registry, "get_capability_catalog", return_value=catalog),
        patch("tools.registry.discover_builtin_tools"),
        patch("hermes_cli.tools_config.load_config", return_value=config),
        patch("hermes_cli.plugins.discover_plugins") as discover_plugins,
    ):
        tools_catalog_command(args)

    discover_plugins.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert "mcp-github" in payload["enabled_toolsets"]
    assert payload["tools"][0]["selected_for_platform"] is True


@pytest.mark.parametrize(
    ("platform_toolsets", "server_config", "builtin_expected", "mcp_expected"),
    [
        (["file"], {"enabled": False}, True, False),
        (["file", "github"], {"enabled": True}, True, False),
        ([], {"enabled": True}, False, True),
    ],
)
def test_tools_catalog_mcp_selection_does_not_collide_with_builtin_toolset(
    capsys, platform_toolsets, server_config, builtin_expected, mcp_expected
):
    catalog = {
        "schema_version": 1,
        "probe_performed": False,
        "toolsets": ["file", "mcp-file"],
        "tools": [
            {
                "name": "read_file",
                "toolset": "file",
                "requires_env": [],
                "origin": {"kind": "builtin", "id": "tools.file_tools"},
            },
            {
                "name": "mcp_read_file",
                "toolset": "mcp-file",
                "requires_env": [],
                "origin": {"kind": "mcp", "id": "file"},
            },
        ],
    }
    config = {
        "platform_toolsets": {"cli": platform_toolsets},
        "mcp_servers": {
            "file": server_config,
            "github": {"enabled": True, "command": "unused"},
        },
    }
    from tools.registry import registry

    args = SimpleNamespace(
        platform="cli", probe=False, include_plugins=False, compact=True
    )
    with (
        patch.object(registry, "get_capability_catalog", return_value=catalog),
        patch("tools.registry.discover_builtin_tools"),
        patch("hermes_cli.tools_config.load_config", return_value=config),
    ):
        tools_catalog_command(args)

    payload = json.loads(capsys.readouterr().out)
    by_name = {tool["name"]: tool for tool in payload["tools"]}
    assert by_name["read_file"]["selected_for_platform"] is builtin_expected
    assert by_name["mcp_read_file"]["selected_for_platform"] is mcp_expected
    assert ("mcp-file" in payload["enabled_toolsets"]) is mcp_expected


def test_tools_catalog_rejects_unknown_platform_before_resolving(capsys):
    args = SimpleNamespace(
        platform="definitely-not-a-platform",
        probe=False,
        include_plugins=False,
        compact=True,
    )

    with (
        patch("tools.registry.discover_builtin_tools"),
        patch("hermes_cli.tools_config.load_config") as load_config,
    ):
        with pytest.raises(SystemExit) as raised:
            tools_catalog_command(args)

    assert raised.value.code == 2
    load_config.assert_not_called()
    assert "Unknown platform" in capsys.readouterr().out
