import subprocess
from pathlib import Path

import pytest

from hermes_cli import web_server

pytest.importorskip("starlette.testclient")
from starlette.testclient import TestClient


@pytest.fixture
def client():
    previous = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False
    test_client = TestClient(web_server.app)
    test_client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    try:
        yield test_client
    finally:
        if previous is None:
            try:
                delattr(web_server.app.state, "auth_required")
            except AttributeError:
                pass
        else:
            web_server.app.state.auth_required = previous


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _git_ref_exists(repo: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref], cwd=repo, check=False
    ).returncode == 0


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("one\ntwo\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    # A tracked modification + a brand-new untracked file (the new-file case the
    # rail/review must surface).
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    (root / "new.py").write_text("print(1)\nprint(2)\n")
    return root


@pytest.fixture
def push_repo(tmp_path):
    root = tmp_path / "push-repo"
    remote = tmp_path / "remote.git"
    root.mkdir()
    remote.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("initial\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    _git(root, "branch", "-M", "main")
    _git(remote, "init", "--bare", "-q")
    _git(root, "remote", "add", "origin", str(remote))
    _git(root, "push", "-qu", "origin", "main")
    (root / "a.txt").write_text("approved\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "approved")

    return root, remote










def test_stage_commit_roundtrip_clears_changes(client, repo):
    assert client.post("/api/git/review/stage", json={"path": str(repo), "file": "a.txt"}).json() == {"ok": True}
    staged = client.get("/api/git/status", params={"path": str(repo)}).json()
    assert staged["staged"] >= 1

    assert client.post(
        "/api/git/review/commit", json={"path": str(repo), "message": "tracked change", "push": False}
    ).json() == {"ok": True}

    after = client.get("/api/git/status", params={"path": str(repo)}).json()
    # The tracked change is committed; only the untracked file remains.
    assert after["changed"] == 1
    assert after["untracked"] == 1


def test_direct_push_endpoint_cannot_bypass_approval(client, push_repo):
    root, remote = push_repo
    remote_before = _git_out(remote, "rev-parse", "refs/heads/main")

    response = client.post("/api/git/review/push", json={"path": str(root)})

    assert response.status_code == 400
    assert "approval" in response.json()["detail"].lower()
    assert _git_out(remote, "rev-parse", "refs/heads/main") == remote_before


def test_push_request_approval_roundtrip_is_single_use(client, push_repo):
    root, remote = push_repo
    request_response = client.post("/api/git/review/push-request", json={"path": str(root)})

    assert request_response.status_code == 200
    request = request_response.json()
    assert request["commitSha"] == _git_out(root, "rev-parse", "HEAD")
    assert len(request["changeSetDigest"]) == 64

    decision = {
        **request,
        "approved": True,
        "approvedBy": "remote-user",
        "decidedAt": "2026-08-06T00:00:00.000Z",
    }
    approved = client.post(
        "/api/git/review/push-approved", json={"path": str(root), "decision": decision}
    )

    assert approved.status_code == 200
    assert approved.json()["commitSha"] == request["commitSha"]
    assert _git_out(remote, "rev-parse", "refs/heads/main") == request["commitSha"]

    replay = client.post(
        "/api/git/review/push-approved", json={"path": str(root), "decision": decision}
    )
    assert replay.status_code == 400


@pytest.mark.parametrize("remote_setting", ["url", "pushurl"])
def test_push_approval_rejects_destination_substitution(
    client, push_repo, tmp_path, remote_setting
):
    root, approved_remote = push_repo
    substituted_remote = tmp_path / f"substituted-{remote_setting}.git"
    substituted_remote.mkdir()
    _git(substituted_remote, "init", "--bare", "-q")
    approved_before = _git_out(approved_remote, "rev-parse", "refs/heads/main")
    request = client.post(
        "/api/git/review/push-request", json={"path": str(root)}
    ).json()
    args = ["remote", "set-url"]
    if remote_setting == "pushurl":
        args.append("--push")
    _git(root, *args, "origin", str(substituted_remote))

    response = client.post(
        "/api/git/review/push-approved",
        json={
            "path": str(root),
            "decision": {
                **request,
                "approved": True,
                "approvedBy": "remote-user",
                "decidedAt": "2026-08-06T00:00:00.000Z",
            },
        },
    )

    assert response.status_code == 400
    assert _git_out(approved_remote, "rev-parse", "refs/heads/main") == approved_before
    assert not _git_ref_exists(substituted_remote, "refs/heads/main")






def test_worktree_add_initializes_plain_folder(client, tmp_path):
    folder = tmp_path / "plain-project"
    folder.mkdir()
    (folder / "notes.txt").write_text("not committed\n")

    added = client.post(
        "/api/git/worktree/add", json={"path": str(folder), "branch": "feature/plain"}
    ).json()

    assert added["branch"] == "feature/plain"
    assert Path(added["path"]).is_dir()
    assert (folder / ".git").exists()
    _git(folder, "rev-parse", "--verify", "HEAD")

    status = client.get("/api/git/status", params={"path": str(folder)}).json()
    assert status["branch"] == status["defaultBranch"]
    assert status["branch"]
    # Existing files are not silently committed by repo initialization.
    assert any(file["path"] == "notes.txt" and file["untracked"] for file in status["files"])




def test_git_endpoints_require_auth(repo):
    unauth = TestClient(web_server.app)

    assert unauth.get("/api/git/status", params={"path": str(repo)}).status_code == 401
    assert unauth.post("/api/git/review/stage", json={"path": str(repo)}).status_code == 401
