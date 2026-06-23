"""Regression tests for terminal runtime cache invalidation."""

import json
import threading
from unittest.mock import MagicMock, patch


def _make_env_config(**overrides):
    config = {
        "env_type": "local",
        "timeout": 180,
        "cwd": "/tmp",
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "docker_mount_cwd_to_workspace": False,
        "docker_volumes": [],
        "docker_forward_env": [],
        "docker_env": {},
        "docker_run_as_host_user": False,
        "docker_network": True,
        "docker_extra_args": [],
        "docker_persist_across_processes": True,
        "docker_orphan_reaper": True,
        "container_cpu": 1,
        "container_memory": 5120,
        "container_disk": 51200,
        "container_persistent": True,
        "ssh_host": "",
        "ssh_user": "",
        "ssh_port": 22,
        "ssh_key": "",
        "ssh_persistent": True,
        "local_persistent": False,
    }
    config.update(overrides)
    return config


def _reset_terminal_cache(tt):
    tt._active_environments.clear()
    tt._last_activity.clear()
    tt._environment_lease_states.clear()
    tt._creation_locks.clear()
    tt._task_env_overrides.clear()
    with tt._session_cwd_lock:
        tt._session_cwd.clear()


def _reset_file_ops_cache(ft):
    ft._file_ops_cache.clear()


def test_terminal_env_cache_invalidates_when_backend_signature_changes(monkeypatch):
    """A session cached under SSH must not be reused after runtime switches local."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "session-1"
    ssh_config = _make_env_config(
        env_type="ssh",
        cwd="~",
        ssh_host="old-ssh.example.test",
        ssh_user="agent-user",
    )
    old_env = MagicMock(name="old_ssh_env")
    setattr(
        old_env,
        tt._ENV_SIGNATURE_ATTR,
        tt._terminal_env_signature(ssh_config, task_id=task_id),
    )
    tt._active_environments[task_id] = old_env
    tt._last_activity[task_id] = 1

    local_config = _make_env_config(
        env_type="local",
        cwd="/workspace/local-project",
    )
    new_env = MagicMock(name="new_local_env")
    new_env.execute.return_value = {"output": "local-host\n", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value=local_config), \
         patch("tools.terminal_tool._create_environment", return_value=new_env) as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(tt.terminal_tool("hostname", task_id=task_id))

    assert result["output"] == "local-host"
    old_env.cleanup.assert_called_once_with()
    create_env.assert_called_once()
    assert create_env.call_args.kwargs["env_type"] == "local"
    assert create_env.call_args.kwargs["cwd"] == "/workspace/local-project"
    assert tt._active_environments["default"] is new_env
    assert len(tt._environment_lease_states) == 1
    assert not next(iter(tt._environment_lease_states.values())).retired
    assert getattr(new_env, tt._ENV_SIGNATURE_ATTR)["env_type"] == "local"
    assert getattr(new_env, tt._ENV_SIGNATURE_ATTR)["ssh_host"] == ""

    _reset_terminal_cache(tt)


def test_terminal_env_cache_reuses_when_signature_matches(monkeypatch):
    """Matching runtime signature still preserves the intended per-session cache."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "session-2"
    local_config = _make_env_config(env_type="local", cwd="/tmp/project")
    cached_env = MagicMock(name="cached_local_env")
    cached_env.execute.return_value = {"output": "ok\n", "returncode": 0}
    setattr(
        cached_env,
        tt._ENV_SIGNATURE_ATTR,
        tt._terminal_env_signature(local_config, task_id="default"),
    )
    tt._active_environments["default"] = cached_env
    tt._last_activity["default"] = 1

    with patch("tools.terminal_tool._get_env_config", return_value=local_config), \
         patch("tools.terminal_tool._create_environment") as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(tt.terminal_tool("echo ok", task_id=task_id))

    assert result["output"] == "ok"
    create_env.assert_not_called()
    cached_env.cleanup.assert_not_called()
    assert tt._active_environments["default"] is cached_env

    _reset_terminal_cache(tt)


def test_terminal_env_cache_reuses_raw_session_key_when_signature_matches(monkeypatch):
    """A raw-key cached session remains reusable after cwd-only task collapse."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "session-raw"
    local_config = _make_env_config(env_type="local", cwd="/tmp/raw-project")
    cached_env = MagicMock(name="cached_raw_local")
    cached_env.execute.return_value = {"output": "raw-ok\n", "returncode": 0}
    setattr(
        cached_env,
        tt._ENV_SIGNATURE_ATTR,
        tt._terminal_env_signature(local_config, task_id="default"),
    )
    tt._active_environments[task_id] = cached_env
    tt._last_activity[task_id] = 1

    with patch("tools.terminal_tool._get_env_config", return_value=local_config), \
         patch("tools.terminal_tool._create_environment") as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(tt.terminal_tool("echo raw-ok", task_id=task_id))

    assert result["output"] == "raw-ok"
    create_env.assert_not_called()
    cached_env.cleanup.assert_not_called()
    assert tt._active_environments[task_id] is cached_env

    _reset_terminal_cache(tt)


def test_effective_stale_entry_falls_back_to_compatible_raw_session(monkeypatch):
    """Retiring an incompatible effective entry must still try raw fallback."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "raw-fallback-session"
    old_config = _make_env_config(env_type="local", cwd="/tmp/old-default")
    current_config = _make_env_config(env_type="local", cwd="/tmp/current")
    stale_default = MagicMock(name="stale_default_env")
    compatible_raw = MagicMock(name="compatible_raw_env")
    setattr(
        stale_default,
        tt._ENV_SIGNATURE_ATTR,
        tt._terminal_env_signature(old_config, task_id="default"),
    )
    current_signature = tt._terminal_env_signature(current_config, task_id="default")
    setattr(compatible_raw, tt._ENV_SIGNATURE_ATTR, current_signature)
    tt._active_environments["default"] = stale_default
    tt._active_environments[task_id] = compatible_raw
    tt._last_activity["default"] = 1
    tt._last_activity[task_id] = 1

    lease = tt._acquire_active_environment_if_compatible(
        "default",
        current_signature,
        raw_task_id=task_id,
    )

    assert lease is not None
    assert getattr(lease, "env") is compatible_raw
    assert "default" not in tt._active_environments
    assert tt._active_environments[task_id] is compatible_raw
    stale_default.cleanup.assert_called_once_with()
    compatible_raw.cleanup.assert_not_called()
    lease.release()
    _reset_terminal_cache(tt)


def test_file_ops_cache_invalidates_when_backend_signature_changes(monkeypatch):
    """File tools must not keep ShellFileOperations for a stale backend."""
    from tools import file_tools as ft
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    _reset_file_ops_cache(ft)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    ssh_config = _make_env_config(
        env_type="ssh",
        cwd="~",
        ssh_host="old-ssh.example.test",
        ssh_user="agent-user",
    )
    old_backend = MagicMock(name="old_remote")
    setattr(old_backend, "cwd", "~")
    setattr(
        old_backend,
        tt._ENV_SIGNATURE_ATTR,
        tt._terminal_env_signature(ssh_config, task_id="default"),
    )
    tt._active_environments["default"] = old_backend
    tt._last_activity["default"] = 1
    old_file_ops = ft.ShellFileOperations(old_backend)
    ft._file_ops_cache["default"] = old_file_ops

    local_config = _make_env_config(env_type="local", cwd="/workspace/local-project")
    new_backend = MagicMock(name="new_local")
    setattr(new_backend, "cwd", "/workspace/local-project")

    with patch("tools.terminal_tool._get_env_config", return_value=local_config), \
         patch("tools.terminal_tool._create_environment", return_value=new_backend) as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"):
        file_ops = ft._get_file_ops("session-file")

    old_backend.cleanup.assert_called_once_with()
    create_env.assert_called_once()
    assert create_env.call_args.kwargs["env_type"] == "local"
    assert create_env.call_args.kwargs["cwd"] == "/workspace/local-project"
    assert getattr(file_ops, "env") is new_backend
    assert getattr(ft._file_ops_cache["default"], "env") is new_backend
    assert tt._active_environments["default"] is new_backend
    assert tt.get_session_cwd("session-file") is None
    assert len(tt._environment_lease_states) == 1
    assert not next(iter(tt._environment_lease_states.values())).retired
    _reset_file_ops_cache(ft)
    _reset_terminal_cache(tt)


def test_file_ops_reresolves_runtime_after_waiting_for_old_creation_lock(monkeypatch):
    """A stale waiter must not publish an env for the pre-wait effective key."""
    from tools import file_tools as ft
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    _reset_file_ops_cache(ft)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "isolated-file-task"
    old_config = _make_env_config(env_type="local", cwd="/tmp/old-generation")
    current_config = _make_env_config(
        env_type="docker",
        cwd="/workspace/current-generation",
        docker_image="python:3.12",
    )
    first_config_read = threading.Event()
    release_default_lock = threading.Event()
    config_box = {"value": old_config, "reads": 0}

    def current_config_reader():
        config_box["reads"] += 1
        if config_box["reads"] == 1:
            first_config_read.set()
        return config_box["value"]

    default_lock = threading.Lock()
    default_lock.acquire()
    with tt._creation_locks_lock:
        tt._creation_locks["default"] = default_lock

    compatible_env = MagicMock(name="compatible_current_env")
    compatible_env.cwd = "/workspace/current-generation"
    result_box = {}

    def worker():
        try:
            result_box["ops"] = ft._get_file_ops(task_id)
        finally:
            release_default_lock.set()

    thread = threading.Thread(target=worker)
    with patch("tools.terminal_tool._get_env_config", side_effect=current_config_reader), \
         patch("tools.terminal_tool._create_environment") as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"):
        thread.start()
        assert first_config_read.wait(5)
        tt.register_task_env_overrides(task_id, {"docker_image": "python:3.12"})
        current_runtime = tt.resolve_terminal_runtime_identity(
            current_config,
            raw_task_id=task_id,
        )
        setattr(compatible_env, tt._ENV_SIGNATURE_ATTR, current_runtime["signature"])
        tt._store_active_environment(task_id, compatible_env, current_runtime["signature"])
        config_box["value"] = current_config
        default_lock.release()
        thread.join(timeout=5)

    assert release_default_lock.is_set()
    assert not thread.is_alive()
    assert getattr(result_box["ops"], "env") is compatible_env
    assert task_id in ft._file_ops_cache
    assert "default" not in ft._file_ops_cache
    create_env.assert_not_called()
    _reset_file_ops_cache(ft)
    _reset_terminal_cache(tt)


def test_execute_code_reresolves_runtime_after_waiting_for_old_creation_lock(monkeypatch):
    """execute_code must also switch to the current effective creation lock."""
    from tools import code_execution_tool as cet
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "isolated-code-task"
    old_config = _make_env_config(env_type="local", cwd="/tmp/old-generation")
    current_config = _make_env_config(
        env_type="docker",
        cwd="/workspace/current-generation",
        docker_image="python:3.12",
    )
    first_config_read = threading.Event()
    config_box = {"value": old_config, "reads": 0}

    def current_config_reader():
        config_box["reads"] += 1
        if config_box["reads"] == 1:
            first_config_read.set()
        return config_box["value"]

    default_lock = threading.Lock()
    default_lock.acquire()
    with tt._creation_locks_lock:
        tt._creation_locks["default"] = default_lock

    compatible_env = MagicMock(name="compatible_code_env")
    result_box = {}

    def worker():
        env, env_type, lease = cet._get_or_create_env_lease(task_id)
        try:
            result_box["env"] = env
            result_box["env_type"] = env_type
        finally:
            lease.release()

    thread = threading.Thread(target=worker)
    with patch("tools.terminal_tool._get_env_config", side_effect=current_config_reader), \
         patch("tools.terminal_tool._create_environment") as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"):
        thread.start()
        assert first_config_read.wait(5)
        tt.register_task_env_overrides(task_id, {"docker_image": "python:3.12"})
        current_runtime = tt.resolve_terminal_runtime_identity(
            current_config,
            raw_task_id=task_id,
        )
        setattr(compatible_env, tt._ENV_SIGNATURE_ATTR, current_runtime["signature"])
        tt._store_active_environment(task_id, compatible_env, current_runtime["signature"])
        config_box["value"] = current_config
        default_lock.release()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert result_box == {"env": compatible_env, "env_type": "docker"}
    create_env.assert_not_called()
    _reset_terminal_cache(tt)


def test_terminal_tool_reresolves_runtime_after_waiting_for_old_creation_lock(monkeypatch):
    """terminal_tool must retry under the current effective creation lock."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    task_id = "isolated-terminal-task"
    old_config = _make_env_config(env_type="local", cwd="/tmp/old-generation")
    current_config = _make_env_config(
        env_type="docker",
        cwd="/workspace/current-generation",
        docker_image="python:3.12",
    )
    first_config_read = threading.Event()
    config_box = {"value": old_config, "reads": 0}

    def current_config_reader():
        config_box["reads"] += 1
        if config_box["reads"] == 1:
            first_config_read.set()
        return config_box["value"]

    default_lock = threading.Lock()
    default_lock.acquire()
    with tt._creation_locks_lock:
        tt._creation_locks["default"] = default_lock

    compatible_backend = MagicMock(name="compatible_terminal_backend")
    compatible_backend.execute.return_value = {
        "output": "ok\n",
        "returncode": 0,
        "cwd": "/workspace/current-generation",
    }
    result_box = {}

    def worker():
        result_box["result"] = json.loads(
            tt.terminal_tool("printf ok", task_id=task_id)
        )

    thread = threading.Thread(target=worker)
    with patch("tools.terminal_tool._get_env_config", side_effect=current_config_reader), \
         patch("tools.terminal_tool._create_environment") as create_backend, \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        thread.start()
        assert first_config_read.wait(5)
        tt.register_task_env_overrides(task_id, {"docker_image": "python:3.12"})
        current_runtime = tt.resolve_terminal_runtime_identity(
            current_config,
            raw_task_id=task_id,
        )
        setattr(compatible_backend, tt._ENV_SIGNATURE_ATTR, current_runtime["signature"])
        tt._store_active_environment(task_id, compatible_backend, current_runtime["signature"])
        config_box["value"] = current_config
        default_lock.release()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert result_box["result"]["output"] == "ok"
    assert compatible_backend.execute.called
    create_backend.assert_not_called()
    _reset_terminal_cache(tt)


def test_file_ops_signature_replacement_preserves_existing_raw_session_cwd(monkeypatch):
    """Old backend cwd must not overwrite the raw session's current cwd."""
    from tools import file_tools as ft
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    _reset_file_ops_cache(ft)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    ssh_config = _make_env_config(
        env_type="ssh",
        cwd="~",
        ssh_host="old-ssh.example.test",
        ssh_user="agent-user",
    )
    old_backend = MagicMock(name="old_remote")
    setattr(old_backend, "cwd", "~")
    setattr(
        old_backend,
        tt._ENV_SIGNATURE_ATTR,
        tt._terminal_env_signature(ssh_config, task_id="default"),
    )
    tt._active_environments["default"] = old_backend
    tt._last_activity["default"] = 1
    ft._file_ops_cache["default"] = ft.ShellFileOperations(old_backend)
    tt.record_session_cwd("session-file", "/workspace/current")

    local_config = _make_env_config(env_type="local", cwd="/workspace/local-project")
    new_backend = MagicMock(name="new_local")
    setattr(new_backend, "cwd", "/workspace/current")

    with patch("tools.terminal_tool._get_env_config", return_value=local_config), \
         patch("tools.terminal_tool._create_environment", return_value=new_backend) as create_env, \
         patch("tools.terminal_tool._start_cleanup_thread"):
        ft._get_file_ops("session-file")

    assert tt.get_session_cwd("session-file") == "/workspace/current"
    assert create_env.call_args.kwargs["cwd"] == "/workspace/current"
    old_backend.cleanup.assert_called_once_with()
    _reset_file_ops_cache(ft)
    _reset_terminal_cache(tt)


def test_docker_creation_inputs_affect_runtime_signature(monkeypatch):
    """Docker creation knobs that change runtime compatibility are signed."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    monkeypatch.setenv("HERMES_HOME", "/tmp/hermes-default")

    base_config = _make_env_config(
        env_type="docker",
        cwd="/workspace",
        docker_image="python:3.12",
    )
    base = tt.resolve_terminal_runtime_identity(base_config)["signature"]

    for key, value in {
        "docker_extra_args": ["--add-host=host.docker.internal:host-gateway"],
        "docker_network": False,
        "docker_persist_across_processes": False,
        "docker_orphan_reaper": False,
    }.items():
        changed_config = _make_env_config(
            env_type="docker",
            cwd="/workspace",
            docker_image="python:3.12",
            **{key: value},
        )
        changed = tt.resolve_terminal_runtime_identity(changed_config)["signature"]
        assert changed != base
        assert changed[key] != base[key]

    runtime = tt.resolve_terminal_runtime_identity(
        _make_env_config(
            env_type="docker",
            cwd="/workspace",
            docker_image="python:3.12",
            docker_extra_args=["--init"],
            docker_network=False,
            docker_persist_across_processes=False,
            docker_orphan_reaper=False,
        )
    )
    container_config = runtime["container_config"]
    assert container_config["docker_extra_args"] == ["--init"]
    assert container_config["docker_network"] is False
    assert container_config["docker_persist_across_processes"] is False
    assert container_config["docker_orphan_reaper"] is False


def test_shared_cwd_only_overrides_do_not_change_runtime_signature(monkeypatch):
    """Per-session cwd on the shared env is an operation cwd, not identity."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    config = _make_env_config(env_type="local", cwd="/workspace/default")
    tt.register_task_env_overrides("sess-a", {"cwd": "/workspace/a"})
    tt.register_task_env_overrides("sess-b", {"cwd": "/workspace/b"})

    runtime_a = tt.resolve_terminal_runtime_identity(config, raw_task_id="sess-a")
    runtime_b = tt.resolve_terminal_runtime_identity(config, raw_task_id="sess-b")

    assert runtime_a["effective_task_id"] == "default"
    assert runtime_b["effective_task_id"] == "default"
    assert runtime_a["cwd"] == "/workspace/a"
    assert runtime_b["cwd"] == "/workspace/b"
    assert runtime_a["signature"] == runtime_b["signature"]
    assert runtime_a["signature"]["cwd"] == "/workspace/default"

    _reset_terminal_cache(tt)


def test_runtime_creation_cwd_prefers_current_session_record_over_override(monkeypatch):
    """Backend replacement must seed from the live session cwd after a cd."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    config = _make_env_config(env_type="local", cwd="/workspace/default")
    tt.register_task_env_overrides("sess-live", {"cwd": "/workspace/initial"})
    tt.record_session_cwd("sess-live", "/workspace/current")

    runtime = tt.resolve_terminal_runtime_identity(config, raw_task_id="sess-live")

    assert runtime["effective_task_id"] == "default"
    assert runtime["cwd"] == "/workspace/current"
    assert runtime["signature"]["cwd"] == "/workspace/default"

    _reset_terminal_cache(tt)


def test_isolated_override_cwd_still_changes_runtime_signature(monkeypatch):
    """Isolation-keyed task overrides can include cwd in their env identity."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    config = _make_env_config(
        env_type="docker",
        cwd="/workspace/default",
        docker_image="python:3.12",
    )
    tt.register_task_env_overrides(
        "rollout-a",
        {"docker_image": "custom:a", "cwd": "/workspace/a"},
    )

    runtime = tt.resolve_terminal_runtime_identity(config, raw_task_id="rollout-a")

    assert runtime["effective_task_id"] == "rollout-a"
    assert runtime["signature"]["cwd"] == "/workspace/a"
    assert runtime["signature"]["image"] == "custom:a"

    _reset_terminal_cache(tt)


def test_ssh_key_fingerprint_changes_when_key_material_changes(tmp_path):
    """Rotating a key at the same path invalidates cached SSH environments."""
    from tools import terminal_tool as tt

    key_path = tmp_path / "ssh-fixture-key"
    key_path.write_text("first-key\n", encoding="utf-8")
    config = _make_env_config(env_type="ssh", ssh_key=str(key_path))
    first = tt.resolve_terminal_runtime_identity(config)["signature"]

    key_path.write_text("second-key\n", encoding="utf-8")
    second = tt.resolve_terminal_runtime_identity(config)["signature"]

    assert first["ssh_key_present"] == "true"
    assert second["ssh_key_present"] == "true"
    assert first["ssh_key_fingerprint"] != second["ssh_key_fingerprint"]
    assert second["ssh_key_fingerprint"] not in str(tt._safe_signature_for_log(second))
    assert str(key_path) not in str(tt._safe_signature_for_log(second))


def test_ssh_key_fingerprint_value_is_not_logged_when_signature_changes(tmp_path, caplog):
    """The SSH key fingerprint invalidates compatibility without entering logs."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    key_path = tmp_path / "ssh-fixture-key"
    key_path.write_text("first-key\n", encoding="utf-8")
    old_config = _make_env_config(env_type="ssh", ssh_key=str(key_path))
    old_env = MagicMock(name="old_ssh_env")
    old_signature = tt._terminal_env_signature(old_config, task_id="default")
    setattr(old_env, tt._ENV_SIGNATURE_ATTR, old_signature)
    tt._active_environments["default"] = old_env

    key_path.write_text("second-key\n", encoding="utf-8")
    new_signature = tt._terminal_env_signature(
        _make_env_config(env_type="ssh", ssh_key=str(key_path)),
        task_id="default",
    )

    with caplog.at_level("WARNING", logger="tools.terminal_tool"):
        assert tt._get_active_environment_if_compatible("default", new_signature) is None

    log_text = caplog.text
    assert "ssh_key_fingerprint" in log_text
    assert old_signature["ssh_key_fingerprint"] not in log_text
    assert new_signature["ssh_key_fingerprint"] not in log_text
    assert str(key_path) not in log_text

    _reset_terminal_cache(tt)


def test_safe_signature_log_redacts_image_docker_args_and_env_secrets():
    from tools import terminal_tool as tt

    signature = tt._terminal_env_signature(
        _make_env_config(
            env_type="docker",
            docker_image="registry.example.test/user:secret-token@repo/image:latest",
            docker_extra_args=["--env", "API_TOKEN=supersecret", "--env-file", "/tmp/secret-file"],
            docker_env={"PASSWORD": "supersecret"},
            docker_forward_env=["SECRET_TOKEN"],
        ),
        task_id="default",
    )

    safe = tt._safe_signature_for_log(signature)
    safe_text = str(safe)

    assert "supersecret" not in safe_text
    assert "secret-token" not in safe_text
    assert "API_TOKEN" not in safe_text
    assert "PASSWORD" not in safe_text
    assert "/tmp/secret-file" not in safe_text
    assert "image_fingerprint" in safe
    assert "docker_extra_args_fingerprint" in safe
    assert "docker_env_fingerprint" in safe


def test_terminal_retirement_waits_for_inflight_terminal_operation(monkeypatch):
    """Signature retirement must not clean a backend while terminal_tool uses it."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    config = _make_env_config(env_type="local", cwd="/tmp/project-a")
    new_config = _make_env_config(env_type="local", cwd="/tmp/project-b")
    started = threading.Event()
    release = threading.Event()
    old_env = MagicMock(name="leased_terminal_env")

    def _execute(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return {"output": "done\n", "returncode": 0}

    old_env.execute.side_effect = _execute
    setattr(old_env, tt._ENV_SIGNATURE_ATTR, tt._terminal_env_signature(config, task_id="default"))
    tt._active_environments["default"] = old_env

    result_box = {}

    def _run():
        result_box["result"] = json.loads(tt.terminal_tool("echo done", task_id="session-a"))

    worker = threading.Thread(target=_run)
    with patch("tools.terminal_tool._get_env_config", return_value=config), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        worker.start()
        assert started.wait(5)

        new_signature = tt._terminal_env_signature(new_config, task_id="default")
        assert tt._acquire_active_environment_if_compatible("default", new_signature) is None
        old_env.cleanup.assert_not_called()

        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert result_box["result"]["output"] == "done"
    old_env.cleanup.assert_called_once_with()
    assert tt._environment_lease_states == {}
    _reset_terminal_cache(tt)


def test_background_process_lease_cleans_after_registry_completion():
    """Env-backed background processes keep the backend leased until completion."""
    from tools import terminal_tool as tt
    from tools.process_registry import ProcessRegistry

    _reset_terminal_cache(tt)
    config = _make_env_config(env_type="ssh", ssh_host="host", ssh_user="user")
    old_env = MagicMock(name="background_env")
    old_env.get_temp_dir.return_value = "/tmp"
    old_env.execute.return_value = {"output": "1234\n", "returncode": 0}
    setattr(old_env, tt._ENV_SIGNATURE_ATTR, tt._terminal_env_signature(config, task_id="default"))
    tt._store_active_environment("default", old_env, tt._terminal_env_signature(config, task_id="default"))
    lease = tt._acquire_active_environment_if_compatible(
        "default", tt._terminal_env_signature(config, task_id="default")
    )

    registry = ProcessRegistry()
    with patch("tools.process_registry.threading.Thread") as thread_cls, \
         patch.object(registry, "_write_checkpoint"):
        thread_cls.return_value = MagicMock()
        session = registry.spawn_via_env(old_env, "sleep 1", environment_lease=lease)

    tt.cleanup_vm("default")
    old_env.cleanup.assert_not_called()

    session.exit_code = 0
    session.exited = True
    registry._move_to_finished(session)

    old_env.cleanup.assert_called_once_with()
    assert tt._environment_lease_states == {}
    _reset_terminal_cache(tt)


def test_file_ops_lease_defers_cleanup_until_operation_release():
    """File-operation acquisition holds the backend lease across use."""
    from tools import file_tools as ft
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    _reset_file_ops_cache(ft)
    config = _make_env_config(env_type="local", cwd="/tmp/file-a")
    next_config = _make_env_config(env_type="local", cwd="/tmp/file-b")
    old_env = MagicMock(name="file_env")
    setattr(old_env, tt._ENV_SIGNATURE_ATTR, tt._terminal_env_signature(config, task_id="default"))
    tt._store_active_environment("default", old_env, tt._terminal_env_signature(config, task_id="default"))

    with patch("tools.terminal_tool._get_env_config", return_value=config):
        with ft._leased_file_ops("default"):
            assert tt._acquire_active_environment_if_compatible(
                "default", tt._terminal_env_signature(next_config, task_id="default")
            ) is None
            old_env.cleanup.assert_not_called()

    old_env.cleanup.assert_called_once_with()
    assert tt._environment_lease_states == {}
    _reset_file_ops_cache(ft)
    _reset_terminal_cache(tt)


def test_file_ops_bind_raw_session_cwd_during_concurrent_shared_env_use(monkeypatch):
    """Collapsed CWD-only sessions must not inherit the shared backend cwd."""
    from tools import file_tools as ft
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    _reset_file_ops_cache(ft)
    config = _make_env_config(env_type="local", cwd="/workspace/default")
    tt.register_task_env_overrides("sess-a", {"cwd": "/workspace/a"})
    tt.register_task_env_overrides("sess-b", {"cwd": "/workspace/b"})

    a_entered = threading.Event()
    b_finished = threading.Event()
    calls = []

    class RecordingBackend:
        cwd = "/workspace/shared-stale"

        def execute(self, command, **kwargs):
            calls.append((command, kwargs.get("cwd")))
            if command == "probe-a":
                a_entered.set()
                assert b_finished.wait(5)
            elif command == "probe-b":
                assert a_entered.wait(5)
                self.cwd = "/workspace/b"
                b_finished.set()
            return {"output": "ok\n", "returncode": 0}

    backend = RecordingBackend()
    signature = tt._terminal_env_signature(config, task_id="default")
    tt._store_active_environment("default", backend, signature)
    results = {}

    def run_probe(task_id, command):
        with ft._leased_file_ops(task_id) as ops:
            results[task_id] = ops._exec(command).stdout

    with patch("tools.terminal_tool._get_env_config", return_value=config):
        thread_a = threading.Thread(target=run_probe, args=("sess-a", "probe-a"))
        thread_b = threading.Thread(target=run_probe, args=("sess-b", "probe-b"))
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert results == {"sess-a": "ok\n", "sess-b": "ok\n"}
    assert ("probe-a", "/workspace/a") in calls
    assert ("probe-b", "/workspace/b") in calls
    assert ("probe-a", "/workspace/shared-stale") not in calls
    _reset_file_ops_cache(ft)
    _reset_terminal_cache(tt)


def test_execute_code_remote_lease_defers_cleanup_until_script_finishes(monkeypatch):
    """Remote execute_code keeps the backend leased through script and RPC cleanup."""
    from tools import code_execution_tool as cet
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    config = _make_env_config(env_type="ssh", ssh_host="host", ssh_user="user")
    signature = tt._terminal_env_signature(config, task_id="default")
    started = threading.Event()
    release = threading.Event()

    class FakeEnv:
        def __init__(self):
            self.cleanup = MagicMock()
            self.commands = []

        def get_temp_dir(self):
            return "/tmp"

        def execute(self, command, **kwargs):
            self.commands.append((command, kwargs))
            if "command -v python3" in command:
                return {"output": "OK\n", "returncode": 0}
            if "python3 script.py" in command:
                started.set()
                assert release.wait(5)
                return {"output": "remote-ok\n", "returncode": 0}
            return {"output": "", "returncode": 0}

    env = FakeEnv()
    setattr(env, tt._ENV_SIGNATURE_ATTR, signature)
    tt._store_active_environment("default", env, signature)

    result_box = {}

    def _run():
        result_box["result"] = json.loads(cet._execute_remote("print('remote-ok')", "default", []))

    worker = threading.Thread(target=_run)
    with patch("tools.terminal_tool._get_env_config", return_value=config), \
         patch("tools.code_execution_tool._load_config", return_value={"timeout": 30, "max_tool_calls": 5}):
        worker.start()
        assert started.wait(5)
        tt.cleanup_vm("default")
        env.cleanup.assert_not_called()
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert result_box["result"]["status"] == "success"
    assert result_box["result"]["output"] == "remote-ok\n"
    env.cleanup.assert_called_once_with()
    assert tt._environment_lease_states == {}
    _reset_terminal_cache(tt)


def test_repeated_signature_replacements_do_not_accumulate_retired_state():
    """No-lease replacements clean immediately and leave bounded lifecycle state."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    for idx in range(25):
        config = _make_env_config(env_type="local", cwd=f"/tmp/project-{idx}")
        next_config = _make_env_config(env_type="local", cwd=f"/tmp/project-{idx + 1}")
        env = MagicMock(name=f"env_{idx}")
        setattr(env, tt._ENV_SIGNATURE_ATTR, tt._terminal_env_signature(config, task_id="default"))
        tt._active_environments["default"] = env
        tt._last_activity["default"] = 1

        assert tt._acquire_active_environment_if_compatible(
            "default", tt._terminal_env_signature(next_config, task_id="default")
        ) is None

        env.cleanup.assert_called_once_with()
        assert tt._active_environments == {}
        assert tt._environment_lease_states == {}

    _reset_terminal_cache(tt)


def test_explicit_retired_cleanup_drains_releaseable_states():
    """Explicit retired cleanup drains releaseable retired lifecycle state."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    env = MagicMock(name="retired_env")
    state = tt._EnvironmentLeaseState("default", env)
    state.retired = True
    tt._environment_lease_states[id(env)] = state

    cleaned = tt._cleanup_retired_environments()

    assert cleaned == [env]
    env.cleanup.assert_called_once_with()
    assert tt._environment_lease_states == {}
    _reset_terminal_cache(tt)


def test_cleanup_worker_retries_transient_cleanup_failed_retired_state():
    """Periodic cleanup should retry a failed retired backend and drain it."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    env = MagicMock(name="retired_retry_env")
    state = tt._EnvironmentLeaseState("default", env)
    state.retired = True
    state.cleanup_failed = True
    state.cleanup_attempts = 1
    tt._environment_lease_states[id(env)] = state

    def stop_after_one_sleep(_seconds):
        tt._cleanup_running = False

    try:
        tt._cleanup_running = True
        with patch("tools.terminal_tool._get_env_config", return_value={"lifetime_seconds": 999}), \
             patch("tools.terminal_tool._cleanup_inactive_envs"), \
             patch("tools.terminal_tool.time.sleep", side_effect=stop_after_one_sleep):
            tt._cleanup_thread_worker()
    finally:
        tt._cleanup_running = False

    env.cleanup.assert_called_once_with()
    assert tt._environment_lease_states == {}
    _reset_terminal_cache(tt)


def test_cleanup_worker_does_not_retry_retired_state_past_budget():
    """Periodic cleanup retries are bounded for persistent cleanup failures."""
    from tools import terminal_tool as tt

    _reset_terminal_cache(tt)
    env = MagicMock(name="retired_budget_env")
    state = tt._EnvironmentLeaseState("default", env)
    state.retired = True
    state.cleanup_failed = True
    state.cleanup_attempts = tt._RETIRED_CLEANUP_WORKER_MAX_ATTEMPTS
    tt._environment_lease_states[id(env)] = state

    def stop_after_one_sleep(_seconds):
        tt._cleanup_running = False

    try:
        tt._cleanup_running = True
        with patch("tools.terminal_tool._get_env_config", return_value={"lifetime_seconds": 999}), \
             patch("tools.terminal_tool._cleanup_inactive_envs"), \
             patch("tools.terminal_tool.time.sleep", side_effect=stop_after_one_sleep):
            tt._cleanup_thread_worker()
    finally:
        tt._cleanup_running = False

    env.cleanup.assert_not_called()
    assert tt._environment_lease_states[id(env)] is state
    _reset_terminal_cache(tt)
