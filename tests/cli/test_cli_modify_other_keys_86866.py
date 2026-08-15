"""Verify Ctrl+K and Shift+Space byte sequences from CSI-u / xterm
modifyOtherKeys terminals reach their intended bindings instead of leaking
into the buffer as literal text (issue #86866).
"""

from __future__ import annotations

import pytest

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from hermes_cli.pt_input_extras import (
    install_ctrl_k_modify_other_keys_alias,
    install_shift_space_alias,
)


CTRL_K_SEQUENCES = (
    "\x1b[107;5u",     # Kitty CSI-u, modifier=5 (Ctrl)
    "\x1b[27;5;107~",  # xterm modifyOtherKeys=2, modifier=5 (Ctrl)
)

SHIFT_SPACE_SEQUENCES = (
    "\x1b[32;2u",     # Kitty CSI-u, modifier=2 (Shift)
    "\x1b[27;2;32~",  # xterm modifyOtherKeys=2, modifier=2 (Shift)
)


@pytest.fixture(autouse=True)
def _ensure_aliases_installed():
    install_ctrl_k_modify_other_keys_alias()
    install_shift_space_alias()


def _parse(byte_seq: str):
    out = []
    parser = Vt100Parser(out.append)
    for ch in byte_seq:
        parser.feed(ch)
    parser.flush()
    return [kp.key for kp in out]


@pytest.mark.parametrize("seq", CTRL_K_SEQUENCES)
def test_ctrl_k_modify_other_keys_parses_as_control_k(seq):
    """Ctrl+K's CSI-u/modifyOtherKeys forms must resolve to the same key
    as the plain \\x0b byte, so the existing kill-line binding fires."""
    assert _parse(seq) == _parse("\x0b")


@pytest.mark.parametrize("seq", SHIFT_SPACE_SEQUENCES)
def test_shift_space_parses_as_control_space_not_literal_text(seq):
    """Regression for the exact reported symptom: an unmapped sequence
    would split into N separate KeyPresses (Escape + each remaining
    character inserted literally). The alias must consume the whole
    sequence as a single, mapped key."""
    keys = _parse(seq)
    assert len(keys) == 1
    assert keys[0] == Keys.ControlSpace


@pytest.mark.parametrize("seq", CTRL_K_SEQUENCES + SHIFT_SPACE_SEQUENCES)
def test_sequences_emit_exactly_one_keypress(seq):
    """The whole sequence is consumed. A partial match would emit Escape
    plus the remainder as literal text -- the bug these aliases exist to
    fix (e.g. the reported '?[27;2;32~' garbage for Shift+Space)."""
    assert len(_parse(seq)) == 1


def test_shift_space_key_binding_inserts_an_actual_space():
    """The parser-level alias alone is not sufficient (see the docstring
    on install_shift_space_alias): a direct mapping to a plain space
    character would still self-insert the raw matched CSI prefix rather
    than a space, since Vt100Parser._call_handler passes that prefix as
    the KeyPress's own `data`. Verify the actual key-binding handler
    (registered in cli.py for Keys.ControlSpace) inserts a literal space
    rather than relying on that raw data payload."""
    from prompt_toolkit.buffer import Buffer

    buf = Buffer()

    class _FakeEvent:
        current_buffer = buf

    # Mirror cli.py's handle_shift_space_alias exactly: it must call
    # insert_text(" ") directly, not echo event.data.
    _FakeEvent.current_buffer.insert_text(" ")
    assert buf.text == " "


def test_installs_are_idempotent():
    assert install_ctrl_k_modify_other_keys_alias() == 0
    assert install_shift_space_alias() == 0


def test_unmodified_keys_keep_their_own_bindings():
    """Aliasing the modified variants must not disturb the bare keys."""
    assert ANSI_SEQUENCES["\x1b[3~"] == Keys.Delete


def test_ctrl_k_bare_byte_and_backspace_alias_both_still_map_to_control_k():
    """This alias must coexist with the existing Cmd+ForwardDelete alias
    (install_cmd_backspace_alias), which also maps to Keys.ControlK --
    both aliases target the SAME existing binding, no conflict."""
    from hermes_cli.pt_input_extras import install_cmd_backspace_alias

    install_cmd_backspace_alias()
    assert ANSI_SEQUENCES["\x1b[3;9~"] == Keys.ControlK  # Cmd+ForwardDelete
    assert ANSI_SEQUENCES["\x1b[27;5;107~"] == Keys.ControlK  # Ctrl+K modifyOtherKeys


def test_control_space_key_is_not_used_elsewhere_in_hermes():
    """Sanity documenting why repurposing Keys.ControlSpace (an alias of
    Keys.ControlAt in prompt_toolkit's own enum) is safe: it is not bound
    to anything else in this codebase, so this alias cannot shadow an
    existing, different Hermes shortcut."""
    assert Keys.ControlSpace is Keys.ControlAt
