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
    """Reaches a desktop client on ANY backend, including one with no
    HERMES_DESKTOP in its environment (URL / cloud gateways)."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    entry = registry.get_entry("open_preview")

    assert entry is not None
    assert entry.toolset == "desktop_ui"
    assert entry.check_fn is None


def test_emitter_failure_is_reported():
    def _boom(*_a):
        raise RuntimeError("no window")

    desktop_ui.set_emitter(_boom)
    assert "no window" in json.loads(op.open_preview_tool("https://x.example"))["error"]


def test_remote_forward_defaults_to_false_and_is_emitted_only_when_requested():
    emitted = []

    desktop_ui.set_emitter(lambda *_args: emitted.append(_args))

    assert json.loads(op.open_preview_tool("http://localhost:5173"))["remote_forward"] is False
    assert emitted[-1][2] == {"url": "http://localhost:5173", "label": "", "remote_forward": False}

    assert json.loads(op.open_preview_tool("http://localhost:5173", remote_forward=True))["remote_forward"] is True
    assert emitted[-1][2] == {"url": "http://localhost:5173", "label": "", "remote_forward": True}


def test_registry_handler_passes_remote_forward_option():
    emitted = []
    desktop_ui.set_emitter(lambda *_args: emitted.append(_args))

    entry = registry.get_entry("open_preview")
    assert entry is not None

    entry.handler({"url": "http://localhost:5173", "remote_forward": True})

    assert emitted[-1][2]["remote_forward"] is True
    assert op.OPEN_PREVIEW_SCHEMA["parameters"]["properties"]["remote_forward"]["type"] == "boolean"
    assert "user-requested remote dev server" in op.OPEN_PREVIEW_SCHEMA["parameters"]["properties"]["remote_forward"]["description"]
