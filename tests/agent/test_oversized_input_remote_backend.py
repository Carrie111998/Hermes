"""Remote backend coverage for oversized input persistence."""

import json
import os
import shutil
import stat
import subprocess
import types
from pathlib import Path


class _FakeRemoteEnvironment:
    """Remote-shaped environment backed by real temp files for round trips."""

    def __init__(self, cwd: Path, temp_dir: Path):
        self.cwd = str(cwd)
        self._temp_dir = temp_dir
        self._sync_manager: object | None = None

    def get_temp_dir(self):
        return str(self._temp_dir)

    def execute(self, command, timeout=None, stdin_data=None, cwd=None, env=None):
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd or self.cwd,
            env=env,
            input=stdin_data,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {"output": proc.stdout + proc.stderr, "returncode": proc.returncode}


def _agent():
    return types.SimpleNamespace(
        _oversized_input_enabled=True,
        _oversized_input_char_threshold=10,
    )


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "get_hermes_home_override", lambda: None, raising=False
    )
    return home


def _register_remote_env(monkeypatch, task_id, env):
    from tools import file_tools, terminal_tool

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    environment_key = terminal_tool._resolve_container_task_id(task_id)
    with terminal_tool._env_lock:
        monkeypatch.setitem(terminal_tool._active_environments, environment_key, env)
    file_tools.clear_file_ops_cache(task_id)


def test_translated_path_round_trips_exact_bytes_through_task_file_tool(
    tmp_path, monkeypatch
):
    home = _isolate_home(monkeypatch, tmp_path)
    task_id = "remote-translation-task"
    remote_dir = tmp_path / "remote" / "pastes"
    remote_dir.mkdir(parents=True)
    env = _FakeRemoteEnvironment(tmp_path, tmp_path / "remote-tmp")

    class _SyncManager:
        def sync(self, force=False):
            assert force is True
            for source in (home / "pastes").glob("paste_*.txt"):
                shutil.copy2(source, remote_dir / source.name)

    env._sync_manager = _SyncManager()
    _register_remote_env(monkeypatch, task_id, env)
    monkeypatch.setattr(
        "tools.credential_files.to_agent_visible_cache_path",
        lambda host_path: str(remote_dir / Path(host_path).name),
    )

    from agent.oversized_paste import maybe_offload_oversized_message
    from tools.file_tools import read_file_tool

    payload = "REMOTE-TRANSLATED-é-" * 50
    message, _persisted, visible_path = maybe_offload_oversized_message(
        _agent(), payload, task_id=task_id
    )

    assert visible_path is not None
    assert str(visible_path) in message
    assert Path(visible_path).read_bytes() == payload.encode("utf-8")
    if os.name != "nt":
        assert stat.S_IMODE((home / "pastes").stat().st_mode) == 0o700
        host_file = next((home / "pastes").glob("paste_*.txt"))
        assert stat.S_IMODE(host_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(visible_path).stat().st_mode) == 0o600
    read_result = json.loads(read_file_tool(str(visible_path), task_id=task_id))
    assert read_result["content"] == f"1|{payload}"


def test_unreadable_translation_uses_remote_temp_and_round_trips(
    tmp_path, monkeypatch
):
    _isolate_home(monkeypatch, tmp_path)
    task_id = "remote-fallback-task"
    temp_dir = tmp_path / "backend-temp"
    env = _FakeRemoteEnvironment(tmp_path, temp_dir)
    _register_remote_env(monkeypatch, task_id, env)
    monkeypatch.setattr(
        "tools.credential_files.to_agent_visible_cache_path",
        lambda _host_path: "/unmounted/hermes/pastes/input.txt",
    )

    from agent.oversized_paste import maybe_offload_oversized_message
    from tools.file_tools import read_file_tool

    payload = "REMOTE-FALLBACK-ß-" * 50
    message, _persisted, visible_path = maybe_offload_oversized_message(
        _agent(), payload, task_id=task_id
    )

    assert visible_path is not None
    assert str(visible_path).startswith(str(temp_dir / "hermes-results"))
    assert str(visible_path) in message
    assert Path(visible_path).read_bytes() == payload.encode("utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(Path(visible_path).parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(visible_path).stat().st_mode) == 0o600
    read_result = json.loads(read_file_tool(str(visible_path), task_id=task_id))
    assert read_result["content"] == f"1|{payload}"
