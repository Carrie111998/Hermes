"""Tests for sensitive-domain exclusions in the real-profile snapshot.

Inspired by Claude Cowork's cookie import (banking/email/SSO unchecked by
default): Hermes scrubs excluded domains' cookies and saved logins out of the
hermes-owned snapshot copy before any launch. Real SQLite I/O throughout —
the DBs are genuine Chromium-shaped ``cookies`` / ``logins`` tables.
"""
import os
import sqlite3

import pytest

import hermes_cli.browser_connect as bc


def _make_cookie_db(path, hosts):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    con.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?)",
        [(h, "sid", "v") for h in hosts],
    )
    con.commit()
    con.close()


def _make_login_db(path, origins):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE logins (id INTEGER PRIMARY KEY, origin_url TEXT, signon_realm TEXT)"
    )
    con.executemany(
        "INSERT INTO logins (origin_url, signon_realm) VALUES (?, ?)",
        [(o, o) for o in origins],
    )
    con.commit()
    con.close()


def _cookie_hosts(path):
    con = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in con.execute("SELECT host_key FROM cookies"))
    finally:
        con.close()


def _login_origins(path):
    con = sqlite3.connect(path)
    try:
        return sorted(r[0] for r in con.execute("SELECT origin_url FROM logins"))
    finally:
        con.close()


class TestHostMatching:
    def test_exact_and_subdomain(self):
        pats = ("chase.com",)
        assert bc._host_matches_exclude("chase.com", pats)
        assert bc._host_matches_exclude("www.chase.com", pats)
        assert bc._host_matches_exclude(".chase.com", pats)  # Chromium host_key form
        assert bc._host_matches_exclude("SECURE.CHASE.COM", pats)

    def test_label_anchored_no_suffix_bleed(self):
        pats = ("chase.com",)
        assert not bc._host_matches_exclude("notchase.com", pats)
        assert not bc._host_matches_exclude("chase.com.evil.example", pats)

    def test_empty_host_or_patterns(self):
        assert not bc._host_matches_exclude("", ("a.com",))
        assert not bc._host_matches_exclude(None, ("a.com",))
        assert not bc._host_matches_exclude("a.com", ())


class TestPatternResolution:
    def test_defaults_are_empty(self):
        with pytest.MonkeyPatch.context() as mp:
            import hermes_cli.config as cfg
            mp.setattr(cfg, "read_raw_config", lambda: {"browser": {}})
            assert bc._cookie_exclude_patterns() == ()

    def test_curated_list_via_opt_in(self):
        with pytest.MonkeyPatch.context() as mp:
            import hermes_cli.config as cfg
            mp.setattr(cfg, "read_raw_config",
                       lambda: {"browser": {"real_profile_exclude_sensitive": True}})
            pats = bc._cookie_exclude_patterns()
        assert "chase.com" in pats
        assert "accounts.google.com" in pats
        assert "mail.google.com" in pats

    def test_user_list_normalized_and_deduped(self):
        with pytest.MonkeyPatch.context() as mp:
            import hermes_cli.config as cfg
            mp.setattr(cfg, "read_raw_config", lambda: {"browser": {
                "real_profile_cookie_excludes": [" .MyBank.example ", "mybank.example", ""],
            }})
            pats = bc._cookie_exclude_patterns()
        assert pats == ("mybank.example",)

    def test_config_error_means_no_patterns(self):
        with pytest.MonkeyPatch.context() as mp:
            import hermes_cli.config as cfg
            def boom():
                raise OSError("no config")
            mp.setattr(cfg, "read_raw_config", boom)
            assert bc._cookie_exclude_patterns() == ()


class TestScrub:
    def test_scrubs_cookies_and_logins(self, tmp_path):
        dst = tmp_path / "copy"
        _make_cookie_db(str(dst / "Default" / "Cookies"),
                        [".chase.com", "www.chase.com", "github.com"])
        _make_cookie_db(str(dst / "Default" / "Network" / "Cookies"),
                        ["accounts.google.com", "example.org"])
        _make_login_db(str(dst / "Default" / "Login Data"),
                       ["https://www.chase.com/login", "https://github.com/login"])

        removed, err = bc.scrub_snapshot_auth(
            str(dst), ("chase.com", "accounts.google.com"))
        assert err is None
        assert removed == 4
        assert _cookie_hosts(str(dst / "Default" / "Cookies")) == ["github.com"]
        assert _cookie_hosts(str(dst / "Default" / "Network" / "Cookies")) == ["example.org"]
        assert _login_origins(str(dst / "Default" / "Login Data")) == ["https://github.com/login"]

    def test_no_patterns_is_noop(self, tmp_path):
        dst = tmp_path / "copy"
        _make_cookie_db(str(dst / "Default" / "Cookies"), [".chase.com"])
        removed, err = bc.scrub_snapshot_auth(str(dst), ())
        assert (removed, err) == (0, None)
        assert _cookie_hosts(str(dst / "Default" / "Cookies")) == [".chase.com"]

    def test_missing_dbs_are_fine(self, tmp_path):
        removed, err = bc.scrub_snapshot_auth(str(tmp_path / "empty"), ("chase.com",))
        assert (removed, err) == (0, None)

    def test_corrupt_db_reports_error(self, tmp_path):
        dst = tmp_path / "copy"
        p = dst / "Default" / "Cookies"
        p.parent.mkdir(parents=True)
        p.write_text("this is not sqlite")
        removed, err = bc.scrub_snapshot_auth(str(dst), ("chase.com",))
        assert err is not None and "Cookies" in err


class TestSnapshotIntegration:
    """End-to-end through snapshot_real_profile with a real profile tree."""

    def _make_profile(self, root):
        (root / "Default" / "Network").mkdir(parents=True)
        (root / "Local State").write_text('{"profile": {"last_used": "Default"}}')
        _make_cookie_db(str(root / "Default" / "Cookies"),
                        [".chase.com", "github.com"])
        _make_cookie_db(str(root / "Default" / "Network" / "Cookies"),
                        ["mail.google.com", "docs.python.org"])
        _make_login_db(str(root / "Default" / "Login Data"),
                       ["https://chase.com/", "https://github.com/"])
        (root / "Default" / "Preferences").write_text("{}")
        return root

    def _patch_config(self, monkeypatch, browser_cfg):
        import hermes_cli.config as cfg
        monkeypatch.setattr(cfg, "read_raw_config", lambda: {"browser": browser_cfg})

    def test_snapshot_scrubs_excluded_domains(self, tmp_path, monkeypatch):
        src = self._make_profile(tmp_path / "real")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "hh")
        self._patch_config(monkeypatch, {
            "real_profile_exclude_sensitive": True,
            "real_profile_cookie_excludes": ["github.com"],
        })

        dst, err = bc.snapshot_real_profile("chrome", src=str(src))
        assert err is None and dst is not None

        # Copy scrubbed: chase (curated), mail.google.com (curated), github (user).
        assert _cookie_hosts(os.path.join(dst, "Default", "Cookies")) == []
        assert _cookie_hosts(os.path.join(dst, "Default", "Network", "Cookies")) == ["docs.python.org"]
        assert _login_origins(os.path.join(dst, "Default", "Login Data")) == []

        # Source profile is NEVER modified.
        assert _cookie_hosts(str(src / "Default" / "Cookies")) == sorted([".chase.com", "github.com"])
        assert _login_origins(str(src / "Default" / "Login Data")) == sorted(
            ["https://chase.com/", "https://github.com/"])

    def test_refresh_resync_cannot_reintroduce_excluded(self, tmp_path, monkeypatch):
        src = self._make_profile(tmp_path / "real")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "hh")
        self._patch_config(monkeypatch, {"real_profile_cookie_excludes": ["chase.com"]})

        dst, err = bc.snapshot_real_profile("chrome", src=str(src))
        assert err is None and dst is not None
        # Second launch: the per-launch auth re-sync copies chase back in from
        # the live profile; the scrub must remove it again.
        dst2, err2 = bc.snapshot_real_profile("chrome", src=str(src))
        assert err2 is None and dst2 == dst
        assert _cookie_hosts(os.path.join(dst, "Default", "Cookies")) == ["github.com"]

    def test_no_exclusions_leaves_snapshot_untouched(self, tmp_path, monkeypatch):
        src = self._make_profile(tmp_path / "real")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "hh")
        self._patch_config(monkeypatch, {})

        dst, err = bc.snapshot_real_profile("chrome", src=str(src))
        assert err is None and dst is not None
        assert _cookie_hosts(os.path.join(dst, "Default", "Cookies")) == sorted(
            [".chase.com", "github.com"])

    def test_scrub_failure_fails_closed(self, tmp_path, monkeypatch):
        src = self._make_profile(tmp_path / "real")
        monkeypatch.setattr(bc, "get_hermes_home", lambda: tmp_path / "hh")
        self._patch_config(monkeypatch, {"real_profile_cookie_excludes": ["chase.com"]})
        monkeypatch.setattr(bc, "scrub_snapshot_auth",
                            lambda dst, pats: (0, "boom"))

        dst, err = bc.snapshot_real_profile("chrome", src=str(src))
        assert dst is None
        assert err is not None and "domain exclusions" in err
