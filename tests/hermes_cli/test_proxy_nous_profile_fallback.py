"""Regression tests for Nous proxy auth resolution across named profiles."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter


def _write_auth(path: Path, providers: dict) -> None:
    path.write_text(json.dumps({"version": 1, "providers": providers}))


def test_nous_proxy_falls_back_to_global_auth_for_named_profile(tmp_path, monkeypatch):
    """Proxy readiness must agree with the canonical auth resolver."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    profile_home = global_root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    _write_auth(
        global_root / "auth.json",
        {
            "nous": {
                "access_token": "global-access",
                "refresh_token": "global-refresh",
            }
        },
    )
    _write_auth(profile_home / "auth.json", {})

    adapter = NousPortalAdapter()

    assert adapter.is_authenticated()
    assert adapter._read_state()["access_token"] == "global-access"


def test_nous_proxy_keeps_profile_auth_precedence(tmp_path, monkeypatch):
    """A profile-local Nous state must still shadow the global fallback."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    profile_home = global_root / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    _write_auth(
        global_root / "auth.json",
        {
            "nous": {
                "access_token": "global-access",
                "refresh_token": "global-refresh",
            }
        },
    )
    _write_auth(
        profile_home / "auth.json",
        {
            "nous": {
                "access_token": "profile-access",
                "refresh_token": "profile-refresh",
            }
        },
    )

    assert NousPortalAdapter()._read_state()["access_token"] == "profile-access"
