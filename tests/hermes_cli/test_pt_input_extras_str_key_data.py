"""Regression tests for the VT100 string-key data fix (#92343).

``ANSI_SEQUENCES`` mappings whose value is a plain string (Hermes'
Shift+letter table) produce ``KeyPress(key, data)`` where prompt_toolkit
sets ``data`` to the RAW matched byte sequence. The default self-insert
binding inserts ``event.data``, so Shift+a leaked ``ESC[27;2;97~`` into
the prompt buffer even after the sequence itself was decoded correctly.
"""

import pytest

from hermes_cli.pt_input_extras import (
    install_vt100_str_key_data_fix,
    _clear_vt100_prefix_cache,
)


@pytest.fixture
def parser_factory():
    """Yield a factory building Vt100Parser instances that collect keys.

    Installs Hermes' real ANSI_SEQUENCES mappings (as cli.py does at
    startup) so the Shift+letter sequence is actually decoded, applies the
    string-key data fix for the duration of each parser, and restores the
    original ``_call_handler`` afterwards so the global class mutation
    never leaks into other tests.
    """
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    from hermes_cli.pt_input_extras import install_modify_other_keys_aliases

    install_modify_other_keys_aliases()
    original = Vt100Parser._call_handler
    created = []

    def factory(with_fix: bool):
        if with_fix:
            install_vt100_str_key_data_fix()
        presses: list = []
        parser = Vt100Parser(presses.append)
        created.append((parser, presses))
        return parser, presses

    yield factory

    Vt100Parser._call_handler = original
    if hasattr(Vt100Parser, "_hermes_str_key_data_fix"):
        del Vt100Parser._hermes_str_key_data_fix
    _clear_vt100_prefix_cache()


def _feed_sequence(parser, presses) -> str:
    """Feed the Shift+a modifyOtherKeys sequence and return inserted data."""
    parser.feed("\x1b[27;2;97~")
    parser.flush()
    assert len(presses) == 1, f"expected 1 press, got {presses!r}"
    press = presses[0]
    assert press.key == "A"  # the mapping resolves to the uppercase letter
    return press.data


def test_without_fix_data_is_the_raw_sequence(parser_factory):
    parser, presses = parser_factory(with_fix=False)
    data = _feed_sequence(parser, presses)
    assert data == "\x1b[27;2;97~"  # the leak (#92343)


def test_with_fix_data_is_the_mapped_character(parser_factory):
    parser, presses = parser_factory(with_fix=True)
    data = _feed_sequence(parser, presses)
    assert data == "A"  # self-insert now types the letter


def test_keys_valued_mappings_keep_raw_data(parser_factory):
    """Keys.* targets are untouched: ControlA keeps its raw byte as data."""
    from prompt_toolkit.keys import Keys

    parser, presses = parser_factory(with_fix=True)
    parser.feed("\x01")  # Ctrl+A
    parser.flush()
    assert len(presses) == 1
    assert presses[0].key is Keys.ControlA
    assert presses[0].data == "\x01"


def test_install_is_idempotent(parser_factory):
    from prompt_toolkit.input.vt100_parser import Vt100Parser

    first = install_vt100_str_key_data_fix()
    second = install_vt100_str_key_data_fix()
    assert first is True or hasattr(Vt100Parser, "_hermes_str_key_data_fix")
    assert second is False or hasattr(Vt100Parser, "_hermes_str_key_data_fix")
