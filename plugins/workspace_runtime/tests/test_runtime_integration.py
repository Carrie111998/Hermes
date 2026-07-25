"""Load-bearing integration tests for the Workspace Runtime plugin."""

from __future__ import annotations

from pathlib import Path

import pytest
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Lock

import workspace_runtime as plugin
from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from tests.agent.test_api_content_sidecar import _FakeAgent, _build
from workspace_runtime.discovery import DiscoveryVerdict, VerdictState, render_verdict_block


@pytest.fixture(autouse=True)
def _clean_plugin_state():
    with plugin._lock:
        plugin._verdict_by_session.clear()
        plugin._context_by_session.clear()
        plugin._augmented_sessions.clear()
    yield
    with plugin._lock:
        plugin._verdict_by_session.clear()
        plugin._context_by_session.clear()
        plugin._augmented_sessions.clear()


def _manager_with_workspace_runtime(monkeypatch) -> PluginManager:
    manager = PluginManager()
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    manifest = PluginManifest(
        name="workspace_runtime",
        version="0.1.0",
        description="test",
        path=str(Path(plugin.__file__).resolve().parent),
    )
    plugin.register(PluginContext(manifest, manager))
    return manager


def _inside_verdict(root: Path) -> DiscoveryVerdict:
    return DiscoveryVerdict(
        state=VerdictState.INSIDE,
        cwd=root,
        root=root,
        present=("identity", "architecture", "bootstrap_md", "workspace_index"),
        bootstrap_validation="passed",
        questions_answerable=5,
    )


def test_verdict_reaches_api_bound_user_content(monkeypatch, tmp_path: Path):
    """The supported pre_llm_call contract reaches Hermes' API sidecar."""
    _manager_with_workspace_runtime(monkeypatch)
    verdict = _inside_verdict(tmp_path)
    monkeypatch.setattr(plugin, "_build_session_context", render_verdict_block)
    monkeypatch.setattr(plugin._discovery, "discover", lambda: verdict)

    plugin.on_session_start(session_id="sess-1", model="test", platform="cli")
    agent = _FakeAgent()
    agent.session_id = "sess-1"

    ctx = _build(agent)
    api_content = ctx.messages[ctx.current_turn_user_idx]["api_content"]

    assert '<workspace-runtime-verdict' in ctx.plugin_user_context
    assert 'state="inside_workspace"' in api_content
    assert str(tmp_path) in api_content
    assert agent.api_content_at_persist == api_content


def test_sequential_sessions_keep_distinct_verdicts(monkeypatch, tmp_path: Path):
    workspace = _inside_verdict(tmp_path / "workspace")
    bare = DiscoveryVerdict(state=VerdictState.NOT_FOUND, cwd=tmp_path / "bare")
    verdicts = iter((workspace, bare))
    monkeypatch.setattr(plugin, "_build_session_context", render_verdict_block)
    monkeypatch.setattr(plugin._discovery, "discover", lambda: next(verdicts))

    plugin.on_session_start(session_id="session-a")
    plugin.on_session_start(session_id="session-b")

    result_a = plugin.pre_llm_call(
        session_id="session-a", user_message="A", turn_id=0
    )
    result_b = plugin.pre_llm_call(
        session_id="session-b", user_message="B", turn_id=0
    )

    assert result_a is not None
    assert result_b is not None
    assert 'state="inside_workspace"' in result_a["context"]
    assert str(workspace.root) in result_a["context"]
    assert 'state="not_a_workspace"' in result_b["context"]
    assert str(bare.cwd) in result_b["context"]
    assert str(bare.cwd) not in result_a["context"]


def test_repeated_session_start_preserves_initial_verdict(monkeypatch, tmp_path: Path):
    workspace = _inside_verdict(tmp_path / "workspace")
    bare = DiscoveryVerdict(state=VerdictState.NOT_FOUND, cwd=tmp_path / "bare")
    calls = []
    verdicts = iter((workspace, bare))

    def discover_once():
        calls.append(True)
        return next(verdicts)

    monkeypatch.setattr(plugin._discovery, "discover", discover_once)

    plugin.on_session_start(session_id="stable-session")
    plugin.on_session_start(session_id="stable-session")

    assert calls == [True]
    assert plugin._retrieve("stable-session") == workspace


def test_safe_cwd_contains_path_lookup_failure(monkeypatch):
    monkeypatch.setattr(
        plugin.Path,
        "cwd",
        classmethod(lambda cls: (_ for _ in ()).throw(OSError("cwd gone"))),
    )
    assert plugin._safe_cwd() == Path("/")


def test_concurrent_sessions_keep_distinct_verdicts(monkeypatch, tmp_path: Path):
    verdict_a = _inside_verdict(tmp_path / "workspace-a")
    verdict_b = DiscoveryVerdict(
        state=VerdictState.NOT_FOUND, cwd=tmp_path / "bare-b"
    )
    lock = Lock()
    queue = [verdict_a, verdict_b]

    def next_verdict():
        with lock:
            return queue.pop(0)

    monkeypatch.setattr(plugin._discovery, "discover", next_verdict)
    barrier = Barrier(2)

    def start(session_id: str):
        barrier.wait()
        plugin.on_session_start(session_id=session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(start, ("concurrent-a", "concurrent-b")))

    stored = {
        plugin._retrieve("concurrent-a"),
        plugin._retrieve("concurrent-b"),
    }
    assert stored == {verdict_a, verdict_b}
    assert plugin._retrieve("missing-session") is None


def _write_bootstrap_workspace(root: Path, active_sprint: Path) -> None:
    (root / "GOVERNANCE").mkdir(parents=True)
    (root / "CONTEXT").mkdir()
    (root / "IDENTITY.md").write_text(
        "# Engineering Identity\n\n## 1. Core Competency\n\nProduction Engineering.\n",
        encoding="utf-8",
    )
    (root / "ARCHITECTURE.md").write_text(
        "# Architecture\n\n## Subsystem Map\n\nSix subsystems.\n",
        encoding="utf-8",
    )
    (root / "GOVERNANCE" / "BOOTSTRAP.md").write_text(
        "# Bootstrap\n\n## What to ignore by default\n\n- archive/\n",
        encoding="utf-8",
    )
    (root / "GOVERNANCE" / "AUTHORITY-MODEL.md").write_text(
        "# Authority Model\n\nConsumers must not redefine authorities.\n",
        encoding="utf-8",
    )
    (root / "CONTEXT" / "workspace-index.json").write_text(
        json.dumps(
            {
                "active_sprints": [str(active_sprint)],
                "workspace_root": str(root),
            }
        ),
        encoding="utf-8",
    )
    active_sprint.mkdir(parents=True)
    (active_sprint / "source-task.md").write_text(
        "# Source Task\n\nContinue Workspace Runtime stabilization.\n",
        encoding="utf-8",
    )
    (active_sprint / "progress.md").write_text(
        "# Progress\n\nStatus: in progress.\n",
        encoding="utf-8",
    )


def test_inside_context_loads_bootstrap_and_recovers_current_mission(
    monkeypatch, tmp_path: Path
):
    root = tmp_path / "workspace"
    mission = root / ".project-state" / "runtime-remediation"
    _write_bootstrap_workspace(root, mission)
    deep = root / "projects" / "runtime" / "src"
    deep.mkdir(parents=True)
    real_discover = plugin._discovery.discover
    monkeypatch.setattr(plugin._discovery, "discover", lambda: real_discover(deep))

    plugin.on_session_start(session_id="mission-session")
    result = plugin.pre_llm_call(
        session_id="mission-session",
        user_message="Continue Workspace Runtime stabilization.",
        turn_id=0,
    )

    assert result is not None
    context = result["context"]
    assert "Production Engineering." in context
    assert "Six subsystems." in context
    assert "What to ignore by default" in context
    assert '"active_sprints"' in context
    assert f'current_project="{(root / "projects").as_posix()}"' in context
    assert f'current_mission="{mission.as_posix()}"' in context
    assert "Continue Workspace Runtime stabilization." in context
    assert "Status: in progress." in context


def test_canonical_bootstrap_context_reaches_api_without_hook_spill(monkeypatch):
    canonical = Path("/home/taras/projects")
    if not (canonical / "CONTEXT" / "workspace-index.json").exists():
        pytest.skip("canonical workspace unavailable")
    _manager_with_workspace_runtime(monkeypatch)
    real_discover = plugin._discovery.discover
    monkeypatch.setattr(plugin._discovery, "discover", lambda: real_discover(canonical))

    plugin.on_session_start(session_id="canonical-context")
    agent = _FakeAgent()
    agent.session_id = "canonical-context"
    ctx = _build(agent)
    api_content = ctx.messages[ctx.current_turn_user_idx]["api_content"]

    assert "output truncated" not in api_content
    assert len(ctx.plugin_user_context) <= 10_000
    assert "Production Engineering." in api_content
    assert "Six subsystems." in api_content
    assert "What to ignore by default" in api_content
    assert "active_sprints" in api_content


def test_canonical_mission_context_stays_below_hook_spill_threshold():
    mission = Path(
        "/home/taras/projects/.project-state/"
        "workspace-runtime-release-blocker-remediation-2026-07-25"
    )
    if not mission.exists():
        pytest.skip("canonical remediation mission unavailable")
    verdict = plugin._discovery.discover(mission)
    context = plugin._build_session_context(verdict)
    assert len(context) <= 10_000
    assert '<mission-file path="source-task.md">' in context
    assert '<mission-file path="progress.md">' in context
    assert "output truncated" not in context
