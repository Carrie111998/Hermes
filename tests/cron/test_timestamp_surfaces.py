"""Integration coverage for per-job cron timestamp controls."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)

    import hermes_time
    from cron import jobs

    hermes_time.reset_cache()
    monkeypatch.setattr(
        jobs,
        "_compute_provider_model_snapshots",
        lambda **kwargs: (None, None),
    )
    yield home
    hermes_time.reset_cache()


def _stored_job(job_id: str):
    from cron.jobs import load_jobs

    return next(job for job in load_jobs() if job["id"] == job_id)


def test_shared_timestamp_setting_respects_explicit_false_with_other_keys():
    from gateway.message_timestamps import message_timestamps_enabled

    config = {"gateway": {"message_timestamps": {"enabled": False, "other": "stuff"}}}
    # Explicit False under gateway.message_timestamps must be honored even
    # when sibling keys are present, proving the setting is genuinely read
    # from the nested path rather than falling through to the missing-gateway
    # default.
    assert message_timestamps_enabled(config) is False
    assert config["gateway"]["message_timestamps"]["other"] == "stuff"


def test_model_tool_persists_true_false_and_inherit(isolated_home):
    from tools.cronjob_tools import cronjob

    created = json.loads(
        cronjob(
            action="create",
            prompt="model tool timestamps",
            schedule="every 1 hour",
            timestamps=True,
        )
    )
    job_id = created["job_id"]
    assert _stored_job(job_id)["timestamps"] is True

    updated = json.loads(cronjob(action="update", job_id=job_id, timestamps=False))
    assert updated["success"] is True
    assert _stored_job(job_id)["timestamps"] is False

    inherited = json.loads(cronjob(action="update", job_id=job_id, timestamps=None))
    assert inherited["success"] is True
    assert "timestamps" not in _stored_job(job_id)


def test_registered_model_tool_handler_persists_timestamp_overrides(isolated_home):
    import tools.cronjob_tools  # noqa: F401 - registers the tool
    from tools.registry import registry

    entry = registry.get_entry("cronjob")
    assert entry is not None

    created = json.loads(
        entry.handler({
            "action": "create",
            "prompt": "registered handler timestamps",
            "schedule": "every 1 hour",
            "timestamps": True,
        })
    )
    job_id = created["job_id"]
    assert _stored_job(job_id)["timestamps"] is True

    updated = json.loads(
        entry.handler({"action": "update", "job_id": job_id, "timestamps": False})
    )
    assert updated["success"] is True
    assert _stored_job(job_id)["timestamps"] is False

    inherited = json.loads(
        entry.handler({"action": "update", "job_id": job_id, "timestamps": None})
    )
    assert inherited["success"] is True
    assert "timestamps" not in _stored_job(job_id)


def test_rest_api_persists_true_false_and_inherit(isolated_home):
    import asyncio

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    async def exercise():
        adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
        app = web.Application()
        app.router.add_post("/api/jobs", adapter._handle_create_job)
        app.router.add_patch("/api/jobs/{job_id}", adapter._handle_update_job)

        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/jobs",
                json={
                    "name": "REST timestamps",
                    "schedule": "every 1 hour",
                    "prompt": "rest timestamps",
                    "timestamps": True,
                },
            )
            assert response.status == 200
            job_id = (await response.json())["job"]["id"]
            assert _stored_job(job_id)["timestamps"] is True

            response = await client.patch(
                f"/api/jobs/{job_id}", json={"timestamps": False}
            )
            assert response.status == 200
            assert _stored_job(job_id)["timestamps"] is False

            response = await client.patch(
                f"/api/jobs/{job_id}", json={"timestamps": None}
            )
            assert response.status == 200
            assert "timestamps" not in _stored_job(job_id)

    asyncio.run(exercise())


def _cron_parser():
    from hermes_cli.cron import cron_command
    from hermes_cli.subcommands.cron import build_cron_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    return parser


def test_cli_create_and_edit_persist_true_false_and_inherit(isolated_home, monkeypatch):
    import hermes_cli.cron as cron_cli

    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)
    parser = _cron_parser()

    args = parser.parse_args([
        "cron",
        "create",
        "every 1 hour",
        "cli timestamps",
        "--name",
        "CLI timestamps",
        "--timestamps",
        "on",
    ])
    assert args.func(args) == 0

    from cron.jobs import load_jobs

    job_id = next(job["id"] for job in load_jobs() if job["name"] == "CLI timestamps")
    assert _stored_job(job_id)["timestamps"] is True

    args = parser.parse_args(["cron", "edit", job_id, "--timestamps", "off"])
    assert args.func(args) == 0
    assert _stored_job(job_id)["timestamps"] is False

    args = parser.parse_args(["cron", "edit", job_id, "--timestamps", "inherit"])
    assert args.func(args) == 0
    assert "timestamps" not in _stored_job(job_id)


def _run_and_capture_prompt(job, home):
    from cron.scheduler import run_job

    fake_db = MagicMock()
    fake_agent = MagicMock()
    fake_agent.run_conversation.return_value = {"final_response": "ok"}
    runtime = {
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "provider": "openrouter",
        "api_mode": "chat_completions",
    }

    with (
        patch("cron.scheduler._hermes_home", home),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime
        ),
        patch("run_agent.AIAgent", return_value=fake_agent),
    ):
        success, _output, _final, error = run_job(job)

    assert success is True, error
    return fake_agent.run_conversation.call_args.args[0]


def test_run_job_refreshes_stale_prefix_in_configured_timezone(
    isolated_home, monkeypatch
):
    # Deliberately choose a configured timezone different from the host timezone.
    (isolated_home / "config.yaml").write_text(
        "timezone: America/Los_Angeles\n"
        "model:\n  default: test-model\n"
        "gateway:\n  message_timestamps:\n    enabled: true\n",
        encoding="utf-8",
    )

    import hermes_time

    hermes_time.reset_cache()
    first = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    second = datetime(2026, 7, 29, 13, 30, 0, tzinfo=timezone.utc).timestamp()
    job = {
        "id": "timestamp-run",
        "name": "timestamp run",
        "prompt": "[Mon 2020-01-01 01:02:03 UTC] recurring task",
        "model": "test-model",
        "provider": "openrouter",
        "base_url": None,
    }

    monkeypatch.setattr("cron.scheduler.time.time", lambda: first)
    prompt = _run_and_capture_prompt(job, isolated_home)
    assert prompt.startswith("[Wed 2026-07-29 05:00:00 PDT] ")
    assert prompt.endswith("recurring task")
    assert "2020-01-01" not in prompt

    monkeypatch.setattr("cron.scheduler.time.time", lambda: second)
    prompt = _run_and_capture_prompt(job, isolated_home)
    assert prompt.startswith("[Wed 2026-07-29 06:30:00 PDT] ")
    assert prompt.endswith("recurring task")
    assert "2020-01-01" not in prompt


def test_run_job_per_job_true_false_and_inherit(isolated_home, monkeypatch):
    (isolated_home / "config.yaml").write_text(
        "timezone: UTC\n"
        "model:\n  default: test-model\n"
        "gateway:\n  message_timestamps:\n    enabled: true\n",
        encoding="utf-8",
    )

    import hermes_time

    hermes_time.reset_cache()
    epoch = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr("cron.scheduler.time.time", lambda: epoch)
    base = {
        "id": "timestamp-overrides",
        "name": "timestamp overrides",
        "prompt": "run task",
        "model": "test-model",
        "provider": "openrouter",
        "base_url": None,
    }

    inherited = _run_and_capture_prompt(dict(base), isolated_home)
    disabled = _run_and_capture_prompt(
        {
            **base,
            "prompt": "[Mon 2020-01-01 01:02:03 UTC] run task",
            "timestamps": False,
        },
        isolated_home,
    )

    (isolated_home / "config.yaml").write_text(
        "timezone: UTC\n"
        "model:\n  default: test-model\n"
        "gateway:\n  message_timestamps:\n    enabled: false\n",
        encoding="utf-8",
    )
    enabled = _run_and_capture_prompt({**base, "timestamps": True}, isolated_home)

    assert inherited.startswith("[Wed 2026-07-29 12:00:00 UTC] ")
    assert inherited.endswith("run task")
    assert not disabled.startswith("[Wed 2026-07-29")
    assert "[Mon 2020-01-01 01:02:03 UTC] run task" in disabled
    assert disabled.endswith("run task")
    assert enabled.startswith("[Wed 2026-07-29 12:00:00 UTC] ")
    assert enabled.endswith("run task")
