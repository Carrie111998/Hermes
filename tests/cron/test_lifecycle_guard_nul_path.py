"""Regression: the lifecycle guard must not crash on paths with embedded NULs.

``_iter_referenced_shell_scripts`` tokenizes command text, and a binary's
decoded bytes can yield tokens carrying NUL characters. Two call sites raised
``ValueError`` (not ``OSError``) on those tokens and propagated up, killing the
entire terminal tool call (observed 2026-08-04, sibling of #76762):

* ``_resolve_terminal_script_path`` — ``Path.expanduser()`` raises when the
  leading ``~`` segment holds a NUL.
* ``_read_referenced_script`` — ``os.open()`` raises on any NUL in the path.

A token that cannot name a real script must be treated as "nothing to scan",
never as a crash.
"""

from pathlib import Path

from cron.lifecycle_guard import (
    _read_referenced_script,
    _resolve_terminal_script_path,
    contains_gateway_lifecycle_command_or_referenced_script,
)

# Built at runtime so this source file stays free of literal NUL bytes.
NUL = chr(0)


class TestEmbeddedNullPath:
    def test_resolve_script_path_survives_nul_in_tilde_segment(self):
        # Path.expanduser() raises ValueError here; the guard must not.
        resolved = _resolve_terminal_script_path(f"~{NUL}/evil.sh", "/tmp")
        assert isinstance(resolved, Path)

    def test_read_referenced_script_returns_nothing_for_nul_path(self):
        text, unsafe = _read_referenced_script(Path(f"/tmp/has{NUL}nul.sh"))
        assert text is None
        assert unsafe is False

    def test_guard_does_not_raise_on_nul_in_tilde_token(self):
        result = contains_gateway_lifecycle_command_or_referenced_script(
            f"bash ~{NUL}/evil.sh"
        )
        assert result is False

    def test_guard_does_not_raise_on_bare_nul_tilde_token(self):
        result = contains_gateway_lifecycle_command_or_referenced_script(
            f"~{NUL}/evil.sh"
        )
        assert result is False

    def test_guard_does_not_raise_on_nul_in_absolute_token(self):
        result = contains_gateway_lifecycle_command_or_referenced_script(
            f"bash /tmp/evil{NUL}script.sh"
        )
        assert result is False

    def test_real_lifecycle_command_still_blocked(self):
        # The NUL tolerance must not weaken the guard itself.
        result = contains_gateway_lifecycle_command_or_referenced_script(
            "systemctl --user restart hermes-gateway.service"
        )
        assert result is True
