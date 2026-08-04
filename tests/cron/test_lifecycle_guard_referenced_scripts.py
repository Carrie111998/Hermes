"""Tests for referenced-script scanning in the gateway lifecycle guard.

Covers three defects in how `contains_gateway_lifecycle_command_or_referenced_script`
walks the files a command references:

- Directories were treated as unsafe (fail-closed), so an innocent script that
  merely NAMED a directory in a string literal was hard-blocked.
- Non-executable data files (config.yaml, .env, credentials.json) were read and
  regex-scanned as if they were shell scripts, so prose inside them could block a
  command -- and secrets were read for no reason.
- The POSIX dot builtin (`. script.sh`) evaded the scan entirely, because
  `Path(".").name` is `""` rather than `"."`. The spelled-out `source` form was
  caught, so the bypass was inconsistent as well as unsafe.

The unifying rule these tests pin down: a referenced file is scanned when it could
actually be EXECUTED, and not merely because a path-shaped token mentions it.
"""

import os
import stat

import pytest

from cron.lifecycle_guard import (
    contains_gateway_lifecycle_command_or_referenced_script,
)

RESTART = "hermes gateway restart\n"


def _write(path, body, mode=0o644):
    path.write_text(body)
    os.chmod(path, mode)
    return str(path)


# ---------------------------------------------------------------------------
# Directories must not fail closed
# ---------------------------------------------------------------------------

class TestDirectoryReferences:
    """A directory can never execute as a script, so it must not block."""

    def test_script_naming_a_directory_is_not_blocked(self, tmp_path):
        workspace = tmp_path / "deliverables"
        workspace.mkdir()
        script = _write(
            tmp_path / "health-check.py",
            "import os\n"
            f'WORKSPACE_ROOT = os.path.expanduser("{workspace}")\n'
            'print("ok")\n',
            0o755,
        )
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            script, cwd=str(tmp_path)
        )

    def test_directory_passed_directly_is_not_blocked(self, tmp_path):
        target = tmp_path / "somedir"
        target.mkdir()
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            str(target), cwd=str(tmp_path)
        )


# ---------------------------------------------------------------------------
# Data files are scanned only when actually executed
# ---------------------------------------------------------------------------

class TestDataFileReferences:
    """Prose in a config file is not a command -- unless something runs it."""

    @pytest.fixture
    def config_with_prose(self, tmp_path):
        # Mirrors a real denylist entry that reads like a command but is not one.
        return _write(
            tmp_path / "config.yaml",
            "denied_actions:\n  - kill hermes/gateway process (self-termination)\n",
            0o600,
        )

    def test_bare_mention_of_data_file_is_not_blocked(
        self, tmp_path, config_with_prose
    ):
        script = tmp_path / "reader.py"
        _write(script, f'CONFIG = "{config_with_prose}"\nprint(CONFIG)\n', 0o755)
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            str(script), cwd=str(tmp_path)
        )

    def test_data_file_referenced_directly_is_not_blocked(
        self, tmp_path, config_with_prose
    ):
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            config_with_prose, cwd=str(tmp_path)
        )

    @pytest.mark.parametrize("invocation", ["sh {path}", "bash {path}", ". {path}"])
    def test_executing_a_data_file_IS_blocked(self, tmp_path, invocation):
        payload = _write(tmp_path / "payload.json", RESTART, 0o644)
        command = invocation.format(path=payload)
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command, cwd=str(tmp_path)
        ), f"executing a data file must still be scanned: {command!r}"


# ---------------------------------------------------------------------------
# The POSIX dot builtin must not evade the scan
# ---------------------------------------------------------------------------

class TestSourceBuiltin:
    """`. script` executes in the current shell and must be scanned."""

    @pytest.mark.parametrize("verb", [".", "source"])
    def test_sourcing_a_restart_is_blocked(self, tmp_path, verb):
        payload = _write(tmp_path / "payload.sh", RESTART, 0o644)
        assert contains_gateway_lifecycle_command_or_referenced_script(
            f"{verb} {payload}", cwd=str(tmp_path)
        )

    def test_sourcing_relative_path_is_blocked(self, tmp_path):
        _write(tmp_path / "payload.sh", RESTART, 0o644)
        assert contains_gateway_lifecycle_command_or_referenced_script(
            ". ./payload.sh", cwd=str(tmp_path)
        )

    def test_bare_dot_without_argument_is_harmless(self, tmp_path):
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            ".", cwd=str(tmp_path)
        )

    def test_relative_executable_is_unaffected(self, tmp_path):
        _write(tmp_path / "innocent.sh", "#!/bin/sh\necho hi\n", 0o755)
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            "./innocent.sh", cwd=str(tmp_path)
        )


# ---------------------------------------------------------------------------
# Existing detection must not regress
# ---------------------------------------------------------------------------

class TestDetectionNotWeakened:
    """Everything the guard caught before must still be caught."""

    @pytest.mark.parametrize("command", [
        "hermes gateway restart",
        "hermes gateway stop",
        "launchctl kickstart -k gui/501/ai.hermes.gateway",
        "systemctl --user restart hermes-gateway",
        "pkill -f hermes.*gateway",
        "pkill -f gateway.*hermes",
        "launchctl submit -l neutral.label -- /tmp/helper.sh",
    ])
    def test_direct_commands_still_blocked(self, command, tmp_path):
        assert contains_gateway_lifecycle_command_or_referenced_script(
            command, cwd=str(tmp_path)
        )

    def test_executable_script_still_scanned(self, tmp_path):
        script = _write(tmp_path / "deploy.sh", f"#!/bin/sh\n{RESTART}", 0o755)
        assert contains_gateway_lifecycle_command_or_referenced_script(
            script, cwd=str(tmp_path)
        )

    def test_non_executable_shell_extension_still_scanned(self, tmp_path):
        """Extension alone qualifies -- a .sh is a script even without +x."""
        script = _write(tmp_path / "deploy.sh", RESTART, 0o644)
        assert contains_gateway_lifecycle_command_or_referenced_script(
            script, cwd=str(tmp_path)
        )

    def test_gateway_start_remains_allowed(self, tmp_path):
        assert not contains_gateway_lifecycle_command_or_referenced_script(
            "hermes gateway start", cwd=str(tmp_path)
        )

    def test_fifo_still_fails_closed(self, tmp_path):
        """Non-regular files that a shell CAN read must stay fail-closed."""
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        assert stat.S_ISFIFO(os.stat(fifo).st_mode)
        assert contains_gateway_lifecycle_command_or_referenced_script(
            f"sh {fifo}", cwd=str(tmp_path)
        )
