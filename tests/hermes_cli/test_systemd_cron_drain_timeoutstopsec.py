"""Cross-platform regression tests for #94759.

These tests live in their own file (rather than
``test_gateway_service.py``) because the latter is gated on
``pytest.importorskip("pwd")`` and ``pytest.importorskip("grp")`` and
therefore cannot run on Windows. The CI on Linux runs BOTH files, so
the assertions here are the same ones shipped in
``tests/hermes_cli/test_gateway_service.py::TestGeneratedSystemdUnits``.
"""

import pytest

import hermes_cli.gateway as gateway_cli
from gateway.restart import (
    CRON_DRAIN_CLEANUP_RESERVE_S,
    DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT,
)


class TestSystemdUnitCronDrainTimeoutStopSec:
    """Regression for issue #94759: ``generate_systemd_unit``'s
    ``TimeoutStopSec`` ignored ``agent.cron_drain_timeout`` and let
    systemd SIGKILL an in-budget cron drain."""

    def test_user_unit_sizes_from_cron_drain_floor(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 0.0)
        monkeypatch.setattr(
            gateway_cli,
            "_get_cron_drain_timeout",
            lambda: float(DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT),
        )

        unit = gateway_cli.generate_systemd_unit(system=False)

        cron_floor = int(DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT) + int(
            CRON_DRAIN_CLEANUP_RESERVE_S
        )
        expected = int(max(60, cron_floor + 30))
        assert f"TimeoutStopSec={expected}" in unit, (
            f"unit must reflect cron drain floor; got:\n{unit}"
        )
        # Sanity: the bug would have produced 60s with these inputs.
        assert expected > 60, (
            "test invariant: with cron_drain_timeout=30 the floor (40) must "
            "push TimeoutStopSec above the 60s minimum"
        )

    def test_user_unit_uses_max_of_restart_and_cron_drain(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 180.0)
        monkeypatch.setattr(gateway_cli, "_get_cron_drain_timeout", lambda: 5.0)

        unit = gateway_cli.generate_systemd_unit(system=False)

        # restart(180) > cron floor(15) → unit sizes from 180 + 30 = 210
        assert "TimeoutStopSec=210" in unit

    def test_user_unit_uses_cron_drain_when_restart_drain_is_smaller(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 5.0)
        monkeypatch.setattr(gateway_cli, "_get_cron_drain_timeout", lambda: 60.0)

        unit = gateway_cli.generate_systemd_unit(system=False)

        # cron floor(70) > restart(5) → unit sizes from 70 + 30 = 100
        assert "TimeoutStopSec=100" in unit, (
            f"unit must use the cron floor (70) when larger than restart drain; got:\n{unit}"
        )

    def test_user_unit_60s_floor_when_both_drains_zero(self, monkeypatch):
        monkeypatch.setattr(gateway_cli, "_get_restart_drain_timeout", lambda: 0.0)
        monkeypatch.setattr(gateway_cli, "_get_cron_drain_timeout", lambda: 0.0)

        unit = gateway_cli.generate_systemd_unit(system=False)

        # cron floor(10) > restart(0) → max(60, 10 + 30) = 60
        assert "TimeoutStopSec=60" in unit

    def test_user_unit_default_drains_yield_70s(self, monkeypatch):
        """With all defaults, TimeoutStopSec must be 70s (not 60s as pre-fix).

        Defaults: restart_drain_timeout=0, cron_drain_timeout=30, reserve=10.
        Pre-fix: max(60, 0+30)=60 → unit sized 60s, which is exactly what
        bit the operator in the issue journal.
        Post-fix: max(60, max(0, 40)+30) = 70s.
        """
        # Don't monkeypatch the helpers — exercise the real defaults.
        unit = gateway_cli.generate_systemd_unit(system=False)

        assert "TimeoutStopSec=70" in unit, (
            f"defaults must yield TimeoutStopSec=70, not the buggy 60s; got:\n{unit}"
        )

    def test_cron_drain_helper_reads_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_CRON_DRAIN_TIMEOUT", "75")
        assert gateway_cli._get_cron_drain_timeout() == 75.0

    def test_cron_drain_helper_falls_back_to_default_when_config_missing(
        self, monkeypatch
    ):
        monkeypatch.delenv("HERMES_CRON_DRAIN_TIMEOUT", raising=False)
        monkeypatch.setattr(gateway_cli, "read_raw_config", lambda: {})
        assert gateway_cli._get_cron_drain_timeout() == float(
            DEFAULT_GATEWAY_CRON_DRAIN_TIMEOUT
        )