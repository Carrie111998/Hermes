"""Tests for the GUI-surface ``send_bot_group`` tool.

Posts one message into an existing Bot Mode group room through the desktop
event bridge. The tool queues delivery; it does not wait for deliberation.
"""

import json

import pytest

from tools import desktop_ui, send_bot_group_tool as sbg
from tools.registry import registry


@pytest.fixture(autouse=True)
def _reset_emitter():
    desktop_ui.set_emitter(None)
    yield
    desktop_ui.set_emitter(None)


def test_lives_in_the_gui_surface_toolset(monkeypatch):
    """Surface eligibility is the toolset's job, not a process env var — the
    desktop client can be driving a remote/cloud backend that never sees
    HERMES_DESKTOP."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    entry = registry.get_entry("send_bot_group")

    assert entry is not None
    assert entry.toolset == "desktop_ui"
    assert entry.check_fn is None


def test_emits_bots_group_send():
    calls = []
    desktop_ui.set_emitter(lambda sid, event, payload: calls.append((event, payload)))

    out = json.loads(sbg.send_bot_group_tool(group="  Workshop  ", message="  kick Gate 0  "))

    assert out == {"success": True, "queued": True, "group": "Workshop"}
    assert calls == [("bots.group.send", {"group": "Workshop", "text": "kick Gate 0"})]


def test_optional_thread_is_forwarded():
    calls = []
    desktop_ui.set_emitter(lambda sid, event, payload: calls.append((event, payload)))

    out = json.loads(sbg.send_bot_group_tool(group="Workshop", message="follow-up", thread="th-1"))

    assert out["queued"] is True
    assert calls == [("bots.group.send", {"group": "Workshop", "text": "follow-up", "thread": "th-1"})]


def test_empty_group_or_message_is_an_error():
    desktop_ui.set_emitter(lambda sid, event, payload: None)

    assert "group is required" in sbg.send_bot_group_tool(group="  ", message="hello")
    assert "message is required" in sbg.send_bot_group_tool(group="Workshop", message="   ")


def test_reports_desktop_only_without_emitter():
    out = sbg.send_bot_group_tool(group="Workshop", message="hello")

    assert "desktop app" in out
