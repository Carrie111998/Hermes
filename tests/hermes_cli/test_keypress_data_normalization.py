"""Regression tests for issue #87390, part 2 — KeyPress.data normalization.

#87511 populated ``ANSI_SEQUENCES`` so modified-key CSI sequences decode to
the right key (e.g. ``ESC[27;2;75~`` → ``'K'``). But prompt_toolkit inserts
``KeyPress.data`` verbatim for printable keys, and the parser keeps the raw
sequence as the data payload — so the buffer received
``[27;2;75~`` as literal text even though the key resolved correctly.

``install_keypress_data_normalization()`` wraps each ``Vt100Parser``
instance's per-instance ``feed_key_callback`` once: when the resolved key is a
single printable character but its data payload is an escape sequence, the
data is rewritten to the key character. Plain typing (raw bytes never touched
by the alias tables) is unaffected because their data already equals their key.
"""

from __future__ import annotations

import pytest

from prompt_toolkit.input.vt100_parser import Vt100Parser

from hermes_cli.pt_input_extras import (
    install_keypress_data_normalization,
    install_modify_other_keys_aliases,
)


@pytest.fixture()
def _normalized_parser():
    installed = install_keypress_data_normalization()
    yield installed


def _keypresses(byte_seq: str):
    out: list = []
    parser = Vt100Parser(out.append)
    for ch in byte_seq:
        parser.feed(ch)
    parser.flush()
    return out


@pytest.mark.parametrize(
    "seq,expected",
    [
        ("\x1b[27;2;75~", "K"),   # Shift+K, modifyOtherKeys form
        ("\x1b[27;2;76~", "L"),   # Shift+L
        ("\x1b[107;2u", "K"),     # Shift+K, CSI-u base-codepoint form
        ("\x1b[75;2u", "K"),      # Shift+K, CSI-u shifted-codepoint form
        ("\x1b[27;3;108~", ("escape", "l")),  # Alt+L: tuple, first key keeps seq data
    ],
)
def test_modified_letter_data_is_normalized(_normalized_parser, seq, expected):
    """Decoded single-char keys must carry the character — not the escape
    sequence — as their insertable data payload."""
    kps = _keypresses(seq)
    assert kps, f"{seq!r} produced no keypress"
    keys = [kp.key for kp in kps]
    if isinstance(expected, tuple):
        assert tuple(keys) == expected
        # First keypress of an alt-tuple still holds the sequence as data;
        # that behavior is unchanged by this patch.
        assert kps[0].data.startswith("\x1b")
    else:
        assert len(kps) == 1
        assert keys[0] == expected
        assert kps[0].data == expected, (
            f"data for {seq!r} must be normalized to {expected!r}, "
            f"got {kps[0].data!r}"
        )


def test_plain_typing_data_untouched(_normalized_parser):
    """Raw printable input must pass through byte-for-byte."""
    kps = _keypresses("hi")
    assert [(kp.key, kp.data) for kp in kps] == [("h", "h"), ("i", "i")]


def test_navigation_keys_data_untouched(_normalized_parser):
    """Non-printable Keys enum members keep their legacy data payloads."""
    up = _keypresses("\x1b[A")
    assert len(up) == 1 and up[0].key.name == "up" or up[0].key.value == "up"
    enter = _keypresses("\r")
    assert len(enter) == 1 and enter[0].data == "\r"


def test_install_idempotent(_normalized_parser):
    """A second install must be a no-op."""
    assert install_keypress_data_normalization() is False


def test_data_normalization_with_alias_tables_installed():
    """End-to-end: with both layers active, the exact sequence from #87390
    resolves to the letter K whose insertable data is 'K'."""
    install_modify_other_keys_aliases()
    install_keypress_data_normalization()
    kps = _keypresses("\x1b[27;2;75~")
    assert len(kps) == 1
    assert kps[0].key == "K" and kps[0].data == "K"
