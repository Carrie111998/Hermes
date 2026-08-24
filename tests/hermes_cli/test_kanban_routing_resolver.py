"""Unit tests for claim-time routing source resolution."""

import sqlite3

import hermes_cli.kanban_db as kb
import pytest
from hermes_cli.routing_contract import RoutingContractError


def _row(**overrides: object) -> sqlite3.Row:
    """Build a task-shaped SQLite row with optional routing overrides."""
    values = {
        "body": "",
        "routing_role": None,
        "model_override": None,
        "provider_override": None,
        "assignee": "coder",
    }
    values.update(overrides)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT ? AS body, ? AS routing_role, ? AS model_override, "
        "? AS provider_override, ? AS assignee",
        tuple(values[key] for key in values),
    ).fetchone()


def _roster() -> tuple[dict, str]:
    """Return a deterministic semantic-role roster and digest."""
    return ({
        "roles": {
            "executor": {
                "model": "exec-model",
                "provider": "exec-provider",
                "invocation": "auto",
                "may_edit": True,
                "review_capable": False,
            },
            "reviewer": {
                "model": "review-model", "provider": "review-provider",
                "invocation": "auto", "may_edit": False,
                "review_capable": True,
            },
            "auditor": {
                "model": "audit-model", "provider": "audit-provider",
                "invocation": "auto", "may_edit": False,
                "review_capable": True,
            },
        }
    }, "roster-digest")


def test_resolve_routing_snapshot_uses_task_role(monkeypatch) -> None:
    """A task semantic role must win when no enforced envelope exists."""
    monkeypatch.setattr(kb, "_load_roster", _roster)

    snapshot = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "task", _row(routing_role="executor"), "implement"
    )

    assert snapshot["routing_source"] == "task_role"
    assert snapshot["routing_role"] == "executor"
    assert snapshot["routing_model"] == "exec-model"
    assert snapshot["routing_provider"] == "exec-provider"
    assert snapshot["roster_digest"] == "roster-digest"


def test_resolve_routing_snapshot_uses_task_override(monkeypatch) -> None:
    """A raw task model/provider pin must freeze no semantic role or digest."""
    monkeypatch.setattr(kb, "_load_roster", _roster)

    snapshot = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"),
        "task",
        _row(model_override="raw-model", provider_override="raw-provider"),
        "implement",
    )

    assert snapshot["routing_source"] == "task_override"
    assert snapshot["routing_model"] == "raw-model"
    assert snapshot["routing_provider"] == "raw-provider"
    assert snapshot["routing_role"] is None
    assert snapshot["routing_contract"] is None
    assert snapshot["roster_digest"] is None


def _defaults(monkeypatch, board=None, profiles=None) -> None:
    """Install deterministic board and profile routing sources."""
    monkeypatch.setattr(kb, "read_board_metadata", lambda *args: board or {})
    values = profiles or {}
    monkeypatch.setattr(
        kb, "_load_profile_model_config",
        lambda profile: values.get(profile, (None, None)),
    )


def test_enforced_envelope_has_highest_precedence(monkeypatch) -> None:
    """An enforced envelope must override every lower source."""
    monkeypatch.setattr(kb, "_load_roster", _roster)
    _defaults(monkeypatch, {"default_role": "reviewer"})
    body = """Task
# ---routing:v1---
role: executor
action: implement
enforcement_required: true
reason: exact route
# ---/routing:v1---
"""
    snapshot = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "task",
        _row(body=body, routing_role="reviewer", model_override="pin",
             provider_override="pin-p"), "implement",
    )
    assert snapshot["routing_source"] == "envelope"
    assert snapshot["routing_role"] == "executor"
    assert snapshot["routing_contract"] == 1


def test_precedence_task_role_then_override_then_board(monkeypatch) -> None:
    """Task role, raw override, and board default resolve in strict order."""
    monkeypatch.setattr(kb, "_load_roster", _roster)
    _defaults(monkeypatch, {"default_role": "reviewer"})
    role = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t",
        _row(routing_role="executor", model_override="pin",
             provider_override="pin-p"), "implement",
    )
    raw = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t",
        _row(model_override="pin", provider_override="pin-p"), "implement",
    )
    board = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t", _row(), "implement",
    )
    assert (role["routing_source"], raw["routing_source"],
            board["routing_source"]) == (
        "task_role", "task_override", "board_default"
    )


def test_profile_default_is_last_and_raw_fields_are_null(monkeypatch) -> None:
    """A profile default is raw and therefore carries no semantic metadata."""
    _defaults(monkeypatch, profiles={"coder": ("p-model", "p-provider")})
    snapshot = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t", _row(), "implement"
    )
    assert snapshot["routing_source"] == "profile_default"
    for field in ("routing_role", "roster_digest", "routing_policy",
                  "routing_contract"):
        assert snapshot[field] is None


@pytest.mark.parametrize("provider", ["", 7])
def test_invalid_present_provider_fails_closed(monkeypatch, provider) -> None:
    """An invalid present provider must not fall through."""
    _defaults(monkeypatch)
    with pytest.raises(RoutingContractError):
        kb._resolve_routing_snapshot(
            sqlite3.connect(":memory:"), "t",
            _row(model_override="pin", provider_override=provider), "implement",
        )


def test_provider_only_fails_closed(monkeypatch) -> None:
    """A provider override without a model is invalid."""
    _defaults(monkeypatch)
    with pytest.raises(RoutingContractError, match="requires model"):
        kb._resolve_routing_snapshot(
            sqlite3.connect(":memory:"), "t",
            _row(provider_override="provider"), "implement",
        )


def test_model_only_provider_fallback_order(monkeypatch) -> None:
    """Model-only routes prefer assignee provider then default profile."""
    _defaults(monkeypatch, profiles={"coder": (None, "coder-p")})
    monkeypatch.setattr(kb, "_load_global_default_provider", lambda: "default-p")
    assignee = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t", _row(model_override="pin"), "implement"
    )
    _defaults(monkeypatch, profiles={})
    fallback = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t", _row(model_override="pin"), "implement"
    )
    assert assignee["routing_provider"] == "coder-p"
    assert fallback["routing_provider"] == "default-p"


@pytest.mark.parametrize("role", ["", "missing", 7])
def test_invalid_present_task_role_fails_closed(monkeypatch, role) -> None:
    """A malformed present semantic role must not fall through."""
    monkeypatch.setattr(kb, "_load_roster", _roster)
    _defaults(monkeypatch, profiles={"coder": ("p-model", "p-provider")})
    with pytest.raises(RoutingContractError):
        kb._resolve_routing_snapshot(
            sqlite3.connect(":memory:"), "t", _row(routing_role=role), "implement"
        )


def test_invalid_board_and_partial_profile_fail_closed(monkeypatch) -> None:
    """Invalid present fallback sources must reject rather than skip."""
    monkeypatch.setattr(kb, "_load_roster", _roster)
    _defaults(monkeypatch, {"default_role": "missing"},
              {"coder": ("p-model", "p-provider")})
    with pytest.raises(RoutingContractError):
        kb._resolve_routing_snapshot(sqlite3.connect(":memory:"), "t", _row(), "implement")
    _defaults(monkeypatch, profiles={"coder": ("p-model", None)})
    with pytest.raises(RoutingContractError):
        kb._resolve_routing_snapshot(sqlite3.connect(":memory:"), "t", _row(), "implement")


def test_unresolved_route_is_rejected(monkeypatch) -> None:
    """Resolution rejects when no configured source yields a route."""
    _defaults(monkeypatch)
    with pytest.raises(RoutingContractError, match="unresolved"):
        kb._resolve_routing_snapshot(sqlite3.connect(":memory:"), "t", _row(), "implement")


def test_review_capable_and_coerced_sources(monkeypatch) -> None:
    """Review routing records whether the requested role was preserved."""
    monkeypatch.setattr(kb, "_load_roster", _roster)
    _defaults(monkeypatch)
    capable = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t", _row(routing_role="auditor"), "review"
    )
    coerced = kb._resolve_routing_snapshot(
        sqlite3.connect(":memory:"), "t", _row(routing_role="executor"), "review"
    )
    assert (capable["routing_role"], capable["routing_source"]) == (
        "auditor", "review_capable"
    )
    assert (coerced["routing_role"], coerced["routing_source"]) == (
        "reviewer", "review_coerced"
    )
    assert "role 'auditor'" in capable["routing_reason"]
    assert "role 'executor'" in coerced["routing_reason"]