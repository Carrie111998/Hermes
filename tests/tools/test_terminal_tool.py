"""Regression tests for sudo command pass-through and terminal tool schema."""

import tools.terminal_tool as terminal_tool


def test_transform_sudo_command_is_always_a_pass_through(monkeypatch):
    """Hermes no longer pipes a sudo password into commands.

    The sudo-password mechanism (SUDO_PASSWORD env var, interactive prompt,
    session cache) was removed as a security fix — it was a process-global
    secret every agent-spawned command could read. _transform_sudo_command
    now returns every command unchanged and always reports no stdin to
    pipe, regardless of whether the command mentions "sudo" in passing, is
    a real sudo invocation, or SUDO_PASSWORD happens to be set.
    """
    monkeypatch.setenv("SUDO_PASSWORD", "testpass")

    commands = [
        "rg --line-number --no-heading --with-filename 'sudo' . | head -n 20",
        "printf '%s\\n' sudo",
        "grep -n sudo README.md",
        "sudo apt install -y ripgrep",
        "sudo true",
        "sudo a; sudo b",
    ]
    for command in commands:
        transformed, sudo_stdin = terminal_tool._transform_sudo_command(command)
        assert transformed == command
        assert sudo_stdin is None


def test_terminal_schema_advertises_persistent_env_state():
    description = terminal_tool.TERMINAL_TOOL_DESCRIPTION

    assert "exported environment variables persist between calls" in description
    assert "activate a virtualenv" in description
    assert "do not re-source the same environment before every command" in description


def test_validate_workdir_blocks_shell_metacharacters_in_windows_paths():
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project; rm -rf /")
    assert terminal_tool._validate_workdir(r"C:\Users\Alice\project$(whoami)")
    assert terminal_tool._validate_workdir("C:\\Users\\Alice\\project\nwhoami")
