"""Tests for the GUI-surface ``open_preview`` tool."""

import json

import pytest

from tools import desktop_ui, open_preview_tool as op
from tools.registry import registry


@pytest.fixture(autouse=True)
def _reset_emitter():
    """Each test controls the emitter; never leak one across tests."""
    desktop_ui.set_emitter(None)
    yield
    desktop_ui.set_emitter(None)


def test_lives_in_the_gui_surface_toolset(monkeypatch):
    import tools.preview_tool  # noqa: F401 — registers desktop_preview
    """Consolidated (#95681): this module's tool became an action of the
    single `desktop_preview` tool in desktop_ui; the old registration is gone and
    `preview` reaches a desktop client on ANY backend (no env gate)."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    assert registry.get_entry("open_preview") is None
    entry = registry.get_entry("desktop_preview")
    assert entry is not None
    assert entry.toolset == "desktop_ui"
    assert entry.check_fn is None


def test_emitter_failure_is_reported():
    def _boom(*_a):
        raise RuntimeError("no window")

    desktop_ui.set_emitter(_boom)
    assert "no window" in json.loads(op.open_preview_tool("https://x.example"))["error"]


def test_new_tab_defaults_off_and_travels_when_asked(monkeypatch):
    """Re-using its tab is the default; an extra tab has to be asked for."""
    sent = []
    monkeypatch.setattr(
        op.desktop_ui, "emit", lambda event, payload: sent.append((event, payload)) or True
    )

    json.loads(op.open_preview_tool(url="example.com"))
    json.loads(op.open_preview_tool(url="example.com", new_tab=True))

    assert sent[0][1]["new_tab"] is False
    assert sent[1][1]["new_tab"] is True


def test_new_tab_is_offered_to_the_model():
    """A parameter the schema omits is one the model can never reach for."""
    assert "new_tab" in op.OPEN_PREVIEW_SCHEMA["parameters"]["properties"]
