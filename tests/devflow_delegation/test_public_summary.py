from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

import pytest

from devflow_delegation.public_summary import (
    public_artifact_summary,
    public_decision_summary,
    public_evidence_summary,
    public_lease_summary,
    public_request_summary,
    public_transition_summary,
)


FORBIDDEN_VALUES = {
    "admin-diego-42",
    "confirm-super-secret",
    "ghp_livecredential",
    r"C:\Users\diego\.hermes\scripts\run.py",
    "/home/diego/.hermes/scripts/run.py",
    "implement the hidden prompt body",
    "anthropic/claude-opus-secret",
}
FORBIDDEN_KEYS = {
    "actor",
    "confirmation_token",
    "credential",
    "credentials",
    "envelope_json",
    "holder",
    "model",
    "prompt",
    "provider",
    "script_path",
    "worktree_path",
}


def _walk(value: object):
    yield value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield key
            yield from _walk(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from _walk(child)


def assert_browser_safe(summary: object) -> None:
    values = list(_walk(summary))
    assert not (FORBIDDEN_VALUES & set(v for v in values if isinstance(v, str)))
    assert not (FORBIDDEN_KEYS & set(v for v in values if isinstance(v, str)))


def test_request_summary_uses_an_explicit_safe_allowlist() -> None:
    row = {
        "request_id": "req_public_1",
        "idempotency_key": "critic:safe:v1",
        "fingerprint": "fp-secret-internal",
        "envelope_json": """{
            "title": "Bound the gateway query",
            "problem_statement": "implement the hidden prompt body",
            "priority": "P1",
            "confidence": 0.94,
            "acceptance_criteria": ["Keep latency below three seconds"],
            "credentials": "ghp_livecredential",
            "prompt": "implement the hidden prompt body",
            "provider": "anthropic/claude-opus-secret",
            "script_path": "C:\\\\Users\\\\diego\\\\.hermes\\\\scripts\\\\run.py"
        }""",
        "state": "TRIAGED",
        "terminal_reason": None,
        "source_agent": "critic",
        "source_kind": "finding",
        "target_repo": "hermes",
        "target_subsystem": "gateway-health",
        "kind": "bug",
        "severity": "high",
        "created_at": "2026-08-10T10:00:00+00:00",
        "updated_at": "2026-08-10T10:01:00+00:00",
        "lease_attempt_count": 2,
    }

    summary = public_request_summary(row)

    assert summary == {
        "request_id": "req_public_1",
        "idempotency_key": "critic:safe:v1",
        "state": "TRIAGED",
        "source_agent": "critic",
        "source_kind": "finding",
        "target_repo": "hermes",
        "target_subsystem": "gateway-health",
        "kind": "bug",
        "severity": "high",
        "title": "Bound the gateway query",
        "priority": "P1",
        "created_at": "2026-08-10T10:00:00+00:00",
        "updated_at": "2026-08-10T10:01:00+00:00",
        "lease_attempt_count": 2,
    }
    assert_browser_safe(summary)


def test_allowed_text_fields_drop_embedded_sensitive_material() -> None:
    summary = public_request_summary(
        {
            "request_id": "req_1",
            "state": "FAILED",
            "terminal_reason": r"failed at C:\Users\diego\secret.txt with ghp_livecredential",
            "envelope_json": json.dumps(
                {
                    "title": "implement the hidden prompt body using anthropic/claude-opus-secret",
                    "priority": "P1",
                }
            ),
        }
    )
    evidence = public_evidence_summary(
        {
            "id": 1,
            "request_id": "req_1",
            "evidence_json": json.dumps(
                {
                    "kind": "failure",
                    "summary": r"token ghp_livecredential at C:\Users\diego\secret.txt",
                }
            ),
        }
    )
    combined = json.dumps({"request": summary, "evidence": evidence})
    for forbidden in (
        "ghp_livecredential",
        r"C:\Users\diego\secret.txt",
        "implement the hidden prompt body",
        "anthropic/claude-opus-secret",
    ):
        assert forbidden not in combined


def test_pr_url_rejects_query_fragment_userinfo_and_non_pull_paths() -> None:
    for ref in (
        "https://github.com/acme/hermes/pull/42?token=ghp_livecredential",
        "https://github.com/acme/hermes/pull/42#confirm-super-secret",
        "https://user:secret@github.com/acme/hermes/pull/42",
        "https://github.com/acme/hermes/issues/42",
    ):
        assert public_artifact_summary(
            {
                "id": 12,
                "request_id": "req_1",
                "kind": "pr",
                "ref": ref,
                "created_at": "2026-08-10T10:06:00+00:00",
            }
        ) is None


def test_request_summary_tolerates_invalid_envelope_json() -> None:
    summary = public_request_summary(
        {
            "request_id": "req_1",
            "envelope_json": "not-json",
            "state": "REQUESTED",
        }
    )
    assert summary == {"request_id": "req_1", "state": "REQUESTED"}


@pytest.mark.parametrize(
    ("actor", "actor_class"),
    [
        ("admin-diego-42", "operator"),
        ("devflow-triage", "triage"),
        ("stage2-executor", "executor"),
        ("cron-system", "system"),
    ],
)
def test_transition_summary_classifies_actor_without_exposing_identity(
    actor: str, actor_class: str
) -> None:
    summary = public_transition_summary(
        {
            "id": 7,
            "request_id": "req_1",
            "from_state": "TRIAGED",
            "to_state": "PLANNED",
            "actor": actor,
            "policy_version": "policy-v1",
            "evidence_ref": "approval:sha256:abc123",
            "created_at": "2026-08-10T10:02:00+00:00",
        }
    )
    assert summary["actor_class"] == actor_class
    assert summary["transition_id"] == 7
    assert_browser_safe(summary)


def test_decision_summary_omits_actor_and_confirmation_token() -> None:
    summary = public_decision_summary(
        {
            "id": 9,
            "request_id": "req_1",
            "actor": "admin-diego-42",
            "decision": "approve",
            "evidence_ref": "rationale:bounded-query",
            "confirmation_token": "confirm-super-secret",
            "created_at": "2026-08-10T10:03:00+00:00",
        }
    )
    assert summary == {
        "decision_id": 9,
        "request_id": "req_1",
        "actor_class": "operator",
        "decision": "approve",
        "evidence_ref": "rationale:bounded-query",
        "created_at": "2026-08-10T10:03:00+00:00",
    }
    assert_browser_safe(summary)


def test_evidence_summary_parses_only_safe_public_fields() -> None:
    summary = public_evidence_summary(
        {
            "id": 11,
            "request_id": "req_1",
            "evidence_json": """{
                "kind": "test_failure",
                "summary": "Gateway query exceeded three seconds",
                "ref": "test:gateway-health",
                "credential": "ghp_livecredential",
                "prompt": "implement the hidden prompt body",
                "model": "anthropic/claude-opus-secret"
            }""",
            "created_at": "2026-08-10T10:04:00+00:00",
        }
    )
    assert summary == {
        "evidence_id": 11,
        "request_id": "req_1",
        "kind": "test_failure",
        "summary": "Gateway query exceeded three seconds",
        "ref": "test:gateway-health",
        "created_at": "2026-08-10T10:04:00+00:00",
    }
    assert_browser_safe(summary)


def test_lease_summary_preserves_public_branch_but_not_holder_or_local_path() -> None:
    summary = public_lease_summary(
        {
            "request_id": "req_1",
            "lease_id": "lse_internal",
            "holder": "admin-diego-42",
            "acquired_at": "2026-08-10T10:05:00+00:00",
            "expires_at": "2026-08-10T10:06:00+00:00",
            "heartbeat_at": "2026-08-10T10:05:30+00:00",
            "worktree_path": r"C:\Users\diego\.hermes\scripts\run.py",
            "branch": "devflow/req-public-1",
            "attempt_count": 3,
        }
    )
    assert summary == {
        "request_id": "req_1",
        "acquired_at": "2026-08-10T10:05:00+00:00",
        "expires_at": "2026-08-10T10:06:00+00:00",
        "heartbeat_at": "2026-08-10T10:05:30+00:00",
        "branch": "devflow/req-public-1",
        "attempt_count": 3,
    }
    assert_browser_safe(summary)


@pytest.mark.parametrize(
    ("kind", "ref"),
    [
        ("worktree", r"C:\Users\diego\.hermes\worktrees\req_1"),
        ("script", "/home/diego/.hermes/scripts/run.py"),
        ("validation", r"C:\Users\diego\report.txt"),
    ],
)
def test_artifact_summary_drops_local_filesystem_artifacts(kind: str, ref: str) -> None:
    assert public_artifact_summary(
        {
            "id": 12,
            "request_id": "req_1",
            "kind": kind,
            "ref": ref,
            "created_at": "2026-08-10T10:06:00+00:00",
        }
    ) is None


@pytest.mark.parametrize(
    ("kind", "ref", "public_field", "public_value"),
    [
        ("branch", "devflow/req-public-1", "branch", "devflow/req-public-1"),
        ("pr", "https://github.com/acme/hermes/pull/42", "pr_url", "https://github.com/acme/hermes/pull/42"),
        ("pr_number", "42", "pr_number", 42),
    ],
)
def test_artifact_summary_preserves_safe_branch_and_pr_metadata(
    kind: str, ref: str, public_field: str, public_value: object
) -> None:
    summary = public_artifact_summary(
        {
            "id": 12,
            "request_id": "req_1",
            "kind": kind,
            "ref": ref,
            "created_at": "2026-08-10T10:06:00+00:00",
        }
    )
    assert summary is not None
    assert summary[public_field] == public_value
    assert_browser_safe(summary)
