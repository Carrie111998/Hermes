"""Tests: profiles.list avatar fingerprint fields (has_avatar / avatar_size /
avatar_mtime).

Why: #95613 — the desktop Bots roster cached avatar images locally and never
revalidated them, so a server-side avatar change (CLI ``profiles.set_asset``,
an edit on another machine) left stale art on the roster until the cache was
wiped by hand. The gateway now reports a cheap fingerprint — file size and
mtime from a plain stat, no asset read — next to the existing ``has_avatar``
existence flag, so clients can detect a server-side avatar replacement or
removal without probing the asset store on every 5s roster poll.

Contract under test:
- Every profile row carries ``has_avatar`` (bool), ``avatar_size`` and
  ``avatar_mtime`` (int, or None when the profile has no avatar file).
- ``profiles.set_asset`` is immediately visible to the next ``profiles.list``
  (same file the roster stats), and ``clear`` resets all three fields.
"""

from __future__ import annotations

import pytest

import tui_gateway.server as srv


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Temp HERMES_HOME with one named profile."""
    h = tmp_path / ".hermes"
    (h / "profiles" / "ops").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _profiles(params):
    envelope = srv._methods["profiles.list"](1, params)
    return envelope["result"]["profiles"]


def _row(profiles, name):
    return next(p for p in profiles if p["name"] == name)


def test_avatar_fingerprint_is_null_without_an_asset(home):
    row = _row(_profiles({}), "ops")
    assert row["has_avatar"] is False
    assert row["avatar_size"] is None
    assert row["avatar_mtime"] is None


def test_avatar_fingerprint_reports_an_existing_asset(home):
    assets = home / "profiles" / "ops" / "assets"
    assets.mkdir()
    avatar = assets / "avatar.png"
    avatar.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    row = _row(_profiles({}), "ops")
    assert row["has_avatar"] is True
    assert row["avatar_size"] == avatar.stat().st_size
    assert isinstance(row["avatar_mtime"], int)
    assert row["avatar_mtime"] == avatar.stat().st_mtime_ns


def test_avatar_fingerprint_follows_set_asset_and_clear(home):
    assets = home / "profiles" / "ops" / "assets"

    envelope = srv._methods["profiles.set_asset"](1, {
        "name": "ops",
        "asset": "avatar",
        "data": "data:image/png;base64,iVBORw0KGgo=",
    })
    assert envelope["result"]["ok"] is True

    target = assets / "avatar.png"
    row = _row(_profiles({}), "ops")
    assert row["has_avatar"] is True
    assert row["avatar_size"] == target.stat().st_size
    assert row["avatar_mtime"] == target.stat().st_mtime_ns

    srv._methods["profiles.set_asset"](1, {"name": "ops", "asset": "avatar", "clear": True})
    row = _row(_profiles({}), "ops")
    assert row["has_avatar"] is False
    assert row["avatar_size"] is None
    assert row["avatar_mtime"] is None
