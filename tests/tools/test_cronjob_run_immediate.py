"""Tests for cronjob action='run' immediate execution (#41037).

Before this fix, `cronjob(action='run')` only set next_run_at=now and returned
success, relying on the scheduler ticker to actually run the job. With no
gateway/ticker active (e.g. a CLI-only Windows setup) the job never executed and
last_run_at stayed null forever. Now action='run' claims the job (at-most-once,
blocking a concurrent tick) and fires it inline via the shared run_one_job body.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.cronjob_tools import cronjob, _execute_job_now


_JOB = {"id": "job-run-1", "name": "manual run", "prompt": "hi",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


class TestCronjobRunExecutesImmediately:
    def test_run_action_claims_and_fires_via_run_one_job(self):
        """action='run' must claim the job then fire it through run_one_job."""
        ran = {"job": "after-run", "last_status": "ok", "last_error": None}
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True) as m_claim, \
             patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=ran):
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["success"] is True
        assert out["job"]["executed"] is True
        assert out["job"]["execution_success"] is True
        m_claim.assert_called_once_with("job-run-1")   # at-most-once claim taken
        m_run.assert_called_once()                       # fired via the shared body

    def test_execute_job_now_reuses_live_gateway_delivery_context(self):
        """An in-gateway manual run must deliver on the gateway-owned loop."""
        loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)
        adapters = {"matrix": object()}
        runner = SimpleNamespace(
            _running=True,
            _gateway_loop=loop,
            adapters=adapters,
        )
        ran = {"id": "job-run-1", "last_status": "ok", "last_error": None}

        with patch("gateway.run._gateway_runner_ref", return_value=runner), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=ran):
            result = _execute_job_now(dict(_JOB))

        assert result["success"] is True
        m_run.assert_called_once_with(dict(_JOB), adapters=adapters, loop=loop)

    def test_execute_job_now_uses_active_multiplex_profile_adapters(self, tmp_path):
        """A secondary-profile run must never inherit the primary bot adapter."""
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)
        primary_adapters = {"matrix": object()}
        secondary_adapters = {"matrix": object()}
        runner = SimpleNamespace(
            _running=True,
            _gateway_loop=loop,
            adapters=primary_adapters,
            _profile_adapters={"coder": secondary_adapters},
            config=SimpleNamespace(multiplex_profiles=True),
        )
        ran = {"id": "job-run-1", "last_status": "ok", "last_error": None}
        profiles_root = tmp_path / "hermes-root" / "profiles"
        profile_home = profiles_root / "coder"
        profile_home.mkdir(parents=True)
        home_token = set_hermes_home_override(str(profile_home))

        try:
            with patch("gateway.run._gateway_runner_ref", return_value=runner), \
                 patch("hermes_cli.profiles._get_profiles_root", return_value=profiles_root), \
                 patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
                 patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
                 patch("tools.cronjob_tools.get_job", return_value=ran):
                result = _execute_job_now(dict(_JOB))
        finally:
            reset_hermes_home_override(home_token)

        assert result["success"] is True
        m_run.assert_called_once_with(
            dict(_JOB), adapters=secondary_adapters, loop=loop
        )

    @pytest.mark.parametrize(
        "multiplex_metadata",
        [True, False, None],
        ids=["enabled", "disabled", "missing"],
    )
    @pytest.mark.parametrize(
        "registry_present",
        [False, True],
        ids=["registry-absent", "registry-lacks-matrix"],
    )
    def test_secondary_context_never_borrows_primary_transport(
        self, tmp_path, multiplex_metadata, registry_present
    ):
        """Scoped secondary turns fail closed regardless of runner metadata."""
        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway.delivery import resolve_delivery_transport
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from tools.cronjob_tools import _get_live_gateway_delivery_context

        loop = SimpleNamespace(is_running=lambda: True, is_closed=lambda: False)
        primary_adapter = object()
        secondary_adapters = (
            {Platform.TELEGRAM: object()} if registry_present else None
        )
        runner = SimpleNamespace(
            _running=True,
            _gateway_loop=loop,
            adapters={Platform.MATRIX: primary_adapter},
            _profile_adapters=(
                {"coder": secondary_adapters} if registry_present else {}
            ),
        )
        if multiplex_metadata is not None:
            runner.config = SimpleNamespace(
                multiplex_profiles=multiplex_metadata
            )
        profiles_root = tmp_path / "hermes-root" / "profiles"
        profile_home = profiles_root / "coder"
        profile_home.mkdir(parents=True)
        home_token = set_hermes_home_override(str(profile_home))

        try:
            with patch("gateway.run._gateway_runner_ref", return_value=runner), \
                 patch(
                     "hermes_cli.profiles._get_profiles_root",
                     return_value=profiles_root,
                 ):
                context = _get_live_gateway_delivery_context()
        finally:
            reset_hermes_home_override(home_token)

        config = GatewayConfig(
            platforms={Platform.MATRIX: PlatformConfig(enabled=True)}
        )
        transport = resolve_delivery_transport(
            Platform.MATRIX,
            config,
            context[0] if context is not None else None,
        )
        if registry_present:
            assert context is not None
            assert context[0] is secondary_adapters
            assert context[1] is loop
        else:
            assert context is None
        assert transport is None

    def test_execute_job_now_preserves_standalone_call_without_live_gateway(self):
        """CLI-only execution keeps the existing standalone delivery path."""
        ran = {"id": "job-run-1", "last_status": "ok", "last_error": None}

        with patch("gateway.run._gateway_runner_ref", return_value=None), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=ran):
            result = _execute_job_now(dict(_JOB))

        assert result["success"] is True
        m_run.assert_called_once_with(dict(_JOB))

    @pytest.mark.parametrize(
        ("runner_running", "loop_running", "loop_closed"),
        [
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ],
    )
    def test_execute_job_now_rejects_stale_gateway_delivery_context(
        self, runner_running, loop_running, loop_closed
    ):
        """Startup/shutdown races must fall back instead of crossing loops."""
        loop = SimpleNamespace(
            is_running=lambda: loop_running,
            is_closed=lambda: loop_closed,
        )
        runner = SimpleNamespace(
            _running=runner_running,
            _gateway_loop=loop,
            adapters={"matrix": object()},
        )
        ran = {"id": "job-run-1", "last_status": "ok", "last_error": None}

        with patch("gateway.run._gateway_runner_ref", return_value=runner), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=ran):
            result = _execute_job_now(dict(_JOB))

        assert result["success"] is True
        m_run.assert_called_once_with(dict(_JOB))

    def test_run_reconciles_external_provider_after_claimed_execution(self):
        """A direct run must re-arm Chronos after it advances next_run_at.

        Otherwise a scheduled Chronos fire that loses its claim to this direct
        run is consumed without a successor one-shot, permanently stalling the
        recurring job.
        """
        order = []
        ran = {"id": "job-run-1", "last_status": "ok", "last_error": None}
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job",
                   side_effect=lambda *a, **kw: order.append("run") or True), \
             patch("tools.cronjob_tools.get_job", return_value=ran), \
             patch("tools.cronjob_tools._notify_provider_jobs_changed_safe",
                   side_effect=lambda: order.append("notify")) as m_notify:
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["job"]["executed"] is True
        m_notify.assert_called_once_with()
        # Reconcile only AFTER the run persisted its final state (mark_job_run
        # inside run_one_job), so the provider arms the post-run next_run_at.
        assert order == ["run", "notify"]

    def test_run_reconciles_external_provider_even_when_claimed_run_fails(self):
        """A claimed direct run advances next_run_at at claim time, so the
        provider must be reconciled even when the execution itself fails."""
        failed = {"id": "job-run-1", "last_status": "error", "last_error": "provider 500"}
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job", side_effect=RuntimeError("boom")), \
             patch("tools.cronjob_tools.mark_job_run"), \
             patch("tools.cronjob_tools.get_job", return_value=failed), \
             patch("tools.cronjob_tools._notify_provider_jobs_changed_safe") as m_notify:
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["job"]["executed"] is True
        assert out["job"]["execution_success"] is False
        m_notify.assert_called_once_with()

    def test_run_skips_when_claim_lost(self):
        """If the scheduler already holds the fire claim, do NOT double-run."""
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=False), \
             patch("cron.scheduler.run_one_job") as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools._notify_provider_jobs_changed_safe") as m_notify:
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["success"] is True
        assert out["job"]["executed"] is False
        assert out["job"]["execution_success"] is False
        assert "execution_skipped" in out["job"]
        m_run.assert_not_called()  # claim lost -> never fired
        m_notify.assert_not_called()  # the winning scheduler owns the re-arm

    def test_run_reports_failure_from_last_status(self):
        """A failed run is reported via the re-read job's last_status/last_error."""
        failed = {"id": "job-run-1", "last_status": "error", "last_error": "provider 500"}
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=dict(_JOB)), \
             patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job", return_value=True), \
             patch("tools.cronjob_tools.get_job", return_value=failed):
            out = json.loads(cronjob(action="run", job_id="job-run-1"))

        assert out["job"]["executed"] is True
        assert out["job"]["execution_success"] is False
        assert out["job"]["execution_error"] == "provider 500"

    def test_execute_job_now_bails_without_claim(self):
        """_execute_job_now never calls run_one_job when the claim is lost."""
        with patch("tools.cronjob_tools.claim_job_for_fire", return_value=False), \
             patch("cron.scheduler.run_one_job") as m_run:
            res = _execute_job_now(dict(_JOB))
        assert res["claimed"] is False
        assert res["success"] is False
        m_run.assert_not_called()

    def test_execute_job_now_marks_failure_on_exception(self):
        """An exception during fire is captured, marked failed, not propagated."""
        with patch("tools.cronjob_tools.claim_job_for_fire", return_value=True), \
             patch("cron.scheduler.run_one_job", side_effect=RuntimeError("boom")), \
             patch("tools.cronjob_tools.mark_job_run") as m_mark, \
             patch("tools.cronjob_tools.get_job", return_value=dict(_JOB)):
            res = _execute_job_now(dict(_JOB))
        assert res["claimed"] is True
        assert res["success"] is False
        assert "boom" in res["error"]
        m_mark.assert_called_once()
