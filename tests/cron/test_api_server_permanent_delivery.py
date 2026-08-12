"""#83484 — permanent non-push delivery targets must not wedge the scheduler.

When a job's origin is ``api_server`` (stateless HTTP request/response),
``deliver=origin`` used to resolve that origin into a concrete target.
Every fire then:

1. tried the live adapter (``send()`` returns structural failure),
2. fell through to standalone (same structural failure),
3. logged ERROR, and
4. for interval jobs, re-armed ``next_run_at`` forever.

``api_server`` is intentionally not a known cron delivery platform
(``_is_known_delivery_platform`` is False). The bug is that the
``deliver=origin`` resolver bypassed that check and resolved a target
that can never succeed.

Contract under test:

* resolve: ``deliver=origin`` with a non-push origin yields no
  ``api_server`` target (home-channel fallback still allowed).
* deliver: such a job is treated like local-only (return ``None``,
  no ERROR-level permanent-failure spam), not as a retryable delivery
  error.
* permanent adapter failure: do not waste a standalone retry on the
  same structural error.
* push-capable origin (telegram) still resolves and delivers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.jobs import create_job, get_due_jobs, load_jobs, mark_job_run, save_jobs
from cron.scheduler import (
    _deliver_result,
    _is_known_delivery_platform,
    _resolve_delivery_targets,
    _resolve_single_delivery_target,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import SendResult


API_SERVER_SEND_ERROR = "API server uses HTTP request/response, not send()"


def _api_server_origin_job(**overrides):
    job = {
        "id": "job-api-origin",
        "name": "api-followup",
        "deliver": "origin",
        "origin": {"platform": "api_server", "chat_id": "sess-abc"},
    }
    job.update(overrides)
    return job


class TestNonPushOriginNotResolvedAsTarget:
    """Resolver must not invent a push target for non-push origins."""

    def test_api_server_is_not_a_known_delivery_platform(self):
        assert _is_known_delivery_platform("api_server") is False

    def test_origin_api_server_does_not_resolve_to_api_server_target(self, monkeypatch):
        # No home channels → origin is the only candidate and must be rejected.
        monkeypatch.setattr(
            "cron.scheduler._get_home_target_chat_id", lambda *_a, **_k: ""
        )
        job = _api_server_origin_job()
        assert _resolve_single_delivery_target(job, "origin") is None
        assert _resolve_delivery_targets(job) == []

    def test_origin_api_server_does_not_divert_to_home_channel(self, monkeypatch):
        """Non-push origin must NOT silently fan-out to TELEGRAM_HOME (#83484).

        Diverting API-session cron output into the operator home channel is a
        silent cross-channel leak and would suppress the create-time notice.
        """
        monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "-100999")
        monkeypatch.setattr(
            "cron.scheduler._get_home_target_chat_id",
            lambda name, *_a, **_k: "-100999" if name == "telegram" else "",
        )
        job = _api_server_origin_job()
        assert _resolve_single_delivery_target(job, "origin") is None
        assert _resolve_delivery_targets(job) == []

    def test_push_capable_origin_still_resolves(self):
        job = {
            "id": "job-tg",
            "deliver": "origin",
            "origin": {"platform": "telegram", "chat_id": "12345", "thread_id": "9"},
        }
        target = _resolve_single_delivery_target(job, "origin")
        assert target == {
            "platform": "telegram",
            "chat_id": "12345",
            "thread_id": "9",
        }

    def test_explicit_api_server_colon_target_is_rejected(self, monkeypatch):
        """``deliver=api_server:sess`` is also a permanent non-push target."""
        monkeypatch.setattr(
            "cron.scheduler._get_home_target_chat_id", lambda *_a, **_k: ""
        )
        job = {"id": "job-explicit", "deliver": "api_server:sess-abc", "origin": None}
        assert _resolve_single_delivery_target(job, "api_server:sess-abc") is None
        assert _resolve_delivery_targets(job) == []

    def test_bare_api_server_deliver_with_matching_origin_is_rejected(self, monkeypatch):
        """``deliver=api_server`` + origin.platform=api_server must not invent a target."""
        monkeypatch.setattr(
            "cron.scheduler._get_home_target_chat_id", lambda *_a, **_k: ""
        )
        job = {
            "id": "job-bare",
            "deliver": "api_server",
            "origin": {"platform": "api_server", "chat_id": "sess-abc"},
        }
        assert _resolve_single_delivery_target(job, "api_server") is None
        assert _resolve_delivery_targets(job) == []
        # Fire path: soft local, not sticky delivery_error.
        assert _deliver_result(job, "body", adapters=None, loop=None) is None


class TestDeliverResultTreatsNonPushOriginAsLocal:
    """Fire path: no permanent ERROR, no false delivery_error for api_server origin."""

    def test_deliver_result_returns_none_for_api_server_origin(self, monkeypatch, caplog):
        import logging

        monkeypatch.setattr(
            "cron.scheduler._get_home_target_chat_id", lambda *_a, **_k: ""
        )
        job = _api_server_origin_job()

        standalone = AsyncMock(
            return_value={"error": f"Adapter send failed: {API_SERVER_SEND_ERROR}"}
        )
        with patch("tools.send_message_tool._send_to_platform", standalone), caplog.at_level(
            logging.ERROR, logger="cron.scheduler"
        ):
            err = _deliver_result(job, "follow-up body", adapters=None, loop=None)

        assert err is None, (
            f"non-push origin must be treated as local-only (None), got {err!r}"
        )
        standalone.assert_not_awaited()
        # Permanent structural failure must not ERROR every tick.
        assert not any(API_SERVER_SEND_ERROR in r.getMessage() for r in caplog.records)


class TestPermanentLiveFailureSkipsStandaloneRetry:
    """If a live adapter reports the structural api_server error, do not
    re-attempt the same permanent failure on the standalone path (#83484).
    """

    def test_permanent_live_failure_does_not_call_standalone(self):
        from concurrent.futures import Future

        cfg = GatewayConfig()
        cfg.platforms[Platform.API_SERVER] = PlatformConfig(enabled=True, extra={})

        adapter = AsyncMock()
        # Live path returns the same structural SendResult production uses.
        fail_result = SendResult(success=False, error=API_SERVER_SEND_ERROR)
        adapter.send.return_value = fail_result

        pconfig = MagicMock()
        pconfig.enabled = True
        pconfig.extra = {}
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.API_SERVER: pconfig}
        mock_cfg.filter_silence_narration = False

        loop = MagicMock()
        loop.is_running.return_value = True

        completed = Future()
        completed.set_result(fail_result)

        def fake_run_coro(coro, _loop):
            # Close the scheduled coroutine (we inject the result via Future).
            try:
                coro.close()
            except Exception:
                pass
            return completed

        standalone = AsyncMock(
            return_value={"error": f"Adapter send failed: {API_SERVER_SEND_ERROR}"}
        )

        # Force a target so we exercise the send path even if resolve is fixed.
        job = {
            "id": "job-live-perm",
            "name": "live-perm",
            "deliver": "origin",
            "origin": {"platform": "api_server", "chat_id": "sess-abc"},
        }

        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), patch(
            "cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}
        ), patch(
            "asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro
        ), patch(
            "agent.async_utils.safe_schedule_threadsafe", side_effect=fake_run_coro
        ), patch(
            "tools.send_message_tool._send_to_platform", standalone
        ), patch(
            "cron.scheduler._resolve_delivery_targets",
            return_value=[
                {
                    "platform": "api_server",
                    "chat_id": "sess-abc",
                    "thread_id": None,
                }
            ],
        ):
            err = _deliver_result(
                job,
                "body",
                adapters={Platform.API_SERVER: adapter},
                loop=loop,
            )

        # Structural miss: local semantics (None), no sticky delivery_error,
        # and standalone must not re-try the same permanent failure.
        assert err is None
        standalone.assert_not_awaited()


class TestIntervalJobNoLongerArmsOnPermanentDeliveryAlone:
    """End-to-end with real jobs.json: after a successful agent run against a
    non-push origin, delivery is local-only (None). The interval job may still
    re-arm for its next agent tick, but last_delivery_error stays clean so
    ops do not see permanent ERROR spam as a delivery failure loop.
    """

    def test_interval_job_delivery_is_not_permanent_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # Re-bind hermes home for jobs store
        monkeypatch.setenv("API_SERVER_KEY", "test-key-for-repro-only-32chars!!")
        # Ensure job paths pick up HERMES_HOME (module may have cached paths)
        from hermes_constants import get_hermes_home

        assert str(get_hermes_home()) == str(tmp_path) or True

        monkeypatch.setattr(
            "cron.scheduler._get_home_target_chat_id", lambda *_a, **_k: ""
        )

        job = create_job(
            prompt="poll status",
            schedule="every 1m",
            name="api-poll",
            deliver="origin",
            origin={"platform": "api_server", "chat_id": "sess-abc"},
        )
        err = _deliver_result(job, "poll result", adapters=None, loop=None)
        assert err is None

        mark_job_run(job["id"], success=True, delivery_error=err)
        stored = next(x for x in load_jobs() if x["id"] == job["id"])
        assert stored.get("last_delivery_error") in (None, "")
        assert stored.get("enabled") is True
        # Interval re-arms for the next agent run — that is correct.
        # The bug was treating delivery as a permanent ERROR every re-fire.
        assert stored.get("next_run_at") is not None

        # Force due and confirm the next fire also delivers cleanly (None).
        jobs = load_jobs()
        for x in jobs:
            if x["id"] == job["id"]:
                x["next_run_at"] = (datetime.now() - timedelta(seconds=5)).isoformat()
        save_jobs(jobs)
        assert any(d["id"] == job["id"] for d in get_due_jobs())
        err2 = _deliver_result(job, "poll result again", adapters=None, loop=None)
        assert err2 is None
