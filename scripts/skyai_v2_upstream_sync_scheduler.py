#!/usr/bin/env python3
"""Run SkyAI sync/report jobs without creating Codex tasks.

The macOS scheduler invokes this deterministic wrapper. A sync job runs the
existing fail-closed upstream-sync rail and posts exactly one bounded report
through ``hermes send``. A daily job only summarizes existing private reports.
Neither job invokes an LLM or a Codex automation.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from scripts import skyai_v2_upstream_sync_daily_report as daily
from scripts import skyai_v2_upstream_sync_routine as routine


SCHEDULER_SCHEMA = "skyai-v2-upstream-sync-scheduler.v1"
DEFAULT_CHANNEL_ID = "1504852355588423801"
DEFAULT_TIMEZONE = "Europe/Sofia"
MAX_MESSAGE_LENGTH = 1900


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
SyncRunner = Callable[[routine.SyncConfig], dict[str, Any]]


def _short_sha(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if text else "—"


def _safe_int(value: object) -> str:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else "—"


def _status_icon(status: str) -> str:
    return daily.STATUS_ICON.get(status, "⚪")


def _checks_line(report: dict[str, Any]) -> str | None:
    checks = report.get("checks")
    if not isinstance(checks, list):
        return None
    parts: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        name = str(check.get("name", "")).strip()
        if name:
            parts.append(f"{name}={'PASS' if check.get('passed') is True else 'FAIL'}")
    return ", ".join(parts) or None


def format_sync_report(
    report: dict[str, Any],
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> str:
    status = str(report.get("status", "BLOCKED")).upper()
    if status not in {"PASS", "PARTIAL", "BLOCKED"}:
        status = "BLOCKED"
    run_at = daily.parse_timestamp(report.get("run_at")) or datetime.now(timezone.utc)
    local_run_at = run_at.astimezone(ZoneInfo(timezone_name))
    lines = [
        "**SkyAI upstream sync — 3-часов отчет**",
        f"**Статус:** {_status_icon(status)} {status}",
        f"**Време:** {local_run_at:%d.%m.%Y %H:%M} ({timezone_name})",
        f"**Резултат:** `{report.get('outcome', 'unknown')}`",
        (
            f"**Source / upstream:** `{_short_sha(report.get('source_sha'))}` / "
            f"`{_short_sha(report.get('upstream_sha'))}`"
        ),
        (
            f"**Ahead / behind:** {_safe_int(report.get('head_ahead'))} / "
            f"{_safe_int(report.get('head_behind'))}"
        ),
    ]
    candidate_sha = report.get("candidate_sha")
    if candidate_sha:
        lines.append(f"**Candidate:** `{_short_sha(candidate_sha)}`")
    checks = _checks_line(report)
    if checks:
        lines.append(f"**Проверки:** {checks}")
    blocker = str(report.get("blocker", "")).strip()
    if blocker:
        lines.append(f"**Blocker:** `{blocker}`")
    pr_url = str(report.get("pr_url", "")).strip()
    if pr_url:
        lines.append(f"**Candidate PR:** {pr_url}")
    if report.get("duplicate_of_previous") is True:
        lines.append("Няма нова промяна спрямо предишното изпълнение.")
    lines.append("**Safety:** без auto-merge, deploy и runtime промени.")
    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LENGTH:
        return message[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return message


def _delivery_succeeded(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is True or payload.get("success") is True


def deliver_once(
    message: str,
    *,
    channel_id: str,
    hermes_bin: Path,
    runner: ProcessRunner = subprocess.run,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{15,22}", channel_id):
        return {"status": "BLOCKED", "blocker": "invalid_discord_channel_id"}
    if not hermes_bin.is_file():
        return {"status": "BLOCKED", "blocker": "hermes_send_not_found"}
    try:
        completed = runner(
            [
                str(hermes_bin),
                "send",
                "--to",
                f"discord:{channel_id}",
                "--json",
            ],
            input=message,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "BLOCKED", "blocker": "discord_delivery_error"}
    if completed.returncode != 0:
        return {
            "status": "BLOCKED",
            "blocker": "discord_delivery_failed",
            "returncode": completed.returncode,
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"status": "BLOCKED", "blocker": "discord_delivery_invalid_result"}
    if not _delivery_succeeded(payload):
        return {"status": "BLOCKED", "blocker": "discord_delivery_rejected"}
    result: dict[str, Any] = {"status": "PASS"}
    if isinstance(payload, dict) and payload.get("message_id"):
        result["message_id"] = str(payload["message_id"])
    return result


def build_sync_config(repo: Path, state_dir: Path) -> routine.SyncConfig:
    return routine.SyncConfig(
        repo=repo,
        state_dir=state_dir,
        worktree_root=state_dir / "worktrees",
        execute=True,
        push_pr=True,
        fetch=True,
    )


def run_scheduler_job(
    *,
    job: str,
    repo: Path,
    state_dir: Path,
    channel_id: str,
    hermes_bin: Path,
    timezone_name: str,
    sync_runner: SyncRunner = routine.run_sync,
    process_runner: ProcessRunner = subprocess.run,
) -> tuple[dict[str, Any], int]:
    if job == "sync":
        report = sync_runner(build_sync_config(repo, state_dir))
        message = format_sync_report(report, timezone_name=timezone_name)
        job_status = str(report.get("status", "BLOCKED"))
        outcome = str(report.get("outcome", "unknown"))
    else:
        now = datetime.now(timezone.utc)
        reports = daily.reports_in_window(
            daily.load_reports(state_dir),
            now=now,
            window_hours=24,
        )
        message = daily.format_report(
            reports,
            now=now,
            window_hours=24,
            timezone_name=timezone_name,
        )
        job_status = daily.aggregate_status(reports)
        outcome = "daily_summary"

    delivery = deliver_once(
        message,
        channel_id=channel_id,
        hermes_bin=hermes_bin,
        runner=process_runner,
    )
    result: dict[str, Any] = {
        "schema": SCHEDULER_SCHEMA,
        "job": job,
        "job_status": job_status,
        "outcome": outcome,
        "delivery_status": delivery["status"],
    }
    if delivery.get("message_id"):
        result["message_id"] = delivery["message_id"]
    if delivery.get("blocker"):
        result["blocker"] = delivery["blocker"]
    exit_code = (
        0
        if delivery["status"] == "PASS" and job_status in {"PASS", "PARTIAL"}
        else 1
    )
    return result, exit_code


def build_parser() -> argparse.ArgumentParser:
    repo_default = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", choices=("sync", "daily"), required=True)
    parser.add_argument("--repo", type=Path, default=repo_default)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".hermes" / "state" / "skyai-v2-upstream-sync",
    )
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument(
        "--hermes-bin",
        type=Path,
        default=Path(shutil.which("hermes") or Path.home() / ".local/bin/hermes"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ZoneInfo(args.timezone)
    except Exception as exc:
        raise SystemExit(f"unknown timezone: {args.timezone}") from exc
    result, exit_code = run_scheduler_job(
        job=args.job,
        repo=args.repo.expanduser().resolve(),
        state_dir=args.state_dir.expanduser().resolve(),
        channel_id=args.channel_id,
        hermes_bin=args.hermes_bin.expanduser().resolve(),
        timezone_name=args.timezone,
    )
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
