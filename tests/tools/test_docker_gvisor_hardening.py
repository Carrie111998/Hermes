"""Tests for the opt-in Docker runtime override + read-only rootfs option in
the Docker terminal backend (``terminal.docker_runtime`` /
``terminal.docker_readonly``).

Both are additive to the existing cap-drop/no-new-privileges/tmpfs hardening:
``docker_runtime`` lets an operator who already has an alternate Docker
runtime installed (e.g. gVisor's ``runsc``) point the sandbox at it;
``docker_readonly`` makes the image's root filesystem immutable on top of
the existing tmpfs mounts. Both fail loud rather than silently degrading: an
unregistered runtime raises at ``DockerEnvironment`` construction instead of
falling back to ``runc``, and the reuse path rejects a persisted container
that predates either setting — the same bug class the existing
network-mode guard already fixes for ``docker_network``.
"""

import json

import pytest

import tools.terminal_tool as terminal_tool
from tools.environments import docker as docker_env


@pytest.fixture(autouse=True)
def _reset_docker_probe_caches():
    """Module-level probe caches must not leak between tests in this file."""
    docker_env._cgroup_limits_ok = None
    docker_env._storage_opt_ok = None
    docker_env._runtime_availability_cache = {}
    yield
    docker_env._cgroup_limits_ok = None
    docker_env._storage_opt_ok = None
    docker_env._runtime_availability_cache = {}


def _make_fake_run(*, runtimes=None, ps_stdout="", inspect_stdout=""):
    """Build a fake subprocess.run answering docker info/ps/inspect/run.

    Returns ``(fake_run, commands)`` — commands is the list of argv lists
    issued, for assertions.
    """
    commands = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if len(cmd) > 1 and cmd[1] == "info":
            Result.stdout = json.dumps(runtimes or {})
        elif len(cmd) > 1 and cmd[1] == "ps":
            Result.stdout = ps_stdout
        elif len(cmd) > 1 and cmd[1] == "inspect":
            Result.stdout = inspect_stdout
        elif len(cmd) > 1 and cmd[1] == "run":
            Result.stdout = "fake-container-id\n"
        return Result()

    return fake_run, commands


# --- terminal_tool config bridging ------------------------------------------


def test_terminal_env_config_reads_docker_runtime_and_readonly(monkeypatch):
    monkeypatch.setenv("TERMINAL_DOCKER_RUNTIME", "runsc")
    monkeypatch.setenv("TERMINAL_DOCKER_READONLY", "true")

    config = terminal_tool._get_env_config()

    assert config["docker_runtime"] == "runsc"
    assert config["docker_readonly"] is True


def test_create_environment_passes_runtime_and_readonly(monkeypatch):
    captured = {}
    sentinel = object()

    def _fake_docker_environment(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(terminal_tool, "_DockerEnvironment", _fake_docker_environment)

    env = terminal_tool._create_environment(
        env_type="docker",
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        container_config={"docker_runtime": "runsc", "docker_readonly": True},
    )

    assert env is sentinel
    assert captured["runtime"] == "runsc"
    assert captured["read_only"] is True


# --- _docker_runtime_available probe ----------------------------------------


def test_runtime_probe_true_when_registered(monkeypatch):
    fake_run, commands = _make_fake_run(runtimes={"runsc": {"path": "/usr/bin/runsc"}})
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    assert docker_env._docker_runtime_available("/usr/bin/docker", "runsc") is True
    assert any(cmd[1] == "info" for cmd in commands)


def test_runtime_probe_false_when_not_registered(monkeypatch):
    fake_run, _ = _make_fake_run(runtimes={})
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    assert docker_env._docker_runtime_available("/usr/bin/docker", "runsc") is False


def test_runtime_probe_result_is_cached(monkeypatch):
    fake_run, commands = _make_fake_run(runtimes={"runsc": {}})
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    docker_env._docker_runtime_available("/usr/bin/docker", "runsc")
    docker_env._docker_runtime_available("/usr/bin/docker", "runsc")

    info_calls = [c for c in commands if len(c) > 1 and c[1] == "info"]
    assert len(info_calls) == 1


# --- DockerEnvironment construction -----------------------------------------


def test_docker_environment_raises_when_runtime_unregistered(monkeypatch):
    fake_run, _ = _make_fake_run(runtimes={})
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="runsc"):
        docker_env.DockerEnvironment(
            image="python:3.11",
            cwd="/workspace",
            timeout=60,
            task_id="runtime-missing-test",
            runtime="runsc",
        )


def test_docker_environment_adds_runtime_flag_when_available(monkeypatch):
    fake_run, commands = _make_fake_run(runtimes={"runsc": {}})
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="runtime-ok-test",
        runtime="runsc",
    )

    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--runtime" in run_cmd
    assert run_cmd[run_cmd.index("--runtime") + 1] == "runsc"
    env.cleanup()


def test_docker_environment_omits_runtime_flag_by_default(monkeypatch):
    fake_run, commands = _make_fake_run()
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    env = docker_env.DockerEnvironment(
        image="python:3.11", cwd="/workspace", timeout=60,
        task_id="runtime-default-test",
    )

    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--runtime" not in run_cmd
    env.cleanup()


def test_docker_environment_adds_read_only_flag_when_enabled(monkeypatch):
    fake_run, commands = _make_fake_run()
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    env = docker_env.DockerEnvironment(
        image="python:3.11", cwd="/workspace", timeout=60,
        task_id="readonly-test", read_only=True,
    )

    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--read-only" in run_cmd
    env.cleanup()


def test_docker_environment_omits_read_only_flag_by_default(monkeypatch):
    fake_run, commands = _make_fake_run()
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    env = docker_env.DockerEnvironment(
        image="python:3.11", cwd="/workspace", timeout=60,
        task_id="readonly-default-test",
    )

    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--read-only" not in run_cmd
    env.cleanup()


# --- reuse guard -------------------------------------------------------------


def _reuse_harness(monkeypatch, *, actual_runtime: str, actual_readonly: str, **kwargs):
    fake_run, commands = _make_fake_run(
        runtimes={"runsc": {}},
        ps_stdout="existing-container-id\trunning\t<no value>\n",
        inspect_stdout=f"{actual_runtime}\t{actual_readonly}\n",
    )
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env.subprocess, "run", fake_run)

    docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/workspace",
        timeout=60,
        task_id="reuse-hardening-test",
        persist_across_processes=True,
        **kwargs,
    )
    return commands


def test_reuse_rejects_container_with_wrong_runtime(monkeypatch):
    commands = _reuse_harness(
        monkeypatch, actual_runtime="runc", actual_readonly="false", runtime="runsc",
    )

    assert any(cmd[1:3] == ["rm", "-f"] for cmd in commands), (
        "container created under runc must be removed when docker_runtime=runsc "
        "is requested"
    )
    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--runtime" in run_cmd


def test_reuse_rejects_container_not_readonly(monkeypatch):
    commands = _reuse_harness(
        monkeypatch, actual_runtime="runc", actual_readonly="false", read_only=True,
    )

    assert any(cmd[1:3] == ["rm", "-f"] for cmd in commands), (
        "writable-rootfs container must be removed when docker_readonly=true "
        "is requested"
    )
    run_cmd = next(cmd for cmd in commands if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--read-only" in run_cmd


def test_reuse_keeps_matching_hardened_container(monkeypatch):
    commands = _reuse_harness(
        monkeypatch, actual_runtime="runsc", actual_readonly="true",
        runtime="runsc", read_only=True,
    )

    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1:3] == ["run", "-d"] for cmd in commands), (
        "matching container must be reused, not recreated"
    )


def test_reuse_skips_inspect_when_no_hardening_requested(monkeypatch):
    """Default config (no docker_runtime, docker_readonly=False) never churns
    containers, even ones that happen to run under a stricter posture already
    (e.g. created via docker_extra_args)."""
    commands = _reuse_harness(monkeypatch, actual_runtime="runc", actual_readonly="false")

    assert not any(cmd[1] == "inspect" for cmd in commands)
    assert not any(cmd[1] == "rm" for cmd in commands)
    assert not any(cmd[1:3] == ["run", "-d"] for cmd in commands)
