"""Tests for the GUI-surface ``open_preview`` tool."""

import json

import pytest

from tools import desktop_ui, open_preview_tool as op
from tools.registry import registry


@pytest.fixture(autouse=True)
def _reset_emitter():
    """Each test controls the bridge; never leak one across tests."""
    desktop_ui.set_emitter(None)
    desktop_ui.set_requester(None)
    yield
    desktop_ui.set_emitter(None)
    desktop_ui.set_requester(None)


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

    desktop_ui.set_requester(_boom)
    assert "no window" in json.loads(op.open_preview_tool("https://x.example"))["error"]


def test_waits_for_the_renderer_to_confirm_the_pane_is_visible():
    calls = []

    def request(sid, event, payload, timeout):
        calls.append((sid, event, payload, timeout))
        return json.dumps({"success": True, "tab_id": "url:browser", "url": payload["url"]})

    desktop_ui.set_requester(request)

    out = json.loads(op.open_preview_tool("www.example.com", "Example"))

    assert out == {
        "success": True,
        "tab_id": "url:browser",
        "url": "https://www.example.com",
    }
    assert calls == [
        (
            "",
            "preview.open",
            {"url": "https://www.example.com", "label": "Example"},
            10,
        )
    ]


def test_unanswered_renderer_reports_a_stale_or_missing_desktop_pane():
    desktop_ui.set_requester(lambda *_args: "")

    out = json.loads(op.open_preview_tool("https://x.example"))

    assert "Update the Hermes Desktop app" in out["error"]


def test_renderer_rejection_is_returned_instead_of_false_success():
    desktop_ui.set_requester(
        lambda *_args: json.dumps({"success": False, "error": "Preview pane did not become visible."})
    )

    out = json.loads(op.open_preview_tool("https://x.example"))

    assert out == {"error": "Preview pane did not become visible."}
