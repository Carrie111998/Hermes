"""Verify the modifyOtherKeys / CSI-u single-character data fix.

When Ghostty runs in xterm modifyOtherKeys mode (which Hermes enables for
Ghostty terminals), Shift+letters arrive as ``ESC[27;2;<cp>~`` sequences
instead of plain characters.  ``install_modify_other_keys_aliases()`` maps
them to single-character string keys (``'F'``, ``' '``), but
prompt_toolkit's ``Vt100Parser._call_handler`` attaches the *raw matched
sequence* as the KeyPress ``data``, and the self-insert binding inserts
``event.data`` — so Shift+F typed literal ``[27;2;70~`` into the prompt.

``install_vt100_single_char_data_fix()`` rewrites ``data`` to the character
for single-char string keys, fixing every such mapping at the source.
"""

from __future__ import annotations

import pytest

from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from hermes_cli.pt_input_extras import install_modify_other_keys_aliases
from hermes_cli.pt_input_extras import install_vt100_single_char_data_fix


@pytest.fixture(autouse=True)
def _ensure_aliases_installed():
    """Make every test idempotent — install the aliases once per test run."""
    install_modify_other_keys_aliases()
    install_vt100_single_char_data_fix()


def _parse(byte_seq: str):
    out = []
    parser = Vt100Parser(out.append)
    for ch in byte_seq:
        parser.feed(ch)
    parser.flush()
    return [(kp.key, kp.data) for kp in out]


def test_install_returns_one_then_zero():
    """Idempotency — running install twice should not report a second change."""
    install_vt100_single_char_data_fix()
    assert install_vt100_single_char_data_fix() == 0


def test_shift_letter_inserts_the_character_not_the_escape_sequence():
    """Shift+F under modifyOtherKeys must type 'F', not '[27;2;70~'."""
    assert _parse("\x1b[27;2;70~") == [("F", "F")]


def test_shift_space_inserts_a_space():
    """Shift+Space must type a space, not the raw sequence."""
    assert _parse("\x1b[27;2;32~") == [(" ", " ")]


def test_alt_letter_inserts_the_letter():
    """Alt+letter previously died (empty data); it must type the letter."""
    keypresses = _parse("\x1b[27;3;97~")
    assert keypresses[0][0] is Keys.Escape
    assert keypresses[1] == ("a", "a")


def test_plain_letter_unchanged():
    """Normal typing is untouched — key and data are already identical."""
    assert _parse("F") == [("F", "F")]


def test_enum_keys_unchanged():
    """Ctrl+C still parses to Keys.ControlC (its own binding, not self-insert)."""
    assert _parse("\x1b[99;5u")[0][0] is Keys.ControlC
