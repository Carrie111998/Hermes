from __future__ import annotations

from types import SimpleNamespace

from gateway import kanban_watchers


def test_board_rotation_gives_every_board_the_first_slot(monkeypatch):
    monkeypatch.setattr(kanban_watchers, "_KANBAN_BOARD_ROTATION", 0)
    boards = [{"slug": slug} for slug in ("default", "alpha", "beta")]

    first_slots = [
        kanban_watchers._rotate_boards_for_dispatch(boards)[0]["slug"]
        for _ in range(len(boards))
    ]

    assert set(first_slots) == {"default", "alpha", "beta"}
    assert boards == [
        {"slug": "default"}, {"slug": "alpha"}, {"slug": "beta"},
    ]


def test_dispatch_skip_counts_are_visible():
    result = SimpleNamespace(
        skipped_nonspawnable=["a", "b"],
        skipped_unassigned=["c"],
    )

    assert kanban_watchers._dispatch_skip_counts(result) == (2, 1)
    assert kanban_watchers._dispatch_skip_counts(SimpleNamespace()) == (0, 0)
