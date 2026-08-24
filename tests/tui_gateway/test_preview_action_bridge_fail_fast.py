"""Preview actions fail fast when the Desktop renderer bridge is unavailable."""

import json

import pytest

import tui_gateway.server as server


@pytest.fixture
def session(monkeypatch):
    record = {}
    monkeypatch.setitem(server._sessions, "s1", record)
    return record


@pytest.fixture
def bridge(monkeypatch):
    calls = []

    def fake_block(event, sid, payload, timeout=None, **_kw):
        answer = fake_block.answers.pop(0) if fake_block.answers else ""
        calls.append({"event": event, "sid": sid, "payload": payload, "timeout": timeout})
        return answer

    fake_block.answers = []
    fake_block.calls = calls
    monkeypatch.setattr(server, "_block", fake_block)
    return fake_block


def test_first_preview_action_uses_short_bridge_probe(session, bridge):
    bridge.answers = [json.dumps({"success": True})]

    server._preview_action_request("s1", {"action": "elements"})

    assert bridge.calls[0]["event"] == "preview.act.request"
    assert bridge.calls[0]["timeout"] == server._PREVIEW_ACTION_PROBE_TIMEOUT_S
    assert server._PREVIEW_ACTION_PROBE_TIMEOUT_S < server._PREVIEW_ACTION_TIMEOUT_S


def test_answered_preview_bridge_gets_full_action_deadline(session, bridge):
    bridge.answers = [json.dumps({"success": True}), json.dumps({"success": True})]

    server._preview_action_request("s1", {"action": "elements"})
    server._preview_action_request("s1", {"action": "click", "selector": "h1"})

    assert bridge.calls[1]["timeout"] == server._PREVIEW_ACTION_TIMEOUT_S


def test_unanswered_preview_probe_returns_actionable_error(session, bridge):
    result = json.loads(server._preview_action_request("s1", {"action": "elements"}))

    assert result["success"] is False
    assert "desktop" in result["error"].lower()
    assert "session" in result["error"].lower()


def test_repeated_preview_actions_short_circuit_after_unanswered_probe(session, bridge):
    server._preview_action_request("s1", {"action": "elements"})

    for payload in (
        {"action": "elements"},
        {"action": "annotate", "selector": "h1", "label": "Heading"},
    ):
        assert json.loads(server._preview_action_request("s1", payload))["success"] is False

    assert len(bridge.calls) == 1


def test_slow_action_does_not_condemn_proven_preview_bridge(session, bridge):
    bridge.answers = [json.dumps({"success": True})]
    server._preview_action_request("s1", {"action": "elements"})
    server._preview_action_request("s1", {"action": "click", "selector": "h1"})

    bridge.answers = [json.dumps({"success": True})]
    result = server._preview_action_request("s1", {"action": "elements"})

    assert json.loads(result)["success"] is True
    assert len(bridge.calls) == 3
