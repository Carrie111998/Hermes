from __future__ import annotations

import pytest

import cron.scheduler as scheduler
from tools.cron_gateway_guard import contains_gateway_selfkill


@pytest.mark.parametrize(
    "command",
    [
        "launchctl kickstart -k \\\n  gui/$(id -u)/ai.hermes.gateway",
        "hermes gateway \\\n  restart",
        "systemctl restart \\\n  hermes-gateway",
        "pkill -f 'hermes \\\n  gateway'",
    ],
)
def test_gateway_selfkill_guard_normalizes_shell_line_continuations(command):
    assert contains_gateway_selfkill(command) is True


def test_run_job_blocks_gateway_selfkill_in_transient_extra_prompt(monkeypatch):
    def must_not_run(*_args, **_kwargs):
        pytest.fail("transient self-kill prompt reached agent execution")

    monkeypatch.setattr(scheduler, "_build_job_prompt", must_not_run)
    success, document, response, error = scheduler.run_job(
        {
            "id": "transient-selfkill",
            "name": "safe stored job",
            "prompt": "Summarize gateway health",
            "no_agent": False,
        },
        extra_prompt="Now run hermes gateway restart",
    )

    assert success is False
    assert response == ""
    assert "BLOCKED_GATEWAY_SELFKILL" in document
    assert error and "restart/stop/kill" in error


def test_run_job_blocks_gateway_selfkill_before_no_agent_script(monkeypatch):
    def must_not_run(*_args, **_kwargs):
        pytest.fail("self-killing script path reached execution")

    monkeypatch.setattr(scheduler, "_run_job_script_with_claim_heartbeat", must_not_run)
    success, document, response, error = scheduler.run_job(
        {
            "id": "legacy-selfkill",
            "name": "legacy self-kill",
            "prompt": "Run hermes gateway restart now",
            "script": "safe.py",
            "no_agent": True,
        }
    )

    assert success is False
    assert response == ""
    assert "BLOCKED_GATEWAY_SELFKILL" in document
    assert error and "restart/stop/kill" in error
