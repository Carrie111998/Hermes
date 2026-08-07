from __future__ import annotations

import subprocess


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
    process = kb._default_spawn(_make_task(kb, assignee="elias"), str(workspace))

    assert process.pid == 4242
    assert captured["env"]["HERMES_HOME"] == str(profile)
    assert captured["env"]["HERMES_KANBAN_TASK"] == "t_spawn_tools"
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


def test_supervised_attempt_one_precreates_kanban_session_and_preserves_ownership(
    monkeypatch, tmp_path
):
    """Attempt one durably owns a hidden Kanban session selected by --resume."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "elias"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    board_db = tmp_path / "kanban.db"
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db))

    from hermes_cli import kanban_db as kb
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
    from hermes_state import SessionDB

    kb.init_db(board_db)
    conn = kb.connect(board_db)
    try:
        task_id = kb.create_task(
            conn,
            title="bound worker",
            assignee="elias",
            workspace_kind="worktree",
            workspace_path=str(workspace),
            initial_status="running",
        )
        task = kb.claim_task(conn, task_id)
        assert task is not None
    finally:
        conn.close()

    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    class FakeSupervisor:
        def allocate_event_path(self, identity, attempt):
            captured["identity"] = identity
            assert attempt == 1
            return tmp_path / "attempt-1.jsonl"

        def start(self, identity, launch, **kwargs):
            event_path = self.allocate_event_path(identity, 1)
            proc = launch(
                identity, 1, event_path, start_nonce="test-nonce",
            )
            assert isinstance(proc, FakeProc)
            captured["proc"] = proc
            return proc

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(kb, "_dispatcher_worker_supervisor", lambda **kwargs: FakeSupervisor())

    assert kb._start_supervised_worker(task, str(workspace)) == FakeProc.pid
    session_id = captured["identity"].session_id
    assert captured["identity"].task_id == task_id
    assert captured["identity"].run_id == task.current_run_id
    assert captured["env"]["HERMES_WORKER_SESSION_ID"] == session_id
    assert captured["cmd"].count("--resume") == 1
    assert captured["cmd"][captured["cmd"].index("--resume") + 1] == session_id

    parser, _subparsers, _chat_parser = build_top_level_parser()
    assert captured["cmd"][1:3] == ["-p", "elias"]
    parsed = parser.parse_args(captured["cmd"][3:])
    assert parsed.resume == session_id

    session_db = SessionDB(profile / "state.db")
    try:
        stored = session_db.get_session(session_id)
        assert stored["source"] == "kanban"
        assert stored["profile_name"] == "elias"
        assert stored["cwd"] == str(workspace.resolve())
        assert stored["git_repo_root"] == str(workspace.resolve())

        # The resumed worker creates the same row when its CLI startup runs.
        # SessionDB conflict handling intentionally preserves the first source.
        session_db.create_session(session_id, source="kanban")
        assert session_db.get_session(session_id)["source"] == "kanban"

        output = []
        probe = CLIAgentSetupMixin()
        probe._resumed = True
        probe._session_db = session_db
        probe.session_id = parsed.resume
        probe.conversation_history = []
        probe._console_print = output.append
        probe._restore_session_cwd = lambda _meta: None
        assert probe._preload_resumed_session() is False
        assert any("found but has no messages" in line for line in output)
        assert not any("Session not found" in line for line in output)
    finally:
        session_db.close()

    conn = kb.connect(board_db)
    try:
        projected = kb.get_task(conn, task_id)
        assert projected.id == task_id
        assert projected.current_run_id == task.current_run_id
        assert projected.session_id == session_id
    finally:
        conn.close()

    # Negative control: expected-evidence env does not populate parser.resume.
    env_only = parser.parse_args(["--cli", "chat", "-q", "hello", "-Q"])
    assert env_only.resume is None


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
