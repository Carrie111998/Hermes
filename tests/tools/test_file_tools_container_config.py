"""Tests for container config propagation outside terminal commands."""

from unittest.mock import patch, MagicMock

import tools.code_execution_tool as code_execution_tool
import tools.file_tools as file_tools
import tools.terminal_tool as terminal_tool


def _make_env_config(**overrides):
    base = {
        "env_type": "docker",
        "docker_image": "test-image:latest",
        "singularity_image": "docker://test",
        "modal_image": "test",
        "daytona_image": "test",
        "cwd": "/workspace",
        "host_cwd": None,
        "timeout": 180,
        "container_cpu": 2,
        "container_memory": 4096,
        "container_disk": 20480,
        "container_persistent": False,
        "docker_volumes": [],
        "docker_mount_cwd_to_workspace": True,
        "docker_forward_env": ["MY_SECRET", "API_KEY"],
        "docker_env": {"JAVA_HOME": "/opt/jdk", "GITHUB_ACTOR": "hermes"},
        "docker_network": False,
    }
    base.update(overrides)
    return base


class TestFileToolsContainerConfig:
    def _run(self, env_config, task_id, task_env_overrides=None):
        captured = {}
        mock_env = MagicMock()

        def fake_create_env(**kwargs):
            captured.update(kwargs)
            return mock_env

        with patch("tools.terminal_tool._get_env_config", return_value=env_config), \
             patch("tools.terminal_tool._task_env_overrides", task_env_overrides or {}), \
             patch("tools.terminal_tool._active_environments", {}), \
             patch("tools.terminal_tool._creation_locks", {}), \
             patch("tools.terminal_tool._creation_locks_lock", __import__("threading").Lock()), \
             patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
             patch("tools.terminal_tool._start_cleanup_thread"), \
             patch("tools.terminal_tool._check_disk_usage_warning"), \
             patch("tools.file_tools._file_ops_cache", {}), \
             patch("tools.file_tools._file_ops_lock", __import__("threading").Lock()):
            file_tools._get_file_ops(task_id)

        return captured

    def test_docker_mount_cwd_to_workspace_passed(self):
        """docker_mount_cwd_to_workspace is forwarded to container_config."""
        cc = self._run(_make_env_config(docker_mount_cwd_to_workspace=True), "t1").get("container_config", {})
        assert cc.get("docker_mount_cwd_to_workspace") is True

    def test_container_config_matches_terminal_creation_path(self):
        """File-first container creation preserves every terminal setting."""
        config = _make_env_config()

        captured = self._run(config, "docker-env")

        assert captured["container_config"] == terminal_tool._container_config_from_config(config)


    def test_cwd_only_raw_task_override_reaches_file_environment(self):
        """CWD-only task overrides collapse to default but must keep their cwd."""
        captured = self._run(
            _make_env_config(env_type="local", cwd="/config-cwd"),
            "desktop-session-cwd",
            task_env_overrides={"desktop-session-cwd": {"cwd": "/workspace/session"}},
        )

        assert captured["task_id"] == "default"
        assert captured["cwd"] == "/workspace/session"


def test_code_execution_container_config_matches_terminal_creation_path():
    """Execute-code-first creation preserves every terminal setting."""
    config = _make_env_config()
    captured = {}
    mock_env = MagicMock()

    def fake_create_env(**kwargs):
        captured.update(kwargs)
        return mock_env

    with patch("tools.terminal_tool._get_env_config", return_value=config), \
         patch("tools.terminal_tool._task_env_overrides", {}), \
         patch("tools.terminal_tool._active_environments", {}), \
         patch("tools.terminal_tool._creation_locks", {}), \
         patch("tools.terminal_tool._creation_locks_lock", __import__("threading").Lock()), \
         patch("tools.terminal_tool._create_environment", side_effect=fake_create_env), \
         patch("tools.terminal_tool._start_cleanup_thread"):
        code_execution_tool._get_or_create_env("execute-code-env")

    assert captured["container_config"] == terminal_tool._container_config_from_config(config)
