"""#444 — startup cleanup of stranded auth.json.tmp.<pid>.<uuid> orphans."""
import os
import time
import pytest
from hermes_cli import auth as A


def _mk(tmpdir, suffix, age=0.0):
    p = tmpdir / f"auth.json.tmp.{suffix}"
    p.write_text("{}")
    if age:
        old = time.time() - age
        os.utime(p, (old, old))
    return p


def test_dead_pid_orphan_removed(tmp_path):
    auth_file = tmp_path / "auth.json"
    # a PID that is essentially certain to be dead
    dead = _mk(tmp_path, "999999.deadbeef")
    A._prune_auth_tmp_orphans(auth_file)
    assert not dead.exists()


def test_live_pid_orphan_kept(tmp_path):
    auth_file = tmp_path / "auth.json"
    live = _mk(tmp_path, f"{os.getpid()}.abc123")
    A._prune_auth_tmp_orphans(auth_file)
    assert live.exists()  # our own live PID → not pruned


def test_live_store_never_touched(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"providers": {}}')
    A._prune_auth_tmp_orphans(auth_file)
    assert auth_file.exists()


def test_unparseable_pruned_only_when_old(tmp_path):
    auth_file = tmp_path / "auth.json"
    fresh = _mk(tmp_path, "notapid.xyz", age=10)
    old = _mk(tmp_path, "alsobad.xyz", age=90000)  # >24h
    A._prune_auth_tmp_orphans(auth_file)
    assert fresh.exists() and not old.exists()


def test_load_store_triggers_cleanup(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"providers": {}}')
    dead = _mk(tmp_path, "999999.cafef00d")
    A._load_auth_store(auth_file)
    assert not dead.exists()
