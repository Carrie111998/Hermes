from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[3]
MODULE = ROOT / "ops/muncho/runtime/upstream_sync_discord_reporter.py"
SPEC = importlib.util.spec_from_file_location(
    "upstream_sync_discord_reporter_test",
    MODULE,
)
assert SPEC and SPEC.loader
reporter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reporter
SPEC.loader.exec_module(reporter)


def _report(created: str = "2026-07-25T05:00:00Z") -> dict[str, object]:
    return {
        "schema": reporter.REPORT_SCHEMA,
        "created_at_utc": created,
        "status": "PARTIAL",
        "muncho": {
            "status": "PARTIAL",
            "outcome": "sync_pr_opened_review_required",
            "source_sha": "a" * 40,
            "upstream_sha": "b" * 40,
            "ahead": 412,
            "behind": 2075,
            "pr_url": "https://github.com/lomliev/hermes-agent/pull/201",
            "blocker": None,
        },
        "skyai": {
            "status": "PASS",
            "outcome": "candidate_pr_ready",
            "source_sha": "c" * 40,
            "upstream_sha": "d" * 40,
            "ahead": 93,
            "behind": 12,
            "pr_url": "https://github.com/lomliev/hermes-agent/pull/178",
            "blocker": None,
        },
    }


def test_daily_report_contains_both_components_and_safety() -> None:
    report = _report()
    report["_created"] = datetime(2026, 7, 25, 5, tzinfo=timezone.utc)
    message = reporter.format_daily_report(
        [report],
        now=datetime(2026, 7, 25, 6, tzinfo=timezone.utc),
        timezone_name="Europe/Sofia",
        window_hours=24,
    )

    assert "Muncho + SkyAI upstream sync" in message
    assert "**Muncho/Hermes:** ⚠️ PARTIAL" in message
    assert "**SkyAI:** ✅ PASS" in message
    assert "pull/201" in message
    assert "pull/178" in message
    assert "без auto-merge, deploy" in message
    assert len(message) <= reporter.MAX_MESSAGE_LENGTH


def test_loader_rejects_wrong_schema_and_out_of_window(tmp_path: Path) -> None:
    (tmp_path / "report-good.json").write_text(
        json.dumps(_report()),
        encoding="utf-8",
    )
    wrong = _report()
    wrong["schema"] = "other"
    (tmp_path / "report-wrong.json").write_text(json.dumps(wrong), encoding="utf-8")
    (tmp_path / "report-old.json").write_text(
        json.dumps(_report("2026-07-20T05:00:00Z")),
        encoding="utf-8",
    )

    reports = reporter.load_reports(
        tmp_path,
        now=datetime(2026, 7, 25, 6, tzinfo=timezone.utc),
        window_hours=24,
    )
    assert len(reports) == 1
    assert reports[0]["schema"] == reporter.REPORT_SCHEMA


def test_delivery_is_attempted_once_and_uses_hermes_send() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fake_runner(args, **kwargs):
        calls.append((tuple(args), dict(kwargs)))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"ok": True, "message_id": "123"}),
            stderr="",
        )

    result = reporter.deliver_once(
        "bounded report",
        channel_id="1504852355588423801",
        runner=fake_runner,
    )

    assert result == {"status": "PASS", "message_id": "123"}
    assert len(calls) == 1
    assert calls[0][0] == (
        sys.executable,
        "-m",
        "hermes_cli.main",
        "send",
        "--to",
        "discord:1504852355588423801",
        "--json",
    )
    assert calls[0][1]["input"] == "bounded report"


def test_delivery_failure_has_no_retry_loop() -> None:
    attempts = 0

    def fake_runner(args, **kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

    result = reporter.deliver_once(
        "bounded report",
        channel_id="1504852355588423801",
        runner=fake_runner,
    )

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "discord_delivery_failed"
    assert attempts == 1


def test_sender_interpreter_digest_drift_blocks_before_delivery(
    tmp_path: Path,
) -> None:
    sender = tmp_path / "python"
    sender.write_bytes(b"reviewed interpreter")
    sender.chmod(0o755)
    attempts = 0

    def fake_runner(args, **kwargs):
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    result = reporter.deliver_once(
        "bounded report",
        channel_id="1504852355588423801",
        sender_python=sender,
        sender_python_sha256=hashlib.sha256(b"different").hexdigest(),
        runner=fake_runner,
    )

    assert result == {
        "status": "BLOCKED",
        "blocker": "sender_python_digest_drifted",
    }
    assert attempts == 0
