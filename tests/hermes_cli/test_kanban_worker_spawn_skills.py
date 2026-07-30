"""Regressions for fail-closed task-skill validation at worker spawn.

The optional broker cases load the deployed ``boardd_shim.py`` named by
``HERMES_BOARDD_SHIM_PATH``. Upstream does not ship that fleet overlay, so those
cases skip when the path is absent; the live-lineage verification command sets
it explicitly and exercises the overlay's real ``claim_task``/``_row_to_task``
implementation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from hermes_cli import kanban_db as kb


_NO_DB_OVERRIDE = object()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "config.yaml").write_text(
        "platform_toolsets:\n  cli: []\n", encoding="utf-8"
    )
    for name in (
        "HERMES_KANBAN_BROKER",
        "BOARDD_SOCK",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
    ):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


def _task(skills: object) -> kb.Task:
    return kb.Task(
        id="t_spawn_skills",
        title="spawn skills",
        body=None,
        assignee="worker",
        status="running",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        skills=skills,
    )


def _skill_names(cmd: list[str]) -> list[str]:
    return [
        cmd[index + 1]
        for index, token in enumerate(cmd)
        if token == "--skills" and index + 1 < len(cmd)
    ]


def _expected_spawn_cmd(
    task_id: str, env: dict[str, str], skills: list[str]
) -> list[str]:
    cmd = [
        *kb._resolve_hermes_argv(),
        "-p",
        "default",
        "--cli",
        "--accept-hooks",
    ]
    for skill in skills:
        cmd.extend(["--skills", skill])
    worker_toolsets = kb._resolve_worker_cli_toolsets(env.get("HERMES_HOME"))
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task_id}"])
    return cmd


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["alpha", "beta"], ["alpha", "beta"]),
        (("alpha", "beta"), ["alpha", "beta"]),
        ('["alpha", "beta"]', ["alpha", "beta"]),
        ("alpha", ["alpha"]),
        ("alpha, beta", ["alpha", "beta"]),
        (None, []),
        ([], []),
        ("   ", []),
        (
            [" category/alpha ", "plugin:beta", "category/alpha"],
            ["category/alpha", "plugin:beta"],
        ),
    ],
)
def test_direct_task_spawn_emits_normalized_ordered_skill_pairs(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
    expected: list[str],
) -> None:
    captured: dict[str, list[str]] = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd: list[str], **_kwargs: object) -> FakeProc:
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    pid = kb._default_spawn(_task(raw), str(tmp_path))

    assert pid == 4242
    assert _skill_names(captured["cmd"]) == expected
    if expected:
        assert max(
            index for index, token in enumerate(captured["cmd"]) if token == "--skills"
        ) < captured["cmd"].index("chat")


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('["alpha"', "malformed JSON-looking value"),
        ('{"skill": "alpha"}', "JSON value must be an array"),
        ('"alpha"', "JSON value must be an array"),
        (["alpha", 2], "members must be strings"),
        (["alpha", ""], "members must be non-empty"),
        ("alpha,,beta", "members must be non-empty"),
        (["bad skill"], "member is not a valid identifier"),
        (b'["alpha"]', "unsupported value type"),
    ],
)
def test_normalizer_rejects_invalid_values(raw: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        kb._normalize_spawn_skills(raw, task_id="t_bad")


@pytest.mark.parametrize(
    "name",
    [
        "bad$skill",
        "/absolute",
        "relative/../escape",
        "plugin:",
        "alpha\\beta",
        "x" * 257,
    ],
)
def test_normalizer_rejects_invalid_identifiers(name: str) -> None:
    with pytest.raises(ValueError, match="valid identifier"):
        kb._normalize_spawn_skills([name], task_id="t_bad")


def test_validation_error_is_bounded_single_line_and_task_identifying() -> None:
    with pytest.raises(ValueError) as exc_info:
        kb._normalize_spawn_skills(
            ["bad skill" + ("x" * 10_000)],
            task_id="t_" + ("z" * 10_000) + "\nforged",
        )

    message = str(exc_info.value)
    assert message.startswith("task t_zzz")
    assert "expected None, list[str]/tuple[str, ...]" in message
    assert len(message) < 400
    assert "\n" not in message
    assert "x" * 100 not in message


def test_direct_task_rejects_invalid_skills_before_provider_or_popen(
    kanban_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[object] = []
    popen_calls: list[object] = []

    def fail_provider(*args: object, **_kwargs: object) -> None:
        provider_calls.append(args)
        raise AssertionError("profile/provider resolution must not run")

    def fail_popen(*args: object, **_kwargs: object) -> None:
        popen_calls.append(args)
        raise AssertionError("Popen must not run")

    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", fail_provider)
    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with pytest.raises(ValueError, match=r"task t_spawn_skills has invalid skills"):
        kb._default_spawn(_task(["alpha", 2]), str(tmp_path))

    assert provider_calls == []
    assert popen_calls == []


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ('[" alpha ", "beta", "alpha"]', [" alpha ", "beta", "alpha"]),
        ('["alpha", 2]', ["alpha", 2]),
        ('{"skill": "alpha"}', '{"skill": "alpha"}'),
        ('["unterminated"', '["unterminated"'),
        (b'["alpha"]', b'["alpha"]'),
    ],
)
def test_normal_row_hydration_preserves_values_for_final_validation(
    kanban_home: Path,
    stored: object,
    expected: object,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="row hydration",
            assignee="default",
            skills=["seed"],
        )
        conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (stored, task_id))
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    assert row is not None
    assert kb.Task.from_row(row).skills == expected


@pytest.mark.parametrize(
    ("created_skills", "stored_skills", "expected"),
    [
        (["alpha", "beta"], _NO_DB_OVERRIDE, ["alpha", "beta"]),
        (("alpha", "beta"), _NO_DB_OVERRIDE, ["alpha", "beta"]),
        (["seed"], "alpha", ["alpha"]),
        (["seed"], " alpha, beta ", ["alpha", "beta"]),
        (
            ["seed"],
            '[" alpha ", "beta", "alpha", "beta "]',
            ["alpha", "beta"],
        ),
    ],
)
def test_normal_db_dispatch_emits_exact_normalized_argv(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_skills: object,
    stored_skills: object,
    expected: list[str],
) -> None:
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProc:
        pid = 2_000_000_000

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProc:
        popen_calls.append((list(cmd), dict(kwargs)))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="normal DB skills dispatch",
            assignee="default",
            skills=cast(Any, created_skills),
        )
        if stored_skills is not _NO_DB_OVERRIDE:
            conn.execute(
                "UPDATE tasks SET skills = ? WHERE id = ?",
                (stored_skills, task_id),
            )
            conn.commit()

        result = kb.dispatch_once(conn, failure_limit=2)
        task = kb.get_task(conn, task_id)

    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    spawn_env = cast(dict[str, str], kwargs["env"])
    assert cmd == _expected_spawn_cmd(task_id, spawn_env, expected)
    assert result.spawned == [(task_id, "default", result.spawned[0][2])]
    assert task is not None
    assert task.status == "running"
    assert task.worker_pid == FakeProc.pid
    assert task.last_failure_error is None


@pytest.mark.parametrize(
    ("stored", "reason"),
    [
        ('["alpha"', "malformed JSON-looking value"),
        ('{"skill": "alpha"}', "JSON value must be an array"),
        ('["alpha", 2]', "members must be strings"),
        ('["alpha", ""]', "members must be non-empty"),
        ('["bad skill"]', "member is not a valid identifier"),
        (b'["alpha"]', "unsupported value type"),
    ],
)
def test_normal_db_dispatch_records_bounded_spawn_failure_without_popen(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored: object,
    reason: str,
) -> None:
    popen_calls: list[object] = []

    def fail_popen(*args: object, **_kwargs: object) -> None:
        popen_calls.append(args)
        raise AssertionError("Popen must not run")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="invalid DB skills dispatch",
            assignee="default",
            skills=["seed"],
        )
        conn.execute("UPDATE tasks SET skills = ? WHERE id = ?", (stored, task_id))
        conn.commit()

        result = kb.dispatch_once(conn, failure_limit=2)
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT status, outcome, worker_pid, error FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert result.spawned == []
    assert popen_calls == []
    assert task is not None
    assert task.status == "ready"
    assert task.worker_pid is None
    assert task.consecutive_failures == 1
    assert task.last_failure_error is not None
    assert task.last_failure_error.startswith(
        f"task {task_id} has invalid skills ({reason})"
    )
    assert len(task.last_failure_error) < 400
    assert "\n" not in task.last_failure_error
    assert run is not None
    assert run["status"] == "spawn_failed"
    assert run["outcome"] == "spawn_failed"
    assert run["worker_pid"] is None
    assert run["error"] == task.last_failure_error


def _load_live_boardd_shim(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    raw_path = os.environ.get("HERMES_BOARDD_SHIM_PATH")
    if not raw_path:
        pytest.skip("set HERMES_BOARDD_SHIM_PATH to run the live broker overlay tests")
    assert raw_path is not None
    shim_path = Path(raw_path).resolve()
    if not shim_path.is_file():
        pytest.skip(f"live broker overlay not found at {shim_path}")

    import hermes_cli

    package_path = [str(path) for path in hermes_cli.__path__]
    live_package = str(shim_path.parent)
    if live_package not in package_path:
        package_path.append(live_package)
    monkeypatch.setattr(hermes_cli, "__path__", package_path)

    module_name = "hermes_cli.boardd_shim_live_skills_test"
    spec = importlib.util.spec_from_file_location(module_name, shim_path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load broker overlay from {shim_path}")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _install_local_broker_claim(
    monkeypatch: pytest.MonkeyPatch,
    shim: ModuleType,
    conn: Any,
) -> list[kb.Task]:
    original_claim = kb.claim_task
    original_row_to_task = shim._row_to_task
    hydrated: list[kb.Task] = []

    def observed_row_to_task(kdb: ModuleType, row: object) -> kb.Task:
        task = original_row_to_task(kdb, row)
        hydrated.append(task)
        return task

    class LocalBrokerClient:
        def claim(
            self,
            task_id: str,
            *,
            claimer: str | None = None,
            ttl_seconds: int = 7200,
        ) -> dict[str, object]:
            claimed = original_claim(
                conn,
                task_id,
                claimer=claimer,
                ttl_seconds=ttl_seconds,
            )
            return {
                "won": claimed is not None,
                "run_id": claimed.current_run_id if claimed is not None else None,
            }

        def get_task(self, task_id: str) -> dict[str, object] | None:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    client = LocalBrokerClient()
    monkeypatch.setenv("HERMES_KANBAN_BROKER", "1")
    assert shim.enabled() is True
    monkeypatch.setattr(shim, "_c", lambda: client)
    monkeypatch.setattr(shim, "_row_to_task", observed_row_to_task)
    monkeypatch.setattr(kb, "claim_task", shim.claim_task)
    return hydrated


def test_live_broker_row_hydration_rejects_mixed_array_before_popen(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim = _load_live_boardd_shim(monkeypatch)
    popen_calls: list[object] = []

    def fail_popen(*args: object, **_kwargs: object) -> None:
        popen_calls.append(args)
        raise AssertionError("Popen must not run")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)

    with kb.connect() as conn:
        hydrated = _install_local_broker_claim(monkeypatch, shim, conn)
        task_id = kb.create_task(
            conn,
            title="broker invalid skills dispatch",
            assignee="default",
            skills=["seed"],
        )
        conn.execute(
            "UPDATE tasks SET skills = ? WHERE id = ?",
            (json.dumps(["a", 2]), task_id),
        )
        conn.commit()

        result = kb.dispatch_once(conn, failure_limit=2)
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT status, outcome, worker_pid FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert result.spawned == []
    assert popen_calls == []
    assert len(hydrated) == 1
    assert hydrated[0].skills == json.dumps(["a", 2])
    assert task is not None
    assert task.status == "ready"
    assert task.worker_pid is None
    assert task.last_failure_error is not None
    assert "members must be strings" in task.last_failure_error
    assert run is not None
    assert run["status"] == "spawn_failed"
    assert run["outcome"] == "spawn_failed"
    assert run["worker_pid"] is None


def test_live_broker_row_hydration_emits_exact_ordered_argv(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shim = _load_live_boardd_shim(monkeypatch)
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    class FakeProc:
        pid = 2_000_000_001

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProc:
        popen_calls.append((list(cmd), dict(kwargs)))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with kb.connect() as conn:
        hydrated = _install_local_broker_claim(monkeypatch, shim, conn)
        task_id = kb.create_task(
            conn,
            title="broker valid skills dispatch",
            assignee="default",
            skills=["seed"],
        )
        conn.execute(
            "UPDATE tasks SET skills = ? WHERE id = ?",
            (
                json.dumps([" skill/one ", "plugin:two", "skill/one", "third.skill"]),
                task_id,
            ),
        )
        conn.commit()

        result = kb.dispatch_once(conn, failure_limit=2)
        task = kb.get_task(conn, task_id)

    assert len(popen_calls) == 1
    assert len(hydrated) == 1
    assert isinstance(hydrated[0].skills, str)
    cmd, kwargs = popen_calls[0]
    expected_skills = ["skill/one", "plugin:two", "third.skill"]
    spawn_env = cast(dict[str, str], kwargs["env"])
    assert cmd == _expected_spawn_cmd(task_id, spawn_env, expected_skills)
    assert result.spawned == [(task_id, "default", result.spawned[0][2])]
    assert task is not None
    assert task.status == "running"
    assert task.worker_pid == FakeProc.pid
    assert task.last_failure_error is None
