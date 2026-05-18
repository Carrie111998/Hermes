"""Tests for the copilot_remote semantic delegation tool."""

import json

import pytest

from copilot_remote.models import RepoEntry
from hermes_state import SessionDB
from tools.copilot_remote_tool import COPILOT_REMOTE_SCHEMA, copilot_remote


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / ".hermes" / "state.db"
    db_path.parent.mkdir(parents=True)
    test_db = SessionDB(db_path=db_path)
    real_close = test_db.close
    test_db.close = lambda: None
    monkeypatch.setattr("tools.copilot_remote_tool._get_db", lambda: test_db)
    yield test_db
    real_close()


def test_launch_explicit_repo_dry_run(db):
    result = json.loads(
        copilot_remote(
            {
                "action": "launch",
                "prompt": "Build a static webpage about the Macy Conferences",
                "repo": "repo-name",
                "repo_path": "/workspace/repos/corp_it/repo-name",
                "dry_run": True,
            },
            task_id="slack-session-1",
        )
    )

    assert result["success"] is True
    assert result["action"] == "launch"
    assert result["job"]["repo"] == "repo-name"
    assert result["job"]["state"] == "done"
    # Dry-run never spawns the Copilot subprocess, so the launcher cannot
    # extract a real reconnect handle. The tool must NOT fabricate one
    # from the Hermes job UUID — the launcher does not pass that into
    # Copilot via --resume, so it would be a non-functional command.
    assert result["job"]["connect_handle"] is None
    assert result["job"]["connect_command"] is None
    assert result["job"]["resume_command"] is None
    # Without a connect handle there is no web_url.
    assert result["job"]["web_url"] is None

    jobs = db.list_copilot_remote(state="done")
    assert len(jobs) == 1
    assert jobs[0]["repo_slug"] == "repo-name"


def test_launch_routes_repo_and_stores_connect_handle(db, monkeypatch):
    routed_repo = RepoEntry(
        slug="repo-name",
        path="/workspace/repos/corp_it/repo-name",
    )
    monkeypatch.setattr("tools.copilot_remote_tool._route_repo", lambda prompt: routed_repo)

    def fake_launch(repo, prompt, *, session_id, model=None, dry_run=False, on_complete=None):
        assert repo.slug == "repo-name"
        assert "new static webpage" in prompt
        assert dry_run is False
        return {
            "session_id": session_id,
            "connect_id": "task-123",
            "cmd": ["copilot"],
            "proc": None,
            "prompt_delivery_status": "already-submitted",
            "prompt_delivery_warning": None,
        }

    monkeypatch.setattr("copilot_remote.launcher.launch_copilot", fake_launch)

    result = json.loads(
        copilot_remote(
            {
                "action": "launch",
                "prompt": "Please build a new static webpage for the Macy Conferences",
            },
            task_id="slack-session-2",
        )
    )

    assert result["success"] is True
    assert result["job"]["repo"] == "repo-name"
    assert result["job"]["connect_handle"] == "task-123"
    assert result["job"]["connect_command"] == "copilot --connect=task-123"
    # repo_path is not a real git clone in the test environment, so the shared
    # GitHub task URL helper cannot derive an origin-backed web_url.
    assert result["job"]["web_url"] is None

    jobs = db.list_copilot_remote(state="running")
    assert len(jobs) == 1
    # Post-v12: the launcher-discovered connect handle lives in its own
    # column; `signal_ref` is reserved for caller-supplied metadata.
    assert jobs[0]["connect_handle"] == "task-123"


def test_launch_routes_repo_with_web_url(db, monkeypatch):
    """When the repo path is a real git clone and connect handle exists, web_url should be present."""
    routed_repo = RepoEntry(
        slug="repo-name",
        path="/workspace/repos/corp_it/repo-name",
    )
    monkeypatch.setattr("tools.copilot_remote_tool._route_repo", lambda prompt: routed_repo)
    monkeypatch.setattr(
        "tools.copilot_remote_tool.build_github_task_web_url",
        lambda repo_path, repo_slug, connect_handle: (
            f"https://github.com/RosenblattAI/{repo_slug}/tasks/{connect_handle}"
        ),
    )

    def fake_launch(repo, prompt, *, session_id, model=None, dry_run=False, on_complete=None):
        return {
            "session_id": session_id,
            "connect_id": "task-456",
            "cmd": ["copilot"],
            "proc": None,
            "prompt_delivery_status": "already-submitted",
            "prompt_delivery_warning": None,
        }

    monkeypatch.setattr("copilot_remote.launcher.launch_copilot", fake_launch)

    result = json.loads(
        copilot_remote(
            {
                "action": "launch",
                "prompt": "Please build a new static webpage for the Macy Conferences",
            },
            task_id="slack-session-3",
        )
    )

    assert result["success"] is True
    assert result["job"]["connect_handle"] == "task-456"
    assert result["job"]["web_url"] == (
        "https://github.com/RosenblattAI/repo-name/tasks/task-456"
    )


def test_launch_requires_prompt(db):
    result = json.loads(copilot_remote({"action": "launch", "repo": "repo-name"}))

    assert result["success"] is False
    assert "prompt is required" in result["error"]


def test_list_and_show(db):
    db.create_copilot_remote(
        job_id="job-1",
        repo_slug="repo-name",
        repo_path="/workspace/repos/corp_it/repo-name",
        prompt="Build page",
        connect_handle="task-1",
    )

    listing = json.loads(copilot_remote({"action": "list"}))
    assert listing["success"] is True
    assert listing["jobs"][0]["job_id"] == "job-1"

    shown = json.loads(copilot_remote({"action": "show", "job_id": "job-1"}))
    assert shown["success"] is True
    assert shown["job"]["resume_command"] == "copilot --resume=task-1"
    # repo_path is not a real git clone in the test environment.
    assert shown["job"]["web_url"] is None


def test_list_skips_web_url_lookup(db, monkeypatch):
    db.create_copilot_remote(
        job_id="job-2",
        repo_slug="repo-name",
        repo_path="/workspace/repos/corp_it/repo-name",
        prompt="Build page",
        connect_handle="task-2",
    )

    def _unexpected_web_url(*args, **kwargs):
        raise AssertionError("list should not compute web_url")

    monkeypatch.setattr(
        "tools.copilot_remote_tool.build_github_task_web_url",
        _unexpected_web_url,
    )

    listing = json.loads(copilot_remote({"action": "list"}))

    assert listing["success"] is True
    assert listing["jobs"][0]["job_id"] == "job-2"
    assert listing["jobs"][0]["web_url"] is None


def test_hermes_slack_toolset_exposes_copilot_remote():
    from toolsets import resolve_toolset

    assert "copilot_remote" in resolve_toolset("hermes-slack")


def test_schema_discourages_terminal_copilot_probes():
    description = COPILOT_REMOTE_SCHEMA["description"]

    assert "terminal Copilot probes" in description
    assert "call this tool directly" in description


def test_schema_marks_copilot_remote_as_default_implementation_tool():
    description = COPILOT_REMOTE_SCHEMA["description"]

    assert "default implementation tool" in description
    assert "code-writing" in description
    assert "website-building" in description


# =========================================================================
# Path-based routing (deterministic, runs before LLM router)
# =========================================================================


class TestResolveRepoFromPathsInPrompt:
    """Covers _resolve_repo_from_paths_in_prompt + _find_git_root."""

    @pytest.fixture()
    def workspace(self, tmp_path, monkeypatch):
        """Workspace with a monorepo root and a submodule under repos/."""
        (tmp_path / ".git").mkdir()                          # workspace root is a git repo
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "projects").mkdir()
        sub = tmp_path / "repos" / "demos" / "remix-of-nothy-demo"
        sub.mkdir(parents=True)
        (sub / ".git").mkdir()                               # submodule is its own git repo
        (sub / "src").mkdir()
        monkeypatch.setenv("HERMES_WORKSPACE_PATH", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path.parent))
        return tmp_path

    def _resolve(self, prompt):
        from tools.copilot_remote_tool import _resolve_repo_from_paths_in_prompt
        return _resolve_repo_from_paths_in_prompt(prompt)

    def test_absolute_path_under_submodule_routes_to_submodule(self, workspace):
        sub = workspace / "repos" / "demos" / "remix-of-nothy-demo" / "src"
        result = self._resolve(f"create {sub}/foo.ts")
        assert result is not None
        assert result.slug == "remix-of-nothy-demo"
        assert result.path == str(sub.parent)

    def test_absolute_path_under_monorepo_routes_to_workspace_root(self, workspace):
        result = self._resolve(f"please update {workspace}/docs/projects/foo.md")
        assert result is not None
        assert result.slug == workspace.name
        assert result.path == str(workspace)

    def test_dot_slash_anchors_to_workspace_root(self, workspace):
        result = self._resolve("create a test file in ./docs/notes.md")
        assert result is not None
        assert result.slug == workspace.name
        assert result.path == str(workspace)

    def test_dot_slash_bare_routes_to_workspace_root(self, workspace):
        # './' alone matches the workspace root (a git repo).
        result = self._resolve("Please create a test file in ./foo.txt")
        assert result is not None
        assert result.slug == workspace.name

    def test_tilde_path_expands_via_home(self, workspace, monkeypatch):
        # Set HOME so ~/<workspace-name>/docs lands inside the workspace.
        monkeypatch.setenv("HOME", str(workspace.parent))
        result = self._resolve(f"add ~/{workspace.name}/docs/x.md please")
        assert result is not None
        assert result.slug == workspace.name

    def test_bare_repos_path_routes_to_submodule(self, workspace):
        result = self._resolve("Touch repos/demos/remix-of-nothy-demo/src/x.ts")
        assert result is not None
        assert result.slug == "remix-of-nothy-demo"

    def test_no_path_token_returns_none(self, workspace):
        result = self._resolve("Please make the website nicer")
        assert result is None

    def test_path_outside_any_git_repo_returns_none(self, workspace, tmp_path):
        # /tmp (or sibling) has no .git ancestor — not routable.
        outside = tmp_path.parent / "definitely-not-a-repo-xyz"
        result = self._resolve(f"write to {outside}/foo.md")
        assert result is None

    def test_path_outside_workspace_returns_none(self, workspace, tmp_path):
        # An absolute path that is a git repo but lives outside HERMES_WORKSPACE_PATH
        # must not be returned — it bypasses the workspace safety boundary.
        outside_repo = tmp_path.parent / "foreign-repo"
        outside_repo.mkdir(exist_ok=True)
        (outside_repo / ".git").mkdir(exist_ok=True)
        result = self._resolve(f"create {outside_repo}/foo.md")
        assert result is None

    def test_nonexistent_file_still_walks_to_git_root(self, workspace):
        # User describing a file to create — parent walk must still find .git.
        ghost = workspace / "docs" / "projects" / "nothy" / "NEW_FILE.md"
        result = self._resolve(f"create {ghost}")
        assert result is not None
        assert result.slug == workspace.name

    def test_git_as_file_submodule_is_routed(self, workspace):
        # In a real git submodule .git is a *file* (gitlink), not a directory.
        # _find_git_root uses Path.exists() so both forms must be detected.
        gitlink_sub = workspace / "repos" / "demos" / "gitlink-sub"
        gitlink_sub.mkdir(parents=True)
        # Write a gitlink file (mimics what `git submodule` creates).
        (gitlink_sub / ".git").write_text("gitdir: ../../.git/modules/gitlink-sub\n")
        result = self._resolve(f"edit {gitlink_sub}/README.md")
        assert result is not None
        assert result.slug == "gitlink-sub"
        assert result.path == str(gitlink_sub)

    def test_quoted_path_double_quotes(self, workspace):
        result = self._resolve(f'create "{workspace}/docs/foo.md"')
        assert result is not None
        assert result.slug == workspace.name

    def test_quoted_path_single_quotes(self, workspace):
        result = self._resolve(f"create '{workspace}/docs/foo.md'")
        assert result is not None
        assert result.slug == workspace.name

    def test_backtick_path(self, workspace):
        result = self._resolve(f"create `{workspace}/docs/foo.md`")
        assert result is not None
        assert result.slug == workspace.name

    def test_parenthesized_path(self, workspace):
        result = self._resolve(f"create ({workspace}/docs/foo.md)")
        assert result is not None
        assert result.slug == workspace.name


class TestResolveRepoIntegratesPathRouting:
    """End-to-end: _resolve_repo prefers path routing over the LLM router."""

    def test_path_in_prompt_short_circuits_llm_router(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("HERMES_WORKSPACE_PATH", str(tmp_path))

        def _llm_should_not_be_called(prompt):  # pragma: no cover - defensive
            raise AssertionError("LLM router must not be called when a path resolves")

        monkeypatch.setattr(
            "tools.copilot_remote_tool._route_repo", _llm_should_not_be_called
        )
        from tools.copilot_remote_tool import _resolve_repo
        entry, err = _resolve_repo(
            prompt=f"create {tmp_path}/docs/x.md", repo="", repo_path=""
        )
        assert err is None
        assert entry is not None
        assert entry.path == str(tmp_path)
        assert entry.slug == tmp_path.name
