"""NumLock-modified CSI cursor keys must parse as arrows, not leak as text.

Kitty (and xterm-style terminals with the keyboard protocol active) encode
NumLock as bit 8 of the CSI modifier, so a plain Up arrives as
``ESC [ 1 ; 129 A``. Stock prompt_toolkit only tables modifiers 2–9, so
the VT100 parser emits Escape plus the remainder as literal characters —
the classic CLI then inserts ``[1;129A`` into the prompt.
"""

from __future__ import annotations

import pytest

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from hermes_cli.pt_input_extras import install_numlock_cursor_aliases


NUMLOCK_UNMODIFIED = (
    ("\x1b[1;129A", Keys.Up),
    ("\x1b[1;129B", Keys.Down),
    ("\x1b[1;129C", Keys.Right),
    ("\x1b[1;129D", Keys.Left),
    ("\x1b[1;129H", Keys.Home),
    ("\x1b[1;129F", Keys.End),
)

DISAMBIGUATED_UNMODIFIED = (
    ("\x1b[1;1A", Keys.Up),
    ("\x1b[1;1B", Keys.Down),
    ("\x1b[1;1C", Keys.Right),
    ("\x1b[1;1D", Keys.Left),
)


@pytest.fixture(autouse=True)
def _ensure_alias_installed():
    install_numlock_cursor_aliases()


def _parse(byte_seq: str):
    out = []
    parser = Vt100Parser(out.append)
    for ch in byte_seq:
        parser.feed(ch)
    parser.flush()
    return [kp.key for kp in out]


@pytest.mark.parametrize("seq, key", NUMLOCK_UNMODIFIED)
def test_numlock_arrows_parse_as_unmodified_cursor_keys(seq, key):
    assert _parse(seq) == [key]


@pytest.mark.parametrize("seq, key", DISAMBIGUATED_UNMODIFIED)
def test_disambiguated_unmodified_arrows_parse_as_cursor_keys(seq, key):
    """Kitty keyboard protocol flag 1 sends ESC[1;1A for a plain arrow."""
    assert _parse(seq) == [key]


@pytest.mark.parametrize("seq, _key", NUMLOCK_UNMODIFIED + DISAMBIGUATED_UNMODIFIED)
def test_sequences_emit_exactly_one_keypress(seq, _key):
    """The whole sequence is consumed. A partial match would emit Escape
    plus the remainder as literal text — the bug this alias exists to fix."""
    assert len(_parse(seq)) == 1


def test_numlock_shift_up_keeps_shift_binding():
    assert _parse("\x1b[1;130A") == _parse("\x1b[1;2A") == [Keys.ShiftUp]


def test_numlock_ctrl_right_keeps_ctrl_binding():
    assert _parse("\x1b[1;133C") == _parse("\x1b[1;5C") == [Keys.ControlRight]


def test_plain_arrows_keep_their_own_bindings():
    assert ANSI_SEQUENCES["\x1b[A"] == Keys.Up
    assert ANSI_SEQUENCES["\x1b[B"] == Keys.Down
    assert ANSI_SEQUENCES["\x1b[C"] == Keys.Right
    assert ANSI_SEQUENCES["\x1b[D"] == Keys.Left
    assert _parse("\x1b[A") == [Keys.Up]


def test_install_is_idempotent():
    install_numlock_cursor_aliases()
    assert install_numlock_cursor_aliases() == 0
