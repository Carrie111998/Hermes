"""Tests for issue #89315 — Desktop "new profile" must not seed a gateway
kill-loop across profiles.

Two surfaces are pinned:

1. ``create_profile(clone_config=True)`` (the desktop's default clone path)
   must strip gateway runtime files the way ``--clone-all`` already does. A
   stale/poisoned ``gateway.pid`` / ``gateway_state.json`` / ``processes.json``
   riding along into a fresh profile later steers that profile's ``--replace``
   at another profile's live gateway PID (the restart loop's fuel).

2. ``_replace_target_belongs_to_other_profile()`` — the guard consulted by
   ``start_gateway(replace=True)`` — must identify a live process whose
   readable command line belongs to a different profile and refuse, while a
   same-home cmdline and unreadable cmdlines keep the legacy behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME mirroring tests/hermes_cli/test_profiles.py."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


class TestCloneConfigStripsGatewayRuntimeFiles:
    def test_runtime_files_do_not_survive_clone_config(self, profile_env):
        """A source profile's gateway runtime files must not ride along on
        the desktop's default clone_config path (#89315)."""
        from hermes_cli.profiles import create_profile

        default_home = profile_env / ".hermes"
        (default_home / "config.yaml").write_text("model: test\n")
        (default_home / ".env").write_text("KEY=val\n")
        # Poisoned runtime files in the SOURCE — as left by a crash loop.
        poisoned_pid = {
            "pid": 999991,
            "kind": "hermes-gateway",
            "start_time": 1234567890,
        }
        (default_home / "gateway.pid").write_text(json.dumps(poisoned_pid))
        (default_home / "gateway_state.json").write_text(
            json.dumps({"pid": 999991})
        )
        (default_home / "processes.json").write_text(
            json.dumps({"gateways": []})
        )

        profile_dir = create_profile("tim", clone_config=True, no_alias=True)

        assert (profile_dir / "config.yaml").exists(), (
            "clone_config must still copy the config files themselves"
        )
        for stale in ("gateway.pid", "gateway_state.json", "processes.json"):
            assert not (profile_dir / stale).exists(), (
                f"{stale} survived the clone and can steer --replace at "
                "another profile's gateway (#89315)"
            )

    def test_clone_config_without_runtime_files_still_works(self, profile_env):
        """No runtime files in source → clone unaffected (no false failure)."""
        from hermes_cli.profiles import create_profile

        default_home = profile_env / ".hermes"
        (default_home / "config.yaml").write_text("model: test\n")

        profile_dir = create_profile(
            "clean", clone_config=True, no_alias=True
        )
        assert (profile_dir / "config.yaml").exists()


class TestReplaceTargetProfileGuard:
    def test_foreign_profile_cmdline_is_refused(self, profile_env):
        """A cmdline advertising another profile must trip the refusal."""
        from gateway.run import _replace_target_belongs_to_other_profile

        with (
            patch(
                "gateway.status._read_process_cmdline",
                return_value=(
                    "/usr/bin/python -m hermes_cli.main --profile tim "
                    "gateway run"
                ),
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=profile_env / ".hermes",
            ),
        ):
            assert _replace_target_belongs_to_other_profile(424242) is True

    def test_same_profile_cmdline_is_allowed(self, profile_env):
        """A bare/default-gateway cmdline matching this home must pass."""
        from gateway.run import _replace_target_belongs_to_other_profile

        with (
            patch(
                "gateway.status._read_process_cmdline",
                return_value="python -m hermes_cli.main gateway run",
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=profile_env / ".hermes",
            ),
        ):
            assert _replace_target_belongs_to_other_profile(424242) is False

    def test_unreadable_cmdline_falls_open(self, profile_env):
        """When the cmdline cannot be read (Windows/permissions), the guard
        stays out of the way — legacy identity checks remain authoritative."""
        from gateway.run import _replace_target_belongs_to_other_profile

        with patch(
            "gateway.status._read_process_cmdline", return_value=None
        ):
            assert _replace_target_belongs_to_other_profile(424242) is False

    def test_named_profile_home_accepts_own_flag_and_refuses_bare(self):
        """The issue #89315 shape: profile 'tim' (HERMES_HOME under
        profiles/tim) must accept a '--profile tim' target and refuse a
        bare default-profile gateway."""
        from pathlib import Path as _P

        from gateway.run import _replace_target_belongs_to_other_profile

        tim_home = _P("/home/x/.hermes/profiles/tim")

        with (
            patch(
                "gateway.status._read_process_cmdline",
                return_value="python -m hermes_cli.main --profile tim gateway run",
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=tim_home,
            ),
        ):
            assert _replace_target_belongs_to_other_profile(1) is False

        with (
            patch(
                "gateway.status._read_process_cmdline",
                return_value="python -m hermes_cli.main gateway run",
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=tim_home,
            ),
        ):
            assert _replace_target_belongs_to_other_profile(2) is True

    def test_default_home_refuses_named_profile_target(self):
        """Mirror case: default root home must refuse a '--profile <x>'
        target recorded in its PID file."""
        from pathlib import Path as _P

        from gateway.run import _replace_target_belongs_to_other_profile

        with (
            patch(
                "gateway.status._read_process_cmdline",
                return_value="python -m hermes_cli.main --profile sam gateway run",
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=_P("/home/x/.hermes"),
            ),
        ):
            assert _replace_target_belongs_to_other_profile(3) is True
