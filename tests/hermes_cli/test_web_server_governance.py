from fastapi.testclient import TestClient

from tools.approval_store import ApprovalStore
from tools.governance import build_tool_call_envelope


def _client():
    from hermes_cli import web_server

    client = TestClient(web_server.app, base_url="http://127.0.0.1")
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


def _request():
    return ApprovalStore().create_request(
        session_key="cron:daily",
        envelope=build_tool_call_envelope(
            "send_message", {"target": "discord:#ops", "text": "daily"},
            risk_class="external",
        ),
        reason="daily report",
        pattern_key="daily",
    )


def test_governance_inbox_approve_once(_isolate_hermes_home):
    request = _request()
    client = _client()
    listed = client.get("/api/governance/approvals")
    assert listed.status_code == 200
    assert listed.json()["approvals"][0]["id"] == request["id"]
    resolved = client.post(
        f"/api/governance/approvals/{request['id']}/decision",
        json={"decision": "allow-once"},
    )
    assert resolved.status_code == 200
    stored = ApprovalStore().get_request(request["id"])
    assert stored is not None
    assert stored["status"] == "approved"


def test_allow_for_target_is_atomic_bounded_and_secret_free(_isolate_hermes_home):
    request = _request()
    client = _client()
    path = f"/api/governance/approvals/{request['id']}/decision"
    first = client.post(path, json={"decision": "allow-always"})
    second = client.post(path, json={"decision": "allow-always"})
    assert first.status_code == 200
    assert second.status_code == 409
    rules = ApprovalStore().list_standing_rules()
    assert len(rules) == 1
    assert rules[0]["target_pattern"] == "discord:#ops"
    assert rules[0]["max_uses"] == 100
    assert rules[0]["expires_at"] is not None


def test_connector_inventory_does_not_expose_config_secrets(_isolate_hermes_home, monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "load_config", lambda: {
        "mcp_servers": {"github": {"url": "https://mcp.test", "headers": {"token": "secret"}}}
    })
    response = _client().get("/api/governance/connectors")
    assert response.status_code == 200
    payload = response.json()
    assert any(row["id"] == "mcp-github" for row in payload["connectors"])
    assert "secret" not in response.text
