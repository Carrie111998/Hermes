import contextlib
import os
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_worker_supervisor import (
    _remove_workspace_from_sys_path,
    run_supervisor,
)


@pytest.fixture
def kanban_home():
    kb.init_db()
    return Path(os.environ["HERMES_HOME"])


def test_supervisor_releases_max_iteration_worker(kanban_home, tmp_path):
    log_path = tmp_path / "worker.log"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(log_path)!r}).write_text('Iteration budget exhausted (60/60)\\n')\n"
    )

    with contextlib.closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid, ttl_seconds=60)

    rc = run_supervisor(
        task_id=tid,
        ttl_seconds=60,
        heartbeat_interval_seconds=30,
        log_path=log_path,
        workspace=str(tmp_path),
        command=[sys.executable, str(script)],
    )

    with contextlib.closing(kb.connect()) as conn:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        events = kb.list_events(conn, tid)

    assert rc == 0
    assert task.status == "ready"
    assert task.current_run_id is None
    assert runs[-1].outcome == "released"
    assert any(e.kind == "released" for e in events)


def test_supervisor_child_ignores_workspace_stdlib_shadow(kanban_home, tmp_path):
    log_path = tmp_path / "worker.log"
    marker = tmp_path / "inspect_path.txt"
    (tmp_path / "inspect.py").write_text(
        "raise RuntimeError('workspace inspect imported')\n",
        encoding="utf-8",
    )
    script = tmp_path / "worker.py"
    script.write_text(
        "import inspect\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text("
        "inspect.__file__ + '\\n' + str(hasattr(inspect, 'signature'))"
        ")\n"
        f"Path({str(log_path)!r}).write_text('Iteration budget exhausted (60/60)\\n')\n",
        encoding="utf-8",
    )

    with contextlib.closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid, ttl_seconds=60)

    rc = run_supervisor(
        task_id=tid,
        ttl_seconds=60,
        heartbeat_interval_seconds=30,
        log_path=log_path,
        workspace=str(tmp_path),
        command=[sys.executable, str(script)],
    )

    inspect_path, has_signature = marker.read_text(encoding="utf-8").splitlines()

    assert rc == 0
    assert inspect_path != str(tmp_path / "inspect.py")
    assert has_signature == "True"


def test_supervisor_removes_workspace_from_own_sys_path(monkeypatch, tmp_path):
    safe_path = str(tmp_path / "safe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", ["", str(tmp_path), safe_path])

    _remove_workspace_from_sys_path(str(tmp_path))

    assert "" not in sys.path
    assert str(tmp_path) not in sys.path
    assert safe_path in sys.path


def test_supervisor_records_child_crash_before_pid_watchdog(kanban_home, tmp_path):
    log_path = tmp_path / "worker.log"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        f"Path({str(log_path)!r}).write_text('Traceback (most recent call last):\\nKeyboardInterrupt\\n')\n"
        "sys.exit(130)\n"
    )

    with contextlib.closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid, ttl_seconds=60)

    rc = run_supervisor(
        task_id=tid,
        ttl_seconds=60,
        heartbeat_interval_seconds=30,
        log_path=log_path,
        workspace=str(tmp_path),
        command=[sys.executable, str(script)],
    )

    with contextlib.closing(kb.connect()) as conn:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        events = kb.list_events(conn, tid)

    assert rc == 0
    assert task.status == "ready"
    assert task.current_run_id is None
    assert runs[-1].outcome == "crashed"
    assert runs[-1].error
    assert "worker_child_exit" in runs[-1].error
    assert "KeyboardInterrupt" in runs[-1].error
    assert runs[-1].metadata
    assert runs[-1].metadata["child_returncode"] == 130
    assert "KeyboardInterrupt" in runs[-1].metadata["log_tail"]
    assert any(
        e.kind == "crashed"
        and e.payload
        and e.payload.get("child_returncode") == 130
        for e in events
    )


def test_supervisor_releases_quiet_profile_without_spawning(kanban_home, tmp_path):
    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / ".quiet_mode").write_text("token guard", encoding="utf-8")
    marker = tmp_path / "spawned.txt"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('spawned')\n"
    )

    with contextlib.closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid, ttl_seconds=60)

    rc = run_supervisor(
        task_id=tid,
        ttl_seconds=60,
        heartbeat_interval_seconds=30,
        log_path=tmp_path / "worker.log",
        workspace=str(tmp_path),
        command=[sys.executable, str(script)],
    )

    with contextlib.closing(kb.connect()) as conn:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        events = kb.list_events(conn, tid)

    assert rc == 0
    assert not marker.exists()
    assert task.status == "ready"
    assert runs[-1].outcome == "released"
    assert runs[-1].summary and "quiet mode" in runs[-1].summary
    assert any(
        e.kind == "released" and e.payload.get("reason") == "profile_quiet_mode_paused"
        for e in events
    )


def test_supervisor_records_provider_empty_response_exit_as_crash(kanban_home, tmp_path):
    log_path = tmp_path / "worker.log"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(log_path)!r}).write_text("
        "'turn_exit_reason=provider_empty_response failure_code=provider_empty_response\\n'"
        ")\n"
    )

    with contextlib.closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid, ttl_seconds=60)

    rc = run_supervisor(
        task_id=tid,
        ttl_seconds=60,
        heartbeat_interval_seconds=30,
        log_path=log_path,
        workspace=str(tmp_path),
        command=[sys.executable, str(script)],
    )

    with contextlib.closing(kb.connect()) as conn:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)

    assert rc == 0
    assert task.status == "ready"
    assert task.current_run_id is None
    assert runs[-1].outcome == "crashed"
    assert "provider_empty_response" in runs[-1].error
    assert runs[-1].metadata["child_returncode"] == 0


def test_supervisor_ignores_tool_name_when_db_still_running(kanban_home, tmp_path):
    log_path = tmp_path / "worker.log"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\n"
        f"Path({str(log_path)!r}).write_text("
        "'tool available: kanban_complete\\nIteration budget exhausted (60/60)\\n'"
        ")\n"
    )

    with contextlib.closing(kb.connect()) as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.claim_task(conn, tid, ttl_seconds=60)

    rc = run_supervisor(
        task_id=tid,
        ttl_seconds=60,
        heartbeat_interval_seconds=30,
        log_path=log_path,
        workspace=str(tmp_path),
        command=[sys.executable, str(script)],
    )

    with contextlib.closing(kb.connect()) as conn:
        task = kb.get_task(conn, tid)
        events = kb.list_events(conn, tid)

    assert rc == 0
    assert task.status == "ready"
    assert any(e.kind == "released" for e in events)
