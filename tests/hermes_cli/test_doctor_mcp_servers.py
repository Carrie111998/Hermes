"""Tests for the ``MCP Servers`` preflight section of ``hermes doctor``."""

import contextlib
import io
import os

import pytest

import hermes_cli.doctor as doctor


def _run(monkeypatch, servers, *, resolve=None, safe_env=None):
    """Run the preflight against ``servers`` and return (output, manual_issues)."""
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "_load_mcp_config", lambda: servers)
    if resolve is not None:
        monkeypatch.setattr(mcp_tool, "_resolve_stdio_command", resolve)
    if safe_env is not None:
        monkeypatch.setattr(mcp_tool, "_build_safe_env", safe_env)

    issues: list[str] = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        doctor._doctor_mcp_servers(issues)
    return buf.getvalue(), issues


class TestNoServers:
    def test_reports_when_nothing_configured(self, monkeypatch):
        out, issues = _run(monkeypatch, {})
        assert "No MCP servers configured" in out
        assert issues == []


class TestTransportValidation:
    def test_missing_transport_fails(self, monkeypatch):
        out, issues = _run(monkeypatch, {"broken": {"timeout": 30}})
        assert "no transport configured" in out
        assert any("broken" in i for i in issues)

    def test_non_mapping_entry_fails(self, monkeypatch):
        out, issues = _run(monkeypatch, {"weird": ["not", "a", "mapping"]})
        assert "malformed entry" in out
        assert any("weird" in i for i in issues)

    def test_both_url_and_command_warns_and_prefers_http(self, monkeypatch):
        out, _ = _run(
            monkeypatch,
            {"mixed": {"url": "https://example.com/mcp", "command": "npx"}},
        )
        assert "both 'url' and 'command' set" in out
        # HTTP wins, so the entry is validated as an http server.
        assert "(http)" in out


class TestHttpServers:
    def test_valid_url_passes(self, monkeypatch):
        out, issues = _run(
            monkeypatch, {"remote": {"url": "https://mcp.example.com/mcp"}}
        )
        assert "MCP server 'remote' (http)" in out
        assert issues == []

    def test_invalid_url_fails(self, monkeypatch):
        out, issues = _run(monkeypatch, {"remote": {"url": "example.com/mcp"}})
        assert "invalid url" in out
        assert any("remote" in i for i in issues)

    def test_non_mapping_headers_warns(self, monkeypatch):
        out, _ = _run(
            monkeypatch,
            {"remote": {"url": "https://mcp.example.com", "headers": "Bearer x"}},
        )
        assert "'headers' is not a mapping" in out


class TestStdioServers:
    def test_resolvable_command_passes(self, monkeypatch, tmp_path):
        binary = tmp_path / "my-mcp"
        binary.write_text("#!/bin/sh\n")

        out, issues = _run(
            monkeypatch,
            {"local": {"command": "my-mcp", "args": ["--stdio"]}},
            resolve=lambda cmd, env: (str(binary), env),
        )
        assert "MCP server 'local' (stdio)" in out
        assert issues == []

    def test_unresolved_command_fails_with_path_hint(self, monkeypatch):
        # _resolve_stdio_command echoes the bare name back when nothing matches.
        out, issues = _run(
            monkeypatch,
            {"local": {"command": "definitely-not-installed"}},
            resolve=lambda cmd, env: (cmd, env),
        )
        assert "not found on PATH" in out
        assert any("definitely-not-installed" in i for i in issues)

    def test_resolved_but_missing_file_fails(self, monkeypatch, tmp_path):
        ghost = tmp_path / "gone" / "server"
        out, issues = _run(
            monkeypatch,
            {"local": {"command": "server"}},
            resolve=lambda cmd, env: (str(ghost), env),
        )
        assert "command not found" in out
        assert any(str(ghost) in i for i in issues)

    def test_non_string_command_fails(self, monkeypatch):
        out, issues = _run(monkeypatch, {"local": {"command": None}})
        assert "must be a non-empty string" in out
        assert any("local" in i for i in issues)

    def test_non_list_args_warns(self, monkeypatch, tmp_path):
        binary = tmp_path / "srv"
        binary.write_text("#!/bin/sh\n")
        out, _ = _run(
            monkeypatch,
            {"local": {"command": "srv", "args": "--stdio"}},
            resolve=lambda cmd, env: (str(binary), env),
        )
        assert "'args' is not a list" in out

    def test_empty_declared_env_warns(self, monkeypatch, tmp_path):
        binary = tmp_path / "srv"
        binary.write_text("#!/bin/sh\n")
        out, _ = _run(
            monkeypatch,
            {"local": {"command": "srv", "env": {"API_KEY": ""}}},
            resolve=lambda cmd, env: (str(binary), env),
            safe_env=lambda user_env: {"API_KEY": ""},
        )
        assert "empty env value(s)" in out
        assert "API_KEY" in out

    def test_populated_declared_env_passes(self, monkeypatch, tmp_path):
        binary = tmp_path / "srv"
        binary.write_text("#!/bin/sh\n")
        out, issues = _run(
            monkeypatch,
            {"local": {"command": "srv", "env": {"API_KEY": "set"}}},
            resolve=lambda cmd, env: (str(binary), env),
            safe_env=lambda user_env: {"API_KEY": "set"},
        )
        assert "MCP server 'local' (stdio)" in out
        assert issues == []


class TestResilience:
    def test_config_read_failure_warns_but_does_not_raise(self, monkeypatch):
        import tools.mcp_tool as mcp_tool

        def _boom():
            raise RuntimeError("config exploded")

        monkeypatch.setattr(mcp_tool, "_load_mcp_config", _boom)
        issues: list[str] = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            doctor._doctor_mcp_servers(issues)
        assert "Could not read mcp_servers" in buf.getvalue()

    def test_makes_no_subprocess_or_network_calls(self, monkeypatch, tmp_path):
        """The preflight is static: it must not launch or dial anything."""
        import subprocess

        def _fail(*a, **k):
            raise AssertionError("preflight must not spawn subprocesses")

        monkeypatch.setattr(subprocess, "run", _fail)
        monkeypatch.setattr(subprocess, "Popen", _fail)

        binary = tmp_path / "srv"
        binary.write_text("#!/bin/sh\n")
        out, _ = _run(
            monkeypatch,
            {
                "local": {"command": "srv"},
                "remote": {"url": "https://mcp.example.com/mcp"},
            },
            resolve=lambda cmd, env: (str(binary), env),
        )
        assert "(stdio)" in out and "(http)" in out
