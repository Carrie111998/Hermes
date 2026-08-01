"""Contract tests for the session-scoped execution write policy."""

from dataclasses import FrozenInstanceError

import pytest

from tools.environments.execution_policy import (
    WORKSPACE_SCOPE,
    clear_execution_workspace,
    policy_environment_key,
    resolve_execution_write_policy,
)
from hermes_cli.config import terminal_config_env_var_for_key
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_workspace_policy_is_immutable_and_does_not_read_safe_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(tmp_path / "unrelated"))
    clear_execution_workspace("policy-test")

    policy = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id="policy-test",
        workspace_root=str(tmp_path / "workspace"),
        backend="docker",
    )

    assert policy.scope == WORKSPACE_SCOPE
    assert policy.workspace_root == str((tmp_path / "workspace").resolve())
    assert policy.capability.supported
    with pytest.raises(FrozenInstanceError):
        policy.scope = "legacy"


def test_docker_mapping_normalization_rejects_writable_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    policy = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id="mapping-test",
        workspace_root=str(workspace),
        backend="docker",
        docker_volumes=[f"{outside}:/output:rw"],
    )

    assert policy.capability.status == "invalid"
    assert policy.capability.code == "outside_workspace_mapping"


def test_docker_mapping_normalization_keeps_private_and_read_only_mounts(tmp_path):
    workspace = tmp_path / "workspace"
    credentials = tmp_path / "credentials.json"
    policy = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id="mapping-test-ro",
        workspace_root=str(workspace),
        backend="docker",
        docker_volumes=[
            f"{workspace / 'project'}:/workspace/project:rw",
            f"{credentials}:/root/.credentials.json:ro",
        ],
    )

    assert policy.capability.supported
    assert f"{(workspace / 'project').resolve()}:/workspace/project" in {
        mapping.spec for mapping in policy.docker_mappings
    }
    assert f"{credentials.resolve()}:/root/.credentials.json:ro" in {
        mapping.spec for mapping in policy.docker_mappings
    }


@pytest.mark.parametrize(
    "backend",
    ["local", "ssh", "singularity", "modal", "daytona", "vercel_sandbox"],
)
def test_workspace_scope_backend_matrix(backend, tmp_path):
    policy = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id=f"unsupported-{backend}",
        workspace_root=str(tmp_path),
        backend=backend,
    )

    assert policy.capability.status == "unsupported"
    assert policy.capability.code == "unsupported_execution_backend"
    assert backend in policy.capability.message


def test_workspace_policy_identity_separates_sessions(tmp_path):
    first = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id="session-a",
        workspace_root=str(tmp_path / "workspace"),
        backend="docker",
    )
    second = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id="session-b",
        workspace_root=str(tmp_path / "workspace"),
        backend="docker",
    )

    assert first.fingerprint != second.fingerprint
    assert policy_environment_key("default", first) != policy_environment_key("default", second)


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--privileged"],
        ["-v=/outside:/workspace"],
        ["--mount=type=bind,src=/outside,dst=/workspace"],
        ["--volumes-from=other-container"],
        ["--pid=host"],
        ["--network", "host"],
        ["--unknown-flag"],
    ],
)
def test_workspace_policy_rejects_unsafe_extra_args(tmp_path, extra_args):
    policy = resolve_execution_write_policy(
        WORKSPACE_SCOPE,
        session_id="extra-args",
        workspace_root=str(tmp_path / "workspace"),
        backend="docker",
        docker_extra_args=extra_args,
    )

    assert policy.capability.status == "invalid"
    assert policy.capability.code == "invalid_docker_extra_args"


def test_execution_write_scope_is_config_owned_without_env_bridge():
    assert DEFAULT_CONFIG["terminal"]["execution_write_scope"] == "legacy"
    assert terminal_config_env_var_for_key("terminal.execution_write_scope") is None


@pytest.mark.parametrize("scope", ["legacy", WORKSPACE_SCOPE])
def test_prompt_backend_probe_isolated_and_cleans_only_probe(
    monkeypatch, tmp_path, scope
):
    import agent.prompt_builder as prompt_builder
    import tools.terminal_tool as terminal_tool

    config = {
        "env_type": "docker",
        "execution_write_scope": scope,
        "cwd": "/root",
        "host_cwd": str(tmp_path),
        "host_workspace_root": str(tmp_path),
        "docker_mount_cwd_to_workspace": True,
        "docker_volumes": [],
        "docker_extra_args": [],
        "timeout": 30,
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    live_environment = object()
    monkeypatch.setattr(
        terminal_tool, "_active_environments", {"default": live_environment}
    )
    prompt_builder._clear_backend_probe_cache()

    class FakeEnvironment:
        def __init__(self, result):
            self.result = result
            self.cleanup_calls = []

        def execute(self, command, timeout=None):
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

        def cleanup(self, *, force_remove=False):
            self.cleanup_calls.append(force_remove)

    created = []

    def fake_create_environment(**kwargs):
        env = FakeEnvironment(
            {
                "returncode": 0,
                "output": "os=Linux\nkernel=6.8\nhome=/root\ncwd=/workspace\nuser=root\n",
            }
        )
        env.create_kwargs = kwargs
        created.append(env)
        return env

    monkeypatch.setattr(terminal_tool, "_create_environment", fake_create_environment)
    assert prompt_builder._probe_remote_backend("docker") is not None
    assert created[0].cleanup_calls == [True]
    assert created[0].create_kwargs["task_id"].startswith("prompt-backend-probe-")
    assert created[0].create_kwargs["task_id"] != "default"
    assert created[0].create_kwargs["container_config"]["container_persistent"] is False
    assert (
        created[0].create_kwargs["container_config"]["docker_persist_across_processes"]
        is False
    )
    assert terminal_tool._active_environments["default"] is live_environment

    prompt_builder._clear_backend_probe_cache()
    created.clear()

    def failing_create_environment(**kwargs):
        env = FakeEnvironment(RuntimeError("probe failed"))
        created.append(env)
        return env

    monkeypatch.setattr(terminal_tool, "_create_environment", failing_create_environment)
    assert prompt_builder._probe_remote_backend("docker") is None
    assert created[0].cleanup_calls == [True]
    assert prompt_builder._probe_remote_backend("docker") is None
    assert len(created) == 1
