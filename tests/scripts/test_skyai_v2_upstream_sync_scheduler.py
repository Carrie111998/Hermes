from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

from scripts import skyai_v2_upstream_sync_scheduler as scheduler


def _report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "run_at": "2026-07-25T05:00:00+00:00",
        "status": "PASS",
        "outcome": "candidate_pr_ready",
        "source_sha": "a" * 40,
        "upstream_sha": "b" * 40,
        "candidate_sha": "c" * 40,
        "head_ahead": 93,
        "head_behind": 2,
        "checks": [
            {"name": "boundary", "passed": True},
            {"name": "tests", "passed": True},
        ],
        "pr_url": "https://github.com/lomliev/hermes-agent/pull/178",
        "auto_merge": False,
        "deploy": False,
        "runtime_mutation": False,
    }
    report.update(overrides)
    return report


def test_format_sync_report_is_bounded_and_factual() -> None:
    message = scheduler.format_sync_report(_report())

    assert "**Статус:** ✅ PASS" in message
    assert "`candidate_pr_ready`" in message
    assert "`aaaaaaaaaa` / `bbbbbbbbbb`" in message
    assert "**Ahead / behind:** 93 / 2" in message
    assert "boundary=PASS, tests=PASS" in message
    assert "https://github.com/lomliev/hermes-agent/pull/178" in message
    assert "без auto-merge, deploy и runtime промени" in message
    assert len(message) <= scheduler.MAX_MESSAGE_LENGTH


def test_scheduler_script_starts_outside_repo(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(scheduler.__file__).resolve()), "--help"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0
    assert "--job" in completed.stdout


def test_deliver_once_uses_existing_hermes_transport_once(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.touch()
    calls: list[dict[str, object]] = []

    def fake_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"ok": True, "message_id": "123"}),
            stderr="",
        )

    result = scheduler.deliver_once(
        "report",
        channel_id="1504852355588423801",
        hermes_bin=hermes,
        runner=fake_runner,
    )

    assert result == {"status": "PASS", "message_id": "123"}
    assert len(calls) == 1
    assert calls[0]["args"] == [
        str(hermes),
        "send",
        "--to",
        "discord:1504852355588423801",
        "--json",
    ]
    assert calls[0]["input"] == "report"


def test_delivery_failure_is_not_retried(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.touch()
    attempts = 0

    def fake_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr="failed",
        )

    result = scheduler.deliver_once(
        "report",
        channel_id="1504852355588423801",
        hermes_bin=hermes,
        runner=fake_runner,
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "discord_delivery_failed"
    assert attempts == 1


def test_sync_scheduler_runs_rail_and_delivers_one_report(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    hermes.touch()
    seen_config = None
    messages: list[str] = []

    def fake_sync(config: object) -> dict[str, object]:
        nonlocal seen_config
        seen_config = config
        return _report()

    def fake_delivery(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        messages.append(str(kwargs["input"]))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"ok": True, "message_id": "456"}),
            stderr="",
        )

    result, exit_code = scheduler.run_scheduler_job(
        job="sync",
        repo=tmp_path,
        state_dir=tmp_path / "state",
        channel_id="1504852355588423801",
        hermes_bin=hermes,
        timezone_name="Europe/Sofia",
        sync_runner=fake_sync,
        process_runner=fake_delivery,
    )

    assert seen_config is not None
    assert seen_config.execute is True
    assert seen_config.push_pr is True
    assert seen_config.fetch is True
    assert len(messages) == 1
    assert "candidate_pr_ready" in messages[0]
    assert result["delivery_status"] == "PASS"
    assert result["message_id"] == "456"
    assert exit_code == 0


def test_daily_scheduler_uses_existing_reports_without_running_sync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    hermes = tmp_path / "hermes"
    hermes.touch()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    now = datetime.now(timezone.utc)
    (state_dir / "report-current.json").write_text(
        json.dumps(
            {
                "run_at": now.isoformat(),
                "status": "PASS",
                "outcome": "up_to_date",
                "source_sha": "a" * 40,
                "upstream_sha": "b" * 40,
                "head_ahead": 93,
                "head_behind": 0,
            }
        ),
        encoding="utf-8",
    )
    messages: list[str] = []

    def fake_delivery(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        messages.append(str(kwargs["input"]))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"ok": True}),
            stderr="",
        )

    def forbidden_sync(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("daily job must not run the sync rail")

    result, exit_code = scheduler.run_scheduler_job(
        job="daily",
        repo=tmp_path,
        state_dir=state_dir,
        channel_id="1504852355588423801",
        hermes_bin=hermes,
        timezone_name="Europe/Sofia",
        sync_runner=forbidden_sync,
        process_runner=fake_delivery,
    )

    assert len(messages) == 1
    assert "дневен отчет" in messages[0]
    assert result["job"] == "daily"
    assert result["delivery_status"] == "PASS"
    assert exit_code == 0
