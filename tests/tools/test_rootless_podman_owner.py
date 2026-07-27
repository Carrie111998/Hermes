"""Opt-in owner regression for the real rootless Podman backend.

This test is excluded from the normal suite because it needs a live rootless
Podman service and may pull an image. Run it only in an isolated arena with:

    HERMES_RUN_ROOTLESS_PODMAN_TESTS=1 \
      scripts/run_tests.sh -m integration \
      tests/tools/test_rootless_podman_owner.py -q
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.environments import docker as docker_env


pytestmark = pytest.mark.integration


def _rootless_podman_or_skip() -> str:
    if os.environ.get("HERMES_RUN_ROOTLESS_PODMAN_TESTS") != "1":
        pytest.skip("set HERMES_RUN_ROOTLESS_PODMAN_TESTS=1 to run live Podman test")

    podman = shutil.which("podman")
    if podman is None:
        pytest.skip("podman is not installed")

    result = subprocess.run(
        [podman, "info", "--format", "{{.Host.Security.Rootless}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        pytest.skip(f"podman info failed: {result.stderr.strip()}")
    if result.stdout.strip().lower() != "true":
        pytest.skip("Podman is not running rootless")
    return podman


def test_rootless_podman_backend_writes_as_current_owner(monkeypatch, tmp_path):
    """A file written through the real backend must retain the host UID/GID."""
    podman = _rootless_podman_or_skip()
    source_probe = Path(__file__).parents[2] / "scripts" / "uid_probe.sh"
    arena_probe = tmp_path / "uid_probe.sh"
    shutil.copy2(source_probe, arena_probe)

    monkeypatch.setattr(docker_env, "find_docker", lambda: podman)
    # This regression is about identity mapping, not cgroup delegation.
    monkeypatch.setattr(docker_env, "_cgroup_limits_ok", False)

    environment = None
    try:
        environment = docker_env.DockerEnvironment(
            image=os.environ.get(
                "HERMES_ROOTLESS_PODMAN_TEST_IMAGE",
                "docker.io/library/bash:5.2",
            ),
            cwd="/workspace",
            timeout=60,
            persistent_filesystem=False,
            task_id=f"rootless-owner-{os.getpid()}",
            host_cwd=str(tmp_path),
            auto_mount_cwd=True,
            run_as_host_user=True,
            network=False,
            persist_across_processes=False,
        )

        result = environment.execute(
            "bash /workspace/uid_probe.sh /workspace",
            cwd="/workspace",
            timeout=60,
        )
        assert result["returncode"] == 0, result["output"]

        probe_files = list(tmp_path.glob("uid-probe.*"))
        assert len(probe_files) == 1, result["output"]
        owner = probe_files[0].stat()
        assert owner.st_uid == os.getuid(), result["output"]
        assert owner.st_gid == os.getgid(), result["output"]
        assert "owner_matches_process=yes" in result["output"]
    finally:
        if environment is not None:
            environment.cleanup(force_remove=True)
            assert environment.wait_for_cleanup(timeout=45)
