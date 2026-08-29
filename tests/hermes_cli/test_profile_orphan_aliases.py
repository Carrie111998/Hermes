"""Regression coverage for Doctor's orphan profile-alias cleanup (#94750)."""

from pathlib import Path

import pytest

from hermes_cli import doctor, profiles


@pytest.fixture()
def profile_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return tmp_path


def _write_posix_wrapper(home: Path, alias: str, target: str) -> Path:
    wrapper = home / ".local" / "bin" / alias
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        f'#!/bin/sh\nexec /opt/hermes/bin/hermes -p {target} "$@"\n',
        encoding="utf-8",
    )
    return wrapper


def test_scanner_recognizes_generated_posix_wrapper(profile_home, monkeypatch):
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    wrapper = _write_posix_wrapper(profile_home, "pirzl", "pirzl")

    assert profiles._scan_profile_wrappers() == [(wrapper, "pirzl")]


def test_scanner_recognizes_generated_windows_wrapper(profile_home, monkeypatch):
    monkeypatch.setattr(profiles.sys, "platform", "win32")
    wrapper = profiles.create_wrapper_script("pirzl")

    assert profiles._scan_profile_wrappers() == [(wrapper, "pirzl")]


def test_scanner_recognizes_existing_windows_wrapper_with_doubled_crlf(
    profile_home, monkeypatch
):
    monkeypatch.setattr(profiles.sys, "platform", "win32")
    wrapper = profile_home / ".local" / "bin" / "pirzl.bat"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(b"@echo off\r\r\nhermes -p pirzl %*\r\r\n")

    assert profiles._scan_profile_wrappers() == [(wrapper, "pirzl")]


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("unrelated", '#!/bin/sh\nprintf "hermes -p gone"\n'),
        ("malformed", '#!/bin/sh\nexec hermes -p gone "$@"\necho extra\n'),
        ("binary", b"\xff\x00hermes -p gone"),
        ("BadAlias", '#!/bin/sh\nexec hermes -p gone "$@"\n'),
    ],
)
def test_scanner_ignores_untrusted_posix_files(
    profile_home, monkeypatch, name, content
):
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    candidate = profile_home / ".local" / "bin" / name
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        candidate.write_bytes(content)
    else:
        candidate.write_text(content, encoding="utf-8")

    assert profiles._scan_profile_wrappers() == []


def test_scanner_ignores_wrapper_for_other_platform(profile_home, monkeypatch):
    posix = _write_posix_wrapper(profile_home, "posix", "gone")
    windows = posix.with_name("windows.bat")
    windows.write_text(
        "@echo off\r\nhermes -p gone %*\r\n",
        encoding="utf-8",
        newline="",
    )

    monkeypatch.setattr(profiles.sys, "platform", "linux")
    assert profiles._scan_profile_wrappers() == [(posix, "gone")]
    monkeypatch.setattr(profiles.sys, "platform", "win32")
    assert profiles._scan_profile_wrappers() == [(windows, "gone")]


def test_doctor_reports_last_profile_orphan_without_removing_it(
    profile_home, monkeypatch, capsys
):
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    wrapper = _write_posix_wrapper(profile_home, "pirzl", "pirzl")
    issues = []
    manual_issues = []

    fixed = doctor._check_profiles(
        should_fix=False, issues=issues, manual_issues=manual_issues
    )

    output = capsys.readouterr().out
    assert fixed == 0
    assert wrapper.exists()
    assert "Orphan alias: pirzl" in output
    assert "profile 'pirzl' no longer exists" in output
    assert issues == [f"Remove orphan alias {wrapper}: run 'hermes doctor --fix'."]
    assert manual_issues == []


def test_doctor_fix_removes_and_counts_every_orphan(profile_home, monkeypatch, capsys):
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    wrappers = [
        _write_posix_wrapper(profile_home, "pirzl", "pirzl"),
        _write_posix_wrapper(profile_home, "old-family", "old-family"),
    ]

    fixed = doctor._check_profiles(should_fix=True, issues=[], manual_issues=[])

    output = capsys.readouterr().out
    assert fixed == 2
    assert all(not wrapper.exists() for wrapper in wrappers)
    assert "Removed orphan alias: pirzl" in output
    assert "Removed orphan alias: old-family" in output


def test_doctor_leaves_live_profile_wrapper_untouched(profile_home, monkeypatch):
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    profile_dir = profile_home / ".hermes" / "profiles" / "live"
    profile_dir.mkdir(parents=True)
    wrapper = _write_posix_wrapper(profile_home, "talk-to-live", "live")
    issues = []

    fixed = doctor._check_profiles(should_fix=True, issues=issues, manual_issues=[])

    assert fixed == 0
    assert wrapper.exists()
    assert issues == []


def test_doctor_reports_unlink_failure_and_preserves_wrapper(
    profile_home, monkeypatch, capsys
):
    monkeypatch.setattr(profiles.sys, "platform", "linux")
    wrapper = _write_posix_wrapper(profile_home, "pirzl", "pirzl")
    real_unlink = Path.unlink

    def deny_or_unlink(path, *args, **kwargs):
        if path == wrapper:
            raise PermissionError("permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_or_unlink)
    manual_issues = []

    fixed = doctor._check_profiles(
        should_fix=True, issues=[], manual_issues=manual_issues
    )

    output = capsys.readouterr().out
    assert fixed == 0
    assert wrapper.exists()
    assert "Could not remove orphan alias: pirzl" in output
    assert manual_issues == [
        f"Remove orphan alias {wrapper} manually: permission denied"
    ]
