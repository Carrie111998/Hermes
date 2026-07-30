"""Codex app-server must obey the same Hermes request-phase ceiling."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.codex_runtime import (
    _codex_phase_contract,
    _select_codex_turn_workspace,
)
from agent.request_phase import (
    activate_turn_policy,
    clear_turn_policy,
)
from agent.transports.codex_app_server_session import (
    CodexAppServerSession,
    _ServerRequestRouting,
)


QUOTE_ANALYSIS_REQUEST = "analyze existing quote skills/process and work downward."


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


@pytest.fixture(autouse=True)
def _reset_phase():
    clear_turn_policy()
    yield
    clear_turn_policy()


def _auto_approve_session(repo):
    return CodexAppServerSession(
        cwd=str(repo),
        request_routing=_ServerRequestRouting(
            auto_approve_exec=True,
            auto_approve_apply_patch=True,
        ),
    )


def test_exact_quote_investigation_declines_codex_exec_and_patch(clean_repo):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    session = _auto_approve_session(clean_repo)

    assert session._decide_exec_approval(
        {
            "command": f"Set-Content {clean_repo / 'app.py'} VALUE=2",
            "cwd": str(clean_repo),
        }
    ) == "decline"
    assert session._decide_apply_patch_approval(
        {"grantRoot": str(clean_repo)}
    ) == "decline"


def test_dirty_repo_declines_codex_auto_approval_for_implementation(clean_repo):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    activate_turn_policy(
        "Implement the requested source fix.",
        cwd=clean_repo,
    )
    session = _auto_approve_session(clean_repo)

    assert session._decide_apply_patch_approval(
        {"grantRoot": str(clean_repo)}
    ) == "decline"


def test_dirty_implementation_is_automatically_isolated(clean_repo, tmp_path):
    (clean_repo / "app.py").write_text("PREEXISTING = True\n", encoding="utf-8")
    agent = SimpleNamespace()

    cwd, instructions, enforce_read_only = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=True,
        isolation_root=tmp_path / "isolated",
    )

    isolated = Path(cwd)
    assert isolated != clean_repo
    assert isolated.is_dir()
    assert enforce_read_only is False
    assert "created a clean detached Codex worktree automatically" in instructions
    assert _git(isolated, "status", "--porcelain").stdout == ""
    assert (isolated / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == (
        "PREEXISTING = True\n"
    )

    reused_cwd, _, reused_read_only = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=True,
        isolation_root=tmp_path / "isolated",
    )
    assert reused_cwd == cwd
    assert reused_read_only is False
    _git(clean_repo, "worktree", "remove", "--force", str(isolated))


def test_clean_source_always_gets_a_dedicated_codex_lane(
    clean_repo,
    tmp_path,
):
    agent = SimpleNamespace()

    cwd, _, enforce_read_only = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=False,
        isolation_root=tmp_path / "isolated",
    )
    isolated = Path(cwd)
    assert isolated != clean_repo
    assert isolated.is_dir()
    assert agent._codex_isolated_workspace_owned is True

    # An unrelated actor may dirty the original checkout after selection.
    # That must never cause Codex to claim or reuse the shared source lane.
    (clean_repo / "app.py").write_text("CODEX_OWNED = True\n", encoding="utf-8")
    (isolated / "app.py").write_text("CODEX_LANE = True\n", encoding="utf-8")
    reused_cwd, instructions, reused_read_only = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=True,
        isolation_root=tmp_path / "isolated",
    )

    assert reused_cwd == cwd
    assert reused_cwd != str(clean_repo)
    assert enforce_read_only is False
    assert reused_read_only is False
    assert "existing owner-isolated Codex worktree" in instructions
    assert (isolated / "app.py").read_text(encoding="utf-8") == (
        "CODEX_LANE = True\n"
    )
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == (
        "CODEX_OWNED = True\n"
    )
    _git(clean_repo, "worktree", "remove", "--force", str(isolated))


def test_clean_explicit_implementation_keeps_codex_native_power(clean_repo):
    activate_turn_policy(
        "Implement the requested source fix.",
        cwd=clean_repo,
    )
    session = _auto_approve_session(clean_repo)

    assert session._decide_exec_approval(
        {
            "command": f"Set-Content {clean_repo / 'app.py'} VALUE=2",
            "cwd": str(clean_repo),
        }
    ) == "accept"
    assert session._decide_apply_patch_approval(
        {"grantRoot": str(clean_repo)}
    ) == "accept"


def test_codex_receives_enforced_read_only_phase_and_minimal_skill_guidance(
    clean_repo,
):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    phase, instructions, enforce_read_only = _codex_phase_contract()

    assert phase == "investigation"
    assert enforce_read_only is True
    assert "smallest directly governing skill set" in instructions

    captured = {}

    class FakeClient:
        def initialize(self, **_kwargs):
            return {}

        def notify(self, *_args, **_kwargs):
            return None

        def request(self, method, params, **_kwargs):
            captured[method] = params
            return {"thread": {"id": "thread-1"}}

        def close(self):
            return None

    session = CodexAppServerSession(
        cwd=str(clean_repo),
        request_phase=phase,
        developer_instructions=instructions,
        enforce_read_only=enforce_read_only,
        client_factory=lambda **_kwargs: FakeClient(),
    )
    session.ensure_started()

    params = captured["thread/start"]
    assert params["sandbox"] == "read-only"
    assert params["approvalPolicy"] == "never"
    assert "phase=investigation" in params["developerInstructions"]


def test_clean_implementation_codex_thread_keeps_native_permissions(clean_repo):
    activate_turn_policy(
        "Ship it live.",
        cwd=clean_repo,
        prior_context=[
            {
                "role": "assistant",
                "content": (
                    "The repository implementation and regression tests are "
                    "ready in the clean worktree."
                ),
            }
        ],
    )
    phase, instructions, enforce_read_only = _codex_phase_contract()

    assert phase == "implementation"
    assert enforce_read_only is False

    captured = {}

    class FakeClient:
        def initialize(self, **_kwargs):
            return {}

        def notify(self, *_args, **_kwargs):
            return None

        def request(self, method, params, **_kwargs):
            captured[method] = params
            return {"thread": {"id": "thread-implementation"}}

        def close(self):
            return None

    session = CodexAppServerSession(
        cwd=str(clean_repo),
        request_phase=phase,
        developer_instructions=instructions,
        enforce_read_only=enforce_read_only,
        client_factory=lambda **_kwargs: FakeClient(),
    )
    session.ensure_started()

    params = captured["thread/start"]
    assert params["sandbox"] == "workspace-write"
    assert params["approvalPolicy"] == "never"
