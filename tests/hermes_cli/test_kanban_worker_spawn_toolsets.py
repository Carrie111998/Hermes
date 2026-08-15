from __future__ import annotations

import subprocess

import pytest


def _make_task(kb, *, assignee: str):
    return kb.Task(
        id="t_spawn_tools",
        title="spawn tools",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )


def test_default_spawn_pins_assignee_profile_cli_toolsets(monkeypatch, tmp_path):
    """Manual profile assignment should keep that profile's CLI tools.

    Regression guard for dispatcher-spawned workers that boot with
    HERMES_KANBAN_TASK: the worker must not collapse to only kanban lifecycle
    tools when the assigned profile's top-level ``toolsets`` is the default
    composite. The spawned CLI gets an explicit --toolsets pin resolved from
    platform_toolsets.cli; model_tools appends task-scoped kanban tools later.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - clarify
    - code_execution
    - delegation
    - file
    - memory
    - session_search
    - skills
    - terminal
    - web
toolsets:
  - hermes-cli
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
    assert "HERMES_KANBAN_WORKER_SCOPE" not in captured["env"]
    assert "--accept-hooks" in captured["cmd"]
    assert "--toolsets" in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1].split(",")
    for required in ("terminal", "web", "file", "skills", "code_execution", "delegation"):
        assert required in pinned


def test_default_spawn_model_override_survives_real_cli_parse(monkeypatch, tmp_path):
    """The dispatcher's pre-``chat`` model flag must reach ``args.model``.

    This is an integration contract between Kanban's worker argv builder and
    the real CLI parser. A parser default once erased the explicit override,
    silently sending the worker to its profile default or fallback instead.
    """
    root = tmp_path / ".hermes"
    (root / "profiles" / "elias").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb
    from hermes_cli._parser import build_top_level_parser

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4244

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _make_task(kb, assignee="elias")
    task.model_override = "gpt-5.6-sol"
    kb._default_spawn(task, str(workspace))

    parser, _subparsers, _chat_parser = build_top_level_parser()
    # Profile selection is attached by the outer CLI bootstrap rather than
    # build_top_level_parser(); remove that already-validated prefix and parse
    # the worker flags/subcommand through the real shared parser.
    assert captured["cmd"][1:3] == ["-p", "elias"]
    args = parser.parse_args(captured["cmd"][3:])

    assert args.command == "chat"
    assert args.model == "gpt-5.6-sol"
    assert args.query == "work kanban task t_spawn_tools"


def test_resolve_worker_cli_toolsets_uses_profile_home_not_parent_config(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("platform_toolsets:\n  cli:\n    - kanban\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - terminal
    - web
toolsets:
  - hermes-cli
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved is not None
    assert "terminal" in resolved
    assert "web" in resolved
    assert "kanban" in resolved  # recovered worker lifecycle surface
    assert resolved != ["kanban"]


def test_lifecycle_only_worker_surface_excludes_broader_kanban_tools(monkeypatch):
    """A minimal worker profile must not be widened back to the full Kanban API."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_lifecycle_only")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "lifecycle-only")

    from model_tools import _clear_tool_defs_cache, get_tool_definitions

    _clear_tool_defs_cache()
    try:
        definitions = get_tool_definitions(
            enabled_toolsets=["kanban_lifecycle"],
            disabled_toolsets=[],
            quiet_mode=True,
        )
        by_name = {
            item.get("function", {}).get("name"): item.get("function", {})
            for item in definitions
            if item.get("function", {}).get("name")
        }
        names = set(by_name)
        assert names == {
            "kanban_show",
            "kanban_complete",
            "kanban_block",
            "kanban_heartbeat",
        }
        assert set(by_name["kanban_show"]["parameters"]["properties"]) == set()
        assert set(by_name["kanban_complete"]["parameters"]["properties"]) == {
            "summary", "metadata", "result",
        }
        assert set(by_name["kanban_block"]["parameters"]["properties"]) == {
            "reason", "kind",
        }
        assert set(by_name["kanban_heartbeat"]["parameters"]["properties"]) == {
            "note",
        }
    finally:
        _clear_tool_defs_cache()


def test_resolve_worker_cli_toolsets_preserves_lifecycle_only_profile(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "dashboardcontrol"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - kanban_lifecycle
toolsets:
  - kanban_lifecycle
agent:
  disabled_toolsets: []
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    resolved = kb._resolve_worker_cli_toolsets(str(profile))

    assert resolved == ["kanban_lifecycle"]


def test_resolve_worker_cli_toolsets_fails_closed_on_config_error(
    monkeypatch, tmp_path
):
    profile = tmp_path / "profile"
    profile.mkdir()

    from hermes_cli import kanban_db as kb
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: (_ for _ in ()).throw(ValueError("invalid profile config")),
    )

    with pytest.raises(RuntimeError, match="refusing spawn"):
        kb._resolve_worker_cli_toolsets(str(profile))


def test_default_spawn_does_not_start_process_when_toolset_resolution_fails(
    monkeypatch, tmp_path
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "dashboardcontrol"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb,
        "_resolve_worker_cli_toolsets",
        lambda _home: (_ for _ in ()).throw(RuntimeError("resolution failed")),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("worker process must not start"),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(RuntimeError, match="resolution failed"):
        kb._default_spawn(
            _make_task(kb, assignee="dashboardcontrol"),
            str(workspace),
        )


def test_default_spawn_pins_lifecycle_process_scope_and_suppresses_hooks(
    monkeypatch, tmp_path
):
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "dashboardcontrol"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text(
        """
platform_toolsets:
  cli:
    - kanban_lifecycle
toolsets:
  - kanban_lifecycle
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
    monkeypatch.setenv("HERMES_TUI", "1")
    monkeypatch.setenv("HERMES_TENANT", "stale-parent-tenant")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SCOPE", "inherited-invalid")

    from hermes_cli import kanban_db as kb

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4246

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert kb._default_spawn(
        _make_task(kb, assignee="dashboardcontrol"), str(workspace)
    ) == 4246
    assert captured["env"]["HERMES_KANBAN_WORKER_SCOPE"] == "lifecycle-only"
    assert "HERMES_ACCEPT_HOOKS" not in captured["env"]
    assert "HERMES_TUI" not in captured["env"]
    assert "HERMES_TENANT" not in captured["env"]
    assert "--accept-hooks" not in captured["cmd"]
    pinned = captured["cmd"][captured["cmd"].index("--toolsets") + 1]
    assert pinned == "kanban_lifecycle"
