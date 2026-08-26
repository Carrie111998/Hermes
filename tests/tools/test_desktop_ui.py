"""Tests for the desktop-only renderer-event bridge."""

import pytest

from tools import desktop_ui


@pytest.fixture(autouse=True)
def _reset_emitter():
    desktop_ui.set_emitter(None)
    desktop_ui.set_requester(None)
    yield
    desktop_ui.set_emitter(None)
    desktop_ui.set_requester(None)


def test_unavailable_without_emitter():
    assert desktop_ui.available() is False
    assert desktop_ui.emit("preview.open", {"url": "x"}) is False
    assert desktop_ui.request("preview.open", {"url": "x"}, timeout=10) is None


def test_routes_event_to_owning_window(monkeypatch):
    monkeypatch.setattr(
        desktop_ui, "get_session_env",
        lambda name, default="": "win-7" if name == "HERMES_UI_SESSION_ID" else default,
    )
    seen = []
    desktop_ui.set_emitter(lambda sid, event, payload: seen.append((sid, event, payload)))

    assert desktop_ui.available() is True
    assert desktop_ui.emit("pane.reveal", {"pane": "terminal"}) is True
    assert seen == [("win-7", "pane.reveal", {"pane": "terminal"})]


def test_routes_request_to_owning_window(monkeypatch):
    monkeypatch.setattr(
        desktop_ui, "get_session_env",
        lambda name, default="": "win-9" if name == "HERMES_UI_SESSION_ID" else default,
    )
    seen = []
    desktop_ui.set_requester(
        lambda sid, event, payload, timeout: seen.append((sid, event, payload, timeout)) or "answer"
    )

    assert desktop_ui.request("preview.open", {"url": "x"}, timeout=10) == "answer"
    assert seen == [("win-9", "preview.open", {"url": "x"}, 10)]
