import json
import sqlite3

import pytest

import tools.approval as approval
from tools.approval_store import ApprovalStore
from tools.governance import RiskClass, build_tool_call_envelope, infer_risk_class
from tools.registry import ToolRegistry


def _envelope(text="hello", target="discord:#ops", risk="external"):
    return build_tool_call_envelope(
        "send_message", {"target": target, "text": text}, risk_class=risk
    )


def test_risk_inference_and_secret_free_envelope():
    assert infer_risk_class("read_file") is RiskClass.READ
    assert infer_risk_class("terminal") is RiskClass.EXEC
    assert infer_risk_class("delete_project", "mcp-stitch") is RiskClass.DESTRUCTIVE
    assert infer_risk_class("mystery", "third-party") is RiskClass.PRIVILEGED
    env = build_tool_call_envelope(
        "browser_navigate",
        {"url": "https://user:pass@example.test/?token=secret", "api_key": "sk-x"},
        risk_class="external",
    )
    assert "pass" not in env.target
    assert "secret" not in env.target
    assert "sk-x" not in env.args_preview
    assert json.loads(env.args_preview)["api_key"] == "***REDACTED***"


def test_registry_exposes_explicit_and_inferred_governance():
    registry = ToolRegistry()
    schema = lambda name: {"name": name, "description": "x", "parameters": {"type": "object"}}
    handler = lambda args, **kwargs: "ok"
    registry.register(
        name="send_message", toolset="discord", schema=schema("send_message"),
        handler=handler, risk_class="external",
        check_fn=lambda: True,
    )
    entry = registry.get_entry("send_message")
    assert entry is not None
    assert entry.risk_class is RiskClass.EXTERNAL
    assert entry.risk_source == "explicit"
    report = registry.get_connector_report()[0]
    assert report["connector_id"] == "discord"
    assert report["healthy"] is True


def test_durable_exact_approval_is_single_use_and_argument_bound(tmp_path):
    store = ApprovalStore(tmp_path / "state.db", clock=lambda: 1000.0)
    request = store.create_request(
        session_key="cron:daily", envelope=_envelope(), reason="daily",
        pattern_key="daily",
    )
    store.resolve_request(request["id"], "approved")
    consumed = store.consume_matching_approval(
        _envelope(), pattern_key="daily", session_key="cron:daily"
    )
    assert consumed is not None
    assert consumed["status"] == "consumed"
    assert store.consume_matching_approval(
        _envelope(), pattern_key="daily", session_key="cron:daily"
    ) is None
    assert store.consume_matching_approval(
        _envelope("changed"), pattern_key="daily", session_key="cron:daily"
    ) is None


def test_allow_always_resolution_is_atomic_and_bounded(tmp_path):
    store = ApprovalStore(tmp_path / "state.db", clock=lambda: 1000.0)
    request = store.create_request(
        session_key="cron:daily", envelope=_envelope(), reason="daily",
        pattern_key="daily",
    )
    resolved, rule = store.resolve_request_with_standing_rule(
        request["id"], expires_at=2000.0, max_uses=3
    )
    assert resolved["status"] == "approved"
    assert rule["target_pattern"] == "discord:#ops"
    assert rule["max_uses"] == 3
    with pytest.raises(ValueError):
        store.resolve_request_with_standing_rule(
            request["id"], expires_at=2000.0, max_uses=3
        )
    assert len(store.list_standing_rules()) == 1


def test_standing_rule_exact_targets_do_not_become_accidental_globs(tmp_path):
    store = ApprovalStore(tmp_path / "state.db", clock=lambda: 1000.0)
    store.add_standing_rule(
        tool_name="send_message", target_pattern="discord:#ops*",
        risk_ceiling="external", max_uses=2,
    )
    assert store.consume_standing_rule(_envelope(target="discord:#ops-secret")) is None
    assert store.consume_standing_rule(_envelope(target="discord:#ops*")) is not None

    with pytest.raises(ValueError, match="exact"):
        store.add_standing_rule(
            tool_name="send_message", target_pattern="discord:#ops*",
            risk_ceiling="external", max_uses=1, match_mode="glob",
        )


def test_tampered_approval_envelope_cannot_be_resolved(tmp_path):
    db_path = tmp_path / "state.db"
    store = ApprovalStore(db_path, clock=lambda: 1000.0)
    request = store.create_request(
        session_key="cron:daily", envelope=_envelope(), reason="daily",
        pattern_key="daily",
    )
    assert request["envelope_sha256"]
    assert request["integrity_ok"] is True

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE approval_requests SET target=? WHERE id=?",
            ("discord:#attacker", request["id"]),
        )

    with pytest.raises(ValueError, match="integrity"):
        store.resolve_request(request["id"], "approved")
    listed = store.list_requests(status="pending")
    assert listed[0]["integrity_ok"] is False


def test_standing_rule_is_target_risk_scope_and_use_bound(tmp_path):
    now = [1000.0]
    store = ApprovalStore(tmp_path / "state.db", clock=lambda: now[0])
    store.add_standing_rule(
        tool_name="send_message", target_pattern="discord:#ops",
        risk_ceiling="external", profile="default", max_uses=1,
        expires_at=1100.0,
    )
    assert store.consume_standing_rule(_envelope(), profile="default") is not None
    assert store.consume_standing_rule(_envelope(), profile="default") is None
    assert store.consume_standing_rule(_envelope(target="discord:#other"), profile="default") is None


@pytest.fixture
def unattended(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(approval, "get_current_session_key", lambda default="default": "cron:daily")
    monkeypatch.setattr(approval, "is_approved", lambda *args: False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False, raising=False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "env_var_enabled", lambda name: name == "HERMES_CRON_SESSION")
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")


def test_cron_denials_are_blocked_and_left_reviewable(unattended):
    tool = approval.request_tool_approval(
        "send_message", "daily", rule_key="daily",
        args={"target": "discord:#ops", "text": "hello"}, risk_class="external",
    )
    command = approval.check_dangerous_command("rm -rf ./cache", "local")
    assert tool.get("status") != "approval_required"
    assert command.get("status") != "approval_required"
    assert tool["approved"] is command["approved"] is False
    rows = ApprovalStore().list_requests(status="pending")
    assert {(row["tool_name"], row["source"]) for row in rows} == {
        ("send_message", "plugin"), ("terminal", "terminal")
    }
