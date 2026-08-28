"""Regression tests for kitty keypad (PUA codepoint) parity in the classic CLI.

The kitty keyboard protocol differs from xterm modifyOtherKeys in two ways
that repeatedly produce kitty-only defects, because kitty removed
modifyOtherKeys support entirely and therefore has no second protocol to fall
back on:

* it reports the **unshifted** codepoint plus a Shift modifier, where
  modifyOtherKeys emitters report the already-shifted one; and
* it encodes keys that have no legacy form — the keypad, F13+, lock keys — as
  **Private Use Area** codepoints, which modifyOtherKeys never sends.

Each test below pins behaviour that was broken because of one of those two
properties.
"""

from __future__ import annotations

import pytest

from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES

from hermes_cli import pt_input_extras as extras


@pytest.fixture(autouse=True)
def _isolated_sequence_table():
    """Install the aliases per-test and restore the table afterwards, so the
    hundreds of registrations do not leak into sibling test files."""
    saved = dict(ANSI_SEQUENCES)
    extras.install_shift_enter_alias()
    extras.install_ctrl_enter_alias()
    extras.install_cmd_backspace_alias()
    extras.install_modify_other_keys_aliases()
    extras.install_ignored_terminal_sequences()
    yield
    ANSI_SEQUENCES.clear()
    ANSI_SEQUENCES.update(saved)
    from prompt_toolkit.input.vt100_parser import _IS_PREFIX_OF_LONGER_MATCH_CACHE

    _IS_PREFIX_OF_LONGER_MATCH_CACHE.clear()


# ---------------------------------------------------------------------------
# kitty Private Use Area keys — modifyOtherKeys has no equivalent, so any gap
# here is kitty-only by construction.
# ---------------------------------------------------------------------------


KEYPAD_MIRRORS = {
    57417: "\x1b[1;{mod}D",  # KP_Left  mirrors Left
    57418: "\x1b[1;{mod}C",  # KP_Right
    57419: "\x1b[1;{mod}A",  # KP_Up
    57420: "\x1b[1;{mod}B",  # KP_Down
    57414: "\x1b[13;{mod}u",  # KP_Enter mirrors Enter
}


@pytest.mark.parametrize("modifier", [2, 3, 5])
@pytest.mark.parametrize("codepoint,template", sorted(KEYPAD_MIRRORS.items()))
def test_modified_keypad_mirrors_its_non_keypad_twin(codepoint, template, modifier):
    """Modified keypad keys leaked raw CSI entirely before the fix (#90640).

    They must resolve to exactly what the equivalent non-keypad key resolves
    to, so the keypad cannot drift away from the keys it stands in for.
    """
    twin = ANSI_SEQUENCES.get(template.format(mod=modifier))
    if twin is None:
        pytest.skip("the non-keypad equivalent is itself unmapped")
    assert ANSI_SEQUENCES.get(f"\x1b[{codepoint};{modifier}u") == twin


def test_bare_keypad_keys_still_resolve():
    """The unmodified forms were always fine — keep them that way."""
    from prompt_toolkit.keys import Keys

    assert ANSI_SEQUENCES.get("\x1b[57417u") == Keys.Left
    assert ANSI_SEQUENCES.get("\x1b[57419u") == Keys.Up
    assert ANSI_SEQUENCES.get("\x1b[57414u") == Keys.ControlM


def test_lock_bit_twins_exist_for_csi_u_only():
    """kitty ORs CapsLock/NumLock into the modifier; modifyOtherKeys never
    does, so the tilde form must NOT gain lock twins."""
    for modifier in (2, 66, 130, 194):
        assert ANSI_SEQUENCES.get(f"\x1b[97;{modifier}u") == "A"
    for modifier in (66, 130, 194):
        assert ANSI_SEQUENCES.get(f"\x1b[27;{modifier};97~") is None


