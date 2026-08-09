"""Tests for bounded, approval-generated grants.

Generated grants are intentionally separate from legacy command_allowlist
entries, which remain durable explicit operator policy.
"""

from __future__ import annotations

import threading

import pytest

from tools import approval as A


@pytest.fixture(autouse=True)
def isolated_approval_state(monkeypatch):
    with A._lock:
        A._session_approved.clear()
        A._generated_permanent_grants.clear()
        A._permanent_approved.clear()
        A._gateway_queues.clear()
        A._gateway_notify_cbs.clear()
    monkeypatch.setattr(A, "_approval_time", lambda: 10_000.0)
    yield
    with A._lock:
        A._session_approved.clear()
        A._generated_permanent_grants.clear()
        A._permanent_approved.clear()
        A._gateway_queues.clear()
        A._gateway_notify_cbs.clear()


def test_session_grant_expires_on_read(monkeypatch):
    A.approve_session("session", "recursive delete")
    assert A.is_approved("session", "recursive delete") is True

    monkeypatch.setattr(A, "_approval_time", lambda: 13_601.0)
    assert A.is_approved("session", "recursive delete") is False
    assert "session" not in A._session_approved


def test_generated_permanent_grant_expires_on_read(monkeypatch):
    monkeypatch.setattr(A, "save_permanent_allowlist", lambda: True)
    A.approve_permanent("recursive delete")
    assert A.is_approved("session", "recursive delete") is True

    monkeypatch.setattr(A, "_approval_time", lambda: 96_401.0)
    assert A.is_approved("session", "recursive delete") is False
    assert A._generated_permanent_grants == {}


def test_tenth_use_is_permitted_and_eleventh_is_denied():
    A.approve_session("session", "recursive delete")

    assert [A.is_approved("session", "recursive delete") for _ in range(10)] == [True] * 10
    assert A.is_approved("session", "recursive delete") is False


def test_permanent_grant_fails_closed_if_the_updated_count_cannot_persist(monkeypatch):
    A.approve_permanent("recursive delete")
    monkeypatch.setattr(A, "save_permanent_allowlist", lambda: False)

    assert A.is_approved("session", "recursive delete") is False
    assert A._generated_permanent_grants == {}


def test_final_use_is_consumed_atomically_under_concurrency():
    A.approve_session("session", "recursive delete")
    with A._lock:
        A._session_approved["session"]["recursive delete"].remaining_uses = 1

    barrier = threading.Barrier(3)
    results: list[bool] = []

    def consume() -> None:
        barrier.wait()
        results.append(A.is_approved("session", "recursive delete"))

    threads = [threading.Thread(target=consume), threading.Thread(target=consume)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results) == [False, True]


def test_legacy_command_allowlist_literals_and_globs_remain_unchanged(monkeypatch):
    config = {"command_allowlist": ["git status", "podman *"]}
    saved: list[dict] = []
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: dict(config))
    monkeypatch.setattr("hermes_cli.config.save_config", lambda value: saved.append(value))

    assert A.load_permanent_allowlist() == {"git status", "podman *"}
    A.approve_permanent("recursive delete")
    A.save_permanent_allowlist()

    assert config["command_allowlist"] == ["git status", "podman *"]
    assert saved[-1]["command_allowlist"] == ["git status", "podman *"]
    assert saved[-1]["approval_grants"][0]["key"] == "recursive delete"


def test_malformed_or_unknown_generated_grants_fail_closed(monkeypatch):
    config = {
        "command_allowlist": ["podman *"],
        "approval_grants": [
            {"version": 99, "kind": "pattern", "key": "recursive delete", "granted_at": 1, "expires_at": 99_999, "remaining_uses": 10},
            {"version": 1, "kind": "unknown", "key": "recursive delete", "granted_at": 1, "expires_at": 99_999, "remaining_uses": 10},
            {"version": 1, "kind": "pattern", "key": "recursive delete", "granted_at": "bad", "expires_at": 99_999, "remaining_uses": 10},
        ],
    }
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)

    A.load_permanent_allowlist()

    assert A._command_matches_permanent_allowlist("podman ps") is True
    assert A.is_approved("session", "recursive delete") is False


def test_malformed_in_memory_generated_grant_fails_closed():
    with A._lock:
        A._session_approved["session"] = {  # type: ignore[assignment]
            "recursive delete": {"remaining_uses": 10},
        }

    assert A.is_approved("session", "recursive delete") is False
    assert "session" not in A._session_approved


def test_disallowed_scope_resolves_as_deny():
    entry = A._ApprovalEntry({"allow_session": False, "allow_permanent": False})
    with A._lock:
        A._gateway_queues["session"] = [entry]

    assert A.resolve_gateway_approval("session", "always") == 1
    assert entry.result == "deny"
    assert entry.event.is_set()


def test_boundary_cleanup_removes_generated_session_grants():
    A.approve_session("session", "execute_code")
    A.clear_session("session")

    assert A.is_approved("session", "execute_code") is False


def test_execute_code_session_grant_is_bounded(monkeypatch):
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(A, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    token = A.set_current_session_key("session")
    try:
        A.approve_session("session", "execute_code")
        assert [A.check_execute_code_guard("print(1)", "local")["approved"] for _ in range(10)] == [True] * 10
        assert A.check_execute_code_guard("print(1)", "local")["approved"] is False
    finally:
        A.reset_current_session_key(token)
