"""Le closeout registry est une lecture pure et n'adopte jamais un owner."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
import factory_lane
_spec = importlib.util.spec_from_file_location(
    "ai_factory_closeout_check", REPO_ROOT / "scripts" / "ai_factory_closeout_check.py"
)
assert _spec is not None and _spec.loader is not None
closeout_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(closeout_check)


def write_owner(root: Path, lane: str, payload: dict) -> Path:
    owner = root / "locks" / lane / "owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps(payload, sort_keys=True))
    return owner


def test_lists_reclaimable_owners_outside_active_lanes_without_mutating_registry(tmp_path):
    registry = tmp_path / "registry"
    dead = write_owner(registry, "HER-99", {"pid": 11, "heartbeat_at": 100, "ttl_hours": 1})
    expired = write_owner(registry, "SCA-616", {"pid": 12, "heartbeat_at": 0, "ttl_hours": 1})
    active = write_owner(registry, "HER-96", {"pid": 13, "heartbeat_at": 100, "ttl_hours": 1})
    before = {path: path.read_bytes() for path in (dead, expired, active)}

    findings = closeout_check.find_inactive_owners(
        registry, ["HER-96"], now=4_000,
        process_state=lambda owner: "alive" if owner.get("pid") == 13 else "not_found",
        worktree_last_active=lambda path: None,
    )

    assert [(finding["lane"], finding["reason"]) for finding in findings] == [
        ("HER-99", "reclaimable (process not_found, ttl expired, worktree inactive)"),
        ("SCA-616", "reclaimable (process not_found, ttl expired, worktree inactive)"),
    ]
    assert {path: path.read_bytes() for path in before} == before


def test_living_owner_of_an_active_lane_is_never_reported(tmp_path):
    registry = tmp_path / "registry"
    write_owner(registry, "HER-96", {"pid": 13, "heartbeat_at": 3_999, "ttl_hours": 1})

    assert closeout_check.find_inactive_owners(
        registry, ["HER-96"], now=4_000,
        process_state=lambda owner: "alive" if owner.get("pid") == 13 else "not_found",
        worktree_last_active=lambda path: None,
    ) == []


def test_permission_error_pid_counts_as_alive_not_reclaimable(tmp_path, monkeypatch):
    """PermissionError = process vivant d'un autre utilisateur, jamais « mort »."""
    registry = tmp_path / "registry"
    write_owner(registry, "HER-77", {"pid": 4194000, "heartbeat_at": 0, "ttl_hours": 1})

    def deny(pid, sig):
        raise PermissionError(f"operation not permitted: {pid}")

    monkeypatch.setattr(os, "kill", deny)

    assert closeout_check.find_inactive_owners(registry, [], now=4_000) == []


def test_missing_locks_directory_fails_closed(tmp_path):
    """Un registre sans locks/ est une erreur explicite, jamais « rien à signaler »."""
    registry = tmp_path / "registry"
    registry.mkdir()

    with pytest.raises(RuntimeError, match="locks"):
        closeout_check.find_inactive_owners(registry, [])


def test_cli_missing_locks_exits_nonzero_without_fake_empty_report(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setattr(
        sys, "argv", ["ai_factory_closeout_check.py", "--registry", str(registry)]
    )

    rc = closeout_check.main()

    captured = capsys.readouterr()
    assert rc == 2
    assert "locks" in captured.err
    assert captured.out.strip() == ""


def test_alive_owner_with_expired_ttl_agrees_with_production_reclaim(tmp_path):
    """TTL expiré mais process vivant : la production refuse le reclaim, le
    checker ne doit pas prétendre le contraire."""
    registry = tmp_path / "registry"
    write_owner(registry, "HER-88",
                {"pid": os.getpid(), "heartbeat_at": 0, "ttl_hours": 1})
    now = 4_000

    findings = closeout_check.find_inactive_owners(registry, [], now=now)

    assert findings == []
    verdict = factory_lane.evaluate_reclaim(
        now=now, owner={"ttl_hours": 1, "heartbeat_at": 0},
        process_state="alive", worktree_last_active=None,
    )
    assert verdict["reclaimable"] is False


def test_recently_active_worktree_blocks_reporting(tmp_path):
    """Même mort et expiré, un owner dont le worktree bouge n'est pas réclamable."""
    registry = tmp_path / "registry"
    write_owner(registry, "HER-66", {"pid": 4194000, "heartbeat_at": 0, "ttl_hours": 1})

    findings = closeout_check.find_inactive_owners(
        registry, [], now=4_000,
        process_state=lambda owner: "not_found",
        worktree_last_active=lambda path: 3_990,
    )

    assert findings == []


def test_owner_missing_lease_fields_is_reported_invalid_not_guessed(tmp_path):
    """Sans heartbeat_at/ttl_hours on ne devine pas un bail : owner invalide."""
    registry = tmp_path / "registry"
    write_owner(registry, "HER-55", {"pid": 123})

    findings = closeout_check.find_inactive_owners(
        registry, [], now=4_000,
        process_state=lambda owner: "not_found",
        worktree_last_active=lambda path: None,
    )

    assert [(f["lane"], f["reason"]) for f in findings] == [
        ("HER-55", "invalid owner.json"),
    ]
