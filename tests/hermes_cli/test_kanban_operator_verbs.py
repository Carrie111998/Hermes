"""Spec 042 Phase D2 — operator control verbs + the promote gate.

Covers:
* ``kanban.auto_promote: false`` — ``recompute_ready`` promotes nothing;
  only ``promote_task`` reaches ``ready`` (the operator gate).
* The post-filing contract verbs: ``set-runner`` / ``set-prompt`` /
  ``set-swarm`` (mutable until claim, task event per write) and
  ``preview`` (byte-exact kickoff under current resolution + argv line).
* ``kanban show`` prints the CONFIG-RESOLVED runner for NULL-pin cards.
* The ``created_by`` discriminator: a process with ``HERMES_KANBAN_TASK``
  set stamps ``worker:<task_id>``, never the bare profile name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "elias").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_config(home: Path, text: str) -> None:
    home.joinpath("config.yaml").write_text(text, encoding="utf-8")


def _create(**kwargs) -> str:
    with kb.connect_closing() as conn:
        return kb.create_task(conn, **kwargs)


# ---------------------------------------------------------------------------
# kanban.auto_promote gate
# ---------------------------------------------------------------------------


def test_auto_promote_default_true_promotes(kanban_home):
    parent = _create(title="parent", assignee="elias")
    child = _create(title="child", assignee="elias", parents=[parent])
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"
        # complete_task itself runs recompute_ready internally.
        kb.complete_task(conn, parent, summary="done")
        assert kb.get_task(conn, child).status == "ready"


def test_auto_promote_false_blocks_ready_transition(kanban_home):
    _write_config(kanban_home, "kanban:\n  auto_promote: false\n")
    parent = _create(title="parent", assignee="elias")
    child = _create(title="child", assignee="elias", parents=[parent])
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "todo"
        kb.complete_task(conn, parent, summary="done")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"

        # The ONLY way through is the explicit promote verb.
        ok, err = kb.promote_task(conn, child, actor="user")
        assert ok and err is None
        assert kb.get_task(conn, child).status == "ready"


def test_auto_promote_string_forms(kanban_home):
    for text in ("false", "0", "no", "off"):
        _write_config(kanban_home, f"kanban:\n  auto_promote: {text}\n")
        assert kb._auto_promote_enabled() is False, text
    for text in ("true", "1", "yes", "on"):
        _write_config(kanban_home, f"kanban:\n  auto_promote: {text}\n")
        assert kb._auto_promote_enabled() is True, text


# ---------------------------------------------------------------------------
# set-runner / set-prompt / set-swarm
# ---------------------------------------------------------------------------


def test_set_runner_round_trip_via_cli(kanban_home):
    tid = _create(title="r card", assignee="elias")

    text = kc.run_slash(f"set-runner {tid} kimi")
    assert f"Set runner on {tid}: kimi" in text

    shown = json.loads(kc.run_slash(f"show {tid} --json"))
    assert shown["task"]["runner"] == "kimi"
    assert shown["task"]["effective_runner"] == "kimi"
    kinds = [e["kind"] for e in shown["events"]]
    assert "runner_set" in kinds


def test_set_runner_clear_and_validation(kanban_home):
    tid = _create(title="v", assignee="elias")
    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="unknown runner"):
            kb.set_task_runner(conn, tid, "not-a-runner")
        # Pin then clear; clearing falls back to config resolution.
        assert kb.set_task_runner(conn, tid, "omp") is True
        assert kb.set_task_runner(conn, tid, None) is True
        task = kb.get_task(conn, tid)
        assert task.runner is None


def test_set_contract_field_rejected_on_running(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="run", assignee="elias")
        kb.claim_task(conn, tid)
        with pytest.raises(RuntimeError, match="mutable until claim"):
            kb.set_task_runner(conn, tid, "omp")
        with pytest.raises(RuntimeError, match="mutable until claim"):
            kb.set_prompt_template(conn, tid, "{{task_id}} go")
        with pytest.raises(RuntimeError, match="mutable until claim"):
            kb.set_swarm_preset(conn, tid, "bees")


def test_set_prompt_template_and_render(kanban_home):
    tid = _create(title="p card", assignee="elias")
    with kb.connect_closing() as conn:
        kb.set_prompt_template(
            conn, tid, "Do {{title}} now ({{task_id}}) at {{workspace_path}}"
        )
        task = kb.get_task(conn, tid)
    prompt = kb.render_worker_prompt(task, "/tmp/ws")
    assert prompt == f"Do p card now ({tid}) at /tmp/ws"
    # Clear restores the runner default literal.
    with kb.connect_closing() as conn:
        kb.set_prompt_template(conn, tid, None)
        task = kb.get_task(conn, tid)
    assert kb.render_worker_prompt(task, "/tmp/ws") == f"work kanban task {tid}"


def test_set_swarm_preset_via_db(kanban_home):
    tid = _create(title="s", assignee="elias")
    with kb.connect_closing() as conn:
        assert kb.set_swarm_preset(conn, tid, "bees") is True
        assert kb.get_task(conn, tid).swarm_preset == "bees"
        assert kb.set_swarm_preset(conn, tid, "none") is True
        assert kb.get_task(conn, tid).swarm_preset is None


# ---------------------------------------------------------------------------
# preview verb
# ---------------------------------------------------------------------------


def test_preview_renders_byte_exact_prompt_and_argv(kanban_home):
    _write_config(kanban_home, "kanban:\n  default_runner: omp\n")
    tid = _create(title="prev", assignee="elias", body="the body")
    out = kc.run_slash(f"preview {tid}")
    # Byte-exact rendered kickoff under current resolution (omp default).
    assert "work kanban task" not in out          # not the hermes literal
    assert "hermes kanban complete" in out         # omp lifecycle contract
    assert "-- runner:   omp" in out               # config-resolved runner
    argv_line = [l for l in out.splitlines() if l.startswith("-- argv")][0]
    assert "--no-session" in argv_line
    assert "kimi-code/k3" in argv_line
    assert "--thinking max" in argv_line


def test_preview_hermes_pin_shows_literal_and_argv(kanban_home):
    tid = _create(title="hp", assignee="elias", runner="hermes")
    out = kc.run_slash(f"preview {tid}")
    first_line = out.splitlines()[0]
    assert first_line == f"work kanban task {tid}"
    argv_line = [l for l in out.splitlines() if l.startswith("-- argv")][0]
    assert "chat -q" in argv_line
    assert "-p elias" in argv_line
    assert "-- runner:   hermes" in out


# ---------------------------------------------------------------------------
# show: config-resolved runner label
# ---------------------------------------------------------------------------


def test_show_prints_config_resolved_runner_for_null_pin(kanban_home):
    _write_config(kanban_home, "kanban:\n  default_runner: omp\n")
    tid = _create(title="lbl", assignee="elias")
    text = kc.run_slash(f"show {tid}")
    assert "runner:    omp (default; pin is NULL → omp)" in text

    _write_config(kanban_home, "")
    text = kc.run_slash(f"show {tid}")
    assert "runner:    hermes (default)" in text


# ---------------------------------------------------------------------------
# created_by discriminator
# ---------------------------------------------------------------------------


def test_profile_author_stamps_worker_prefix_in_worker_context(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert kc._profile_author() == "worker:t_abc"


def test_profile_author_unaffected_outside_worker(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    assert kc._profile_author() == "default"


def test_worker_created_card_is_discriminable(kanban_home, monkeypatch):
    """A card filed from inside a worker context carries a created_by that
    no human-side list treats as human."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    monkeypatch.delenv("HERMES_PROFILE_NAME", raising=False)
    author = kc._profile_author()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="child of worker",
                             assignee="elias", created_by=author)
    shown = json.loads(kc.run_slash(f"show {tid} --json"))
    assert shown["task"]["created_by"] == "worker:t_parent"
