"""Tests for issue #89315 — ``--replace`` must never signal a gateway it
cannot prove belongs to this HERMES_HOME.

Review-corrected scope (andrexibiza's review of the first attempt): current
``create_profile(clone_config=True)`` only copies config/skills/SOUL files —
it does NOT copy gateway runtime files on any released base, so a clone-side
strip would be a no-op. The real defect class is a POISONED PID RECORD at
replace time steering a destructive SIGTERM at another profile's live
gateway. The defense therefore lives entirely at the destructive boundary:

``_replace_target_belongs_to_other_profile()`` FAILS CLOSED — readable
cmdline decides by profile match; unreadable cmdline falls back to the
persisted record's ``hermes_home`` only while that record stays bound to the
live target by PID + start-time identity; any probe failure or unprovable
ownership refuses the signal.
"""

from __future__ import annotations

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

    def test_unreadable_cmdline_with_bound_foreign_record_is_refused(
        self, profile_env
    ):
        """Fail-closed (review blocker 2): an unreadable cmdline falls back
        to the persisted record; a VALID, BOUND record whose hermes_home
        differs from ours must REFUSE the signal."""
        from gateway.run import _replace_target_belongs_to_other_profile

        foreign_record = {
            "pid": 424242,
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 111222333,
            "hermes_home": "/home/other/.hermes/profiles/tim",
        }

        with (
            patch("gateway.status._read_process_cmdline", return_value=None),
            patch(
                "gateway.status._get_pid_path",
                return_value=profile_env / ".hermes" / "gateway.pid",
            ),
            patch(
                "gateway.status._read_pid_record",
                return_value=foreign_record,
            ),
            patch(
                "gateway.status._get_process_start_time",
                return_value=111222333,
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=profile_env / ".hermes",
            ),
        ):
            # argv is bare (default-gateway shape) but the persisted home is
            # another profile's — ownership for OUR home is not proven.
            assert _replace_target_belongs_to_other_profile(424242) is True

    def test_unreadable_cmdline_with_bound_same_home_record_passes(
        self, profile_env
    ):
        """Bound same-home record + unreadable cmdline → replace proceeds.
        The fallback must not over-refuse legitimate same-home replaces on
        platforms where cmdlines are unreadable (Windows)."""
        from gateway.run import _replace_target_belongs_to_other_profile

        our_record = {
            "pid": 424242,
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 111222333,
            "hermes_home": str(profile_env / ".hermes"),
        }

        with (
            patch("gateway.status._read_process_cmdline", return_value=None),
            patch(
                "gateway.status._get_pid_path",
                return_value=profile_env / ".hermes" / "gateway.pid",
            ),
            patch(
                "gateway.status._read_pid_record",
                return_value=our_record,
            ),
            patch(
                "gateway.status._get_process_start_time",
                return_value=111222333,
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=profile_env / ".hermes",
            ),
        ):
            assert _replace_target_belongs_to_other_profile(424242) is False

    def test_unreadable_cmdline_with_unbound_record_is_refused(
        self, profile_env
    ):
        """A record describing a different pid/start-time proves nothing —
        it is exactly the poisoned-record shape and must refuse."""
        from gateway.run import _replace_target_belongs_to_other_profile

        stale_record = {
            "pid": 999999,  # not our target
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 1,
            "hermes_home": str(profile_env / ".hermes"),
        }

        with (
            patch("gateway.status._read_process_cmdline", return_value=None),
            patch(
                "gateway.status._get_pid_path",
                return_value=profile_env / ".hermes" / "gateway.pid",
            ),
            patch(
                "gateway.status._read_pid_record",
                return_value=stale_record,
            ),
            patch(
                "gateway.status._get_process_start_time",
                return_value=42,
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=profile_env / ".hermes",
            ),
        ):
            assert _replace_target_belongs_to_other_profile(424242) is True

    def test_probe_exception_fails_closed(self, profile_env):
        """Probe error on a destructive action → refuse (fail closed)."""
        from gateway.run import _replace_target_belongs_to_other_profile

        with patch(
            "gateway.status._read_process_cmdline",
            side_effect=RuntimeError("probe exploded"),
        ):
            assert _replace_target_belongs_to_other_profile(424242) is True

    def test_named_profile_home_accepts_own_flag_and_refuses_bare(self):
        """The issue #89315 shape: profile 'tim' (HERMES_HOME under
        profiles/tim) must accept a '--profile tim' target and refuse a
        bare default-profile gateway."""
        from gateway.run import _replace_target_belongs_to_other_profile

        tim_home = Path("/home/x/.hermes/profiles/tim")

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
        from gateway.run import _replace_target_belongs_to_other_profile

        with (
            patch(
                "gateway.status._read_process_cmdline",
                return_value="python -m hermes_cli.main --profile sam gateway run",
            ),
            patch(
                "gateway.status._get_process_hermes_home",
                return_value=Path("/home/x/.hermes"),
            ),
        ):
            assert _replace_target_belongs_to_other_profile(3) is True
