from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from scripts import skyai_v2_upstream_sync_daily_report as daily


NOW = datetime(2026, 7, 25, 6, 0, tzinfo=timezone.utc)


def _write_report(state_dir: Path, name: str, payload: dict[str, object]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def test_daily_report_aggregates_window_and_latest_verified_checks(
    tmp_path: Path,
) -> None:
    _write_report(
        tmp_path,
        "report-1.json",
        {
            "run_at": "2026-07-24T09:00:00+00:00",
            "status": "PASS",
            "outcome": "candidate_verified",
            "source_sha": "a" * 40,
            "upstream_sha": "b" * 40,
            "head_ahead": 92,
            "head_behind": 1,
            "checks": [
                {"name": "boundary", "passed": True},
                {"name": "tests", "passed": True},
            ],
        },
    )
    _write_report(
        tmp_path,
        "report-2.json",
        {
            "run_at": "2026-07-25T05:00:00+00:00",
            "status": "PASS",
            "outcome": "up_to_date",
            "source_sha": "c" * 40,
            "upstream_sha": "d" * 40,
            "head_ahead": 93,
            "head_behind": 0,
        },
    )

    reports = daily.reports_in_window(
        daily.load_reports(tmp_path),
        now=NOW,
        window_hours=24,
    )
    message = daily.format_report(
        reports,
        now=NOW,
        window_hours=24,
        timezone_name="Europe/Sofia",
    )

    assert "**Статус:** ✅ PASS" in message
    assert "**Изпълнения:** 2 (PASS 2 · PARTIAL 0 · BLOCKED 0)" in message
    assert "`up_to_date`" in message
    assert "`cccccccccc` / `dddddddddd`" in message
    assert "**Ahead / behind:** 93 / 0" in message
    assert "boundary=PASS, tests=PASS" in message
    assert "auto-merge, deploy и runtime промени са изключени" in message


def test_daily_report_surfaces_blocker_and_candidate_pr(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "report-blocked.json",
        {
            "run_at": "2026-07-25T04:00:00+00:00",
            "status": "BLOCKED",
            "outcome": "fail_closed",
            "blocker": "verification_failed",
            "pr_url": "https://github.com/lomliev/hermes-agent/pull/123",
        },
    )

    reports = daily.reports_in_window(
        daily.load_reports(tmp_path),
        now=NOW,
        window_hours=24,
    )
    message = daily.format_report(
        reports,
        now=NOW,
        window_hours=24,
        timezone_name="Europe/Sofia",
    )

    assert "**Статус:** ⛔ BLOCKED" in message
    assert "**Blocker-и:** verification_failed" in message
    assert "https://github.com/lomliev/hermes-agent/pull/123" in message


def test_daily_report_marks_missing_data_without_claiming_sync_failure(
    tmp_path: Path,
) -> None:
    message = daily.format_report(
        [],
        now=NOW,
        window_hours=24,
        timezone_name="Europe/Sofia",
    )

    assert "**Статус:** ⚪ NO DATA" in message
    assert "**Изпълнения:** 0" in message
    assert "Няма структуриран sync отчет" in message
    assert "BLOCKED" not in message


def test_loader_ignores_latest_pointer_and_malformed_reports(tmp_path: Path) -> None:
    _write_report(
        tmp_path,
        "latest.json",
        {
            "run_at": "2026-07-25T05:30:00+00:00",
            "status": "PASS",
        },
    )
    (tmp_path / "report-malformed.json").write_text("{", encoding="utf-8")
    _write_report(
        tmp_path,
        "report-valid.json",
        {
            "run_at": "2026-07-25T05:00:00+00:00",
            "status": "PASS",
        },
    )

    reports = daily.load_reports(tmp_path)

    assert len(reports) == 1
    assert reports[0]["status"] == "PASS"


def test_message_is_bounded_for_discord() -> None:
    reports = [
        {
            "_run_at": NOW,
            "run_at": NOW.isoformat(),
            "status": "BLOCKED",
            "outcome": "fail_closed",
            "blocker": f"blocker_{index}_" + ("x" * 120),
        }
        for index in range(30)
    ]

    message = daily.format_report(
        reports,
        now=NOW,
        window_hours=24,
        timezone_name="Europe/Sofia",
    )

    assert len(message) <= daily.MAX_MESSAGE_LENGTH
    assert message.endswith("…")
