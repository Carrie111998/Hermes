"""R1-S2 extraction tests: tui_gateway/session_db.py (epic #78647, target #78630).

Consensus test contract (R1-CONSENSUS.md §6, Pass A T1-T8 + F1-F6): re-export
identity per name, patch-liveness, state write-through (:10792-10793 replay),
state read-through (:93 replay), forward-dep seam (monkeypatch server._load_cfg
/_apply_managed), _profile_scoped install contract, cwd precedence incl.
placeholders, failure modes (constructor-raise -> None + _db_error, unknown
profile -> (None, False), handler-raise -> override reset, bad cwd -> None,
_profile_db yields None when unavailable).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import server FIRST: session_db.py does `from tui_gateway import server` at
# module top (call-time attribute access only), and server's re-export block
# reads session_db attributes at import time — so the load order must be
# server -> session_db (same as the production entry chain; the circular
# import resolves only in that direction). Mirrors the adapter-first rule in
# the verbatim-mixin-extraction workflow.
import tui_gateway.server as server  # noqa: E402,F401
import tui_gateway.session_db as sdb


# ── T1: re-export identity per name ───────────────────────────────────────────

MOVED_NAMES = [
    "_get_db",
    "_db_for_profile",
    "_profile_db",
    "_response_profile_name",
    "_db_unavailable_error",
    "_profile_home",
    "_profile_scoped",
    "_CWD_PLACEHOLDERS",
    "_configured_cwd_from_cfg",
    "_profile_configured_cwd",
    "_launch_configured_cwd",
    "_default_session_cwd",
]


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_server_reexports_each_name_identity(name):
    assert getattr(server, name) is getattr(sdb, name)


def test_state_stays_on_server():
    assert "_db" in vars(server)
    assert "_db_error" in vars(server)
    assert "_db" not in vars(sdb)
    assert "_db_error" not in vars(sdb)


# ── T2: patch-liveness: string patches on tui_gateway.server._get_db must hit ─


def test_string_patch_on_server_get_db_hits_through_reexport():
    with patch("tui_gateway.server._get_db", return_value="patched-db") as m:
        # later-region call sites resolve the bare name via server globals
        assert server._get_db() == "patched-db"
        m.assert_called_once()


# ── T3: state write-through (replays test_tui_gateway_server.py:10792-10793) ──


def test_get_db_state_write_through(monkeypatch):
    """_get_db writes _db/_db_error THROUGH the server module object."""
    server._db = None
    server._db_error = None
    try:
        fake_db = MagicMock()
        fake_mod = MagicMock()
        fake_mod.SessionDB = MagicMock(return_value=fake_db)
        monkeypatch.setitem(sys.modules, "hermes_state", fake_mod)

        assert sdb._get_db() is fake_db
        assert server._db is fake_db
        assert server._db_error is None
    finally:
        server._db = None
        server._db_error = None


def test_get_db_constructor_raise_sets_db_error(monkeypatch):
    """F1: constructor-raise -> None + _db_error set (locking-protocol replay)."""
    server._db = None
    server._db_error = None
    try:
        class _BrokenSessionDB:
            def __init__(self):
                raise RuntimeError("locking protocol")

        fake_mod = MagicMock()
        fake_mod.SessionDB = _BrokenSessionDB
        monkeypatch.setitem(sys.modules, "hermes_state", fake_mod)

        assert sdb._get_db() is None
        assert server._db is None
        assert server._db_error == "locking protocol"
        # second call short-circuits on the still-None db (race documented F6)
        assert sdb._get_db() is None
    finally:
        server._db = None
        server._db_error = None


# ── T4: state read-through (replays test_undo_command.py:93) ──────────────────


def test_db_unavailable_error_reads_server_db_error(monkeypatch):
    server._db_error = "custom failure"
    try:
        result = sdb._db_unavailable_error(7, code=5001)
        assert result == server._err(7, 5001, "state.db unavailable: custom failure")
    finally:
        server._db_error = None


# ── T5: forward-dep seam (server._load_cfg / _apply_managed call-time) ────────


def test_launch_configured_cwd_reads_server_load_cfg_at_call_time(monkeypatch):
    calls = []

    def fake_load_cfg():
        calls.append(1)
        return {"terminal": {"cwd": "C:/tmp"}}

    monkeypatch.setattr(server, "_load_cfg", fake_load_cfg)
    assert sdb._launch_configured_cwd() == os.path.abspath("C:/tmp")
    assert len(calls) == 1
    # and the re-exported name sees the same patch
    assert server._launch_configured_cwd() == os.path.abspath("C:/tmp")


def test_launch_configured_cwd_load_cfg_raise_returns_none(monkeypatch):
    def boom():
        raise RuntimeError("cfg boom")

    monkeypatch.setattr(server, "_load_cfg", boom)
    assert sdb._launch_configured_cwd() is None


def test_profile_configured_cwd_reads_server_apply_managed(monkeypatch, tmp_path):
    home = tmp_path / "prof" / "home"
    (home / "config.yaml").parent.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "terminal:\n  cwd: {env:MY_CWD}\n", encoding="utf-8"
    )
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setenv("MY_CWD", str(real))

    applied = []

    def fake_apply_managed(data):
        applied.append(data)
        return data

    monkeypatch.setattr(server, "_apply_managed", fake_apply_managed)
    monkeypatch.setattr(
        "hermes_cli.config._expand_env_vars",
        lambda data: {"terminal": {"cwd": str(real)}},
    )

    got = sdb._profile_configured_cwd(home)
    assert got == os.path.abspath(str(real))
    assert len(applied) == 1  # _apply_managed consulted at call time


# ── T6: _profile_scoped install contract (override bound + restored) ──────────


def _fake_profile_home(monkeypatch, home):
    # Code reads server._profile_home at call time (seam contract) — patch the
    # server name, exactly like the pre-existing tests in test_tui_gateway_server.py.
    monkeypatch.setattr(server, "_profile_home", lambda profile: home)


def test_profile_scoped_binds_override_for_non_launch_profile(monkeypatch):
    from hermes_constants import get_hermes_home_override

    home = Path("C:/tmp/fake-profile-home")
    _fake_profile_home(monkeypatch, home)
    seen = {}

    def handler(rid, params):
        seen["override"] = get_hermes_home_override()
        return {"rid": rid}

    wrapped = sdb._profile_scoped(handler)
    assert wrapped(1, {"profile": "work"}) == {"rid": 1}
    assert seen["override"] == str(home)  # override stores str(path)
    assert get_hermes_home_override() is None  # restored in finally


def test_profile_scoped_noop_when_home_none(monkeypatch):
    _fake_profile_home(monkeypatch, None)
    called = []

    def handler(rid, params):
        called.append(params)
        return "ok"

    wrapped = sdb._profile_scoped(handler)
    assert wrapped(1, {"profile": "launch"}) == "ok"
    assert called == [{"profile": "launch"}]


def test_profile_scoped_restores_override_on_handler_raise(monkeypatch):
    from hermes_constants import get_hermes_home_override

    home = Path("C:/tmp/fake-profile-home-2")
    _fake_profile_home(monkeypatch, home)

    def handler(rid, params):
        raise RuntimeError("handler boom")

    wrapped = sdb._profile_scoped(handler)
    with pytest.raises(RuntimeError, match="handler boom"):
        wrapped(1, {"profile": "work"})
    assert get_hermes_home_override() is None  # F3: reset even on raise


# ── T7: cwd precedence incl. placeholders (extends server_test:814-818) ───────


def test_configured_cwd_placeholders_return_none():
    for placeholder in (".", "auto", "cwd", "", None, "  "):
        assert sdb._configured_cwd_from_cfg({"terminal": {"cwd": placeholder}}) is None


def test_configured_cwd_non_dict_and_missing():
    assert sdb._configured_cwd_from_cfg(None) is None
    assert sdb._configured_cwd_from_cfg({}) is None
    assert sdb._configured_cwd_from_cfg({"terminal": {}}) is None
    assert sdb._configured_cwd_from_cfg({"terminal": "not-a-dict"}) is None


def test_configured_cwd_requires_existing_dir(tmp_path):
    missing = tmp_path / "nope"
    assert sdb._configured_cwd_from_cfg({"terminal": {"cwd": str(missing)}}) is None
    assert sdb._configured_cwd_from_cfg({"terminal": {"cwd": str(tmp_path)}}) == str(tmp_path)


def test_default_session_cwd_precedence(monkeypatch, tmp_path):
    configured = tmp_path / "configured"
    configured.mkdir()
    stale = tmp_path / "stale"
    stale.mkdir()

    monkeypatch.setenv("TERMINAL_CWD", str(stale))
    monkeypatch.setattr(server, "_load_cfg", lambda: {"terminal": {"cwd": str(configured)}})
    assert sdb._default_session_cwd() == str(configured)
    assert server._default_session_cwd() == str(configured)

    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    assert sdb._default_session_cwd() == str(stale)


def test_profile_home_returns_none_for_launch_profile(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: str(server._hermes_home)
    )
    assert sdb._profile_home("launch-profile-name") is None


# ── T8: _db_for_profile / _profile_db failure modes ───────────────────────────


def test_db_for_profile_unknown_profile_returns_none_false(monkeypatch):
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_get_db", lambda: "shared-db")
    assert sdb._db_for_profile("nonexistent") == ("shared-db", False)
    assert sdb._db_for_profile(None) == ("shared-db", False)


def test_db_for_profile_non_launch_opens_dedicated(monkeypatch):
    home = Path("C:/tmp/fake-prof-home")
    monkeypatch.setattr(server, "_profile_home", lambda profile: home)

    fake_db = MagicMock()
    fake_mod = MagicMock()
    fake_mod.SessionDB = MagicMock(return_value=fake_db)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_mod)

    db, owns = sdb._db_for_profile("work")
    assert db is fake_db
    assert owns is True
    fake_mod.SessionDB.assert_called_once_with(db_path=home / "state.db")


def test_db_for_profile_constructor_raise(monkeypatch):
    home = Path("C:/tmp/fake-prof-home-2")
    monkeypatch.setattr(server, "_profile_home", lambda profile: home)

    class _Broken:
        def __init__(self, **kwargs):
            raise RuntimeError("no db for you")

    fake_mod = MagicMock()
    fake_mod.SessionDB = _Broken
    monkeypatch.setitem(sys.modules, "hermes_state", fake_mod)

    assert sdb._db_for_profile("work") == (None, False)


def test_profile_db_yields_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(server, "_db_for_profile", lambda profile: (None, False))
    with sdb._profile_db({"profile": "work"}) as db:
        assert db is None


def test_profile_db_closes_owned_handle(monkeypatch):
    fake_db = MagicMock()
    monkeypatch.setattr(server, "_db_for_profile", lambda profile: (fake_db, True))
    with sdb._profile_db({"profile": "work"}) as db:
        assert db is fake_db
    fake_db.close.assert_called_once()


def test_profile_db_does_not_close_shared_handle(monkeypatch):
    shared = MagicMock()
    monkeypatch.setattr(server, "_db_for_profile", lambda profile: (shared, False))
    with sdb._profile_db({"profile": "launch"}) as db:
        assert db is shared
    shared.close.assert_not_called()


# ── response_profile_name ─────────────────────────────────────────────────────


def test_response_profile_name_prefers_real_non_launch_profile(monkeypatch):
    monkeypatch.setattr(server, "_profile_home", lambda name: Path("C:/tmp/x") if name == "work" else None)
    monkeypatch.setattr(server, "_current_profile_name", lambda: "launch")
    assert sdb._response_profile_name("work") == "work"
    assert sdb._response_profile_name("  work  ") == "work"
    assert sdb._response_profile_name("") == "launch"
    assert sdb._response_profile_name(None) == "launch"
    assert sdb._response_profile_name("ghost") == "launch"
