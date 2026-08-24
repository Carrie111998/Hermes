"""Exception-path failure alerts must use the fire-claim delivery fence."""

import threading
from unittest.mock import patch

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Keep the real fire-claim locks and jobs store inside an isolated home."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


def test_exception_alert_send_linearizes_before_replacement_claim(temp_home):
    """A replacement cannot claim while the old owner's failure alert is sending.

    The ordered events are the regression contract: if a replacement completes
    before the alert send, the alert came from a stale owner and must not land.
    """
    import cron.jobs as jobs
    import cron.scheduler as scheduler

    created = jobs.create_job(
        prompt="x",
        schedule="every 5m",
        name="exception-alert-fence",
        deliver="origin",
        origin={"platform": "telegram", "chat_id": "123"},
    )
    claimed = jobs.claim_job_for_fire(created["id"], return_job=True)
    assert isinstance(claimed, dict)

    replacement_started = threading.Event()
    replacement_finished = threading.Event()
    events: list[str] = []

    def replace_claim() -> None:
        replacement_started.set()
        # A zero TTL models a replacement after the old worker's lease expired.
        # It still must serialize on the same per-job fence as delivery.
        assert jobs.claim_job_for_fire(
            created["id"], claim_ttl_seconds=0, force=True
        ) is True
        events.append("replacement-claimed")
        replacement_finished.set()

    replacement = threading.Thread(target=replace_claim)

    def send_failure_alert(*_args, **_kwargs):
        replacement.start()
        assert replacement_started.wait(timeout=1)
        # With no delivery fence the forced claimant reaches the store before
        # this blocked send returns.  The fixed path must keep it blocked.
        replacement_finished.wait(timeout=0.1)
        events.append("alert-sent")
        return None

    with patch.object(scheduler, "run_job", side_effect=RuntimeError("boom")), \
         patch.object(scheduler, "_deliver_result", side_effect=send_failure_alert):
        assert scheduler.run_one_job(claimed) is False

    replacement.join(timeout=1)
    assert replacement_finished.is_set()
    assert events == ["alert-sent", "replacement-claimed"]
