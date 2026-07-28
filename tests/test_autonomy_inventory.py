import json
import argparse
import hashlib
import os
import socket
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import requests

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli import autonomy_inventory
from hermes_cli.autonomy_inventory import (
    _redact,
    build_inventory,
    cmd_security_inventory,
    inventory_mcp,
    inventory_skills,
)
from hermes_cli.subcommands.security import build_security_parser


def test_inventory_skills_reports_frontmatter_without_reading_other_files(tmp_path):
    skill_dir = tmp_path / "skills" / "research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: research
description: Test hypotheses.
---
# Objective
# Preconditions
# Procedure
# Error handling
# Success criteria
# Tests
""",
        encoding="utf-8",
    )

    rows = inventory_skills(tmp_path / "skills")

    assert rows == [
        {
            "name": "research",
            "path": str(skill_dir / "SKILL.md"),
            "yaml_valid": True,
            "valid": True,
            "recommended_sections_present": [
                "objective",
                "preconditions",
                "procedure",
                "error handling",
                "success criteria",
                "tests",
            ],
            "issues": [],
        }
    ]


def test_inventory_mcp_redacts_connection_material_and_keeps_only_key_names():
    rows = inventory_mcp(
        {
            "mcp_servers": {
                "trader": {
                    "url": "https://alice:hunter2@example.test/mcp",
                    "headers": {
                        "Authorization": "Bearer ${TRADER_TOKEN}",
                        "X-Unusual": "hunter2",
                    },
                    "env": {"UNUSUAL_NAME": "hunter2"},
                    "command": "server --credential hunter2",
                    "args": ["--password", "hunter2"],
                    "tools": {"include": ["read_market"]},
                }
            }
        }
    )

    dumped = json.dumps(rows)
    assert rows[0]["env_refs"] == ["TRADER_TOKEN"]
    assert rows[0]["env_keys"] == ["UNUSUAL_NAME"]
    assert rows[0]["header_keys"] == ["Authorization", "X-Unusual"]
    assert rows[0]["tool_allowlist"] == ["read_market"]
    assert "hunter2" not in dumped
    assert "alice" not in dumped
    assert "Bearer " not in dumped
    assert "TRADER_TOKEN" in dumped


def test_build_inventory_never_reads_environment_values(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    try:
        (tmp_path / "skills").mkdir()
        (tmp_path / ".env").write_text(
            "OPENAI_API_KEY=sk-live-secret\nNORMAL_SETTING=visible-value\n",
            encoding="utf-8",
        )
        (tmp_path / "config.yaml").write_text(
            """
mcp_servers:
  trader:
    url: https://alice:secret@example.test/mcp
    headers:
      Authorization: Bearer secret
approvals:
  token: approval-secret
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            autonomy_inventory,
            "inventory_tools",
            lambda: {"imported_modules": [], "tool_count": 0, "toolsets": {}, "aliases": {}},
        )

        report = build_inventory()
    finally:
        reset_hermes_home_override(token)

    dumped = json.dumps(report)
    assert report["secrets"]["env_keys"] == ["NORMAL_SETTING", "OPENAI_API_KEY"]
    assert "sk-live-secret" not in dumped
    assert "visible-value" not in dumped
    assert "approval-secret" not in dumped
    assert "alice" not in dumped


def test_string_redaction_covers_unstructured_secret_shapes():
    sentinels = {
        "bearer": "sentinel-bearer-value",
        "env": "sentinel-env-value",
        "cli": "sentinel-cli-value",
        "password": "sentinel-password-value",
        "query": "sentinel-query-value",
        "json": "sentinel-json-value",
        "known": "sentinel-known-env-value",
    }
    value = {
        "script": (
            f"Authorization: Bearer {sentinels['bearer']} "
            f"SERVICE_TOKEN={sentinels['env']} "
            f"--api-key {sentinels['cli']} "
            f"https://alice:{sentinels['password']}@example.test/run"
            f"?token={sentinels['query']} "
            f'{{"secret": "{sentinels["json"]}"}} '
            f"prefix-{sentinels['known']}-suffix"
        )
    }

    dumped = json.dumps(
        _redact(value, frozenset({sentinels["known"]})),
        ensure_ascii=False,
    )

    assert "<redacted>" in dumped
    for sentinel in sentinels.values():
        assert sentinel not in dumped
    assert "example.test/run" in dumped


def _security_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_security_parser(subparsers, cmd_security=lambda _args: 0)
    return parser


def test_inventory_cli_parser_and_removed_output_option(capsys):
    parser = _security_parser()
    args = parser.parse_args(["security", "inventory", "--json"])
    assert args.security_command == "inventory"
    assert args.json is True

    with pytest.raises(SystemExit) as help_exit:
        parser.parse_args(["security", "inventory", "--help"])
    assert help_exit.value.code == 0
    assert "read-only" in capsys.readouterr().out

    with pytest.raises(SystemExit) as invalid_exit:
        parser.parse_args(["security", "inventory", "--output", "forbidden.json"])
    assert invalid_exit.value.code != 0
    assert "--output" in capsys.readouterr().err


def test_inventory_cli_exit_codes_and_json_stdout(monkeypatch, capsys):
    report = {
        "skills": [],
        "tools": {"tool_count": 0},
        "mcp_servers": [],
        "secrets": {"count": 0},
    }
    monkeypatch.setattr(autonomy_inventory, "build_inventory", lambda: report)

    assert cmd_security_inventory(SimpleNamespace(json=True)) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == report
    assert captured.err == ""

    assert cmd_security_inventory(SimpleNamespace(json=False)) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("Hermes autonomy inventory\n")
    assert captured.err == ""

    def fail():
        raise RuntimeError("sentinel-must-not-leak")

    monkeypatch.setattr(autonomy_inventory, "build_inventory", fail)
    assert cmd_security_inventory(SimpleNamespace(json=True)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RuntimeError" in captured.err
    assert "sentinel-must-not-leak" not in captured.err


def _tree_snapshot(root: Path) -> dict[str, tuple[int, str]]:
    snapshot = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            snapshot[str(path.relative_to(root))] = (
                path.stat().st_mtime_ns,
                hashlib.sha256(payload).hexdigest(),
            )
    return snapshot


def test_real_inventory_is_offline_read_only_and_restores_registry(tmp_path, monkeypatch):
    token = set_hermes_home_override(tmp_path)
    sentinel = "sentinel-machine-independent-value"
    try:
        (tmp_path / "skills").mkdir()
        (tmp_path / "cron").mkdir()
        (tmp_path / ".env").write_text(
            f"INVENTORY_SENTINEL={sentinel}\n",
            encoding="utf-8",
        )
        (tmp_path / "config.yaml").write_text(
            f"""
command_allowlist:
  - "runner --token {sentinel}"
cron:
  script: "Authorization: Bearer {sentinel}"
""",
            encoding="utf-8",
        )
        (tmp_path / "state.db").write_bytes(b"state-sentinel")

        import tools.registry as registry_module

        registry = registry_module.registry
        registry_before = {
            "tools": dict(registry._tools),
            "policy": dict(registry._plugin_override_policy),
            "checks": dict(registry._toolset_checks),
            "aliases": dict(registry._toolset_aliases),
            "generation": registry._generation,
            "cache": dict(registry_module._check_fn_cache),
            "last_good": dict(registry_module._check_fn_last_good),
        }
        environ_before = dict(os.environ)
        files_before = _tree_snapshot(tmp_path)
        pycache_before = _tree_snapshot(
            Path(autonomy_inventory.__file__).resolve().parents[1] / "tools"
        )
        isolated_homes_before = {
            path.name
            for path in Path(tempfile.gettempdir()).glob("hermes-inventory-*")
        }
        attempts = []

        def deny_network(*_args, **_kwargs):
            attempts.append("attempt")
            raise AssertionError("network access attempted")

        monkeypatch.setattr(socket, "create_connection", deny_network)
        monkeypatch.setattr(socket, "getaddrinfo", deny_network)
        monkeypatch.setattr(socket.socket, "connect", deny_network)
        monkeypatch.setattr(httpx.Client, "request", deny_network)
        monkeypatch.setattr(httpx.AsyncClient, "request", deny_network)
        monkeypatch.setattr(requests.sessions.Session, "request", deny_network)
        monkeypatch.setattr(urllib.request, "urlopen", deny_network)

        report = build_inventory()

        assert attempts == []
        assert sentinel not in json.dumps(report)
        assert _tree_snapshot(tmp_path) == files_before
        assert _tree_snapshot(
            Path(autonomy_inventory.__file__).resolve().parents[1] / "tools"
        ) == pycache_before
        assert {
            path.name
            for path in Path(tempfile.gettempdir()).glob("hermes-inventory-*")
        } == isolated_homes_before
        assert registry._tools == registry_before["tools"]
        assert registry._plugin_override_policy == registry_before["policy"]
        assert registry._toolset_checks == registry_before["checks"]
        assert registry._toolset_aliases == registry_before["aliases"]
        assert registry._generation == registry_before["generation"]
        assert registry_module._check_fn_cache == registry_before["cache"]
        assert registry_module._check_fn_last_good == registry_before["last_good"]
        assert dict(os.environ) == environ_before
        assert all(
            row["availability_checked"] is False
            for row in report["tools"]["toolsets"].values()
        )
    finally:
        reset_hermes_home_override(token)
