"""A supervised gateway must never run `hermes update` from its own tool.

The update mutates the checkout the gateway is importing from, and the
terminal tool owns the updater's lifetime: a tool timeout, a cancelled
turn, or the gateway's own restart kills the updater mid-mutation and
leaves a moved HEAD with a live pre-update interpreter — the exact torn
module-graph window this work closes.

The detached `/update` slash command stays the supported route (it
spawns the updater outside the gateway's ownership), and read-only
`hermes update --check` / `--plan` remain runnable.
"""

from __future__ import annotations

import json

import pytest

from cron.lifecycle_guard import (
    contains_hermes_update_command,
    contains_hermes_update_command_or_referenced_script,
)

BLOCKED = (
    "hermes update",
    "hermes update --backup --yes",
    "hermes update --yes --backup",
    "hermes update --gateway",
    "hermes -p zeus update",
    "hermes --profile zeus update --yes",
    "/usr/local/bin/hermes update",
    "cd /tmp && hermes update --backup --yes",
    "nohup hermes update --yes &",
    "python -m hermes_cli.main update --yes",
)

ALLOWED = (
    "hermes update --check",
    "hermes update --plan",
    "hermes -p zeus update --check",
    "hermes gateway status",
    "git pull",
    "echo 'run hermes update from a shell' > notes.txt",
    "grep -r 'hermes update' docs/",
)


class TestUpdateCommandDetection:
    def test_blocked_shapes_are_detected(self):
        for command in BLOCKED:
            assert contains_hermes_update_command(command) is True, command

    def test_readonly_and_unrelated_shapes_are_not_detected(self):
        for command in ALLOWED:
            assert contains_hermes_update_command(command) is False, command

    def test_referenced_script_is_scanned(self, tmp_path):
        script = tmp_path / "deploy.sh"
        script.write_text("#!/bin/sh\nhermes update --backup --yes\n", encoding="utf-8")
        script.chmod(0o755)
        assert (
            contains_hermes_update_command_or_referenced_script(
                f"bash {script}", cwd=str(tmp_path)
            )
            is True
        )

    def test_unrelated_script_is_not_flagged(self, tmp_path):
        script = tmp_path / "ok.sh"
        script.write_text("#!/bin/sh\nhermes gateway status\n", encoding="utf-8")
        script.chmod(0o755)
        assert (
            contains_hermes_update_command_or_referenced_script(
                f"bash {script}", cwd=str(tmp_path)
            )
            is False
        )

    @pytest.mark.parametrize(
        "command",
        [
            "sh -c 'hermes update --yes'",
            'bash -lc "hermes update --yes"',
            'bash -lic "hermes update"',
            "sh -ec 'hermes update'",
        ],
    )
    def test_shell_command_payloads_are_scanned(self, command):
        """`-lc` is `-c` with extra letters — the payload still gets scanned.

        Detection is command-position-aware, so the update only shows up
        inside the payload string. A walk that recognized a bare ``-c``
        only would have been one flag letter away from being bypassed.
        """
        assert contains_hermes_update_command_or_referenced_script(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            'bash -lc "hermes update --check"',
            'bash -lc "echo hermes update"',
            "bash -lc 'ls -la'",
        ],
    )
    def test_read_only_and_prose_payloads_stay_allowed(self, command):
        assert contains_hermes_update_command_or_referenced_script(command) is False


class TestTerminalToolRejectsUpdate:
    """The tool must refuse without ever handing the command to a shell."""

    GUARD_MARKER = "cannot run from inside the gateway process"

    @pytest.fixture
    def supervised_gateway(self, monkeypatch):
        import tools.process_registry as process_registry

        monkeypatch.setattr(
            process_registry, "_is_supervised_gateway_process", lambda: True
        )
        return process_registry

    @pytest.fixture
    def spawn_log(self, monkeypatch):
        """Record every process spawn attempt; still runs the real thing.

        Environment bootstrap legitimately spawns a probe shell, so the
        assertion is about WHAT was spawned, not whether anything was.
        """
        import subprocess

        seen: list[str] = []
        real_popen = subprocess.Popen
        real_run = subprocess.run

        def _popen(cmd, *args, **kwargs):
            seen.append(str(cmd))
            return real_popen(cmd, *args, **kwargs)

        def _run(cmd, *args, **kwargs):
            seen.append(str(cmd))
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", _popen)
        monkeypatch.setattr(subprocess, "run", _run)
        return seen

    @pytest.fixture
    def stub_hermes(self, tmp_path):
        """An executable literally named ``hermes``, but harmless.

        The guard keys on the executable's BASENAME, so this is the same
        shape it must reject — while the not-blocked assertions can safely
        let the command actually run instead of invoking the real updater
        on the host running the suite.
        """
        stub = tmp_path / "hermes"
        stub.write_text("#!/bin/sh\necho stub-hermes-ran\n", encoding="utf-8")
        stub.chmod(0o755)
        return stub

    # The suite's live-system guard matches the literal text `hermes
    # update` in any spawned command, so the not-blocked assertions need
    # its documented opt-out. Safe here precisely BECAUSE of the stub:
    # the only thing that can run is the throwaway script above, never
    # the real updater.

    @pytest.mark.parametrize(
        "command", ["hermes update", "hermes update --backup --yes"]
    )
    def test_direct_update_is_rejected_without_spawning(
        self, supervised_gateway, spawn_log, command
    ):
        from tools.terminal_tool import terminal_tool

        result = json.loads(terminal_tool(command, task_id="guard-direct"))

        assert result["status"] == "error"
        assert result["exit_code"] == 1
        assert "/update" in result["error"]
        assert not any(command in entry for entry in spawn_log)

    def test_wrapper_script_is_rejected_without_spawning(
        self, supervised_gateway, spawn_log, tmp_path
    ):
        from tools.terminal_tool import terminal_tool

        script = tmp_path / "up.sh"
        script.write_text("#!/bin/sh\nhermes update --yes\n", encoding="utf-8")
        script.chmod(0o755)

        result = json.loads(
            terminal_tool(f"bash {script}", task_id="guard-script", workdir=str(tmp_path))
        )
        assert result["status"] == "error"
        assert "/update" in result["error"]
        assert not any(str(script) in entry for entry in spawn_log)

    @pytest.mark.live_system_guard_bypass
    def test_read_only_update_check_is_not_blocked(
        self, supervised_gateway, stub_hermes
    ):
        """`--check` mutates nothing; the agent must keep being able to ask."""
        from tools.terminal_tool import terminal_tool

        result = json.loads(
            terminal_tool(f"{stub_hermes} update --check", task_id="guard-check")
        )
        assert self.GUARD_MARKER not in (result.get("error") or "")
        assert "stub-hermes-ran" in result["output"]

    def test_unrelated_commands_still_run_in_a_supervised_gateway(
        self, supervised_gateway
    ):
        from tools.terminal_tool import terminal_tool

        result = json.loads(
            terminal_tool("echo guard-scope-ok", task_id="guard-scope")
        )
        assert result["exit_code"] == 0
        assert "guard-scope-ok" in result["output"]

    @pytest.mark.live_system_guard_bypass
    def test_outside_a_supervised_gateway_the_guard_does_not_fire(
        self, monkeypatch, stub_hermes
    ):
        """An external shell / plain CLI must stay able to update."""
        import tools.process_registry as process_registry

        monkeypatch.setattr(
            process_registry, "_is_supervised_gateway_process", lambda: False
        )
        from tools.terminal_tool import terminal_tool

        result = json.loads(
            terminal_tool(
                f"{stub_hermes} update --backup --yes", task_id="guard-external"
            )
        )
        assert self.GUARD_MARKER not in (result.get("error") or "")
        assert "stub-hermes-ran" in result["output"]
