"""Regression guard — script-job failures must not be reported as provider errors.

``_summarize_cron_failure_for_delivery`` classifies a failed cron run into a
one-line chat message using substring heuristics ("timed out", "429",
"authenticat"). Those heuristics describe an agent run talking to an LLM
provider, but they were applied to every job, including ``no_agent`` script
jobs that never contact a provider at all.

Real-world symptom: a nightly ``no_agent`` script hit its script timeout and
the runner returned ``Script timed out after 12600s: /path/podcast-nightly.sh``.
That string contains "timed out", so the operator was told:

    ⚠️ Cron 'podcast-adcut-nightly' failed: provider timeout.
    Fallback chain was exhausted or unavailable.

— and went debugging LLM providers and fallback chains for a shell-script
timeout. Script jobs now fall through to the generic cleaned-message path so
the real error is shown; agent jobs keep the provider wording.
"""

import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _summarize_cron_failure_for_delivery


PROVIDER_WORDING = ("provider", "fallback chain")


def _script_job(name="podcast-adcut-nightly"):
    return {"id": "job-1", "name": name, "no_agent": True, "script": "/x.sh"}


def _agent_job(name="daily-briefing"):
    return {"id": "job-2", "name": name, "prompt": "summarize the news"}


def _assert_no_provider_wording(msg):
    lowered = msg.lower()
    for phrase in PROVIDER_WORDING:
        assert phrase not in lowered, f"{phrase!r} leaked into script-job message: {msg}"


# --- script jobs: no provider, no fallback chain -------------------------

def test_script_timeout_is_not_reported_as_provider_timeout():
    """The #podcast-adcut-nightly symptom: "timed out" is the SCRIPT's."""
    msg = _summarize_cron_failure_for_delivery(
        _script_job(),
        "Script timed out after 12600s: /Users/x/scripts/podcast-nightly.sh",
    )
    _assert_no_provider_wording(msg)
    assert "Script timed out after 12600s" in msg
    assert "podcast-nightly.sh" in msg
    assert "podcast-adcut-nightly" in msg


def test_script_nonzero_exit_surfaces_the_script_error():
    msg = _summarize_cron_failure_for_delivery(
        _script_job(), "Script exited with code 1\nstderr:\nboom",
    )
    _assert_no_provider_wording(msg)
    assert "Script exited with code 1" in msg


def test_script_job_rate_limit_wording_in_script_output_is_not_provider_framed():
    """A script whose own output says "rate limit" is still a script failure."""
    msg = _summarize_cron_failure_for_delivery(
        _script_job(),
        "Script exited with code 1\nstderr:\ncurl: API returned 429 rate limit",
    )
    _assert_no_provider_wording(msg)
    assert "Script exited with code 1" in msg


def test_script_job_auth_wording_in_script_output_is_not_provider_framed():
    msg = _summarize_cron_failure_for_delivery(
        _script_job(),
        "Script exited with code 1\nstderr:\nrsync: authentication failed (403)",
    )
    _assert_no_provider_wording(msg)
    assert "Script exited with code 1" in msg


def test_script_job_without_script_field_still_avoids_provider_wording():
    """``no_agent`` alone is enough to disqualify the provider branches."""
    msg = _summarize_cron_failure_for_delivery(
        {"id": "job-3", "name": "watchdog", "no_agent": True},
        "no_agent=True but no script is set for this job",
    )
    _assert_no_provider_wording(msg)
    assert "no script is set" in msg


# --- agent jobs: provider wording must be preserved ----------------------

def test_agent_job_provider_timeout_message_unchanged():
    msg = _summarize_cron_failure_for_delivery(
        _agent_job(), "httpx.ReadTimeout: request timed out after 600s",
    )
    assert msg == (
        "⚠️ Cron 'daily-briefing' failed: provider timeout. "
        "Fallback chain was exhausted or unavailable. "
        "Full details saved in cron output."
    )


def test_agent_job_rate_limit_message_unchanged():
    msg = _summarize_cron_failure_for_delivery(
        _agent_job(), "Error code: 429 - rate limit exceeded",
    )
    assert msg == (
        "⚠️ Cron 'daily-briefing' failed: provider rate limit. "
        "Fallback chain was exhausted or unavailable. "
        "Full details saved in cron output."
    )


def test_agent_job_weekly_usage_limit_message_unchanged():
    msg = _summarize_cron_failure_for_delivery(
        _agent_job(), "weekly usage limit reached for this account",
    )
    assert "provider weekly usage limit" in msg
    assert "Fallback chain was exhausted or unavailable." in msg


def test_agent_job_auth_message_unchanged():
    msg = _summarize_cron_failure_for_delivery(
        _agent_job(), "401 authentication_error: invalid x-api-key",
    )
    assert msg == (
        "⚠️ Cron 'daily-briefing' failed: provider authentication error. "
        "Full details saved in cron output."
    )


def test_agent_job_with_prerun_script_keeps_provider_wording():
    """A ``script`` without ``no_agent`` is a data-collection pre-run step; the
    job still calls a provider, so provider failures keep their message."""
    job = {"id": "job-4", "name": "market-watch", "script": "/collect.sh",
           "prompt": "analyze"}
    msg = _summarize_cron_failure_for_delivery(
        job, "httpx.ReadTimeout: request timed out",
    )
    assert "provider timeout" in msg
    assert "Fallback chain was exhausted or unavailable." in msg
