"""Tests for plugin auto-update: update --all sweep, autoupdate flag, startup sweep.

Inspired by Copilot CLI v1.0.79's marketplace ``autoUpdate`` setting.
Real-git E2E where the behavior depends on git (pull, revision recording),
mocks only for pure dispatch/throttle logic.
"""

import json
import subprocess as sp
import time
from unittest.mock import MagicMock, patch

import pytest

import hermes_cli.plugins_cmd as pc


def _git(cwd, *args):
    r = sp.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _make_plugin_env(tmp_path, monkeypatch, names=("alpha",)):
    """Create a fake HERMES_HOME with git-installed plugins cloned from origins."""
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    monkeypatch.setattr(pc, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pc, "_plugins_dir", lambda: plugins_dir)

    origins = {}
    for name in names:
        origin = tmp_path / f"origin-{name}"
        origin.mkdir()
        _git(origin, "init", "-q", "-b", "main")
        _git(origin, "config", "user.email", "t@t")
        _git(origin, "config", "user.name", "t")
        (origin / "plugin.yaml").write_text(f"name: {name}\n", encoding="utf-8")
        (origin / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        _git(origin, "add", ".")
        _git(origin, "commit", "-qm", "init")
        checkout = plugins_dir / name
        _git(tmp_path, "clone", "-q", str(origin), str(checkout))
        _git(checkout, "config", "user.email", "t@t")
        _git(checkout, "config", "user.name", "t")
        origins[name] = origin
    return home, plugins_dir, origins


def _advance_origin(origin, value):
    (origin / "mod.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    _git(origin, "add", ".")
    _git(origin, "commit", "-qm", f"bump {value}")


def _write_metadata(home, metadata):
    path = home / "plugins" / pc._INSTALL_METADATA_FILE
    path.write_text(json.dumps(metadata), encoding="utf-8")


def _read_metadata(home):
    path = home / "plugins" / pc._INSTALL_METADATA_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


class TestIterUpdatablePlugins:
    def test_lists_git_dirs_only(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch, ("alpha", "beta"))
        (plugins_dir / "not-git").mkdir()          # plain dir — excluded
        (plugins_dir / ".hidden").mkdir()          # dot dir — excluded
        names = [t.name for t, _ in pc._iter_updatable_plugins()]
        assert names == ["alpha", "beta"]

    def test_missing_plugins_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pc, "_plugins_dir", lambda: tmp_path / "nope")
        assert pc._iter_updatable_plugins() == []


class TestUpdateOnePluginDir:
    def test_pull_updates_and_records_revision(self, tmp_path, monkeypatch):
        home, plugins_dir, origins = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(home, {"alpha": {"pinned": False, "revision": "old", "source": "x"}})
        _advance_origin(origins["alpha"], 2)

        res = pc._update_one_plugin_dir(plugins_dir / "alpha", {})
        assert res["ok"] is True and res["unchanged"] is False
        assert (plugins_dir / "alpha" / "mod.py").read_text() == "VALUE = 2\n"
        rev = _read_metadata(home)["alpha"]["revision"]
        assert len(rev) == 40 and rev != "old"

    def test_unchanged_pull(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        res = pc._update_one_plugin_dir(plugins_dir / "alpha", {})
        assert res["ok"] is True and res["unchanged"] is True

    def test_pinned_is_skipped(self, tmp_path, monkeypatch):
        home, plugins_dir, origins = _make_plugin_env(tmp_path, monkeypatch)
        _advance_origin(origins["alpha"], 3)
        res = pc._update_one_plugin_dir(
            plugins_dir / "alpha", {"pinned": True, "revision": "a" * 40}
        )
        assert res["skipped"] is True
        # Checkout untouched
        assert (plugins_dir / "alpha" / "mod.py").read_text() == "VALUE = 1\n"

    def test_clears_stale_bytecode(self, tmp_path, monkeypatch):
        home, plugins_dir, origins = _make_plugin_env(tmp_path, monkeypatch)
        cache = plugins_dir / "alpha" / "__pycache__"
        cache.mkdir()
        (cache / "mod.cpython-311.pyc").write_bytes(b"stale")
        _advance_origin(origins["alpha"], 4)
        res = pc._update_one_plugin_dir(plugins_dir / "alpha", {})
        assert res["ok"] is True and res["unchanged"] is False
        assert not cache.exists()


class TestUpdateAllPlugins:
    def test_mixed_sweep(self, tmp_path, monkeypatch):
        home, plugins_dir, origins = _make_plugin_env(
            tmp_path, monkeypatch, ("alpha", "beta", "gamma")
        )
        _write_metadata(home, {"beta": {"pinned": True, "revision": "b" * 40, "source": "x"}})
        _advance_origin(origins["alpha"], 9)

        console = MagicMock()
        results = {r["name"]: r for r in pc._update_all_plugins(console)}
        assert results["alpha"]["unchanged"] is False
        assert results["beta"]["skipped"] is True
        assert results["gamma"]["unchanged"] is True


class TestCmdUpdateDispatch:
    def test_all_flag_runs_sweep(self, tmp_path, monkeypatch):
        with patch.object(pc, "_update_all_plugins", return_value=[]) as sweep:
            pc.cmd_update(None, all_plugins=True)
        sweep.assert_called_once()

    def test_name_plus_all_rejected(self):
        with pytest.raises(SystemExit):
            pc.cmd_update("alpha", all_plugins=True)

    def test_no_name_no_all_rejected(self):
        with pytest.raises(SystemExit):
            pc.cmd_update(None)


class TestCmdAutoupdate:
    def test_enable_persists_flag(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(home, {"alpha": {"pinned": False, "revision": "r", "source": "s"}})
        pc.cmd_autoupdate("alpha", "on")
        assert _read_metadata(home)["alpha"]["auto_update"] is True

    def test_disable_removes_flag(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(
            home,
            {"alpha": {"pinned": False, "revision": "r", "source": "s", "auto_update": True}},
        )
        pc.cmd_autoupdate("alpha", "off")
        assert "auto_update" not in _read_metadata(home)["alpha"]

    def test_pinned_plugin_rejected(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(home, {"alpha": {"pinned": True, "revision": "a" * 40, "source": "s"}})
        with pytest.raises(SystemExit):
            pc.cmd_autoupdate("alpha", "on")

    def test_non_git_plugin_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "hh"
        plugins_dir = home / "plugins"
        (plugins_dir / "plain").mkdir(parents=True)
        monkeypatch.setattr(pc, "get_hermes_home", lambda: home)
        monkeypatch.setattr(pc, "_plugins_dir", lambda: plugins_dir)
        with pytest.raises(SystemExit):
            pc.cmd_autoupdate("plain", "on")

    def test_missing_plugin_rejected(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            pc.cmd_autoupdate("ghost", "on")


class TestStartupSweep:
    def test_only_flagged_plugins_pulled(self, tmp_path, monkeypatch):
        home, plugins_dir, origins = _make_plugin_env(
            tmp_path, monkeypatch, ("alpha", "beta")
        )
        _write_metadata(
            home,
            {
                "alpha": {"pinned": False, "revision": "r", "source": "s", "auto_update": True},
                "beta": {"pinned": False, "revision": "r", "source": "s"},
            },
        )
        _advance_origin(origins["alpha"], 7)
        _advance_origin(origins["beta"], 7)

        results = pc.run_startup_auto_update_sweep(force=True)
        assert [r["name"] for r in results] == ["alpha"]
        assert (plugins_dir / "alpha" / "mod.py").read_text() == "VALUE = 7\n"
        assert (plugins_dir / "beta" / "mod.py").read_text() == "VALUE = 1\n"

    def test_no_flagged_plugins_is_noop(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        assert pc.run_startup_auto_update_sweep(force=True) == []
        # No stamp written when nothing is opted in
        assert not pc._autoupdate_stamp_path().exists()

    def test_throttled_by_stamp(self, tmp_path, monkeypatch):
        home, plugins_dir, origins = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(
            home,
            {"alpha": {"pinned": False, "revision": "r", "source": "s", "auto_update": True}},
        )
        stamp = pc._autoupdate_stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()  # fresh stamp → throttled
        assert pc.run_startup_auto_update_sweep() == []

    def test_stale_stamp_runs(self, tmp_path, monkeypatch):
        import os

        home, plugins_dir, origins = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(
            home,
            {"alpha": {"pinned": False, "revision": "r", "source": "s", "auto_update": True}},
        )
        stamp = pc._autoupdate_stamp_path()
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        old = time.time() - (pc._AUTOUPDATE_INTERVAL_SECONDS + 60)
        os.utime(stamp, (old, old))
        results = pc.run_startup_auto_update_sweep()
        assert [r["name"] for r in results] == ["alpha"]
        # Stamp refreshed
        assert time.time() - stamp.stat().st_mtime < 60

    def test_never_raises_on_broken_checkout(self, tmp_path, monkeypatch):
        home, plugins_dir, _ = _make_plugin_env(tmp_path, monkeypatch)
        _write_metadata(
            home,
            {"alpha": {"pinned": False, "revision": "r", "source": "s", "auto_update": True}},
        )
        with patch.object(pc, "_update_one_plugin_dir", side_effect=RuntimeError("boom")):
            assert pc.run_startup_auto_update_sweep(force=True) == []
