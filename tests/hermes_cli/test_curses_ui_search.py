from hermes_cli.curses_ui import (
    _SearchState,
    _extended_key_modes_active,
    _filter_indices,
    _handle_active_search_key,
    _move_filtered_cursor,
    _reconcile_cursor,
    _toggle_extended_key_modes,
    curses_single_select,
)


class _FakeCurses:
    KEY_BACKSPACE = 263
    KEY_DOWN = 258
    KEY_ENTER = 343




def test_reconcile_cursor_moves_to_first_visible_match():
    assert _reconcile_cursor([2, 4], 0) == (2, 0)
    assert _reconcile_cursor([2, 4], 4) == (4, 1)




def test_active_search_consumes_query_editing_and_confirm_keys():
    search = _SearchState(active=True, query="op")

    assert _handle_active_search_key(_FakeCurses, ord("u"), search) == (True, False, True)
    assert search.query == "opu"

    assert _handle_active_search_key(_FakeCurses, _FakeCurses.KEY_ENTER, search) == (
        True,
        True,
        False,
    )


def test_toggle_extended_key_modes_noop_when_not_a_tty(monkeypatch, capsys):
    """No stray escape bytes should be written when stdout isn't a real TTY.

    Regression guard for the curses-picker Ctrl+C/Ctrl+W leak fix: the
    pop/push around ``curses.wrapper()`` must stay silent outside a real
    terminal (pytest's captured stdout, CI, piped output) instead of writing
    raw CSI bytes that would show up as literal text.
    """
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    _toggle_extended_key_modes("\x1b[<u\x1b[>4m")
    assert capsys.readouterr().out == ""


def test_extended_key_modes_active_false_without_cli_module(monkeypatch):
    """Falls back to False (never raises) if cli.py can't be imported.

    Guards the lazy import in ``_extended_key_modes_active`` — a standalone
    import of ``hermes_cli.curses_ui`` (e.g. from a plugin, or a test that
    hasn't loaded ``cli.py``) must not crash the pop/push guard.
    """
    import builtins

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "cli":
            raise ImportError("simulated: cli module unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    assert _extended_key_modes_active() is False


def test_curses_single_select_search_labels_length_mismatch_falls_back(monkeypatch):
    """A mismatched ``search_labels`` length must not raise or crash the menu.

    ``curses_single_select`` appends the synthetic cancel row internally; a
    caller-supplied ``search_labels`` that doesn't match ``len(items)`` (a
    caller bug) silently disables search for that call instead of raising
    or corrupting the filter with misaligned indices. Verified via the
    non-TTY early return, which is the cheapest path that exercises the
    length check without needing a real curses screen.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    result = curses_single_select(
        "title",
        ["a", "b"],
        searchable=True,
        search_labels=["only-one"],
    )
    assert result is None  # non-TTY cancel_value; no exception raised
