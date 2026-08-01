from __future__ import annotations

import tui_gateway.entry as entry


def test_signal_handlers_are_disabled_off_main_thread(monkeypatch):
    worker = object()
    monkeypatch.setattr(entry.threading, "current_thread", lambda: worker)
    monkeypatch.setattr(entry.threading, "main_thread", lambda: object())

    assert entry._can_install_signal_handlers() is False
