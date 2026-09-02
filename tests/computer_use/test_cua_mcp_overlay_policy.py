"""Regression tests for #81220 — overlay policy on user-registered MCP.

A user-registered ``mcp_servers.cua-driver`` entry (``args: [mcp]``) used to
spawn cua-driver verbatim: the args bypassed the ``--no-overlay`` policy the
embedded cua-backend already applies at ``_resolve_mcp_invocation``, so the
cursor overlay (a fullscreen InputOutput override-redirect X11 window) could
map across the whole virtual desktop and swallow every click outside Hermes
until a manual ``set_agent_cursor_enabled(false)``.

These assert the behavior contract of the MCP-launch normalization —
policy-derived flag injection, deduplication of an explicit flag, explicit
opt-in preservation, byte-identical passthrough for unrelated commands, and
the older-driver support-probe invariant — not snapshots of config.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.computer_use import cua_backend


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    cua_backend._cua_driver_supports_no_overlay.cache_clear()
    yield
    cua_backend._cua_driver_supports_no_overlay.cache_clear()


class TestLooksLikeCuaDriverCommand:
    """The detector recognizes every *class* of cua-driver invocation."""

    @pytest.mark.parametrize(
        "command",
        [
            "cua-driver",                       # bare name
            "/usr/local/bin/cua-driver",        # absolute POSIX path
            "./cua-driver",                     # relative path
            "C:\\Program Files\\cua\\cua-driver.exe",  # Windows path + .exe
            "cua-driver-rs",                    # renamed binary
            "cua_driver",                       # underscore spelling
        ],
    )
    def test_recognizes_cua_driver_binaries(self, command):
        assert cua_backend.looks_like_cua_driver_command(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "",                          # empty
            None,                        # absent
            "npx",                       # unrelated launcher
            "mcp-remote",                # unrelated server
            "my-cua-driver-wrapper",     # lookalike — must NOT match
            "driver-cua",                # reordered lookalike
        ],
    )
    def test_rejects_non_cua_driver_commands(self, command):
        assert cua_backend.looks_like_cua_driver_command(command) is False


class TestNormalizeUserCuaDriverArgs:
    """Overlay policy applied to a user-configured MCP launch."""

    def test_appends_flag_when_policy_true(self):
        """The #81220 repro: policy resolves True (Linux/X11 auto-detect),
        driver supports the flag — the user's ``args: [mcp]`` must gain
        ``--no-overlay``."""
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ):
            result = cua_backend.normalize_user_cua_driver_args(
                "/usr/local/bin/cua-driver", ["mcp"],
            )
        assert result == ["mcp", "--no-overlay"]

    @pytest.mark.parametrize(
        "flag", ["--no-overlay", "--no-overlay=true", "--no-overlay=false"]
    )
    def test_deduplicates_explicit_flag(self, flag):
        """A user who already supplied ``--no-overlay`` keeps one token —
        duplicate flags are noise at best and a driver-side error at worst."""
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ):
            result = cua_backend.normalize_user_cua_driver_args(
                "cua-driver", ["mcp", flag],
            )
        assert result == ["mcp", flag]

    def test_explicit_optin_false_preserves_overlay(self):
        """``computer_use.no_overlay: false`` is the documented opt-in to
        keep the cursor visualization — normalization must not fight it."""
        with patch.object(cua_backend, "_cua_no_overlay", return_value=False), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ):
            result = cua_backend.normalize_user_cua_driver_args(
                "cua-driver", ["mcp"],
            )
        assert result == ["mcp"]

    def test_unrelated_command_is_byte_identical(self):
        """Non-cua-driver servers must pass through untouched — this is what
        keeps the change class-level instead of touching every MCP spawn."""
        args = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ):
            result = cua_backend.normalize_user_cua_driver_args(
                "npx", list(args),
            )
        assert result == args

    def test_older_driver_falls_back_without_flag(self):
        """Older drivers reject unknown flags; when the installed binary
        doesn't recognise ``--no-overlay`` the launch must proceed without
        it (the embedded-backend probe invariant)."""
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=False,
             ):
            result = cua_backend.normalize_user_cua_driver_args(
                "cua-driver", ["mcp"],
            )
        assert result == ["mcp"]

    def test_does_not_mutate_caller_list(self):
        original = ["mcp"]
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ):
            cua_backend.normalize_user_cua_driver_args("cua-driver", original)
        assert original == ["mcp"]

    def test_probe_runs_against_user_resolved_binary(self):
        """The support probe must run against the user's binary, not the
        embedded default — a relocated driver with a different feature set
        is the exact case the embedded path guards."""
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ) as mock_probe:
            cua_backend.normalize_user_cua_driver_args(
                "/opt/relocated/cua-driver", ["mcp"],
            )
        mock_probe.assert_called_with("/opt/relocated/cua-driver")


class TestCuaDriverSupportProbeCache:
    def test_retains_results_for_multiple_driver_paths(self):
        """Side-by-side cua-driver installs are each probed only once."""
        completed = SimpleNamespace(
            stdout="--no-overlay",
            stderr="",
        )
        with patch.object(
            cua_backend.subprocess, "run", return_value=completed,
        ) as mock_run:
            assert cua_backend._cua_driver_supports_no_overlay("/opt/a/cua-driver")
            assert cua_backend._cua_driver_supports_no_overlay("/opt/b/cua-driver")
            assert cua_backend._cua_driver_supports_no_overlay("/opt/a/cua-driver")
        assert mock_run.call_count == 2


class TestMcpStdioHookAppliesPolicy:
    """One integration proof through the real ``_run_stdio``: normalization
    lands AFTER command resolution and BEFORE the OSV preflight / watchdog
    wrap, so the policy rides the real user launch line.

    The transport is faked at the two SDK seams (``stdio_client`` /
    ``ClientSession``): the fake session raises on ``__aenter__``, so the
    coroutine unwinds through the real ``finally`` right after the spawn
    seam — deterministically, with no subprocess and no network.
    """

    @pytest.fixture
    def server_task(self):
        pytest.importorskip("mcp")
        import tools.mcp_tool as mcp_tool_mod
        from tools.mcp_tool import MCPServerTask

        # Bind the lazy SDK symbols (stdio_client et al. are PEP 562
        # lazy attrs) so the monkeypatches below target real module state.
        assert mcp_tool_mod._ensure_mcp_sdk() is True

        task = MCPServerTask.__new__(MCPServerTask)
        task.name = "cua-driver"
        task._sampling = None
        task._elicitation = None
        return task

    @staticmethod
    def _capture_spawn(monkeypatch):
        """Drive ``_run_stdio`` to the spawn seam with every external
        effect stubbed; return the dict the fake transport fills."""
        captured = {}

        class _FakeStdioCM:
            async def __aenter__(self):
                return (None, None)

            async def __aexit__(self, *exc):
                return False

        def _fake_stdio_client(server_params, errlog=None):
            captured["command"] = server_params.command
            captured["args"] = list(server_params.args)
            return _FakeStdioCM()

        class _ExplodingSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                raise RuntimeError("stop at spawn seam")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            "tools.mcp_tool._resolve_stdio_command",
            lambda command, env: (command, env),
        )
        monkeypatch.setattr(
            "tools.mcp_tool._wrap_command_with_watchdog",
            lambda command, args: (command, args),
        )
        monkeypatch.setattr("tools.mcp_tool._build_safe_env", lambda user_env: {})
        monkeypatch.setattr(
            "tools.mcp_tool._kill_orphaned_mcp_children", lambda: None
        )
        monkeypatch.setattr("tools.mcp_tool._snapshot_child_pids", lambda: set())
        monkeypatch.setattr(
            "tools.mcp_tool._write_stderr_log_header", lambda name: None
        )
        monkeypatch.setattr("tools.mcp_tool._get_mcp_stderr_log", lambda: None)
        monkeypatch.setattr("tools.mcp_tool._filter_mcp_children", lambda pids: set())
        monkeypatch.setattr(
            "tools.osv_check.check_package_for_malware", lambda *a, **k: None
        )
        monkeypatch.setattr("tools.mcp_tool.ClientSession", _ExplodingSession)
        monkeypatch.setattr("tools.mcp_tool.stdio_client", _fake_stdio_client)
        return captured

    @staticmethod
    def _drive(server_task, config):
        import asyncio

        async def _run():
            # The exploding fake session intentionally aborts the coroutine
            # at the spawn seam; any exception type is acceptable so long as
            # the args were captured first.
            try:
                await server_task._run_stdio(config)
            except Exception:
                pass

        asyncio.run(_run())

    def test_user_cua_driver_entry_receives_flag_before_spawn(
        self, server_task, monkeypatch
    ):
        """End-to-end through the real ``_run_stdio``: a user-registered
        ``cua-driver`` entry with ``args: [mcp]`` gets ``--no-overlay``
        appended to the launch args (Linux/X11 auto-detect path)."""
        captured = self._capture_spawn(monkeypatch)
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay", return_value=True
             ):
            self._drive(server_task, {"command": "cua-driver", "args": ["mcp"]})
        assert captured["args"] == ["mcp", "--no-overlay"], (
            "user-configured cua-driver MCP must receive --no-overlay so "
            "#81220 cannot reproduce via mcp_servers"
        )

    def test_unrelated_entry_untouched_through_run_stdio(
        self, server_task, monkeypatch
    ):
        """A filesystem MCP server's args must reach the spawn untouched."""
        captured = self._capture_spawn(monkeypatch)
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay", return_value=True
             ):
            self._drive(server_task, {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            })
        assert captured["args"] == [
            "-y", "@modelcontextprotocol/server-filesystem", "/tmp",
        ]
