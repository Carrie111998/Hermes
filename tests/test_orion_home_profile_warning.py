"""Tests for get_orion_home() profile-mode fallback warning.

Regression test for https://github.com/zacharyjleach-stack/Aries/issues/18594.

When ORION_HOME is unset but an active_profile file indicates a non-default
profile is active, get_orion_home() should:
  1. STILL return ~/.orion (raising would brick 30+ module-level callers)
  2. Emit a loud one-shot warning to stderr so operators can diagnose
     cross-profile data contamination after the fact.

The warning goes to stderr directly (not through logging) because this
function is called at module-import time from 30+ sites, often before the
logging subsystem has been configured.
"""

from pathlib import Path

import pytest


@pytest.fixture
def fresh_constants(monkeypatch, tmp_path):
    """Import orion_constants fresh and reset the one-shot warn flag."""
    import importlib
    import orion_constants
    importlib.reload(orion_constants)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ORION_HOME", raising=False)
    return orion_constants


class TestGetOrionHomeProfileWarning:
    def test_classic_mode_no_active_profile_no_warning(
        self, fresh_constants, tmp_path, capsys
    ):
        """Classic mode: no active_profile file → silent, returns ~/.orion."""
        result = fresh_constants.get_orion_home()
        assert result == tmp_path / ".orion"
        assert "ORION_HOME fallback" not in capsys.readouterr().err


    def test_named_profile_unset_home_warns_once(
        self, fresh_constants, tmp_path, capsys
    ):
        """active_profile=coder + ORION_HOME unset → warn loudly, still return fallback."""
        orion_dir = tmp_path / ".orion"
        orion_dir.mkdir()
        (orion_dir / "active_profile").write_text("coder\n")

        result = fresh_constants.get_orion_home()

        # 1. Still returns the fallback — no import-time crash
        assert result == tmp_path / ".orion"
        # 2. Stderr got the warning exactly once
        err = capsys.readouterr().err
        assert err.count("ORION_HOME fallback") == 1
        assert "'coder'" in err
        assert "#18594" in err

        # 3. One-shot: second and third calls don't re-warn
        fresh_constants.get_orion_home()
        fresh_constants.get_orion_home()
        err2 = capsys.readouterr().err
        assert "ORION_HOME fallback" not in err2

    def test_orion_home_set_suppresses_warning(
        self, fresh_constants, tmp_path, capsys, monkeypatch
    ):
        """Even if active_profile is 'coder', setting ORION_HOME suppresses warning."""
        profile_dir = tmp_path / ".orion" / "profiles" / "coder"
        profile_dir.mkdir(parents=True)
        (tmp_path / ".orion" / "active_profile").write_text("coder\n")
        monkeypatch.setenv("ORION_HOME", str(profile_dir))

        result = fresh_constants.get_orion_home()

        assert result == profile_dir
        assert "ORION_HOME fallback" not in capsys.readouterr().err

    def test_unreadable_active_profile_no_crash(
        self, fresh_constants, tmp_path, capsys
    ):
        """active_profile that can't be decoded → fall through silently."""
        orion_dir = tmp_path / ".orion"
        orion_dir.mkdir()
        # Write bytes that aren't valid utf-8
        (orion_dir / "active_profile").write_bytes(b"\xff\xfe\x00\x00")

        result = fresh_constants.get_orion_home()

        assert result == tmp_path / ".orion"
        # Shouldn't crash; shouldn't warn either (can't tell what profile was intended)
        assert "ORION_HOME fallback" not in capsys.readouterr().err

