"""First-class cron feedback configuration."""

import json

import pytest

from cron.jobs import create_job, get_job, update_job
from tools.cronjob_tools import CRONJOB_SCHEMA, cronjob


def _feedback():
    return {
        "prompt": "What happened?",
        "choices": [
            {"code": "done", "label": "Done"},
            {"code": "skip", "label": "Skipped"},
        ],
    }


def test_create_job_stores_normalized_feedback():
    job = create_job("Do the thing", "every 1h", feedback=_feedback())
    assert job["feedback"] == _feedback()
    assert get_job(job["id"])["feedback"] == _feedback()


def test_update_job_can_replace_and_clear_feedback():
    job = create_job("Do the thing", "every 1h", feedback=_feedback())
    replacement = {
        "prompt": "Status?",
        "choices": [{"code": "later", "label": "Later"}],
    }
    update_job(job["id"], {"feedback": replacement})
    assert get_job(job["id"])["feedback"] == replacement
    update_job(job["id"], {"feedback": {}})
    assert get_job(job["id"])["feedback"] is None


@pytest.mark.parametrize(
    "feedback",
    [
        {"prompt": "Missing choices"},
        {"choices": []},
        {"choices": [{"code": "BAD CODE", "label": "Bad"}]},
        {"choices": [{"code": "done", "label": ""}]},
        {"choices": [{"code": "x" * 25, "label": "Too long"}]},
        {"choices": [{"code": str(i), "label": str(i)} for i in range(9)]},
    ],
)
def test_create_job_rejects_invalid_feedback(feedback):
    with pytest.raises(ValueError, match="feedback"):
        create_job("Do the thing", "every 1h", feedback=feedback)


def test_cronjob_tool_schema_and_create_round_trip(monkeypatch):
    monkeypatch.setattr("tools.cronjob_tools._notify_provider_jobs_changed_safe", lambda: None)
    assert "feedback" in CRONJOB_SCHEMA["parameters"]["properties"]
    result = json.loads(
        cronjob(
            action="create",
            prompt="Do the thing",
            schedule="every 1h",
            feedback=_feedback(),
        )
    )
    assert result["success"] is True
    assert result["job"]["feedback"] == _feedback()


def test_legacy_telegram_feedback_is_read_but_new_jobs_use_generic_key():
    from cron.feedback import feedback_for_job

    legacy = {"telegram_feedback": _feedback()}
    assert feedback_for_job(legacy) == _feedback()
    job = create_job("Do the thing", "every 1h", feedback=_feedback())
    assert "telegram_feedback" not in job


def test_reminder_jobs_get_default_feedback_when_omitted():
    job = create_job(
        "Reminder: follow up with Wendell Butler about the sponsorship.",
        "30m",
        name="Follow up with Wendell Butler",
        deliver="origin",
    )
    assert job["feedback"] == {
        "prompt": "What happened?",
        "choices": [
            {"code": "done", "label": "✅ Done"},
            {"code": "not_yet", "label": "⏳ Not yet"},
            {"code": "skip", "label": "❌ Didn't do it"},
        ],
    }


def test_non_reminder_jobs_do_not_get_default_feedback():
    job = create_job("Check server status", "every 1h", deliver="origin")
    assert job["feedback"] is None


def test_explicit_empty_feedback_suppresses_reminder_default():
    job = create_job(
        "Remind me to check the build",
        "30m",
        deliver="origin",
        feedback={},
    )
    assert job["feedback"] is None
