"""Job-local cron limits must override global defaults without leaking."""
import concurrent.futures

from cron.scheduler import (
    _cron_job_positive_limit,
    _cron_job_wall_limit,
    _poll_cron_future_once,
)


def test_job_positive_limit_uses_valid_job_value():
    assert _cron_job_positive_limit({"max_turns": 28}, "max_turns", 500) == 28


def test_job_positive_limit_fails_closed_to_default_for_invalid_value():
    assert _cron_job_positive_limit({"max_turns": "zero"}, "max_turns", 500) == 500
    assert _cron_job_positive_limit({"max_turns": 0}, "max_turns", 500) == 500


def test_job_positive_limit_uses_default_when_not_present():
    assert _cron_job_positive_limit({}, "max_wall_seconds", 1080) == 1080


def test_wall_limit_disables_non_positive_and_invalid_values():
    assert _cron_job_wall_limit({}) is None
    assert _cron_job_wall_limit({"max_wall_seconds": 0}) is None
    assert _cron_job_wall_limit({"max_wall_seconds": -1}) is None
    assert _cron_job_wall_limit({"max_wall_seconds": "bad"}) is None


def test_wall_limit_accepts_positive_integer_seconds():
    assert _cron_job_wall_limit({"max_wall_seconds": 45}) == 45
    assert _cron_job_wall_limit({"max_wall_seconds": "45"}) == 45


def test_completed_future_wins_before_wall_check():
    future = concurrent.futures.Future()
    future.set_result("done")
    calls: list[str] = []

    finished, result = _poll_cron_future_once(
        {future},
        future,
        lambda: calls.append("claim"),
        lambda: calls.append("wall"),
    )

    assert finished is True
    assert result == "done"
    assert calls == ["claim"]


def test_running_future_checks_wall():
    future = concurrent.futures.Future()
    calls: list[str] = []

    finished, result = _poll_cron_future_once(
        set(),
        future,
        lambda: calls.append("claim"),
        lambda: calls.append("wall"),
    )

    assert finished is False
    assert result is None
    assert calls == ["wall"]
