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


def _staged_paths(repo: Path) -> set[str]:
    """Paths the index carries for the next commit. Also valid on an unborn HEAD,
    where `diff --cached` compares the index against the empty tree."""
    listing = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return {line for line in listing.stdout.splitlines() if line}


def _repo_before_first_commit(tmp_path: Path) -> Path:
    """A `git init` repo with work already staged and no commit yet, which is what
    the review pane sees on a new project the agent has just written files into."""
    root = tmp_path / "fresh"
    (root / "nested").mkdir(parents=True)
    _git(root, "init", "-q")
    (root / "new.py").write_text("print(1)\n")
    (root / "nested" / "deep.py").write_text("print(2)\n")
    _git(root, "add", "-A")
    return root


def test_unstage_all_before_the_first_commit(client, tmp_path):
    fresh = _repo_before_first_commit(tmp_path)
    assert _staged_paths(fresh) == {"new.py", "nested/deep.py"}

    response = client.post("/api/git/review/unstage", json={"path": str(fresh)})

    assert response.status_code == 200
    assert _staged_paths(fresh) == set()
    # A mixed reset moves the index only; the work itself has to survive.
    assert (fresh / "new.py").read_text() == "print(1)\n"
    assert (fresh / "nested" / "deep.py").read_text() == "print(2)\n"


def test_unstage_one_file_before_the_first_commit(client, tmp_path):
    fresh = _repo_before_first_commit(tmp_path)

    response = client.post("/api/git/review/unstage", json={"path": str(fresh), "file": "new.py"})

    assert response.status_code == 200
    assert _staged_paths(fresh) == {"nested/deep.py"}


def test_unstage_all_covers_the_whole_index(client, repo):
    _git(repo, "add", "-A")
    assert _staged_paths(repo) == {"a.txt", "new.py"}

    assert client.post("/api/git/review/unstage", json={"path": str(repo)}).json() == {"ok": True}

    assert _staged_paths(repo) == set()
    assert (repo / "a.txt").read_text() == "one\ntwo\nthree\n"
