"""Opt-in owner regression for the real rootless Podman backend.

This test is excluded from the normal suite because it needs a live rootless
Podman service and may pull an image. Run it only in an isolated arena with:

    HERMES_RUN_ROOTLESS_PODMAN_TESTS=1 \
      scripts/run_tests.sh -m integration \
      tests/tools/test_rootless_podman_owner.py -q
"""

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.environments import docker as docker_env


pytestmark = pytest.mark.integration


def _subordinate_ranges(path: str) -> list[tuple[int, int]]:
    """Return this user's ``(start, count)`` ranges from /etc/subuid|subgid.

    These are the IDs a rootless container maps into. A probe file owned by one
    of them is the exact symptom of the missing keep-id mapping, so the test
    refuses them by construction instead of hard-coding an observed number like
    100999 — which is specific to one machine's subuid base.

    An unreadable or malformed file yields no ranges: the caller still has the
    ``== os.getuid()`` assertion, so a missing file weakens the check rather
    than breaking the run on systems without subordinate IDs at all.
    """
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):  # pragma: no cover - unusual passwd setups
        return []

    ranges: list[tuple[int, int]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                fields = line.strip().split(":")
                if len(fields) != 3 or fields[0] != user:
                    continue
                try:
                    ranges.append((int(fields[1]), int(fields[2])))
                except ValueError:
                    continue
    except OSError:
        return []
    return ranges


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

        # THE GATE. This stat() runs in the pytest process — on the host,
        # outside the container's user namespace — which is the only vantage
        # point from which the bug is visible at all. The identical file
        # reports uid=1000 from inside the container even when unpatched.
        owner = probe_files[0].stat()
        assert owner.st_uid == os.getuid(), result["output"]
        assert owner.st_gid == os.getgid(), result["output"]

        # Reject the subordinate ID explicitly instead of trusting the equality
        # above to have compared the right thing. Without keep-id the owner
        # lands inside this range (measured: subuid base 100000 + 999 = 100999
        # for uid 1000); a refactor that accidentally compared a mapped ID
        # would slip past `==` but not past this.
        for value, ranges in (
            (owner.st_uid, _subordinate_ranges("/etc/subuid")),
            (owner.st_gid, _subordinate_ranges("/etc/subgid")),
        ):
            for start, count in ranges:
                assert not (start <= value < start + count), (
                    f"owner id {value} falls in the subordinate range "
                    f"{start}..{start + count - 1}: the file is owned by a "
                    f"mapped id, not by the caller.\n{result['output']}"
                )

        # In-namespace consistency is reported, never used as the verdict: it
        # reads "yes" both with and without the fix.
        assert "host_verification=required" in result["output"]
    finally:
        if environment is not None:
            environment.cleanup(force_remove=True)
            assert environment.wait_for_cleanup(timeout=45)
