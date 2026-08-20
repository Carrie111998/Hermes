"""Tests for the GUI-surface ``send_bot_group`` tool.

Posts one message into an existing Bot Mode group room through the desktop
blocking bridge. The renderer/plugin answers whether the room accepted it.
"""

import json

from tools import send_bot_group_tool as sbg
from tools.registry import registry


def _run(**kwargs):
    kwargs.setdefault("callback", lambda _payload: json.dumps({"success": True, "queued": True, "group": "Workshop"}))
    return json.loads(sbg.send_bot_group_tool(**kwargs))


def test_lives_in_the_gui_surface_toolset(monkeypatch):
    """Surface eligibility is the toolset's job, not a process env var — the
    desktop client can be driving a remote/cloud backend that never sees
    HERMES_DESKTOP."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    entry = registry.get_entry("send_bot_group")

    assert entry is not None
    assert entry.toolset == "desktop_ui"
    assert entry.check_fn is None


def test_requires_callback():
    assert "desktop app" in json.loads(sbg.send_bot_group_tool(group="Workshop", message="hello", callback=None))["error"]


def test_forwards_trimmed_payload_to_the_bridge():
    seen = {}

    def cb(payload):
        seen.update(payload)
        return json.dumps({"success": True, "queued": True, "group": "Workshop"})

    out = json.loads(sbg.send_bot_group_tool(group="  Workshop  ", message="  kick Gate 0  ", callback=cb))

    assert out == {"success": True, "queued": True, "group": "Workshop"}
    assert seen == {"group": "Workshop", "text": "kick Gate 0"}


def test_optional_thread_is_forwarded():
    seen = {}

    def cb(payload):
        seen.update(payload)
        return json.dumps({"success": True, "queued": True, "group": "Workshop", "thread": "th-1"})

    sbg.send_bot_group_tool(group="Workshop", message="follow-up", thread="th-1", callback=cb)

    assert seen == {"group": "Workshop", "text": "follow-up", "thread": "th-1"}


def test_empty_or_non_string_group_or_message_is_an_error():
    cb = lambda _p: json.dumps({"success": True, "queued": True, "group": "Workshop"})

    assert "group is required" in sbg.send_bot_group_tool(group="  ", message="hello", callback=cb)
    assert "message is required" in sbg.send_bot_group_tool(group="Workshop", message="   ", callback=cb)
    assert "group is required" in sbg.send_bot_group_tool(group=["Workshop"], message="hello", callback=cb)
    assert "message is required" in sbg.send_bot_group_tool(group="Workshop", message=["hello"], callback=cb)


def test_unknown_group_from_the_bridge_is_an_error():
    out = _run(group="Nope", message="hello", callback=lambda _p: json.dumps({"error": "Unknown Bot Mode group, or the message was empty."}))

    assert "Unknown Bot Mode group" in out["error"]


def test_unanswered_bridge_is_reported_rather_than_faked_as_success():
    assert "error" in _run(group="Workshop", message="hello", callback=lambda _p: "")
