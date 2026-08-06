import json

import pytest

from hermes_cli.workspace_approval_store import WorkspaceApprovalStore


REQUEST = {
    "changeSetDigest": "digest",
    "commitSha": "abc123",
    "createdAt": "2026-08-06T03:00:00Z",
    "destinationBranch": "main",
    "expiresAt": "2099-08-06T03:10:00Z",
    "remote": "origin",
    "remoteUrl": "https://github.com/acme/repo.git",
    "remoteUrlDigest": "url-digest",
    "requestId": "request-1",
}


def test_public_approval_store_never_persists_paths_or_private_url(tmp_path):
    store = WorkspaceApprovalStore(tmp_path / "approvals.db")
    request = {
        **REQUEST,
        "cwd": "/secret/repo",
        "effectivePushUrl": "https://token@github.com/acme/repo.git",
    }

    store.publish(request, binding_id="binding-1", project_id="project-1")
    pending = store.list_pending()

    assert len(pending) == 1
    serialized = json.dumps(pending)
    assert "/secret/repo" not in serialized
    assert "token@" not in serialized
    assert pending[0]["request"] == REQUEST
    store.close()


def test_decision_is_single_use_and_expiry_checked(tmp_path):
    store = WorkspaceApprovalStore(tmp_path / "approvals.db")
    store.publish(REQUEST, binding_id="binding-1", project_id="project-1")

    decision = store.decide(
        "request-1",
        approved=True,
        approved_by="user-1",
        now=1_800_000_000,
    )

    assert decision["approved"] is True
    assert decision["commitSha"] == REQUEST["commitSha"]
    assert decision["remoteUrlDigest"] == REQUEST["remoteUrlDigest"]
    with pytest.raises(ValueError, match="already decided"):
        store.decide(
            "request-1",
            approved=True,
            approved_by="user-1",
            now=1_800_000_001,
        )
    store.close()


def test_expired_request_cannot_be_approved(tmp_path):
    store = WorkspaceApprovalStore(tmp_path / "approvals.db")
    store.publish(
        {**REQUEST, "expiresAt": "2020-01-01T00:00:00Z", "requestId": "expired"},
        binding_id="binding-1",
        project_id="project-1",
    )

    with pytest.raises(ValueError, match="expired"):
        store.decide(
            "expired",
            approved=True,
            approved_by="user-1",
            now=1_800_000_000,
        )
    store.close()
