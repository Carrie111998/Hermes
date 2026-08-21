"""Regression test for `initial_query` pre-seeding the search prompt.

Covers the `/ms foo` flow: a caller that already knows the user's query
should land on an active, pre-filled, still-editable filter instead of
forcing a second `/` keypress before the query can be changed.
"""
from unittest.mock import MagicMock, patch

from hermes_cli.curses_ui import _KEEP, _run_curses_menu


def test_initial_query_seeds_active_editable_search_state():
    captured = {}

    def draw_header(stdscr, max_y, max_x, search=None):
        # First frame only: record what the loop seeded before any keypress.
        if "search_active" not in captured:
            captured["search_active"] = search.active
            captured["search_query"] = search.query
        return 3

    def draw_row(stdscr, y, idx, is_cursor, max_x):
        pass

    def on_action(action, cursor):
        return cursor  # resolve immediately, whatever the cursor is

    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (30, 120)
    # ENTER while search is active: confirms the (already-filtered)
    # selection without requiring a nav key first.
    mock_stdscr.getch.return_value = 10

    with patch("sys.stdin.isatty", return_value=True):
        with patch("curses.wrapper", side_effect=lambda func: func(mock_stdscr)):
            with patch("curses.curs_set"):
                with patch("curses.has_colors", return_value=False):
                    result = _run_curses_menu(
                        initial_cursor=0,
                        item_count=3,
                        draw_header=draw_header,
                        draw_row=draw_row,
                        on_action=on_action,
                        fallback=lambda: None,
                        cancel_value=None,
                        searchable=True,
                        search_labels=["alpha", "opus", "omega"],
                        initial_query="op",
                    )

    assert captured["search_active"] is True
    assert captured["search_query"] == "op"
    assert result != _KEEP


def test_empty_initial_query_leaves_search_inactive():
    captured = {}

    def draw_header(stdscr, max_y, max_x, search=None):
        if "search_active" not in captured:
            captured["search_active"] = search.active
            captured["search_query"] = search.query
        return 3

    def draw_row(stdscr, y, idx, is_cursor, max_x):
        pass

    def on_action(action, cursor):
        return cursor

    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (30, 120)
    mock_stdscr.getch.return_value = 10  # ENTER selects the initial cursor

    with patch("sys.stdin.isatty", return_value=True):
        with patch("curses.wrapper", side_effect=lambda func: func(mock_stdscr)):
            with patch("curses.curs_set"):
                with patch("curses.has_colors", return_value=False):
                    _run_curses_menu(
                        initial_cursor=0,
                        item_count=3,
                        draw_header=draw_header,
                        draw_row=draw_row,
                        on_action=on_action,
                        fallback=lambda: None,
                        cancel_value=None,
                        searchable=True,
                        search_labels=["alpha", "opus", "omega"],
                        initial_query="",
                    )

    assert captured["search_active"] is False
    assert captured["search_query"] == ""
