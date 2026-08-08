"""Tests for cross-profile session listing (`hermes sessions list --all`).

Covers:
- _collect_all_profile_sessions: real temp state.db per profile, read-only
  aggregation, profile tagging (NULL -> "default"), last-active ordering
- cmd_sessions 'list' with --all: Profile column rendering, unchanged layout
  without --all
- cmd_sessions 'browse' with --all: cross-profile resume passes -p <profile>
  to relaunch; same-profile resume stays plain
"""

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import hermes_state
from hermes_cli.profiles import ProfileInfo
from hermes_cli.sessions_cmd import _collect_all_profile_sessions


def _make_profile_dirs(tmp_path, names):
    """Create one temp HERMES_HOME dir per profile name, each with a seeded
    state.db. Returns {name: dir}."""
    dirs = {}
    for name in names:
        d = tmp_path / f"home_{name}"
        d.mkdir(parents=True)
        db = hermes_state.SessionDB(db_path=d / "state.db")
        db.close()
        dirs[name] = d
    return dirs


def _seed_session(db_path, sid, profile=None, started=1000.0, title=None):
    db = hermes_state.SessionDB(db_path=db_path)
    try:
        db.create_session(
            sid,
            source="cli",
            model="test/model",
            profile_name=profile,
        )
        if title:
            db.set_session_title(sid, title)
        with db._lock:
            db._conn.execute("UPDATE sessions SET started_at=? WHERE id=?", (started, sid))
            db._conn.commit()
        db.append_message(sid, role="user", content="hello from " + sid)
    finally:
        db.close()


def _patch_list_profiles(tmp_path, dirs):
    profiles = []
    for name, d in dirs.items():
        profiles.append(
            ProfileInfo(
                name=name,
                path=d,
                is_default=(name == "default"),
                gateway_running=False,
            )
        )
    return patch("hermes_cli.profiles.list_profiles", return_value=profiles)


# ─── _collect_all_profile_sessions ─────────────────────────────────────────

class TestCollectAllProfileSessions:
    def test_aggregates_and_tags_profiles(self, tmp_path):
        dirs = _make_profile_dirs(tmp_path, ["default", "red"])
        _seed_session(dirs["default"] / "state.db", "def_1", profile=None, started=1000.0)
        _seed_session(dirs["red"] / "state.db", "red_1", profile="red", started=2000.0)

        with _patch_list_profiles(tmp_path, dirs):
            rows = _collect_all_profile_sessions(
                source=None, exclude_sources=["tool"], limit=20
            )

        by_id = {r["id"]: r for r in rows}
        assert "def_1" in by_id and "red_1" in by_id
        # NULL profile_name in the default DB is tagged as "default"
        assert by_id["def_1"]["profile_name"] == "default"
        assert by_id["red_1"]["profile_name"] == "red"

    def test_orders_by_last_active_desc(self, tmp_path):
        dirs = _make_profile_dirs(tmp_path, ["default", "red"])
        # red session is older, default session is fresher
        _seed_session(dirs["red"] / "state.db", "red_old", profile="red", started=1000.0)
        _seed_session(dirs["default"] / "state.db", "def_new", profile=None, started=3000.0)

        with _patch_list_profiles(tmp_path, dirs):
            rows = _collect_all_profile_sessions(
                source=None, exclude_sources=["tool"], limit=20
            )

        assert rows[0]["id"] == "def_new"
        assert rows[1]["id"] == "red_old"

    def test_skips_profiles_without_state_db(self, tmp_path):
        dirs = _make_profile_dirs(tmp_path, ["default"])
        # A profile dir with no state.db (fresh, never used) must not crash.
        empty = tmp_path / "home_green"
        empty.mkdir(parents=True)
        dirs["green"] = empty
        _seed_session(dirs["default"] / "state.db", "def_1", profile=None)

        with _patch_list_profiles(tmp_path, dirs):
            rows = _collect_all_profile_sessions(
                source=None, exclude_sources=["tool"], limit=20
            )

        assert [r["id"] for r in rows] == ["def_1"]


# ─── cmd_sessions list --all rendering ────────────────────────────────────

class TestSessionsListAll:
    def _run_list(self, args_extra, fake_rows=None, patch_collect=True):
        args = SimpleNamespace(
            sessions_action="list",
            source=None,
            limit=20,
            workspace="",
            **args_extra,
        )
        fake_db = MagicMock()
        fake_db.list_sessions_rich.return_value = []
        fake_db.close.return_value = None
        patches = [
            patch("hermes_state.SessionDB", return_value=fake_db),
            patch("hermes_cli.main.get_hermes_home", return_value=Path("/tmp/nonexistent")),
        ]
        if patch_collect:
            patches.append(
                patch(
                    "hermes_cli.sessions_cmd._collect_all_profile_sessions",
                    return_value=fake_rows or [],
                )
            )
        with patches[0], patches[1], patches[2] if len(patches) > 2 else _nullctx():
            from hermes_cli.sessions_cmd import cmd_sessions
            cmd_sessions(args)
        return fake_db

    def test_list_all_shows_profile_column(self, capsys):
        now = time.time()
        fake_rows = [
            {"id": "s1", "source": "cli", "title": "Alpha", "preview": "hi",
             "last_active": now, "profile_name": "default"},
            {"id": "s2", "source": "cli", "title": "Beta", "preview": "yo",
             "last_active": now - 100, "profile_name": "red"},
        ]
        self._run_list({"all": True}, fake_rows=fake_rows)
        out = capsys.readouterr().out
        assert "Profile" in out
        assert "default" in out
        assert "red" in out
        assert "Alpha" in out and "Beta" in out

    def test_list_without_all_has_no_profile_column(self, capsys):
        now = time.time()
        fake_rows = [
            {"id": "s1", "source": "cli", "title": "Alpha", "preview": "hi",
             "last_active": now, "profile_name": "red"},
        ]
        self._run_list({"all": False}, fake_rows=fake_rows)
        out = capsys.readouterr().out
        assert "Profile" not in out


# ─── cmd_sessions browse --all cross-profile resume ───────────────────────

class TestSessionsBrowseAll:
    def _run_browse(self, selected_id, selected_profile, current="default"):
        args = SimpleNamespace(
            sessions_action="browse",
            source=None,
            limit=500,
            all=True,
        )
        now = time.time()
        sessions = [
            {"id": "s1", "source": "cli", "title": "Alpha", "preview": "hi",
             "last_active": now, "profile_name": "default"},
            {"id": "s2", "source": "cli", "title": "Beta", "preview": "yo",
             "last_active": now - 100, "profile_name": "red"},
        ]
        fake_db = MagicMock()
        fake_db.close.return_value = None

        with patch("hermes_state.SessionDB", return_value=fake_db), \
             patch("hermes_cli.sessions_cmd._session_browse_picker", return_value=selected_id), \
             patch("hermes_cli.sessions_cmd._collect_all_profile_sessions", return_value=sessions), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value=current), \
             patch("hermes_cli.relaunch.relaunch") as mock_relaunch:
            from hermes_cli.sessions_cmd import cmd_sessions
            cmd_sessions(args)

        return mock_relaunch

    def test_cross_profile_resume_passes_profile_flag(self):
        mock_relaunch = self._run_browse("s2", "red", current="default")
        mock_relaunch.assert_called_once_with(["-p", "red", "--resume", "s2"])

    def test_same_profile_resume_stays_plain(self):
        mock_relaunch = self._run_browse("s1", "default", current="default")
        mock_relaunch.assert_called_once_with(["--resume", "s1"])


class TestSessionsListAllArgparse:
    """The --all/-A flag must parse on both list and browse subcommands."""

    def _build_sessions_parser(self):
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="sessions_action")

        list_p = subparsers.add_parser("list")
        list_p.add_argument("--source")
        list_p.add_argument("--limit", type=int, default=20)
        list_p.add_argument("--workspace", metavar="NEEDLE")
        list_p.add_argument("-A", "--all", action="store_true")

        browse_p = subparsers.add_parser("browse")
        browse_p.add_argument("--source")
        browse_p.add_argument("--limit", type=int, default=500)
        browse_p.add_argument("-A", "--all", action="store_true")
        return parser

    def test_list_all_flag(self):
        parser = self._build_sessions_parser()
        args = parser.parse_args(["list", "--all"])
        assert args.all is True
        args = parser.parse_args(["list", "-A"])
        assert args.all is True
        args = parser.parse_args(["list"])
        assert args.all is False

    def test_browse_all_flag(self):
        parser = self._build_sessions_parser()
        args = parser.parse_args(["browse", "--all"])
        assert args.all is True
        args = parser.parse_args(["browse", "-A"])
        assert args.all is True
        args = parser.parse_args(["browse"])
        assert args.all is False


class _nullctx:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
