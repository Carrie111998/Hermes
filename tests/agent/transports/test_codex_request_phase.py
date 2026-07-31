"""Codex app-server must obey the same Hermes request-phase ceiling."""

import json
import os
import subprocess
import sys
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

    reused_cwd, reused_instructions, reused_read_only = (
        _select_codex_turn_workspace(
            agent,
            source_cwd=str(clean_repo),
            phase_name="implementation",
            phase_instructions="phase=implementation.",
            enforce_read_only=True,
            isolation_root=tmp_path / "isolated",
        )
    )
    assert reused_cwd == cwd
    assert reused_read_only is False
    assert "freshly revalidated clean" in reused_instructions
    _git(clean_repo, "worktree", "remove", "--force", str(isolated))


def test_cached_clean_implementation_lane_is_revalidated_and_reused(
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
    reused_cwd, instructions, reused_read_only = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=False,
        isolation_root=tmp_path / "isolated",
    )

    assert enforce_read_only is False
    assert reused_cwd == cwd
    assert reused_read_only is False
    assert "freshly revalidated clean" in instructions
    assert _git(reused_cwd, "status", "--porcelain").stdout == ""
    _git(clean_repo, "worktree", "remove", "--force", str(reused_cwd))


def test_cached_clean_lane_is_replaced_after_source_clean_commit(
    clean_repo,
    tmp_path,
):
    agent = SimpleNamespace()
    first_cwd, _, _ = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=False,
        isolation_root=tmp_path / "isolated",
    )
    first_lane = Path(first_cwd)

    (clean_repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(clean_repo, "add", "app.py")
    _git(clean_repo, "commit", "-q", "-m", "source advanced")
    assert _git(clean_repo, "status", "--porcelain").stdout == ""

    replacement_cwd, instructions, replacement_read_only = (
        _select_codex_turn_workspace(
            agent,
            source_cwd=str(clean_repo),
            phase_name="implementation",
            phase_instructions="phase=implementation.",
            enforce_read_only=False,
            isolation_root=tmp_path / "isolated",
        )
    )
    replacement = Path(replacement_cwd)

    assert replacement != first_lane
    assert replacement_read_only is False
    assert "source repository committed identity advanced" in instructions
    assert first_lane.is_dir()
    assert (first_lane / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (replacement / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    _git(clean_repo, "worktree", "remove", "--force", str(replacement))
    _git(clean_repo, "worktree", "remove", "--force", str(first_lane))


def test_cached_clean_lane_is_replaced_after_its_own_clean_commit(
    clean_repo,
    tmp_path,
):
    agent = SimpleNamespace()
    first_cwd, _, _ = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=False,
        isolation_root=tmp_path / "isolated",
    )
    first_lane = Path(first_cwd)
    _git(first_lane, "config", "user.name", "Hermes Test")
    _git(first_lane, "config", "user.email", "hermes-test@example.invalid")
    (first_lane / "app.py").write_text("STALE = True\n", encoding="utf-8")
    _git(first_lane, "add", "app.py")
    _git(first_lane, "commit", "-q", "-m", "stale lane commit")
    assert _git(first_lane, "status", "--porcelain").stdout == ""

    replacement_cwd, instructions, replacement_read_only = (
        _select_codex_turn_workspace(
            agent,
            source_cwd=str(clean_repo),
            phase_name="implementation",
            phase_instructions="phase=implementation.",
            enforce_read_only=False,
            isolation_root=tmp_path / "isolated",
        )
    )
    replacement = Path(replacement_cwd)

    assert replacement != first_lane
    assert replacement_read_only is False
    assert "cached worktree committed identity changed" in instructions
    assert first_lane.is_dir()
    assert (first_lane / "app.py").read_text(encoding="utf-8") == (
        "STALE = True\n"
    )
    assert (replacement / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    _git(clean_repo, "worktree", "remove", "--force", str(replacement))
    _git(clean_repo, "worktree", "remove", "--force", str(first_lane))


def test_cached_dirty_implementation_lane_is_preserved_and_replaced(
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
    replacement_cwd, instructions, replacement_read_only = (
        _select_codex_turn_workspace(
            agent,
            source_cwd=str(clean_repo),
            phase_name="implementation",
            phase_instructions="phase=implementation.",
            enforce_read_only=True,
            isolation_root=tmp_path / "isolated",
        )
    )
    replacement = Path(replacement_cwd)

    assert replacement != isolated
    assert replacement != clean_repo
    assert replacement_read_only is False
    assert agent._codex_isolated_workspace == str(replacement)
    assert "prior cached Codex worktree" in instructions
    assert "was not deleted or overwritten" in instructions
    assert _git(replacement, "status", "--porcelain").stdout == ""
    assert (replacement / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert isolated.is_dir()
    assert (isolated / "app.py").read_text(encoding="utf-8") == (
        "CODEX_LANE = True\n"
    )
    assert (clean_repo / "app.py").read_text(encoding="utf-8") == (
        "CODEX_OWNED = True\n"
    )

    _git(clean_repo, "worktree", "remove", "--force", str(replacement))
    _git(clean_repo, "worktree", "remove", "--force", str(isolated))


def test_cached_dirty_lane_replacement_failure_is_read_only_and_preserves_work(
    clean_repo,
    tmp_path,
    monkeypatch,
):
    agent = SimpleNamespace()
    cwd, _, _ = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=False,
        isolation_root=tmp_path / "isolated",
    )
    isolated = Path(cwd)
    (isolated / "app.py").write_text("PARTIAL = True\n", encoding="utf-8")

    def fail_replacement(*_args, **_kwargs):
        raise RuntimeError("injected replacement failure")

    monkeypatch.setattr(
        "agent.codex_runtime._create_codex_isolated_worktree",
        fail_replacement,
    )
    selected_cwd, instructions, enforce_read_only = _select_codex_turn_workspace(
        agent,
        source_cwd=str(clean_repo),
        phase_name="implementation",
        phase_instructions="phase=implementation.",
        enforce_read_only=False,
        isolation_root=tmp_path / "isolated",
    )

    assert selected_cwd == str(clean_repo)
    assert enforce_read_only is True
    assert str(isolated) in instructions
    assert "injected replacement failure" in instructions
    assert "Remain read-only" in instructions
    assert (isolated / "app.py").read_text(encoding="utf-8") == (
        "PARTIAL = True\n"
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
    tmp_path,
):
    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)

    phase, instructions, enforce_read_only = _codex_phase_contract()

    assert phase == "investigation"
    assert enforce_read_only is True
    assert "smallest directly governing skill set" in instructions

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """
[mcp_servers.hermes-tools]
command = "python"

[mcp_servers."external.writer"]
command = "external-writer"

[plugins."external-plugin@marketplace"]
enabled = true
""".strip()
        + "\n",
        encoding="utf-8",
    )
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

    def client_factory(**kwargs):
        captured["client_kwargs"] = kwargs
        return FakeClient()

    session = CodexAppServerSession(
        cwd=str(clean_repo),
        codex_home=str(codex_home),
        request_phase=phase,
        developer_instructions=instructions,
        enforce_read_only=enforce_read_only,
        client_factory=client_factory,
    )
    session.ensure_started()

    params = captured["thread/start"]
    assert params["sandbox"] == "read-only"
    assert params["approvalPolicy"] == "never"
    assert "phase=investigation" in params["developerInstructions"]
    extra_args = captured["client_kwargs"]["extra_args"]
    assert "features.apps=false" in extra_args
    assert "features.plugins=false" in extra_args
    assert 'mcp_servers."external.writer".enabled=false' in extra_args
    assert not any("hermes-tools" in arg for arg in extra_args)


def test_real_codex_subprocess_never_launches_external_mcp_in_investigation(
    clean_repo,
    tmp_path,
):
    """Exercise session -> process args -> fake external MCP launch end to end."""

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    marker = tmp_path / "external-mcp-launched.txt"
    external_server = tmp_path / "fake_external_mcp.py"
    external_server.write_text(
        (
            "from pathlib import Path\n"
            "import sys\n"
            "Path(sys.argv[1]).write_text('launched', encoding='utf-8')\n"
            "for _line in sys.stdin:\n"
            "    pass\n"
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        (
            '[mcp_servers."external.writer"]\n'
            f"command = {json.dumps(sys.executable)}\n"
            f"args = {json.dumps([str(external_server), str(marker)])}\n"
        ),
        encoding="utf-8",
    )

    fake_codex_script = tmp_path / "fake_codex_app_server.py"
    fake_codex_script.write_text(
        """
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib

overrides = sys.argv[1:]
disabled = (
    'mcp_servers."external.writer".enabled=false' in overrides
)
config = tomllib.loads(
    (Path(os.environ["CODEX_HOME"]) / "config.toml").read_text(
        encoding="utf-8"
    )
)
entry = config["mcp_servers"]["external.writer"]
child = None

def maybe_start_external():
    global child
    if disabled or child is not None:
        return
    child = subprocess.Popen(
        [entry["command"], *entry.get("args", [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = Path(entry["args"][-1])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.01)

try:
    for raw in sys.stdin:
        request = json.loads(raw)
        request_id = request.get("id")
        method = request.get("method")
        if request_id is None:
            continue
        if method == "initialize":
            result = {
                "userAgent": "fake-codex",
                "codexHome": os.environ["CODEX_HOME"],
                "platformOs": sys.platform,
                "platformFamily": "test",
            }
        elif method == "thread/start":
            maybe_start_external()
            result = {"thread": {"id": "fake-thread"}}
        else:
            result = {}
        sys.stdout.write(
            json.dumps({"id": request_id, "result": result}) + "\\n"
        )
        sys.stdout.flush()
finally:
    if child is not None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
""".lstrip(),
        encoding="utf-8",
    )
    if os.name == "nt":
        fake_codex = tmp_path / "fake-codex.cmd"
        fake_codex.write_text(
            (
                "@echo off\r\n"
                f'"{sys.executable}" "{fake_codex_script}" %*\r\n'
            ),
            encoding="utf-8",
        )
    else:
        fake_codex = tmp_path / "fake-codex"
        fake_codex.write_text(
            (
                "#!/bin/sh\n"
                f'exec {json.dumps(sys.executable)} '
                f'{json.dumps(str(fake_codex_script))} "$@"\n'
            ),
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)

    activate_turn_policy(QUOTE_ANALYSIS_REQUEST, cwd=clean_repo)
    investigation = CodexAppServerSession(
        cwd=str(clean_repo),
        codex_bin=str(fake_codex),
        codex_home=str(codex_home),
        request_phase="investigation",
        enforce_read_only=True,
    )
    try:
        investigation.ensure_started()
    finally:
        investigation.close()
    assert not marker.exists()

    activate_turn_policy(
        "Move the exact reversible internal record.",
        cwd=clean_repo,
    )
    operation = CodexAppServerSession(
        cwd=str(clean_repo),
        codex_bin=str(fake_codex),
        codex_home=str(codex_home),
        request_phase="operation",
        enforce_read_only=False,
    )
    try:
        operation.ensure_started()
        assert marker.read_text(encoding="utf-8") == "launched"
    finally:
        operation.close()


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


def test_operation_codex_thread_keeps_native_external_tools(clean_repo):
    captured = {}

    class FakeClient:
        def initialize(self, **_kwargs):
            return {}

        def notify(self, *_args, **_kwargs):
            return None

        def request(self, method, params, **_kwargs):
            captured[method] = params
            return {"thread": {"id": "thread-operation"}}

        def close(self):
            return None

    def client_factory(**kwargs):
        captured["client_kwargs"] = kwargs
        return FakeClient()

    session = CodexAppServerSession(
        cwd=str(clean_repo),
        request_phase="operation",
        developer_instructions="phase=operation.",
        enforce_read_only=True,
        client_factory=client_factory,
    )
    session.ensure_started()

    assert "extra_args" not in captured["client_kwargs"]
