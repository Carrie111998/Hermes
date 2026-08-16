"""Regression tests for the character-valued half of
``install_modify_other_keys_aliases()`` — Shift+letter under
modifyOtherKeys level 2 must *type a capital*, not leak its escape sequence.

Mapping ``ESC[27;2;72~`` → ``"H"`` in ``ANSI_SEQUENCES`` fixes what the key
*is*, but prompt_toolkit's parser reports ``KeyPress(key="H",
data="\\x1b[27;2;72~")`` and the default ``Keys.Any`` binding inserts
``event.data``. The raw sequence therefore still reached the prompt buffer.

Key-level assertions cannot catch that — ``_parse()`` compares ``KeyPress.key``,
which was already correct. These tests drive a real ``PromptSession`` over a
pipe input and assert on the *submitted text*, which is what users see.
"""

from __future__ import annotations

import pytest

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.shortcuts import PromptSession

from hermes_cli.pt_input_extras import (
    install_ctrl_enter_alias,
    install_modify_other_keys_aliases,
    install_shift_enter_alias,
)


@pytest.fixture(autouse=True)
def _ensure_alias_installed():
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES as _seq
    saved = dict(_seq)
    # Same order as cli.py — also proves the Enter aliases are not clobbered.
    install_shift_enter_alias()
    install_ctrl_enter_alias()
    install_modify_other_keys_aliases()
    yield
    _seq.clear()
    _seq.update(saved)
    from prompt_toolkit.input.vt100_parser import _IS_PREFIX_OF_LONGER_MATCH_CACHE
    _IS_PREFIX_OF_LONGER_MATCH_CACHE.clear()


def _type(byte_seq: str) -> str:
    """Feed raw terminal bytes to a real prompt and return what was submitted."""
    with create_pipe_input() as inp:
        inp.send_text(byte_seq + "\r")
        return PromptSession(input=inp, output=DummyOutput()).prompt()


def _mok(codepoint: int, modifier: int = 2) -> str:
    return f"\x1b[27;{modifier};{codepoint}~"


def _csiu(codepoint: int, modifier: int = 2) -> str:
    return f"\x1b[{codepoint};{modifier}u"


def test_shift_letter_types_a_capital():
    assert _type(_mok(ord("h")) + "ello") == "Hello"


def test_shift_letter_shifted_codepoint_form_types_a_capital():
    """Terminals that report the already-shifted codepoint (72 = 'H')."""
    assert _type(_mok(ord("H")) + "ello") == "Hello"


def test_shift_letter_csi_u_form_types_a_capital():
    assert _type(_csiu(ord("h")) + "ello") == "Hello"


def test_mixed_sentence_round_trips():
    typed = _mok(ord("h")) + "ermes " + _mok(ord("c")) + "LI"
    assert _type(typed) == "Hermes CLI"


def test_shift_space_types_a_space():
    assert _type("a" + _mok(32) + "b") == "a b"


@pytest.mark.parametrize("letter", ["a", "m", "z"])
def test_every_letter_inserts_its_own_character(letter):
    assert _type(_mok(ord(letter))) == letter.upper()


def test_key_press_data_matches_the_character():
    """The mechanism, asserted directly: data drives insertion, so it must
    be the character rather than the bytes that produced it."""
    presses = []
    parser = Vt100Parser(presses.append)
    for ch in _mok(ord("q")):
        parser.feed(ch)
    parser.flush()
    assert len(presses) == 1
    assert presses[0].key == "Q"
    assert presses[0].data == "Q"


def test_ctrl_combo_bindings_still_fire():
    """Keys-valued entries are untouched: Ctrl+A moves to line start."""
    assert _type("bc" + _mok(ord("a"), modifier=5) + "X") == "Xbc"


def test_named_keys_keep_their_raw_data():
    presses = []
    parser = Vt100Parser(presses.append)
    for ch in "\x1b[A":
        parser.feed(ch)
    parser.flush()
    assert [(kp.key, kp.data) for kp in presses] == [(Keys.Up, "\x1b[A")]


def test_shift_enter_alias_is_not_clobbered():
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    assert ANSI_SEQUENCES["\x1b[27;2;13~"] == (Keys.Escape, Keys.ControlM)


def test_plain_typing_is_unaffected():
    assert _type("Hi there!") == "Hi there!"
    assert _type("café ☕") == "café ☕"


def test_bracketed_paste_is_unaffected():
    assert _type("\x1b[200~Pasted TEXT\x1b[201~") == "Pasted TEXT"
