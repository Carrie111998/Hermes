"""Tests for the real-profile cookie-purge detection (#96993).

Chrome >= 151 on Windows binds cookie encryption to the original profile, so
the copy-browser's first launch actively purges the cookies the snapshot just
copied in (556 -> 6, 3507 -> ~0 in the issue's measurements). The fix detects
the drop and surfaces a notice on the first navigation instead of letting the
agent discover site-by-site login failures.

The detection is deliberately platform-independent (a before/after cookie
count around the launch), so these tests run everywhere: they build real
SQLite cookie DBs in tmp_path and exercise the counting helper, the purge
predicate, and the notice -> session wiring.
"""
import os
import sqlite3
from unittest.mock import patch

import pytest


def _write_cookie_db(path: str, count: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = [("host.example", "c", b"v10x")] * count
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cookies (host_key TEXT, name TEXT, encrypted_value BLOB)"
        )
        conn.execute("DELETE FROM cookies")
        conn.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_real_profile_state():
    import tools.browser_tool as bt

    bt._real_profile_cdp_cache.pop("cdp", None)
    bt._real_profile_purge_notice.pop("msg", None)
    yield
    bt._real_profile_cdp_cache.pop("cdp", None)
    bt._real_profile_purge_notice.pop("msg", None)


class TestCountRealProfileCookies:
    def test_counts_network_location(self, tmp_path):
        from tools.browser_tool import _count_real_profile_cookies

        copy_dir = str(tmp_path)
        _write_cookie_db(os.path.join(copy_dir, "Default", "Network", "Cookies"), 7)
        assert _count_real_profile_cookies(copy_dir) == 7

    def test_falls_back_to_legacy_root_location(self, tmp_path):
        from tools.browser_tool import _count_real_profile_cookies

        copy_dir = str(tmp_path)
        _write_cookie_db(os.path.join(copy_dir, "Default", "Cookies"), 3)
        assert _count_real_profile_cookies(copy_dir) == 3

    def test_prefers_network_location_when_both_exist(self, tmp_path):
        from tools.browser_tool import _count_real_profile_cookies

        copy_dir = str(tmp_path)
        _write_cookie_db(os.path.join(copy_dir, "Default", "Cookies"), 100)
        _write_cookie_db(os.path.join(copy_dir, "Default", "Network", "Cookies"), 9)
        assert _count_real_profile_cookies(copy_dir) == 9

    def test_missing_db_returns_none(self, tmp_path):
        from tools.browser_tool import _count_real_profile_cookies

        assert _count_real_profile_cookies(str(tmp_path)) is None

    def test_non_sqlite_file_returns_none(self, tmp_path):
        from tools.browser_tool import _count_real_profile_cookies

        db = tmp_path / "Default" / "Network" / "Cookies"
        db.parent.mkdir(parents=True)
        db.write_text("this is not a database")
        assert _count_real_profile_cookies(str(tmp_path)) is None

    def test_empty_jar_is_zero_not_none(self, tmp_path):
        from tools.browser_tool import _count_real_profile_cookies

        copy_dir = str(tmp_path)
        _write_cookie_db(os.path.join(copy_dir, "Default", "Network", "Cookies"), 0)
        assert _count_real_profile_cookies(copy_dir) == 0


class TestCookiesPurgedAfterLaunch:
    @pytest.mark.parametrize(
        "before,after,expected",
        [
            # The reported purge shapes: 556 -> 6 and 3507 -> ~0.
            (556, 6, True),
            (3507, 0, True),
            # Normal startup churn (expired-cookie sweep, visitor cookies).
            (556, 550, False),
            (120, 121, False),
            # Baseline too small to be worth a warning.
            (4, 0, False),
            # Unknown counts (locked / missing DB) never claim a purge.
            (None, 0, False),
            (556, None, False),
            (None, None, False),
            # Exactly halving is not a purge; anything past it is.
            (100, 50, False),
            (100, 49, True),
        ],
    )
    def test_predicate(self, before, after, expected):
        from tools.browser_tool import _cookies_purged_after_launch

        assert _cookies_purged_after_launch(before, after) is expected


class TestPurgeNoticeWiring:
    def test_create_local_session_carries_notice_once(self):
        """A pending purge notice lands on the session features and is consumed."""
        import tools.browser_tool as bt

        bt._real_profile_purge_notice["msg"] = "purge notice under test"
        with patch.object(bt, "_real_profile_cdp", return_value=("http://127.0.0.1:9222", None)), \
             patch.object(bt, "_resolve_cdp_override", side_effect=lambda u: u):
            first = bt._create_local_session("task-1")
            second = bt._create_local_session("task-2")

        assert first["features"]["real_profile"] is True
        assert first["features"]["real_profile_cookies_purged"] is True
        assert first["real_profile_purge_warning"] == "purge notice under test"
        # One-shot: sessions created later (the copy-browser is reused, the
        # launch — and therefore the purge — already happened) stay clean.
        assert "real_profile_cookies_purged" not in second["features"]
        assert "real_profile_purge_warning" not in second

    def test_create_local_session_without_notice_unchanged(self):
        import tools.browser_tool as bt

        with patch.object(bt, "_real_profile_cdp", return_value=("http://127.0.0.1:9222", None)), \
             patch.object(bt, "_resolve_cdp_override", side_effect=lambda u: u):
            session = bt._create_local_session("task-1")
        assert session["features"] == {"local": True, "real_profile": True}
        assert "real_profile_purge_warning" not in session


def _arm_real_profile_launch(monkeypatch, copy_dir, on_cdp_visible):
    """Stub everything _real_profile_cdp needs around the (fake) launch."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "_use_real_profile", lambda: True)
    monkeypatch.setattr(bt, "_using_lightpanda_engine", lambda: False)
    monkeypatch.setattr(bt, "_find_agent_browser", lambda: "agent-browser")
    monkeypatch.setattr(bt, "_agent_browser_argv", lambda cmd: [cmd])
    monkeypatch.setattr(bt, "_build_browser_env", lambda: dict(os.environ))
    monkeypatch.setattr(bt, "_get_open_command_timeout", lambda first_open=False: 30)
    monkeypatch.setattr(bt, "_cdp_http_ready", lambda c: False)
    monkeypatch.setattr(bt, "_cdp_on_data_dir", lambda c, d: False)
    monkeypatch.setattr(bt, "_agent_browser_close_session", lambda s: None)

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = ""

    monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: FakeProc())

    def cdp_visible(session):
        # First call is the pre-launch reuse probe (no copy-browser is up) —
        # report none. The second call is the post-launch endpoint lookup,
        # which is where the (fake) browser gets to rewrite the cookie jar.
        if cdp_visible.seen:
            on_cdp_visible()
            return "http://127.0.0.1:9222"
        cdp_visible.seen = True
        return None

    cdp_visible.seen = False

    monkeypatch.setattr(bt, "_agent_browser_get_cdp", cdp_visible)

    import hermes_cli.browser_connect as bc

    monkeypatch.setattr(bc, "detect_default_chromium", lambda system=None: "chrome")
    monkeypatch.setattr(bc, "real_profile_copy_dir", lambda browser: copy_dir)
    monkeypatch.setattr(
        bc, "snapshot_real_profile", lambda browser, src=None: (copy_dir, None)
    )


class TestPurgeDetectionAroundLaunch:
    def test_detected_drop_sets_notice_and_keeps_launching(self, tmp_path, monkeypatch):
        """End-to-end through _real_profile_cdp: snapshot counts 8, launch leaves 1.

        The heavy launch path is stubbed; what is under test is the real
        counting against real DB files and the decision that follows it —
        the purge must produce a notice, not a launch failure.
        """
        import tools.browser_tool as bt

        copy_dir = str(tmp_path / "browser-profile" / "chrome")
        cookies = os.path.join(copy_dir, "Default", "Network", "Cookies")
        _write_cookie_db(cookies, 8)

        def purge_on_launch():
            # The purge: Chrome rewrites the jar down to one survivor.
            _write_cookie_db(cookies, 1)

        _arm_real_profile_launch(monkeypatch, copy_dir, purge_on_launch)

        cdp, err = bt._real_profile_cdp()
        assert cdp == "http://127.0.0.1:9222"
        assert err is None
        assert "#96993" in bt._real_profile_purge_notice["msg"]

    def test_surviving_jar_sets_no_notice(self, tmp_path, monkeypatch):
        """No meaningful drop -> no notice; the launch result is untouched."""
        import tools.browser_tool as bt

        copy_dir = str(tmp_path / "browser-profile" / "chrome")
        cookies = os.path.join(copy_dir, "Default", "Network", "Cookies")
        _write_cookie_db(cookies, 8)

        def churn_on_launch():
            # Normal startup churn only: one cookie expired.
            _write_cookie_db(cookies, 7)

        _arm_real_profile_launch(monkeypatch, copy_dir, churn_on_launch)

        cdp, err = bt._real_profile_cdp()
        assert cdp == "http://127.0.0.1:9222"
        assert err is None
        assert "msg" not in bt._real_profile_purge_notice

    def test_missing_post_launch_db_never_claims_purge(self, tmp_path, monkeypatch):
        """A post-launch DB that cannot be read must not fabricate a purge notice."""
        import tools.browser_tool as bt

        copy_dir = str(tmp_path / "browser-profile" / "chrome")
        cookies = os.path.join(copy_dir, "Default", "Network", "Cookies")
        _write_cookie_db(cookies, 8)

        def remove_db_on_launch():
            os.unlink(cookies)

        _arm_real_profile_launch(monkeypatch, copy_dir, remove_db_on_launch)

        cdp, err = bt._real_profile_cdp()
        assert cdp == "http://127.0.0.1:9222"
        assert err is None
        assert "msg" not in bt._real_profile_purge_notice
