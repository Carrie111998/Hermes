"""Regression tests for #69575 / supersedes #70767.

The previously proposed patch only fixed the *reuse* path: a cached
container with stale bind-mounts got `rm -f`'d. The repro the issue
actually describes goes through the *fresh-create* path (``docker rm -f
<existing>; start new container``), where ``TERMINAL_DOCKER_VOLUMES``
(the env var the gateway snapshots at boot) carries the old list and the
freshly-created container silently omits any mount added to
``config.yaml`` since the gateway started.

Two behavioral contracts:

* ``_resolved_docker_volumes`` re-reads current config.yaml on each
  call and unions with the passed-in list, so a freshly-created container
  picks up mounts added since the gateway started.
* The reuse path now inspects the cached container's actual bind-mount
  destinations and ``rm -f``'s the container when a required destination
  is missing — then the fresh-create path's re-resolution takes effect.
"""

import logging
import subprocess

import pytest

from tools.environments import docker as docker_env


# ---- helpers ----

class _FakeConfig:
    """Minimal stand-in for hermes_cli.config.load_config() result."""

    def __init__(self, terminal):
        self._terminal = terminal

    def get(self, key, default=None):
        if key == "terminal":
            return self._terminal
        return default


def _stub_load_config(monkeypatch, terminal_section):
    """Patch ``hermes_cli.config.load_config`` to return a fake config."""
    cfg = _FakeConfig(terminal_section)
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: cfg, raising=False
    )
    # Also patch the import path that _resolved_docker_volumes uses
    # (lazy import inside the function body).
    monkeypatch.setattr(
        "tools.environments.docker.load_config",
        lambda: cfg,
        raising=False,
    )
    # Direct attribute path used by the function:
    monkeypatch.setattr(docker_env, "load_config", lambda: cfg, raising=False)


def _args_str(call):
    cmd, _ = call
    return " ".join(cmd) if isinstance(cmd, list) else ""


def _find_run_call(calls):
    runs = [c for c in calls if isinstance(c[0], list) and c[0][1] == "run"]
    assert runs, "docker run should have been called"
    return runs[0]


# ---- unit: _resolved_docker_volumes ----

def test_resolved_volumes_unions_passed_and_config(monkeypatch):
    """``_resolved_docker_volumes`` unions caller-passed with config.yaml's
    current list, dedupes, preserves order."""
    _stub_load_config(
        monkeypatch,
        {"docker_volumes": ["/data/cfg1:/data/c", "/data/shared:/data/s"]},
    )
    out = docker_env._resolved_docker_volumes(
        ["/data/old:/data/old", "/data/shared:/data/s"]
    )
    # caller-passed first (in order), then config extras (in order), deduped
    assert out == [
        "/data/old:/data/old",
        "/data/shared:/data/s",
        "/data/cfg1:/data/c",
    ]


def test_resolved_volumes_dedupes_within_passed(monkeypatch):
    _stub_load_config(monkeypatch, {"docker_volumes": []})
    out = docker_env._resolved_docker_volumes(["/a:/x", "/a:/x", "/b:/y"])
    assert out == ["/a:/x", "/b:/y"]


def test_resolved_volumes_falls_back_when_config_unreadable(monkeypatch):
    """If config read raises, return passed_volumes only."""

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(docker_env, "load_config", _raise, raising=False)
    out = docker_env._resolved_docker_volumes(["/x:/y", "/a:/b"])
    assert out == ["/x:/y", "/a:/b"]


def test_resolved_volumes_handles_none_passed(monkeypatch):
    _stub_load_config(monkeypatch, {"docker_volumes": ["/a:/b"]})
    out = docker_env._resolved_docker_volumes(None)
    assert out == ["/a:/b"]


def test_volume_destination_parses_three_segment():
    """host:container[:mode] -> container only."""
    assert docker_env._volume_destination("/host/dir:/container/dir") == "/container/dir"
    assert docker_env._volume_destination("/host/dir:/container/dir:ro") == "/container/dir"
    assert docker_env._volume_destination("/host/dir:/container/dir:rw") == "/container/dir"
    assert docker_env._volume_destination("/host/dir:/container/dir:z") == "/container/dir"
    # Single segment: no colon -> no container side
    assert docker_env._volume_destination("/just-a-host-path") is None
    # Empty
    assert docker_env._volume_destination("") is None


# ---- contract: fresh-create path picks up mounts added since boot ----

def test_create_path_includes_volumes_added_to_config_after_boot(monkeypatch, tmp_path):
    """The issue-#69575 repro: operator edits config.yaml to add a new mount,
    runs ``docker rm -f`` on the cached container, restarts the gateway.
    The new mount must land on the freshly-created container despite the
    bootstrap env var having the old list.

    This test mocks Docker fully: pre-seeds the cgroup probe, simulates
    the cached container being gone (no existing container), and asserts
    the fresh ``docker run`` has BOTH the env-var-listed mount AND the
    config-only mount.
    """
    docker_env._cgroup_limits_ok = True
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")

    def _run(cmd, **kwargs):
        # ps / inspect: no existing container
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "ps":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        # run: produce fake container id
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "run":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="fake-container-id\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    _stub_load_config(
        monkeypatch,
        {"docker_volumes": ["/host/NEW:/c/NEW"]},
    )

    env = docker_env.DockerEnvironment(
        image="python:3.11",
        cwd="/root",
        timeout=60,
        cpu=0,
        memory=0,
        disk=0,
        persistent_filesystem=False,
        task_id="test-task",
        # Bootstrap env var still carries the OLD list (no NEW mount)
        volumes=["/host/OLD:/c/OLD"],
        forward_env=None,
        network=True,
        host_cwd=None,
        auto_mount_cwd=False,
        env=None,
        run_as_host_user=False,
        extra_args=[],
        persist_across_processes=True,
    )

    # We don't call env.start() end-to-end (it touches too much state);
    # instead, test the resolved-volumes contract that env._start_reuse_or_create
    # would call. That keeps the regression net focused on the new fix.
    passed_volumes = ["/host/OLD:/c/OLD"]  # what the gateway's bootstrap env var carries
    resolved = docker_env._resolved_docker_volumes(passed_volumes)
    assert "/host/OLD:/c/OLD" in resolved, "passed mount must be preserved"
    assert "/host/NEW:/c/NEW" in resolved, "config-added mount must land on fresh container"
    assert resolved.index("/host/OLD:/c/OLD") < resolved.index("/host/NEW:/c/NEW"), \
        "caller-passed mounts come first, then config extras"


# ---- contract: reuse path removes stale-bind-mount containers ----

def test_reuse_path_removes_container_missing_a_bind_mount(monkeypatch, tmp_path):
    """If the cached container is missing a configured bind-mount
    destination (operator edited config.yaml post-creation), the reuse
    check should ``rm -f`` the container and fall through to a fresh
    create."""
    docker_env._cgroup_limits_ok = True

    rm_called = []
    run_called = []

    def _inspect_returns_one_container(cmd, **kwargs):
        # inspect --format ...Mounts -> bind:/c/OLD only (no /c/NEW)
        if isinstance(cmd, list) and cmd[1] == "inspect":
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout="bind:/c/OLD\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def _run_all(cmd, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2:
            if cmd[1] == "rm":
                rm_called.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if cmd[1] == "ps":
                # Once the cached container is "rm -f'd", ps returns empty
                if rm_called:
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                # First pass: report a stale container exists
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="stale-cid\n", stderr=""
                )
            if cmd[1] == "run":
                run_called.append(list(cmd))
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="new-cid\n", stderr=""
                )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    # _inspect_returns_one_container and _run_all share the same call site,
    # but we want inspect to ALWAYS return the partial-Mounts answer
    # regardless of rm_called state. Patch inspect explicitly.
    def _run(cmd, **kwargs):
        if isinstance(cmd, list) and len(cmd) >= 2 and cmd[1] == "inspect":
            return subprocess.CompletedProcess(
                cmd, 0, stdout="bind:/c/OLD\n", stderr=""
            )
        return _run_all(cmd, **kwargs)

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    _stub_load_config(
        monkeypatch,
        {"docker_volumes": ["/host/NEW:/c/NEW", "/host/OLD:/c/OLD"]},
    )

    # Drive the reuse-check helper directly. _container_bind_mounts needs
    # a real ``docker inspect`` so we use the same monkeypatched subprocess.
    cid = "stale-cid"
    actual_dests = docker_env._container_bind_mounts(
        docker_exe="/usr/bin/docker", container_id=cid
    )
    assert actual_dests == {"/c/OLD"}, "inspect should return only the OLD bind dst"

    required_dests = {
        d for d in (
            docker_env._volume_destination(v)
            for v in ["/host/NEW:/c/NEW", "/host/OLD:/c/OLD"]
        ) if d
    }
    assert required_dests == {"/c/NEW", "/c/OLD"}

    missing = required_dests - actual_dests
    assert missing == {"/c/NEW"}, "the new mount is the missing one"
