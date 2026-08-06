from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _make_real_cli(**kwargs):
    clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as cli_mod

        cli_mod = importlib.reload(cli_mod)
        with patch.object(cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            cli_mod.__dict__, {"CLI_CONFIG": clean_config}
        ):
            return cli_mod.HermesCLI(**kwargs)


class _DummyCLI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.session_id = "session-123"
        self.system_prompt = "base prompt"
        self.preloaded_skills = []

    def show_banner(self):
        return None

    def show_tools(self):
        return None

    def show_toolsets(self):
        return None

    def run(self):
        return None


def test_main_applies_preloaded_skills_to_system_prompt(monkeypatch):
    import cli as cli_mod

    created = {}

    def fake_cli(**kwargs):
        created["cli"] = _DummyCLI(**kwargs)
        return created["cli"]

    monkeypatch.setattr(cli_mod, "HermesCLI", fake_cli)
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None: ("skill prompt", ["hermes-agent-dev", "github-auth"], []),
    )

    with pytest.raises(SystemExit):
        cli_mod.main(skills="hermes-agent-dev,github-auth", list_tools=True)

    cli_obj = created["cli"]
    assert cli_obj.system_prompt == "base prompt\n\nskill prompt"
    assert cli_obj.preloaded_skills == ["hermes-agent-dev", "github-auth"]


def test_main_raises_for_unknown_preloaded_skill(monkeypatch):
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "HermesCLI", lambda **kwargs: _DummyCLI(**kwargs))
    monkeypatch.setattr(
        cli_mod,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None: ("", [], ["missing-skill"]),
    )

    with pytest.raises(ValueError, match=r"Unknown skill\(s\): missing-skill"):
        cli_mod.main(skills="missing-skill", list_tools=True)


def test_kanban_worker_blocks_removed_forced_skill_before_cli_construction(
    tmp_path, monkeypatch,
):
    """A dispatcher claim cannot start the model after a skill vanishes."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    profile = home / "profiles" / "alpha"
    skill_md = profile / "skills" / "vanished" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: vanished\n---\nbody\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="startup gate", assignee="alpha", skills=["vanished"]
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        skill_md.unlink()
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "dispatcher")

        import cli as cli_mod

        constructed = []
        monkeypatch.setattr(
            cli_mod, "HermesCLI", lambda **kwargs: constructed.append(kwargs)
        )
        cli_mod.main(skills="vanished", list_tools=True)

        row = conn.execute(
            "SELECT status, block_kind, claim_lock, current_run_id, consecutive_failures "
            "FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert constructed == []
        assert row["status"] == "blocked"
        assert row["block_kind"] == "capability"
        assert row["claim_lock"] is None
        run = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        assert dict(run) == {"status": "blocked", "outcome": "blocked"}
        assert row["consecutive_failures"] == 0
    finally:
        conn.close()


def test_nested_cli_skill_mismatch_cannot_block_parent_kanban_claim(
    tmp_path, monkeypatch,
):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    profile = home / "profiles" / "alpha"
    skill_md = profile / "skills" / "parent-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: parent-skill\n---\nbody\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="parent claim", assignee="alpha", skills=["parent-skill"]
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        monkeypatch.setenv("HERMES_HOME", str(profile))
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "dispatcher")

        import cli as cli_mod

        assert cli_mod._block_missing_kanban_forced_skills(["nested-missing"]) is False
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "running"
        assert task.current_run_id == claimed.current_run_id
        assert task.claim_lock == "dispatcher"
    finally:
        conn.close()


def test_show_banner_does_not_print_skills():
    """show_banner() no longer prints the activated skills line — it moved to run()."""
    cli_obj = _make_real_cli(compact=False)
    cli_obj.preloaded_skills = ["hermes-agent-dev", "github-auth"]
    cli_obj.console = MagicMock()

    with patch("cli.build_welcome_banner") as mock_banner, patch(
        "shutil.get_terminal_size", return_value=os.terminal_size((120, 40))
    ):
        cli_obj.show_banner()

    print_calls = [
        call.args[0]
        for call in cli_obj.console.print.call_args_list
        if call.args and isinstance(call.args[0], str)
    ]
    startup_lines = [line for line in print_calls if "Activated skills:" in line]
    assert len(startup_lines) == 0
    assert mock_banner.call_count == 1
