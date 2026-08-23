"""Behavioral contract for image-managed update refusal.

The baked provenance marker lives outside both the immutable install tree and
the mutable data volume.  Even when an operator bind-mounts checkout-shaped
hints over ``/opt/hermes``, the runtime must still classify itself as an image
and refuse an in-place update before any mutation starts.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.docker.conftest import docker_exec, docker_exec_sh, wait_for_container_ready


_REFUSAL_CODE = "image_managed_update_refused"
_CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")


def _start_with_checkout_hints(
    image: str,
    container: str,
    *,
    install_method: Path,
    git_dir: Path,
) -> None:
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--mount",
            f"type=bind,src={install_method},dst=/opt/hermes/.install_method,readonly",
            "--mount",
            f"type=bind,src={git_dir},dst=/opt/hermes/.git,readonly",
            image,
            "sleep",
            "infinity",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"docker run failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    wait_for_container_ready(container)


def _inspect_baked_marker(container: str) -> dict:
    script = r"""
import json
import importlib.metadata
import pathlib
import stat

path = pathlib.Path("/etc/hermes/image-provenance.json")
marker = json.loads(path.read_text(encoding="utf-8"))
sha_path = pathlib.Path("/opt/hermes/.hermes_build_sha")
print(json.dumps({
    "marker": marker,
    "mode": stat.S_IMODE(path.stat().st_mode),
    "uid": path.stat().st_uid,
    "installed_version": importlib.metadata.version("hermes-agent"),
    "build_sha": sha_path.read_text(encoding="utf-8").strip()
        if sha_path.is_file() else None,
}))
"""
    result = docker_exec(container, "python3", "-c", script, timeout=15)
    assert result.returncode == 0, (
        f"could not inspect image marker: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return json.loads(result.stdout)


@pytest.mark.live_system_guard_bypass
def test_baked_image_refuses_update_before_mutation(
    built_image: str,
    container_name: str,
    tmp_path: Path,
) -> None:
    """A bind-mounted ``.git`` cannot turn a baked image into a git install."""
    install_method = tmp_path / "install-method"
    install_method.write_text("git\n", encoding="utf-8")
    install_method.chmod(0o444)
    git_dir = tmp_path / "git-checkout"
    git_dir.mkdir(mode=0o755)

    _start_with_checkout_hints(
        built_image,
        container_name,
        install_method=install_method,
        git_dir=git_dir,
    )

    inspected = _inspect_baked_marker(container_name)
    marker = inspected["marker"]
    assert marker == {
        "schema": 1,
        "deployment_kind": "image",
        "manager": "docker",
        "image": "nousresearch/hermes-agent",
        "version": inspected["installed_version"],
        "revision": inspected["build_sha"],
    }
    assert inspected["mode"] == 0o444
    assert inspected["uid"] == 0

    mounted_hints = docker_exec_sh(
        container_name,
        "test -d /opt/hermes/.git && "
        "test \"$(cat /opt/hermes/.install_method)\" = git",
        timeout=10,
    )
    assert mounted_hints.returncode == 0, (
        "checkout-shaped bind mounts were not installed as intended: "
        f"stdout={mounted_hints.stdout!r} stderr={mounted_hints.stderr!r}"
    )

    update = docker_exec(container_name, "hermes", "update", timeout=60)
    output = f"{update.stdout}\n{update.stderr}".lower()
    assert update.returncode == 2, (
        f"image-managed update exited {update.returncode}, expected 2: "
        f"stdout={update.stdout!r} stderr={update.stderr!r}"
    )
    assert _REFUSAL_CODE in output
    assert "image-managed" in output
    assert "pull or select" in output
    assert "recreate" in output

    receipt_result = docker_exec(
        container_name,
        "cat",
        "/opt/data/logs/update_receipts/latest.json",
        timeout=10,
    )
    assert receipt_result.returncode == 0, (
        "refusal did not leave a durable latest receipt: "
        f"stderr={receipt_result.stderr!r}"
    )
    receipt = json.loads(receipt_result.stdout)
    assert receipt["outcome"] == "refused"
    assert receipt["stop_reason"] == _REFUSAL_CODE
    assert receipt["surface"] == "cli"
    assert _CORRELATION_ID.fullmatch(receipt["correlation_id"])
    assert receipt["started_at"]
    assert receipt["finished_at"]
    assert receipt["requested_target"] is None
    assert receipt["plan"]["deployment_kind"] == "image"
    assert receipt["refusal"]["code"] == _REFUSAL_CODE
    assert receipt["refusal"]["deployment_kind"] == "image"
    assert receipt["refusal"]["baked_identity"]["revision"] == marker["revision"]
    assert "sha" in receipt["refusal"]["current_identity"]
    assert "version" in receipt["refusal"]["current_identity"]

    no_mutation = docker_exec_sh(
        container_name,
        "test ! -e /opt/data/logs/update.log && "
        "test ! -e /opt/data/state-snapshots && "
        "test -z \"$(find /opt/data/backups -mindepth 1 -print -quit)\" && "
        "test -z \"$(find /opt/hermes/.git -mindepth 1 -print -quit)\" && "
        "test \"$(cat /opt/hermes/.install_method)\" = git",
        timeout=10,
    )
    assert no_mutation.returncode == 0, (
        "the refused update crossed a mutation boundary: "
        f"stdout={no_mutation.stdout!r} stderr={no_mutation.stderr!r}"
    )
