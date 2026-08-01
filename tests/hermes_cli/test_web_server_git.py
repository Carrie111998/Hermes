import subprocess
from pathlib import Path

import pytest

from hermes_cli import web_git, web_server

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


def _review_paths(client, repo: Path) -> set[str]:
    """Paths the review pane still lists — i.e. what git really reports as changed."""
    body = client.get("/api/git/review/list", params={"path": str(repo)}).json()
    return {file["path"] for file in body["files"]}


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


def test_revert_removes_a_staged_new_file(client, repo):
    # Staging is one click away from reverting in the review pane, and a staged
    # new file is exactly what `checkout HEAD` (not in HEAD) and `clean` (tracked
    # in the index) both refuse to touch.
    _git(repo, "add", "new.py")

    assert client.post(
        "/api/git/review/revert", json={"path": str(repo), "file": "new.py"}
    ).json() == {"ok": True}

    assert not (repo / "new.py").exists()
    # Scoped: the unrelated tracked edit is left alone.
    assert _review_paths(client, repo) == {"a.txt"}


def test_revert_removes_a_plain_untracked_file(client, repo):
    # `checkout HEAD` legitimately fails here (the path is not in HEAD); `clean`
    # is the whole job, so that failure must not abort the revert.
    assert client.post(
        "/api/git/review/revert", json={"path": str(repo), "file": "new.py"}
    ).json() == {"ok": True}

    assert not (repo / "new.py").exists()
    assert _review_paths(client, repo) == {"a.txt"}


def test_revert_restores_a_staged_modification(client, repo):
    _git(repo, "add", "a.txt")

    assert client.post(
        "/api/git/review/revert", json={"path": str(repo), "file": "a.txt"}
    ).json() == {"ok": True}

    assert (repo / "a.txt").read_text() == "one\ntwo\n"
    assert _review_paths(client, repo) == {"new.py"}


def test_revert_restores_a_staged_deletion(client, repo):
    _git(repo, "rm", "-q", "-f", "a.txt")

    assert client.post(
        "/api/git/review/revert", json={"path": str(repo), "file": "a.txt"}
    ).json() == {"ok": True}

    assert (repo / "a.txt").read_text() == "one\ntwo\n"
    assert _review_paths(client, repo) == {"new.py"}


def test_revert_all_clears_staged_unstaged_and_untracked(client, repo):
    (repo / "staged-new.txt").write_text("staged\n")
    _git(repo, "add", "staged-new.txt")

    assert client.post(
        "/api/git/review/revert", json={"path": str(repo), "file": None}
    ).json() == {"ok": True}

    assert (repo / "a.txt").read_text() == "one\ntwo\n"
    assert not (repo / "new.py").exists()
    assert not (repo / "staged-new.txt").exists()
    assert _review_paths(client, repo) == set()


def test_revert_removes_new_files_before_the_first_commit(client, tmp_path):
    # An unborn HEAD has nothing to restore, but the new files still have to go.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q")
    (fresh / "staged.txt").write_text("staged\n")
    (fresh / "loose.txt").write_text("loose\n")
    _git(fresh, "add", "staged.txt")

    assert client.post(
        "/api/git/review/revert", json={"path": str(fresh), "file": None}
    ).json() == {"ok": True}

    assert not (fresh / "staged.txt").exists()
    assert not (fresh / "loose.txt").exists()


def test_revert_all_does_not_require_head_during_reset(client, tmp_path, monkeypatch):
    """Newer Git rejects an explicit HEAD in an unborn repo; bare reset does not."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git(fresh, "init", "-q")
    (fresh / "staged.txt").write_text("staged\n")
    (fresh / "loose.txt").write_text("loose\n")
    _git(fresh, "add", "staged.txt")

    real_git = web_git._git

    def reject_unborn_head_reset(cwd, args, **kwargs):
        if args[:3] == ["reset", "-q", "HEAD"]:
            return 128, "", "fatal: ambiguous argument 'HEAD'"
        return real_git(cwd, args, **kwargs)

    monkeypatch.setattr(web_git, "_git", reject_unborn_head_reset)

    response = client.post(
        "/api/git/review/revert", json={"path": str(fresh), "file": None}
    )

    assert response.json() == {"ok": True}
    assert not (fresh / "staged.txt").exists()
    assert not (fresh / "loose.txt").exists()


def test_revert_surfaces_ls_tree_failure_when_head_exists(client, repo, monkeypatch):
    real_git = web_git._git

    def fail_ls_tree(cwd, args, **kwargs):
        if args and args[0] == "ls-tree":
            return 1, "", "injected ls-tree failure"
        return real_git(cwd, args, **kwargs)

    monkeypatch.setattr(web_git, "_git", fail_ls_tree)

    response = client.post(
        "/api/git/review/revert", json={"path": str(repo), "file": "a.txt"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "injected ls-tree failure"


def test_revert_reports_a_git_failure_instead_of_ok(client, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "note.txt").write_text("keep me\n")

    response = client.post("/api/git/review/revert", json={"path": str(plain), "file": "note.txt"})

    assert response.status_code == 400
    assert (plain / "note.txt").read_text() == "keep me\n"


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
