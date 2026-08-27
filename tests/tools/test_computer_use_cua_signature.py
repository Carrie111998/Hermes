"""Regression coverage for the macOS CuaDriver.app identity gate."""

from __future__ import annotations

import subprocess

import pytest

from tools.computer_use import cua_backend


def _patch_codesign(monkeypatch, *, identifier: str, team_id: str) -> None:
    monkeypatch.setattr(
        cua_backend.shutil,
        "which",
        lambda name: "/usr/bin/codesign" if name == "codesign" else None,
    )
    monkeypatch.setattr(cua_backend, "_computer_use_cfg", lambda: {})
    monkeypatch.setattr(
        cua_backend.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="",
            stderr=f"Identifier={identifier}\nTeamIdentifier={team_id}\n",
        ),
    )


def test_driver_signature_accepts_official_release_identity(monkeypatch):
    _patch_codesign(
        monkeypatch,
        identifier="com.trycua.driver",
        team_id="YCK386LBJ7",
    )

    cua_backend._validate_cua_driver_app_signature("/Applications/CuaDriver.app")


def test_driver_signature_rejects_stale_team_id(monkeypatch):
    _patch_codesign(
        monkeypatch,
        identifier="com.trycua.driver",
        team_id="4YEC26S9KF",
    )

    with pytest.raises(RuntimeError, match="expected 'YCK386LBJ7'"):
        cua_backend._validate_cua_driver_app_signature("/Applications/CuaDriver.app")


@pytest.mark.parametrize(
    ("identifier", "team_id", "message"),
    [
        ("com.trycua.driver.evil", "YCK386LBJ7", "identifier"),
        ("com.trycua.driver", "not set", "team"),
    ],
)
def test_driver_signature_keeps_fail_closed_rejections(
    monkeypatch, identifier, team_id, message
):
    _patch_codesign(monkeypatch, identifier=identifier, team_id=team_id)

    with pytest.raises(RuntimeError, match=message):
        cua_backend._validate_cua_driver_app_signature("/Applications/CuaDriver.app")
