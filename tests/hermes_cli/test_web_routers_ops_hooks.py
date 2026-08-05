"""Regression tests for the extracted ops/hooks router modules (s4 w1a).

Covers the pure logic moved out of ``hermes_cli/web_server.py`` into
``hermes_cli/web_routers/ops.py`` (c11) and ``hermes_cli/web_routers/hooks.py``
(c12): backup-path helpers, checkpoint listing, upload-name sanitising, and
hook config writes — plus route registration on the new routers.

Sandbox mode: when the patch is not yet applied to the working tree
(``S4_W1A_SANDBOX_NEW`` env var points at the patch's ``new/`` tree), the
modules are loaded from that tree by file path so the tests exercise the exact
extracted code.  In the applied repo the normal import path is used.
"""

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SANDBOX_NEW = os.environ.get("S4_W1A_SANDBOX_NEW")


def _load_router(name: str):
    """Import ``hermes_cli.web_routers.<name>``, falling back to the sandbox
    ``new/`` tree when the module does not exist in the working tree yet."""
    modname = f"hermes_cli.web_routers.{name}"
    try:
        return importlib.import_module(modname)
    except ImportError:
        if not _SANDBOX_NEW:
            raise
    path = Path(_SANDBOX_NEW) / "hermes_cli" / "web_routers" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(modname, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


ops = _load_router("ops")
hooks = _load_router("hooks")


# ---------------------------------------------------------------------------
# c11 ops helpers
# ---------------------------------------------------------------------------


def test_safe_backup_upload_name_sanitises():
    assert ops._safe_backup_upload_name("my backup v2.zip") == "my-backup-v2.zip"
    assert ops._safe_backup_upload_name("../../etc/passwd") == "passwd.zip"
    assert ops._safe_backup_upload_name("") == "backup.zip"
    assert ops._safe_backup_upload_name(None) == "backup.zip"
    assert ops._safe_backup_upload_name("scan.tar.gz") == "scan.tar.gz.zip"
    assert ops._safe_backup_upload_name("already.zip") == "already.zip"


def test_dashboard_backup_dir_and_new_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "get_hermes_home", lambda: tmp_path)
    assert ops._dashboard_backup_dir() == tmp_path / "backups"
    p = ops._new_dashboard_backup_path()
    assert p.parent == tmp_path / "backups"
    assert p.name.startswith("hermes-backup-")
    assert p.suffix == ".zip"
    # Unique per call (timestamp + token hex).
    assert ops._new_dashboard_backup_path() != p


def test_list_checkpoints(monkeypatch, tmp_path):
    home = tmp_path
    cp = home / "checkpoints"
    (cp / "sess-a").mkdir(parents=True)
    (cp / "sess-b").mkdir()
    (cp / "sess-a" / "x.json").write_text("a" * 10)
    (cp / "sess-a" / "y.json").write_text("a" * 5)
    (cp / "sess-b" / "z.json").write_text("a" * 7)
    (cp / "not-a-dir.txt").write_text("skip me")
    monkeypatch.setattr(ops, "get_hermes_home", lambda: home)
    result = asyncio.run(ops.list_checkpoints())
    sessions = {s["session"]: s for s in result["sessions"]}
    assert set(sessions) == {"sess-a", "sess-b"}
    assert sessions["sess-a"]["files"] == 2
    assert sessions["sess-a"]["bytes"] == 15
    assert sessions["sess-b"]["bytes"] == 7
    assert result["total_bytes"] == 22


def test_list_checkpoints_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "get_hermes_home", lambda: tmp_path)
    result = asyncio.run(ops.list_checkpoints())
    assert result == {"sessions": [], "total_bytes": 0}


# ---------------------------------------------------------------------------
# c12 hooks helpers
# ---------------------------------------------------------------------------


def test_create_hook_writes_config(monkeypatch):
    cfg: dict = {}
    saved: list = []

    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "save_config", lambda c: saved.append(c))

    import agent.shell_hooks as real_shell_hooks
    monkeypatch.setattr(real_shell_hooks, "_record_approval", lambda *a, **k: None)

    class _Body:
        event = "on_session_start"
        command = "scripts/notify.sh"
        matcher = "*"
        timeout = 30
        approve = False

    result = asyncio.run(hooks.create_hook(_Body()))
    assert result["ok"] is True
    assert cfg["hooks"]["on_session_start"] == [
        {"command": "scripts/notify.sh", "matcher": "*", "timeout": 30}
    ]
    assert saved[-1] is cfg


def test_delete_hook_removes_and_revokes(monkeypatch):
    cfg = {"hooks": {"on_session_start": [{"command": "scripts/notify.sh"}]}}
    saved: list = []

    monkeypatch.setattr(hooks, "load_config", lambda: cfg)
    monkeypatch.setattr(hooks, "save_config", lambda c: saved.append(c))

    import agent.shell_hooks as real_shell_hooks
    revoked: list = []
    monkeypatch.setattr(real_shell_hooks, "revoke", lambda cmd: revoked.append(cmd))

    class _Body:
        event = "on_session_start"
        command = "scripts/notify.sh"

    result = asyncio.run(hooks.delete_hook(_Body()))
    assert result == {"ok": True}
    assert cfg == {}
    assert revoked == ["scripts/notify.sh"]


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_ops_router_registers_all_eight_ops_routes():
    paths = {(r.path, tuple(sorted(r.methods))) for r in ops.router.routes}
    assert ("/api/ops/doctor", ("POST",)) in paths
    assert ("/api/ops/security-audit", ("POST",)) in paths
    assert ("/api/ops/backup", ("POST",)) in paths
    assert ("/api/ops/backup/download", ("GET",)) in paths
    assert ("/api/ops/import", ("POST",)) in paths
    assert ("/api/ops/import-upload", ("POST",)) in paths
    assert ("/api/ops/checkpoints", ("GET",)) in paths
    assert ("/api/ops/checkpoints/prune", ("POST",)) in paths


def test_hooks_router_registers_all_three_routes():
    paths = {(r.path, tuple(sorted(r.methods))) for r in hooks.router.routes}
    assert ("/api/ops/hooks", ("GET",)) in paths
    assert ("/api/ops/hooks", ("POST",)) in paths
    assert ("/api/ops/hooks", ("DELETE",)) in paths
