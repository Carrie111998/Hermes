import argparse
import json
from pathlib import Path
import sqlite3
import time

import pytest

from hermes_cli import business
from hermes_cli import compliance_db


def _parser():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    business.build_parser(sub)
    return parser


def test_example_charter_grants_bounded_successor_authority():
    charter = json.loads(
        (Path(__file__).parents[2] / "examples" / "agentic-charter.json")
        .read_text(encoding="utf-8")
    )
    assert "objectives.create" in charter["allowed_capabilities"]
    assert "objectives" in charter["allowed_systems"]


def test_provider_verify_exposes_supersession_controls():
    args = _parser().parse_args(
        [
            "business",
            "provider-verify",
            "--provider",
            "rail",
            "--direction",
            "outbound",
            "--jurisdiction",
            "CA",
            "--registry-authority",
            "registry",
            "--registry-reference",
            "ref",
            "--expires-at",
            "9999999999",
            "--evidence",
            "{}",
            "--supersedes-id",
            "assessment_old",
            "--supersession-reason",
            "screening interpretation changed",
        ]
    )
    assert args.supersedes_id == "assessment_old"
    assert args.supersession_reason == "screening interpretation changed"


def test_business_bootstrap_is_noninteractive_and_persists_charter(
    tmp_path, monkeypatch, capsys
):
    charter_path = tmp_path / "charter.json"
    charter = {"enabled": True, "initial_mandate": {"desired_outcome": "revenue"}}
    charter_path.write_text(json.dumps(charter), encoding="utf-8")
    saved = []
    monkeypatch.setattr(
        "hermes_cli.setup._bootstrap_agentic_business",
        lambda value: ("org_bootstrap", "objective_bootstrap"),
    )
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda value, **kwargs: saved.append((value, kwargs)),
    )

    args = _parser().parse_args(
        ["business", "bootstrap", "--charter-file", str(charter_path)]
    )
    assert business.business_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "bootstrapped"
    assert output["organization_id"] == "org_bootstrap"
    assert saved == [({"agentic": charter}, {"merge_existing": True})]


def test_business_bootstrap_rejects_disabled_charter(tmp_path, monkeypatch):
    charter_path = tmp_path / "charter.json"
    charter_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    monkeypatch.setattr(
        "hermes_cli.setup._bootstrap_agentic_business",
        lambda value: pytest.fail("disabled charter must not bootstrap"),
    )
    args = _parser().parse_args(
        ["business", "bootstrap", "--charter-file", str(charter_path)]
    )
    with pytest.raises(ValueError, match="enabled charter"):
        business.business_command(args)


def test_unconfigured_snapshot_exposes_safe_first_run_handoff():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    snapshot = business.build_business_snapshot(conn)
    assert snapshot["configured"] is False
    assert snapshot["next_step"] == {
        "action": "review_and_bootstrap_charter",
        "command": (
            "charterforge business bootstrap "
            "--charter-file examples/agentic-charter.json"
        ),
        "authority": "advisor must review and provide an enabled charter",
        "autonomy_started": False,
    }


def test_payment_rails_is_read_only_and_surfaces_unavailable_provider(
    monkeypatch, capsys
):
    class EntryPoint:
        name = "stripe"

        def load(self):
            raise ValueError("STRIPE_SECRET_KEY is required for the Stripe rail")

    class EntryPoints:
        def select(self, *, group):
            return [EntryPoint()] if group == "charterforge.inbound_payment_rails" else []

    monkeypatch.setattr("hermes_cli.payments.metadata.entry_points", lambda: EntryPoints())
    args = _parser().parse_args(["business", "payment-rails"])
    assert business.business_command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["inbound"] == [{
        "available": False,
        "direction": "inbound",
        "group": "charterforge.inbound_payment_rails",
        "name": "stripe",
        "reason": "ValueError: STRIPE_SECRET_KEY is required for the Stripe rail",
    }]
    assert output["outbound"] == []


def test_business_readiness_is_unconfigured_without_mutation():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    readiness = business.build_business_readiness(conn)
    assert readiness["ready"] is False
    assert readiness["state"] == "unconfigured"
    assert readiness["blockers"] == [{
        "code": "bootstrap_required",
        "summary": "Solo-founder business has not been bootstrapped",
    }]
    assert readiness["next_step"]["autonomy_started"] is False


def test_business_readiness_reports_authoritative_runtime_blockers(monkeypatch):
    monkeypatch.setattr(
        business,
        "build_business_snapshot",
        lambda _conn: {
            "configured": True,
            "organization": {"id": "org-readiness"},
            "autonomy": {"mode": "paused"},
            "runtime_deployment": {
                "ready": False,
                "selected_host": "standalone",
                "expected_roles": ["objective-runtime"],
            },
            "runtime_drift": {"blocked": True, "reason": "manifest mismatch"},
            "interventions": [{
                "id": "int-1", "status": "open", "category": "security",
                "summary": "Isolation is not configured",
            }],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.business_security.evaluate_security_readiness",
        lambda _config: type("Readiness", (), {"ready": True, "violations": ()})(),
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    readiness = business.build_business_readiness(conn)
    assert readiness["ready"] is False
    assert readiness["state"] == "blocked"
    assert readiness["runtime_active"] is False
    assert [item["code"] for item in readiness["blockers"]] == [
        "autonomy_not_enabled",
        "runtime_drift_blocked",
        "advisor_intervention_open",
    ]


def test_business_readiness_blocks_declared_payments_without_ready_rail(monkeypatch):
    monkeypatch.setattr(
        business,
        "build_business_snapshot",
        lambda _conn: {
            "configured": True,
            "organization": {"id": "org-payments"},
            "autonomy": {"mode": "autonomous"},
            "runtime_deployment": {
                "ready": True,
                "selected_host": "standalone",
                "expected_roles": ["objective-runtime"],
            },
            "runtime_drift": {"blocked": False},
            "interventions": [],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agentic": {"allowed_capabilities": ["payments.receive"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.business_security.evaluate_security_readiness",
        lambda _config: type("Readiness", (), {"ready": True, "violations": ()})(),
    )
    monkeypatch.setattr(
        "hermes_cli.payments.payment_rail_status",
        lambda: {"inbound": [], "outbound": []},
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    readiness = business.build_business_readiness(conn)
    assert readiness["ready"] is False
    assert [item["code"] for item in readiness["blockers"]] == [
        "payment_compliance_profile_missing",
        "payment_rail_unavailable",
        "payment_provider_assessment_missing",
    ]


def test_business_readiness_blocks_declared_email_without_agentmail(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "agentic": {
                "allowed_capabilities": ["email.send"],
                "communications": {
                    "email": {
                        "provider": "agentmail",
                        "inbox_id": "ceo@agentmail.to",
                    }
                },
            }
        },
    )
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.business_security.evaluate_security_readiness",
        lambda _config: type("Readiness", (), {"ready": True, "violations": ()})(),
    )
    monkeypatch.setattr(
        business,
        "build_business_snapshot",
        lambda _conn: {
            "configured": True,
            "organization": {"id": "org-email"},
            "autonomy": {"mode": "autonomous"},
            "runtime_deployment": {"ready": True},
            "runtime_drift": {"blocked": False},
            "interventions": [],
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    readiness = business.build_business_readiness(conn)
    blocker = next(
        item for item in readiness["blockers"]
        if item["code"] == "company_email_unavailable"
    )
    assert blocker["inbox_configured"] is True
    assert blocker["api_key_configured"] is False


def test_business_readiness_rejects_assessment_for_different_ready_rail(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.payments.payment_rail_status",
        lambda: {
            "inbound": [{
                "name": "stripe",
                "rail_name": "stripe",
                "available": True,
            }],
            "outbound": [],
        },
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    compliance_db.configure_profile(
        conn,
        organization_id="org-payments",
        legal_entity_type="corporation",
        home_jurisdiction="CA-ON",
    )
    compliance_db.verify_payment_provider(
        conn,
        organization_id="org-payments",
        provider="different-rail",
        direction="inbound",
        jurisdiction="GLOBAL",
        registry_authority="test-registry",
        registry_reference="different-rail-inbound",
        aml_screening_delegated=True,
        sanctions_screening_delegated=True,
        verified_at=int(time.time()) - 1,
        expires_at=int(time.time()) + 3600,
        evidence={"test": True},
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"agentic": {"allowed_capabilities": ["payments.receive"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.business_security.evaluate_security_readiness",
        lambda _config: type("Readiness", (), {"ready": True, "violations": ()})(),
    )
    monkeypatch.setattr(
        business,
        "build_business_snapshot",
        lambda _conn: {
            "configured": True,
            "organization": {"id": "org-payments"},
            "autonomy": {"mode": "autonomous"},
            "runtime_deployment": {"ready": False},
            "runtime_drift": {"blocked": False},
            "interventions": [],
        },
    )
    readiness = business.build_business_readiness(conn)
    assessment_blocker = next(
        item for item in readiness["blockers"]
        if item["code"] == "payment_provider_assessment_missing"
    )
    assert assessment_blocker["available_providers"] == ["stripe"]
    assert assessment_blocker["assessed_providers"] == ["different-rail"]
