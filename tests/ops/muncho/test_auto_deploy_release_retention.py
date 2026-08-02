from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ops.muncho.runtime import release_retention as retention


ACTIVE_PREFIX = "ced9572a82b9"
ROLLBACK_PREFIX = "5a53e5a9dfa9"
SYNC_PREFIX = "9d4a56cb069c"
SENDER_PREFIX = "f8733e2f44da"
REMOVABLE_PREFIX = "60ce968bdde0"
ROOT = Path(__file__).parents[3]
DEPLOY_HELPER = ROOT / "ops/muncho/runtime/muncho-auto-deploy-release"


def _executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _release(root: Path, prefix: str, mtime_ns: int) -> Path:
    path = root / f"hermes-agent-{prefix}"
    path.mkdir(parents=True)
    marker = path / ".codex-source-commit"
    marker.write_text(prefix + "0" * 28 + "\n", encoding="ascii")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _systemctl(
    tmp_path: Path,
    *,
    loaded_references: dict[str, list[str]],
    malformed_list: bool = False,
    omit_empty_properties: bool = False,
    property_overrides: dict[str, dict[str, str]] | None = None,
) -> Path:
    state = tmp_path / "systemctl-state.json"
    state.write_text(
        json.dumps({
            "loaded_references": loaded_references,
            "malformed_list": malformed_list,
            "omit_empty_properties": omit_empty_properties,
            "property_overrides": property_overrides or {},
        }),
        encoding="utf-8",
    )
    executable = tmp_path / "bin" / "systemctl"
    executable.parent.mkdir()
    _executable(
        executable,
        """#!/usr/bin/env python3
import json
import sys

state = json.load(open(__STATE_PATH__, encoding="utf-8"))
if sys.argv[1] == "list-units":
    if state["malformed_list"]:
        print("not-a-systemd-unit loaded active running malformed")
    else:
        for name in sorted(state["loaded_references"]):
            print(f"{name} loaded active exited test")
    raise SystemExit(0)
if sys.argv[1] != "show":
    raise SystemExit(2)
name = sys.argv[-1]
references = state["loaded_references"].get(name)
if references is None:
    raise SystemExit(3)
values = {
    "Id": name,
    "Names": name,
    "LoadState": "loaded",
    "FragmentPath": f"/etc/systemd/system/{name}",
    "DropInPaths": "",
    "ExecStart": " ".join(references),
    "ExecStartPre": "",
    "ExecStartPost": "",
    "ExecStop": "",
    "ExecReload": "",
    "WorkingDirectory": "",
    "RootDirectory": "",
    "Environment": "",
    "EnvironmentFiles": "",
    "AssertPathExists": "",
    "ConditionPathExists": "",
    "ReadOnlyPaths": "",
    "BindReadOnlyPaths": "",
    "ReadWritePaths": "",
    "InaccessiblePaths": "",
}
values.update(state["property_overrides"].get(name, {}))
for key in (
    "Id", "Names", "LoadState", "FragmentPath", "DropInPaths",
    "ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload",
    "WorkingDirectory", "RootDirectory", "Environment", "EnvironmentFiles",
    "AssertPathExists", "ConditionPathExists", "ReadOnlyPaths",
    "BindReadOnlyPaths", "ReadWritePaths", "InaccessiblePaths",
):
    if (
        state["omit_empty_properties"]
        and not values[key]
        and key not in {"Id", "Names", "LoadState", "FragmentPath", "DropInPaths", "RootDirectory"}
    ):
        continue
    print(f"{key}={values[key]}")
""".replace("__STATE_PATH__", repr(str(state))),
    )
    return executable


def _release_path(releases: Path, prefix: str) -> str:
    return str(releases / f"hermes-agent-{prefix}")


def _layout(tmp_path: Path) -> tuple[Path, dict[str, Path], Path]:
    releases = (tmp_path / "releases").resolve()
    releases.mkdir()
    paths = {
        ACTIVE_PREFIX: _release(releases, ACTIVE_PREFIX, 5_000_000_000),
        ROLLBACK_PREFIX: _release(releases, ROLLBACK_PREFIX, 4_000_000_000),
        SYNC_PREFIX: _release(releases, SYNC_PREFIX, 3_000_000_000),
        SENDER_PREFIX: _release(releases, SENDER_PREFIX, 2_000_000_000),
        REMOVABLE_PREFIX: _release(releases, REMOVABLE_PREFIX, 1_000_000_000),
    }
    unit_root = (tmp_path / "systemd").resolve()
    unit_root.mkdir()
    return releases, paths, unit_root


def test_cleanup_preserves_exact_loaded_and_unit_file_dependencies(
    tmp_path: Path,
) -> None:
    """Reproduce the 9d4/f873 retention incident without deleting either root."""

    releases, paths, unit_root = _layout(tmp_path)
    # 9d4 is visible only in the loaded service properties.  f873 is visible
    # only in an installed, currently inactive reporter unit file.  Both are
    # older than the normal two-release rollback window.
    (unit_root / "muncho-dual-upstream-sync-report.service").write_text(
        f"[Service]\nWorkingDirectory={_release_path(releases, SENDER_PREFIX)}\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={
            "muncho-dual-upstream-sync.service": [
                _release_path(releases, SYNC_PREFIX) + "/.venv/bin/python"
            ]
        },
    )

    removed = retention.cleanup(
        releases_root=releases,
        active_release=paths[ACTIVE_PREFIX],
        keep=2,
        systemctl=systemctl,
        unit_roots=(unit_root,),
    )

    assert removed == (paths[REMOVABLE_PREFIX],)
    assert not paths[REMOVABLE_PREFIX].exists()
    assert paths[ACTIVE_PREFIX].is_dir()
    assert paths[ROLLBACK_PREFIX].is_dir()
    assert paths[SYNC_PREFIX].is_dir()
    assert paths[SENDER_PREFIX].is_dir()


def test_production_shaped_systemctl_output_omits_empty_optional_properties(
    tmp_path: Path,
) -> None:
    releases, paths, unit_root = _layout(tmp_path)
    (unit_root / "muncho-dual-upstream-sync.service").write_text(
        "[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={
            "muncho-dual-upstream-sync.service": [
                _release_path(releases, SYNC_PREFIX) + "/.venv/bin/python"
            ]
        },
        omit_empty_properties=True,
        property_overrides={
            "muncho-dual-upstream-sync.service": {
                "Environment": ("PYTHONPATH=" + _release_path(releases, SYNC_PREFIX)),
                "WorkingDirectory": _release_path(releases, SYNC_PREFIX),
                "RootDirectory": "",
                "ReadWritePaths": "/var/lib/muncho-dual-upstream-sync",
                "ReadOnlyPaths": _release_path(releases, SYNC_PREFIX),
                "InaccessiblePaths": "/home /root",
                "BindReadOnlyPaths": "/run/systemd/resolve/resolv.conf",
            }
        },
    )

    plan = retention.build_cleanup_plan(
        releases_root=releases,
        active_release=paths[ACTIVE_PREFIX],
        keep=2,
        systemctl=systemctl,
        unit_roots=(unit_root,),
    )

    assert paths[SYNC_PREFIX] in plan.protected
    assert paths[SENDER_PREFIX] in plan.removable


def test_missing_referenced_release_blocks_every_deletion(tmp_path: Path) -> None:
    releases, paths, unit_root = _layout(tmp_path)
    # The test release has a marker, so remove that exact fixture root before
    # inventory.  The installed unit still points to the now-missing release.
    (paths[SENDER_PREFIX] / ".codex-source-commit").unlink()
    paths[SENDER_PREFIX].rmdir()
    (unit_root / "muncho-dual-upstream-sync-report.service").write_text(
        f"[Service]\nWorkingDirectory={_release_path(releases, SENDER_PREFIX)}\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={"stable.service": []},
    )

    with pytest.raises(
        retention.ReleaseRetentionError,
        match=(f"release_cleanup_referenced_release_missing:{SENDER_PREFIX}"),
    ):
        retention.cleanup(
            releases_root=releases,
            active_release=paths[ACTIVE_PREFIX],
            keep=2,
            systemctl=systemctl,
            unit_roots=(unit_root,),
        )

    assert all(
        path.is_dir() for prefix, path in paths.items() if prefix != SENDER_PREFIX
    )


def test_ambiguous_loaded_unit_inventory_blocks_every_deletion(
    tmp_path: Path,
) -> None:
    releases, paths, unit_root = _layout(tmp_path)
    (unit_root / "stable.service").write_text(
        "[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={"stable.service": []},
        malformed_list=True,
    )

    with pytest.raises(
        retention.ReleaseRetentionError,
        match="release_cleanup_systemd_inventory_ambiguous",
    ):
        retention.cleanup(
            releases_root=releases,
            active_release=paths[ACTIVE_PREFIX],
            keep=2,
            systemctl=systemctl,
            unit_roots=(unit_root,),
        )

    assert all(path.is_dir() for path in paths.values())


def test_cleanup_plan_keeps_active_and_recent_rollback_candidates(
    tmp_path: Path,
) -> None:
    releases, paths, unit_root = _layout(tmp_path)
    (unit_root / "stable.service").write_text(
        "[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={"stable.service": []},
    )

    plan = retention.build_cleanup_plan(
        releases_root=releases,
        active_release=paths[ACTIVE_PREFIX],
        keep=2,
        systemctl=systemctl,
        unit_roots=(unit_root,),
    )

    assert paths[ACTIVE_PREFIX] in plan.protected
    assert paths[ROLLBACK_PREFIX] in plan.protected
    assert paths[SYNC_PREFIX] in plan.removable
    assert paths[SENDER_PREFIX] in plan.removable
    assert paths[REMOVABLE_PREFIX] in plan.removable


def test_new_unit_reference_after_plan_blocks_every_deletion(
    tmp_path: Path,
) -> None:
    releases, paths, unit_root = _layout(tmp_path)
    (unit_root / "stable.service").write_text(
        "[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={"stable.service": []},
    )
    plan = retention.build_cleanup_plan(
        releases_root=releases,
        active_release=paths[ACTIVE_PREFIX],
        keep=2,
        systemctl=systemctl,
        unit_roots=(unit_root,),
    )
    assert paths[REMOVABLE_PREFIX] in plan.removable

    state_path = tmp_path / "systemctl-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["loaded_references"]["stable.service"] = [
        _release_path(releases, REMOVABLE_PREFIX) + "/.venv/bin/python"
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(
        retention.ReleaseRetentionError,
        match="release_cleanup_inventory_changed",
    ):
        retention.apply_cleanup_plan(plan)

    assert all(path.is_dir() for path in paths.values())


def test_legacy_deploy_helper_invokes_release_local_retention_runtime(
    tmp_path: Path,
) -> None:
    releases, paths, unit_root = _layout(tmp_path)
    (unit_root / "stable.service").write_text(
        "[Service]\nExecStart=/bin/true\n",
        encoding="utf-8",
    )
    systemctl = _systemctl(
        tmp_path,
        loaded_references={"stable.service": []},
    )
    helper = paths[ACTIVE_PREFIX] / "ops/muncho/runtime/release_retention.py"
    helper.parent.mkdir(parents=True)
    shutil.copy2(retention.__file__, helper)
    active_link = tmp_path / "active"
    active_link.symlink_to(paths[ACTIVE_PREFIX], target_is_directory=True)
    command = """
source "$DEPLOY_HELPER"
RELEASES="$TEST_RELEASES"
ACTIVE_LINK="$TEST_ACTIVE_LINK"
KEEP_RELEASES=2
SYSTEM_PYTHON="$TEST_PYTHON"
SYSTEMCTL="$TEST_SYSTEMCTL"
SYSTEMD_UNIT_ROOTS="$TEST_UNIT_ROOT"
cleanup_old_releases
"""
    completed = subprocess.run(
        ["bash", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env={
            **os.environ,
            "DEPLOY_HELPER": str(DEPLOY_HELPER),
            "TEST_RELEASES": str(releases),
            "TEST_ACTIVE_LINK": str(active_link),
            "TEST_PYTHON": sys.executable,
            "TEST_SYSTEMCTL": str(systemctl),
            "TEST_UNIT_ROOT": str(unit_root),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert "release_cleanup_removed=" in completed.stdout
    assert not paths[REMOVABLE_PREFIX].exists()
    assert paths[ACTIVE_PREFIX].is_dir()
    assert paths[ROLLBACK_PREFIX].is_dir()
