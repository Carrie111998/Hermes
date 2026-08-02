import concurrent.futures
import os
import threading

import pytest

import cron.scheduler as scheduler
import tools.approval as approval


@pytest.mark.parametrize("initial", [None, "legacy"])
def test_run_job_preserves_original_cron_env(monkeypatch, initial):
    if initial is None:
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    else:
        monkeypatch.setenv("HERMES_CRON_SESSION", initial)

    seen = {}

    def fake_impl(job, *, defer_agent_teardown=None):
        seen["is_cron"] = approval.is_hermes_cron_session()
        seen["env"] = os.environ.get("HERMES_CRON_SESSION")
        return True, "out", "final", None

    monkeypatch.setattr(scheduler, "_run_job_impl", fake_impl)

    assert scheduler.run_job({"id": "env-preserve"}) == (
        True,
        "out",
        "final",
        None,
    )

    assert seen == {"is_cron": True, "env": initial}
    assert os.environ.get("HERMES_CRON_SESSION") == initial


def test_run_job_resets_cron_context_after_setup_exception(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)

    with pytest.raises(KeyError):
        scheduler.run_job({"prompt": "missing id"})

    assert approval.is_hermes_cron_session() is False


def test_cron_context_does_not_leak_to_parallel_gateway_context(monkeypatch):
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")

    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def fake_impl(job, *, defer_agent_teardown=None):
        seen["cron_thread_is_cron"] = approval.is_hermes_cron_session()
        seen["cron_thread_is_gateway"] = approval._is_gateway_approval_context()
        entered.set()
        assert release.wait(5), "test timed out waiting to release cron job"
        return True, "out", "final", None

    monkeypatch.setattr(scheduler, "_run_job_impl", fake_impl)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        cron_future = pool.submit(scheduler.run_job, {"id": "parallel-cron"})
        assert entered.wait(5), "test timed out waiting for cron job to start"

        seen["gateway_thread_is_cron"] = approval.is_hermes_cron_session()
        seen["gateway_thread_is_gateway"] = approval._is_gateway_approval_context()

        release.set()
        assert cron_future.result(timeout=5) == (True, "out", "final", None)

    assert seen == {
        "cron_thread_is_cron": True,
        "cron_thread_is_gateway": False,
        "gateway_thread_is_cron": False,
        "gateway_thread_is_gateway": True,
    }
    assert os.environ.get("HERMES_CRON_SESSION") is None
