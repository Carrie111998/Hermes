"""Shift+key under xterm modifyOtherKeys level 2 must type the character.

Ghostty is pushed modifyOtherKeys=2 (CSI >4;2m) by the classic CLI, so it
re-encodes every Shift combo as ESC[27;2;<codepoint>~.  Two defects made those
keypresses leak the raw escape text into the prompt:

  1. ANSI_SEQUENCES resolved the key to 'T' correctly, but prompt_toolkit
     builds KeyPress(key, data) with data = the matched byte sequence, and the
     default Keys.Any binding inserts event.data.
  2. shift_map covered letters only, so shifted symbols had no mapping at all.
"""

import pytest

from hermes_cli.pt_input_extras import (
    install_ctrl_enter_alias,
    install_modify_other_keys_aliases,
    install_plain_char_data_fix,
    install_shift_enter_alias,
)


@pytest.fixture(scope="module", autouse=True)
def _installed():
    install_shift_enter_alias()
    install_ctrl_enter_alias()
    install_modify_other_keys_aliases()
    install_plain_char_data_fix()


def _parse(sequence):
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    presses = []
    parser = Vt100Parser(presses.append)
    for char in sequence:
        parser.feed(char)
    parser.flush()
    return [(kp.key, kp.data) for kp in presses]


@pytest.mark.parametrize(
    "sequence,char",
    [
        ("\x1b[27;2;84~", "T"),
        ("\x1b[27;2;72~", "H"),
        ("\x1b[27;2;73~", "I"),
        ("\x1b[84;2u", "T"),
    ],
)
def test_shift_letter_inserts_the_capital(sequence, char):
    assert _parse(sequence) == [(char, char)]


@pytest.mark.parametrize(
    "sequence,char",
    [
        ("\x1b[27;2;126~", "~"),
        ("\x1b[27;2;33~", "!"),
        ("\x1b[27;2;64~", "@"),
        ("\x1b[27;2;123~", "{"),
    ],
)
def test_shift_symbol_inserts_the_symbol(sequence, char):
    """The already-shifted codepoint IS the character — no layout guessing."""
    assert _parse(sequence) == [(char, char)]


def test_no_shift_sequence_leaks_escape_text():
    for codepoint in range(33, 127):
        for key, data in _parse(f"\x1b[27;2;{codepoint}~"):
            assert "\x1b" not in data, (codepoint, key, data)
            assert "[27;" not in data, (codepoint, key, data)


def test_control_keys_keep_their_raw_data():
    """Only single-character keys are rewritten; Keys.* payloads are untouched."""
    from prompt_toolkit.keys import Keys

    assert _parse("\x1b[27;5;99~") == [(Keys.ControlC, "\x1b[27;5;99~")]
    assert _parse("\x1b[A") == [(Keys.Up, "\x1b[A")]
    assert _parse("\x1b[27;2;13~") == [
        (Keys.Escape, "\x1b[27;2;13~"),
        (Keys.ControlM, ""),
    ]


def test_plain_characters_are_unaffected():
    assert _parse("a") == [("a", "a")]
    assert _parse("Z") == [("Z", "Z")]


def test_install_is_idempotent():
    assert install_plain_char_data_fix() == 0
