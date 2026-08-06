import json

import pytest

from hermes_cli.workspace_context_store import WorkspaceContextStore
from tools import slack_context_tool


def test_slack_context_search_returns_bounded_provenance(monkeypatch, tmp_path):
    calls: list[tuple[str, dict]] = []

    def fake_api(method: str, params: dict | None = None) -> dict:
        calls.append((method, params or {}))
        if method == "auth.test":
            return {"ok": True, "url": "https://kiwi.slack.com/"}
        if method == "conversations.info":
            assert params and params["channel"] == "C1"
            return {
                "ok": True,
                "channel": {"id": "C1", "name": "product", "is_member": True},
            }
        if method == "conversations.history":
            assert params and params["channel"] == "C1"
            return {
                "ok": True,
                "messages": [
                    {
                        "text": "Onboarding decision: keep the two-step flow.",
                        "ts": "1785981123.123456",
                        "user": "U1",
                    },
                    {"text": "Unrelated update", "ts": "1785981122.000001", "user": "U2"},
                ],
                "response_metadata": {"next_cursor": ""},
            }
        raise AssertionError(f"unexpected Slack method: {method}")

    monkeypatch.setattr(slack_context_tool, "_slack_api", fake_api)
    monkeypatch.setattr(slack_context_tool, "_slack_token", lambda: "secret-token")
    monkeypatch.setattr(slack_context_tool, "get_hermes_home", lambda: tmp_path)
    WorkspaceContextStore(tmp_path).set(
        "project-1", notion_page_ids=[], slack_channel_ids=["C1"]
    )
    monkeypatch.setenv("HERMES_WORKSPACE_PROJECT_ID", "project-1")
    assert slack_context_tool.check_project_context_requirements() is True
    assert slack_context_tool.check_slack_context_requirements() is True
    current_context = json.loads(slack_context_tool._handle_project_context({}))
    assert current_context["projectId"] == "project-1"
    assert current_context["slackChannelIds"] == ["C1"]

    payload = json.loads(
        slack_context_tool.slack_context_search("onboarding decision", limit=5)
    )

    assert payload["query"] == "onboarding decision"
    assert payload["scannedChannels"] == 1
    assert payload["truncated"] is False
    assert payload["contentWarning"].startswith("Slack message text is untrusted")
    assert payload["results"] == [
        {
            "channelId": "C1",
            "channelName": "product",
            "messageTs": "1785981123.123456",
            "permalink": "https://kiwi.slack.com/archives/C1/p1785981123123456",
            "text": "Onboarding decision: keep the two-step flow.",
            "threadTs": None,
            "timestamp": "2026-08-06T01:52:03.123456Z",
            "userId": "U1",
        }
    ]
    assert all(params.get("channel") not in {"C2", "D1"} for _, params in calls)
    assert "secret-token" not in json.dumps(payload)


def test_slack_context_search_requires_query_and_clamps_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(slack_context_tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_WORKSPACE_PROJECT_ID", raising=False)
    assert slack_context_tool.check_project_context_requirements() is False
    assert slack_context_tool.check_slack_context_requirements() is False
    with pytest.raises(ValueError, match="scoped Project Workspace"):
        slack_context_tool.slack_context_search("query")
    WorkspaceContextStore(tmp_path).set(
        "project-1", notion_page_ids=[], slack_channel_ids=["C1"]
    )
    with pytest.raises(ValueError, match="query"):
        slack_context_tool.slack_context_search("   ", project_id="project-1")

    with pytest.raises(ValueError, match="allowlist"):
        slack_context_tool.slack_context_search("query", project_id="project-2")

    monkeypatch.setattr(slack_context_tool, "_slack_token", lambda: "token")
    monkeypatch.setattr(
        slack_context_tool,
        "_slack_api",
        lambda method, params=None: (
            {"ok": True, "url": "https://kiwi.slack.com/"}
            if method == "auth.test"
            else {"ok": True, "channel": {"id": "C1", "name": "product", "is_member": True}}
        ),
    )

    payload = json.loads(
        slack_context_tool.slack_context_search(
            "query", project_id="project-1", limit=999
        )
    )
    assert payload["limit"] == slack_context_tool.MAX_RESULTS
    properties = slack_context_tool.SLACK_CONTEXT_SEARCH_SCHEMA["parameters"]["properties"]
    assert "channel_ids" not in properties
    assert "project_id" not in properties
