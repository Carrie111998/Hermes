from __future__ import annotations

from tools import file_tools


def _enable_gate(monkeypatch):
    monkeypatch.setattr(
        file_tools,
        "_protected_instruction_config",
        lambda: (True, []),
    )
    monkeypatch.setattr(
        file_tools,
        "_request_protected_instruction_approval",
        lambda reasons, task_id="default": "BLOCKED:" + ",".join(reasons),
    )


def test_blocks_python_open_write_bypass(monkeypatch):
    _enable_gate(monkeypatch)
    command = (
        "python3 -c \"p='.claude/CLAUDE.md'; "
        "data=open(p).read(); open(p, 'w').write(data)\""
    )
    result = file_tools._check_protected_instruction_command(command)
    assert result is not None
    assert "claude.md" in result.casefold()


def test_blocks_pathlib_write_from_execute_code(monkeypatch):
    _enable_gate(monkeypatch)
    code = "from pathlib import Path\nPath('AGENTS.md').write_text('replace')"
    result = file_tools._check_protected_instruction_command(code)
    assert result is not None
    assert "agents.md" in result.casefold()


def test_blocks_shell_redirection_to_instruction_file(monkeypatch):
    _enable_gate(monkeypatch)
    result = file_tools._check_protected_instruction_command(
        "printf x > ~/.config/opencode/AGENTS.md"
    )
    assert result is not None
    assert "agents.md" in result.casefold()


def test_allows_read_only_reference(monkeypatch):
    _enable_gate(monkeypatch)
    assert file_tools._check_protected_instruction_command(
        "shasum -a 256 ~/.claude/CLAUDE.md"
    ) is None


def test_allows_unrelated_file_write(monkeypatch):
    _enable_gate(monkeypatch)
    assert file_tools._check_protected_instruction_command(
        "Path('report.md').write_text('ok')"
    ) is None