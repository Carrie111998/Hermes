from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "scripts" / "factory_admission_hook.py"
LANE = REPO_ROOT / "scripts" / "factory_lane.py"


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def _profile(root: Path, registry: Path, name: str = "hermes-code-a") -> Path:
    registry.mkdir(parents=True, exist_ok=True)
    profile = root / "profiles" / name
    profile.mkdir(parents=True)
    command = " ".join([
        sys.executable,
        str(HOOK),
        "--registry", str(registry),
        "--agent", name,
        "--profile", name,
        "--only-mutating",
        "--require-owned-git",
    ])
    profile.joinpath("config.yaml").write_text(
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - matcher: '.*'\n"
        "      fail_closed: true\n"
        f"      command: {json.dumps(command)}\n",
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    return profile


@pytest.fixture(autouse=True)
def _isolate_kanban_spawn_environment(monkeypatch):
    """Keep every owner-bootstrap fixture inside its per-test HERMES_HOME."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles as profile_module

    def test_home() -> Path:
        return Path(
            os.environ.get("HERMES_KANBAN_HOME")
            or os.environ.get("HERMES_HOME")
            or Path.home() / ".hermes"
        )

    def resolve_test_profile(profile_name: str) -> str:
        profile = test_home() / "profiles" / profile_name
        if not profile.is_dir():
            raise FileNotFoundError(profile)
        return str(profile)

    monkeypatch.setattr(kb, "kanban_home", test_home)
    monkeypatch.setattr(profile_module, "resolve_profile_env", resolve_test_profile)


def _task(kb, *, task_id: str = "t_owner", run_id: int = 7, title: str = "HER-118 — owner"):
    return kb.Task(
        id=task_id,
        title=title,
        body="Linear: https://linear.app/example/issue/HER-118/owner",
        assignee="hermes-code-a",
        status="running",
        priority=1,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="worktree",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=run_id,
    )


def _hook(registry: Path, repo: Path, tool: str, tool_input: dict, *, session: str):
    payload = {
        "hook_event_name": "pre_tool_call",
        "tool_name": tool,
        "tool_input": tool_input,
        "session_id": session,
        "cwd": str(repo),
    }
    result = subprocess.run(
        [sys.executable, str(HOOK), "--registry", str(registry),
         "--agent", "hermes-code-a", "--profile", "hermes-code-a",
         "--only-mutating", "--require-owned-git"],
        input=json.dumps(payload), text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_default_spawn_claims_exact_owner_and_pins_deterministic_session(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = os.getpid()

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            captured["waited"] = timeout

    def fake_start(cmd, *, workspace, log_f, env):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env)
        return FakeProc()

    monkeypatch.setattr(kb, "_start_kanban_worker_process", fake_start)
    class FakeReaper:
        def poll(self):
            return None

    def fake_reaper(**kwargs):
        captured["reaper"] = kwargs
        ready_r, ready_w = os.pipe()
        os.write(ready_w, b"R")
        os.close(ready_w)
        return FakeReaper(), ready_r

    monkeypatch.setattr(kb, "_start_worker_owner_reaper", fake_reaper)
    task = _task(kb)
    pid = kb._default_spawn(task, str(workspace))

    session = "kanban-t_owner-run-7"
    owner = json.loads((registry / "locks" / "HER-118" / "owner.json").read_text())
    assert pid == os.getpid()
    assert captured["cmd"][captured["cmd"].index("--resume") + 1] == session
    assert captured["env"]["HERMES_SESSION_ID"] == session
    assert owner == {
        "agent": "hermes-code-a",
        "heartbeat_at": owner["heartbeat_at"],
        "host": owner["host"],
        "pid": os.getpid(),
        "process_start_time": kb._worker_process_start_time(os.getpid()),
        "profile": "hermes-code-a",
        "session_id": session,
        "started_at": owner["started_at"],
        "ttl_hours": 72.0,
        "worktree": str(workspace.resolve()),
    }
    assert captured["reaper"] == {
        "task": task,
        "workspace": str(workspace),
        "profile_home": profile,
        "pid": os.getpid(),
        "process_start_time": owner["process_start_time"],
        "session_id": session,
    }
    assert profile.exists()
    assert not captured.get("terminated")

    from hermes_state import SessionDB

    session_db = SessionDB(db_path=profile / "state.db")
    try:
        session_meta = session_db.get_session(session)
    finally:
        session_db.close()
    assert session_meta is not None
    assert session_meta["source"] == "cli"
    assert session_meta["cwd"] == str(workspace.resolve())
    assert session_meta["profile_name"] == "hermes-code-a"
    assert not (root / "state.db").exists()


def test_worker_session_id_is_distinct_per_run():
    from hermes_cli import kanban_db as kb

    first = kb._kanban_worker_session_id(_task(kb, run_id=7))
    retry = kb._kanban_worker_session_id(_task(kb, run_id=8))

    assert first == "kanban-t_owner-run-7"
    assert retry == "kanban-t_owner-run-8"
    assert first != retry


def test_spawned_worker_proves_exact_pre_tool_session_before_first_read_and_mutation(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    isolated_db = root / "kanban-test.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(isolated_db))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setenv(
        "HERMES_KANBAN_WORKSPACES_ROOT", str(root / "kanban" / "workspaces")
    )

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    kb._INITIALIZED_PATHS.clear()
    assert kb.kanban_db_path() == isolated_db
    assert kb.worker_logs_dir() == root / "kanban" / "logs"
    kb.init_db()
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="HER-118 — spawned lifecycle proof",
            body="Exact worker lifecycle",
            assignee="hermes-code-a",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        task = kb.claim_task(conn, task_id)
    assert task is not None

    result_path = tmp_path / "probe-result.json"
    probe = tmp_path / "worker_probe.py"
    probe.write_text(
        """
import json
import os
import time
from pathlib import Path

import yaml

from agent import shell_hooks
from hermes_cli import plugins
import model_tools
import tools.kanban_tools  # noqa: F401

plugins._plugin_manager = plugins.PluginManager()
shell_hooks.reset_for_tests()
config = yaml.safe_load((Path(os.environ['HERMES_HOME']) / 'config.yaml').read_text())
shell_hooks.register_from_config(config, accept_hooks=True)
session = os.environ['HERMES_SESSION_ID']
owner_path = Path(os.environ['PROBE_REGISTRY']) / 'locks' / 'HER-118' / 'owner.json'
owner = json.loads(owner_path.read_text())
show = json.loads(model_tools.handle_function_call('kanban_show', {}, session_id=session))
target = Path(os.environ['HERMES_KANBAN_WORKSPACE']) / 'first-mutation.txt'
write = json.loads(model_tools.handle_function_call(
    'write_file', {'path': str(target), 'content': 'admitted\\n'}, session_id=session,
))
Path(os.environ['PROBE_RESULT']).write_text(json.dumps({
    'session': session,
    'pid': os.getpid(),
    'cwd': str(Path.cwd().resolve()),
    'owner': owner,
    'show': show,
    'write': write,
}))
release = Path(os.environ['PROBE_RELEASE'])
for _ in range(400):
    if release.exists():
        break
    time.sleep(0.025)
else:
    raise TimeoutError('parent did not release worker identity probe')
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROBE_RESULT", str(result_path))
    release_path = tmp_path / "probe-release"
    monkeypatch.setenv("PROBE_RELEASE", str(release_path))
    monkeypatch.setenv("PROBE_REGISTRY", str(registry))
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: [sys.executable, str(probe)],
    )

    pid = kb._default_spawn(task, str(workspace))
    for _ in range(200):
        if result_path.exists():
            break
        import time
        time.sleep(0.025)
    assert result_path.exists(), (root / "kanban" / "worker-logs" / f"{task_id}.log").read_text()

    proof = json.loads(result_path.read_text())
    try:
        live_process_start_time = kb._worker_process_start_time(pid)
    finally:
        release_path.write_text("release\n", encoding="utf-8")
    expected_session = f"kanban-{task_id}-run-{task.current_run_id}"
    assert proof["session"] == expected_session
    assert proof["pid"] == pid == proof["owner"]["pid"]
    assert proof["owner"]["process_start_time"] == live_process_start_time
    assert proof["cwd"] == str(workspace.resolve())
    assert proof["owner"]["worktree"] == str(workspace.resolve())
    assert proof["owner"]["session_id"] == expected_session
    assert proof["show"]["task"]["id"] == task_id
    assert "error" not in proof["write"]
    assert (workspace / "first-mutation.txt").read_text() == "admitted\n"


def test_real_cli_resumes_preseeded_profile_session_before_first_action(tmp_path):
    from hermes_cli import kanban_db as kb

    root = tmp_path / ".hermes"
    profile = root / "profiles" / "hermes-code-a"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = "kanban-t_canary-run-1"
    kb._ensure_kanban_worker_session(profile, session_id, str(workspace))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            if request.get("stream"):
                chunks = [
                    {
                        "id": "chatcmpl-her118",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "test-model",
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "HER118_CANARY_OK",
                            },
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "chatcmpl-her118",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "test-model",
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }],
                    },
                ]
                body = "".join(
                    f"data: {json.dumps(chunk)}\n\n" for chunk in chunks
                ) + "data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                body = json.dumps({
                    "id": "chatcmpl-her118",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test-model",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "HER118_CANARY_OK",
                        },
                        "finish_reason": "stop",
                    }],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                })
                content_type = "application/json"
            payload = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        profile.joinpath("config.yaml").write_text(
            "model:\n"
            "  provider: custom\n"
            "  name: test-model\n"
            f"  base_url: http://127.0.0.1:{server.server_port}/v1\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update({
            "HOME": str(tmp_path),
            "HERMES_HOME": str(root),
            "OPENAI_API_KEY": "test-key",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "HERMES_SESSION_ID": session_id,
        })
        result = subprocess.run(
            [
                sys.executable, "-m", "hermes_cli.main",
                "-p", "hermes-code-a",
                "--resume", session_id,
                "--no-restore-cwd",
                "--cli",
                "--provider", "custom",
                "-m", "test-model",
                "chat", "-q", "run canary", "-Q",
            ],
            cwd=workspace,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
    finally:
        server.shutdown()
        server.server_close()

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "HER118_CANARY_OK" in result.stdout
    assert "Session not found" not in output
    assert (profile / "state.db").exists()
    assert not (root / "state.db").exists()


def test_default_spawn_kills_child_and_preserves_foreign_owner_on_collision(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    foreign = subprocess.run(
        [sys.executable, str(LANE), "--registry", str(registry), "admit", "HER-118",
         "--mode", "owner", "--hard", "--agent", "foreign", "--profile", "foreign",
         "--session", "foreign-session", "--worktree", str(workspace),
         "--owner-pid", str(os.getpid())],
        capture_output=True, text=True,
    )
    assert foreign.returncode == 0, foreign.stderr
    before = (registry / "locks" / "HER-118" / "owner.json").read_bytes()
    state = {}

    class FakeProc:
        pid = os.getpid()

        def terminate(self):
            state["terminated"] = True

        def wait(self, timeout=None):
            state["waited"] = timeout

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb,
        "_start_kanban_worker_process",
        lambda cmd, *, workspace, log_f, env: FakeProc(),
    )

    with pytest.raises(RuntimeError, match="already claimed"):
        kb._default_spawn(_task(kb), str(workspace))

    assert state == {"terminated": True, "waited": 5}
    assert (registry / "locks" / "HER-118" / "owner.json").read_bytes() == before


def test_default_spawn_releases_its_claim_when_reaper_start_fails(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    state = {}

    class FakeProc:
        pid = os.getpid()

        def terminate(self):
            state["terminated"] = True

        def wait(self, timeout=None):
            state["waited"] = timeout

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb, "_start_kanban_worker_process",
        lambda cmd, *, workspace, log_f, env: FakeProc(),
    )
    monkeypatch.setattr(
        kb, "_start_worker_owner_reaper",
        lambda **kwargs: (_ for _ in ()).throw(OSError("reaper unavailable")),
    )

    with pytest.raises(OSError, match="reaper unavailable"):
        kb._default_spawn(_task(kb), str(workspace))

    assert state == {"terminated": True, "waited": 5}
    assert not (registry / "locks" / "HER-118" / "owner.json").exists()


def test_default_spawn_fails_closed_when_session_bootstrap_fails(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    state = {}

    class FakeWorker:
        pid = os.getpid()

        def poll(self):
            return None

        def terminate(self):
            state["worker_terminated"] = True

        def wait(self, timeout=None):
            state["worker_waited"] = timeout

    class FakeReaper:
        def poll(self):
            return None

        def terminate(self):
            state["reaper_terminated"] = True

        def wait(self, timeout=None):
            state["reaper_waited"] = timeout

    def fake_reaper(**kwargs):
        ready_r, ready_w = os.pipe()
        os.write(ready_w, b"R")
        os.close(ready_w)
        return FakeReaper(), ready_r

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb, "_start_kanban_worker_process",
        lambda cmd, *, workspace, log_f, env: FakeWorker(),
    )
    monkeypatch.setattr(kb, "_start_worker_owner_reaper", fake_reaper)
    monkeypatch.setattr(
        kb, "_ensure_kanban_worker_session",
        lambda *_args: (_ for _ in ()).throw(OSError("state db unavailable")),
    )

    with pytest.raises(OSError, match="state db unavailable"):
        kb._default_spawn(_task(kb), str(workspace))

    assert state == {
        "worker_terminated": True,
        "worker_waited": 5,
        "reaper_terminated": True,
        "reaper_waited": 5,
    }
    assert not (registry / "locks" / "HER-118" / "owner.json").exists()
    assert not list((root / "kanban" / "worker-logs").glob("*.owner-ready-*"))


@pytest.mark.parametrize("first_release", ["false", "error"])
def test_session_bootstrap_failure_retries_exact_release_and_closes_resources(
    monkeypatch, tmp_path, first_release,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    state = {}

    class FakeWorker:
        pid = os.getpid()

        def poll(self):
            return None

        def terminate(self):
            state["worker_terminated"] = True

        def wait(self, timeout=None):
            state["worker_waited"] = timeout

    class FakeReaper:
        def poll(self):
            return None

        def terminate(self):
            state["reaper_terminated"] = True

        def wait(self, timeout=None):
            state["reaper_waited"] = timeout

    def fake_start(cmd, *, workspace, log_f, env):
        state["log_fd"] = log_f.fileno()
        state["gate_path"] = Path(env["HERMES_KANBAN_OWNER_GATE"])
        return FakeWorker()

    def fake_reaper(**kwargs):
        ready_r, ready_w = os.pipe()
        os.write(ready_w, b"R")
        os.close(ready_w)
        state["ready_fd"] = ready_r
        return FakeReaper(), ready_r

    real_release = kb._release_worker_owner
    release_calls = []

    def flaky_release(*args):
        release_calls.append(args)
        if len(release_calls) == 1:
            if first_release == "false":
                return False
            raise OSError("transient exact-release failure")
        return real_release(*args)

    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_start_kanban_worker_process", fake_start)
    monkeypatch.setattr(kb, "_start_worker_owner_reaper", fake_reaper)
    monkeypatch.setattr(kb, "_release_worker_owner", flaky_release)
    monkeypatch.setattr(
        kb, "_ensure_kanban_worker_session",
        lambda *_args: (_ for _ in ()).throw(OSError("state db unavailable")),
    )

    with pytest.raises(OSError, match="state db unavailable") as exc_info:
        kb._default_spawn(_task(kb), str(workspace))

    assert str(exc_info.value) == "state db unavailable"
    assert len(release_calls) == 2
    assert state["worker_terminated"] is True
    assert state["worker_waited"] == 5
    assert state["reaper_terminated"] is True
    assert state["reaper_waited"] == 5
    assert not (registry / "locks" / "HER-118" / "owner.json").exists()
    assert not state["gate_path"].exists()
    with pytest.raises(OSError):
        os.fstat(state["ready_fd"])
    with pytest.raises(OSError):
        os.fstat(state["log_fd"])


def test_default_spawn_fails_closed_when_reaper_dies_before_ready(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    state = {}

    class FakeWorker:
        pid = os.getpid()

        def terminate(self):
            state["worker_terminated"] = True

        def wait(self, timeout=None):
            state["worker_waited"] = timeout

    class DeadReaper:
        def poll(self):
            return 1

        def terminate(self):
            state["reaper_terminated"] = True

        def wait(self, timeout=None):
            state["reaper_waited"] = timeout

    ready_r, ready_w = os.pipe()
    os.close(ready_w)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(
        kb, "_start_kanban_worker_process",
        lambda cmd, *, workspace, log_f, env: FakeWorker(),
    )
    monkeypatch.setattr(
        kb, "_start_worker_owner_reaper",
        lambda **kwargs: (DeadReaper(), ready_r),
    )

    with pytest.raises(RuntimeError, match="reaper.*ready"):
        kb._default_spawn(_task(kb), str(workspace))

    assert state["worker_terminated"] is True
    assert state["worker_waited"] == 5
    assert not (registry / "locks" / "HER-118" / "owner.json").exists()
    assert not list((root / "kanban" / "worker-logs").glob("*.owner-ready-*"))
    with pytest.raises(OSError):
        os.fstat(ready_r)


def test_reaper_ready_wait_times_out_and_closes_pipe():
    from hermes_cli import kanban_db as kb

    class SilentReaper:
        def poll(self):
            return None

    ready_r, ready_w = os.pipe()
    try:
        with pytest.raises(RuntimeError, match="reaper.*timeout"):
            kb._wait_for_worker_owner_reaper_ready(
                SilentReaper(), ready_r, timeout=0.01,
            )
        with pytest.raises(OSError):
            os.fstat(ready_r)
    finally:
        os.close(ready_w)


@pytest.mark.parametrize("ack_failure", ["broken-pipe", "bad-fd"])
def test_owner_reaper_ack_disconnect_still_waits_and_releases(
    monkeypatch, tmp_path, ack_failure,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    task = _task(kb)
    session = kb._kanban_worker_session_id(task)
    pid = os.getpid()
    start = kb._worker_process_start_time(pid)
    kb._claim_worker_owner(task, str(workspace), profile, pid, start, session)
    release_argv = kb._worker_owner_release_argv(
        task, str(workspace), profile, pid, start, session,
    )
    assert release_argv is not None

    ready_r, ready_w = os.pipe()
    os.close(ready_r)
    if ack_failure == "bad-fd":
        os.close(ready_w)
    alive = iter([True, False])
    identity_checks = []

    def fake_identity(pid, process_start_time):
        identity_checks.append((pid, process_start_time))
        return next(alive)

    monkeypatch.setattr(kb, "_worker_identity_is_alive", fake_identity)
    monkeypatch.setattr(kb.time, "sleep", lambda _interval: None)

    assert kb._wait_for_pid_exit_and_run_release(
        pid=pid,
        process_start_time=start,
        release_argv=release_argv,
        ready_fd=ready_w,
    ) is True

    assert identity_checks == [(pid, start), (pid, start)]
    assert not (registry / "locks" / "HER-118" / "owner.json").exists()
    with pytest.raises(OSError):
        os.fstat(ready_w)


def test_owner_reaper_without_ack_fd_still_waits_and_releases(monkeypatch):
    from hermes_cli import kanban_db as kb

    alive = iter([True, False])
    identity_checks = []
    releases = []

    def fake_identity(pid, process_start_time):
        identity_checks.append((pid, process_start_time))
        return next(alive)

    def fake_run(argv, **kwargs):
        releases.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(kb, "_worker_identity_is_alive", fake_identity)
    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    monkeypatch.setattr(kb.time, "sleep", lambda _interval: None)

    assert kb._wait_for_pid_exit_and_run_release(
        pid=456,
        process_start_time="exact-start",
        release_argv=["factory-lane", "release-owner", "HER-118"],
        ready_fd=None,
    ) is True

    assert identity_checks == [(456, "exact-start"), (456, "exact-start")]
    assert len(releases) == 1


def test_owner_reaper_unexpected_ack_error_is_raised_after_release(monkeypatch):
    from hermes_cli import kanban_db as kb

    ready_r, ready_w = os.pipe()
    alive = iter([True, False])
    identity_checks = []
    releases = []

    def fake_identity(pid, process_start_time):
        identity_checks.append((pid, process_start_time))
        return next(alive)

    def fake_run(argv, **kwargs):
        releases.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(kb, "_worker_identity_is_alive", fake_identity)
    monkeypatch.setattr(kb.os, "write", lambda _fd, _marker: (_ for _ in ()).throw(
        PermissionError("unexpected ack failure")
    ))
    monkeypatch.setattr(kb.subprocess, "run", fake_run)
    monkeypatch.setattr(kb.time, "sleep", lambda _interval: None)

    try:
        with pytest.raises(PermissionError, match="unexpected ack failure"):
            kb._wait_for_pid_exit_and_run_release(
                pid=789,
                process_start_time="exact-start",
                release_argv=["factory-lane", "release-owner", "HER-118"],
                ready_fd=ready_w,
            )
        assert identity_checks == [(789, "exact-start"), (789, "exact-start")]
        assert len(releases) == 1
        with pytest.raises(OSError):
            os.fstat(ready_w)
    finally:
        os.close(ready_r)


def test_owner_release_requires_exact_identity_and_removes_matching_claim(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    task = _task(kb)
    session = kb._kanban_worker_session_id(task)
    start = kb._worker_process_start_time(os.getpid())
    kb._claim_worker_owner(task, str(workspace), profile, os.getpid(), start, session)

    assert kb._release_worker_owner(
        task, str(workspace), profile, os.getpid(), "wrong-start", session
    ) is False
    assert (registry / "locks" / "HER-118" / "owner.json").exists()
    assert kb._release_worker_owner(
        task, str(workspace), profile, os.getpid(), start, session
    ) is True
    assert not (registry / "locks" / "HER-118" / "owner.json").exists()


def test_failed_spawn_release_retry_preserves_replacement_owner(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    task = _task(kb)
    session = kb._kanban_worker_session_id(task)
    pid = os.getpid()
    start = kb._worker_process_start_time(pid)
    kb._claim_worker_owner(task, str(workspace), profile, pid, start, session)
    real_release = kb._release_worker_owner
    replacement = {}
    calls = 0

    def release_then_replace(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert real_release(*args) is True
            foreign = subprocess.run(
                [
                    sys.executable, str(LANE), "--registry", str(registry),
                    "admit", "HER-118", "--mode", "owner", "--hard",
                    "--agent", "foreign", "--profile", "foreign",
                    "--session", "foreign-session", "--worktree", str(workspace),
                    "--owner-pid", str(pid), "--owner-start-time", start,
                ],
                capture_output=True,
                text=True,
            )
            assert foreign.returncode == 0, foreign.stderr
            replacement["bytes"] = (
                registry / "locks" / "HER-118" / "owner.json"
            ).read_bytes()
            return False
        return real_release(*args)

    monkeypatch.setattr(kb, "_release_worker_owner", release_then_replace)

    assert kb._release_worker_owner_after_failed_spawn(
        task, str(workspace), profile, pid, start, session,
    ) is False
    assert calls == 2
    assert (
        registry / "locks" / "HER-118" / "owner.json"
    ).read_bytes() == replacement["bytes"]


def test_owner_reaper_releases_exact_claim_after_worker_crash(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    task = _task(kb)
    session = kb._kanban_worker_session_id(task)
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        start = kb._worker_process_start_time(worker.pid)
        kb._claim_worker_owner(
            task, str(workspace), profile, worker.pid, start, session,
        )
        reaper_handle = kb._start_worker_owner_reaper(
            task=task,
            workspace=str(workspace),
            profile_home=profile,
            pid=worker.pid,
            process_start_time=start,
            session_id=session,
        )
        assert reaper_handle is not None
        reaper, ready_fd = reaper_handle
        kb._wait_for_worker_owner_reaper_ready(reaper, ready_fd)
        worker.kill()
        worker.wait(timeout=5)
        assert reaper.wait(timeout=5) == 0
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)

    assert not (registry / "locks" / "HER-118" / "owner.json").exists()


def test_real_hook_allows_owned_write_and_commit_but_blocks_foreign_commit(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    owned = tmp_path / "owned"
    foreign = tmp_path / "foreign"
    _init_repo(owned)
    _init_repo(foreign)
    task = _task(kb)
    session = kb._kanban_worker_session_id(task)
    kb._claim_worker_owner(
        task, str(owned), profile, os.getpid(),
        kb._worker_process_start_time(os.getpid()), session,
    )

    write_decision = _hook(
        registry, owned, "write_file",
        {"path": str(owned / "inside.txt"), "content": "inside\n"},
        session=session,
    )
    assert write_decision == {"decision": "allow"}
    (owned / "inside.txt").write_text("inside\n", encoding="utf-8")

    for command in ("git add inside.txt", "git commit -m inside"):
        verdict = _hook(
            registry, owned, "terminal", {"command": command, "workdir": str(owned)},
            session=session,
        )
        assert verdict == {"decision": "allow"}
        subprocess.run(command.split(), cwd=owned, check=True, capture_output=True, text=True)

    foreign_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=foreign, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    foreign_command = f"git -C {foreign} commit --allow-empty -m foreign"
    assert _hook(
        registry, owned, "terminal",
        {"command": foreign_command, "workdir": str(owned)}, session=session,
    )["decision"] == "block"
    foreign_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=foreign, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert foreign_after == foreign_before
    assert subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=owned, check=True,
        capture_output=True, text=True,
    ).stdout.strip() == "inside"


# ---------------------------------------------------------------------------
# S-B2 — an active owner must never coexist with an unarmed worker
# ---------------------------------------------------------------------------

def _spawn_with_fakes(monkeypatch, kb, captured, *, attest=None):
    """Wire the common spawn doubles and return the captured env/cmd dict."""
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])

    class FakeProc:
        pid = os.getpid()

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            captured["waited"] = timeout

    def fake_start(cmd, *, workspace, log_f, env):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env)
        return FakeProc()

    class FakeReaper:
        def poll(self):
            return None

    def fake_reaper(**kwargs):
        ready_r, ready_w = os.pipe()
        os.write(ready_w, b"R")
        os.close(ready_w)
        return FakeReaper(), ready_r

    monkeypatch.setattr(kb, "_start_kanban_worker_process", fake_start)
    monkeypatch.setattr(kb, "_start_worker_owner_reaper", fake_reaper)
    if attest is not None:
        monkeypatch.setattr(kb, "_attest_worker_admission_hook_armed", attest)
    return captured


@pytest.mark.parametrize("hostile", [
    {"HERMES_SAFE_MODE": "1"},
    {"HERMES_IGNORE_USER_CONFIG": "1"},
    {"HERMES_SAFE_MODE": "true", "HERMES_IGNORE_USER_CONFIG": "yes"},
])
def test_hook_disabling_env_never_reaches_the_gated_worker(
    monkeypatch, tmp_path, hostile,
):
    """A dispatcher started in safe mode must not spawn an unarmed worker."""
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    captured: dict = {}
    _spawn_with_fakes(monkeypatch, kb, captured, attest=lambda **kw: None)

    kb._default_spawn(_task(kb), str(workspace))

    env = captured["env"]
    assert "HERMES_SAFE_MODE" not in env
    assert "HERMES_IGNORE_USER_CONFIG" not in env


def test_gate_stays_shut_and_owner_released_when_hook_is_not_armed(
    monkeypatch, tmp_path,
):
    """Attestation failure must kill the wrapper and leave no active owner."""
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    workspace = tmp_path / "owned"
    _init_repo(workspace)
    captured: dict = {}

    def refuse(**kwargs):
        raise RuntimeError("worker did not arm the AI Factory admission hook")

    _spawn_with_fakes(monkeypatch, kb, captured, attest=refuse)

    with pytest.raises(RuntimeError, match="admission hook"):
        kb._default_spawn(_task(kb), str(workspace))

    assert captured.get("terminated") is True
    owner_path = registry / "locks" / "HER-118" / "owner.json"
    if owner_path.exists():
        assert json.loads(owner_path.read_text()).get("state") != "active"


def test_attestation_runs_the_real_worker_resolution_chain(monkeypatch, tmp_path):
    """The armed check must exercise the profile's real hook registration."""
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile)
    # Correctly configured profile: attestation succeeds.
    kb._attest_worker_admission_hook_armed(profile_home=profile, env=env)

    # Safe mode disarms hooks at registration time — must be refused.
    hostile = dict(env)
    hostile["HERMES_SAFE_MODE"] = "1"
    with pytest.raises(RuntimeError, match="admission hook"):
        kb._attest_worker_admission_hook_armed(profile_home=profile, env=hostile)


def test_attestation_refuses_a_profile_without_the_factory_hook(
    monkeypatch, tmp_path,
):
    root = tmp_path / ".hermes"
    registry = tmp_path / "registry"
    profile = _profile(root, registry)
    monkeypatch.setenv("HERMES_HOME", str(root))

    from hermes_cli import kanban_db as kb

    profile.joinpath("config.yaml").write_text(
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - matcher: '.*'\n"
        "      fail_closed: true\n"
        "      command: /bin/true\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(profile)
    with pytest.raises(RuntimeError, match="admission hook"):
        kb._attest_worker_admission_hook_armed(profile_home=profile, env=env)
