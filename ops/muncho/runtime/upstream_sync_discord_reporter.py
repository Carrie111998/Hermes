#!/usr/bin/env python3
"""Send one bounded daily Muncho + SkyAI sync report through Hermes.

The reporter reads only world-readable sanitized reports produced by the
mechanical sync service.  It has no GitHub credential and makes exactly one
delivery attempt per invocation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


REPORT_SCHEMA = "muncho-dual-upstream-sync-public.v1"
DELIVERY_SCHEMA = "muncho-dual-upstream-sync-discord-delivery.v1"
DEFAULT_CHANNEL_ID = "1504852355588423801"
DEFAULT_TIMEZONE = "Europe/Sofia"
DEFAULT_WINDOW_HOURS = 24
MAX_MESSAGE_LENGTH = 1900
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_PR_URL = re.compile(r"^https://github\.com/lomliev/hermes-agent/pull/[0-9]+$")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_sha(value: object) -> str:
    text = str(value or "")
    return text[:10] if _SHA40.fullmatch(text) else "—"


def safe_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "—"


def safe_code(value: object) -> str:
    text = str(value or "")
    return text if _CODE.fullmatch(text) else "unknown"


def safe_pr(value: object) -> str | None:
    text = str(value or "")
    return text if _PR_URL.fullmatch(text) else None


def load_reports(
    public_dir: Path,
    *,
    now: datetime,
    window_hours: int,
) -> list[dict[str, Any]]:
    lower = now.astimezone(timezone.utc) - timedelta(hours=window_hours)
    reports: list[dict[str, Any]] = []
    for path in sorted(public_dir.glob("report-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != REPORT_SCHEMA:
            continue
        created = parse_timestamp(payload.get("created_at_utc"))
        if created is None or not (lower < created <= now.astimezone(timezone.utc)):
            continue
        payload = dict(payload)
        payload["_created"] = created
        reports.append(payload)
    return sorted(reports, key=lambda item: item["_created"])


def overall_status(reports: Iterable[Mapping[str, Any]]) -> str:
    priority = {"PASS": 0, "PARTIAL": 1, "BLOCKED": 2}
    statuses = [
        str(report.get("status") or "").upper()
        for report in reports
        if str(report.get("status") or "").upper() in priority
    ]
    return max(statuses, key=priority.__getitem__) if statuses else "NO DATA"


def _component_line(label: str, component: Mapping[str, Any]) -> list[str]:
    status = str(component.get("status") or "BLOCKED").upper()
    if status not in {"PASS", "PARTIAL", "BLOCKED"}:
        status = "BLOCKED"
    icon = {"PASS": "✅", "PARTIAL": "⚠️", "BLOCKED": "⛔"}[status]
    source = safe_sha(component.get("source_sha"))
    upstream = safe_sha(component.get("upstream_sha"))
    outcome = safe_code(component.get("outcome"))
    lines = [
        (
            f"**{label}:** {icon} {status} · `{outcome}` · "
            f"`{source}` / `{upstream}` · "
            f"ahead {safe_count(component.get('ahead'))}, "
            f"behind {safe_count(component.get('behind'))}"
        )
    ]
    blocker = component.get("blocker")
    if blocker:
        lines.append(f"  blocker: `{safe_code(blocker)}`")
    pr_url = safe_pr(component.get("pr_url"))
    if pr_url:
        lines.append(f"  PR: {pr_url}")
    return lines


def format_daily_report(
    reports: Sequence[dict[str, Any]],
    *,
    now: datetime,
    timezone_name: str,
    window_hours: int,
) -> str:
    local_tz = ZoneInfo(timezone_name)
    start = (now - timedelta(hours=window_hours)).astimezone(local_tz)
    end = now.astimezone(local_tz)
    status = overall_status(reports)
    icon = {
        "PASS": "✅",
        "PARTIAL": "⚠️",
        "BLOCKED": "⛔",
        "NO DATA": "⚪",
    }[status]
    counts = Counter(str(item.get("status") or "").upper() for item in reports)
    lines = [
        "**Muncho + SkyAI upstream sync — дневен отчет**",
        f"**Статус:** {icon} {status}",
        f"**Период:** {start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M} ({timezone_name})",
        (
            f"**Изпълнения:** {len(reports)} "
            f"(PASS {counts['PASS']} · PARTIAL {counts['PARTIAL']} · "
            f"BLOCKED {counts['BLOCKED']})"
        ),
    ]
    if not reports:
        lines.extend(
            [
                "⚠️ Няма структуриран 3-часов sync отчет за периода.",
                "**Safety:** без auto-merge, deploy и runtime промени.",
            ]
        )
        return "\n".join(lines)

    latest = reports[-1]
    created = latest["_created"].astimezone(local_tz)
    lines.append(f"**Последно изпълнение:** {created:%d.%m.%Y %H:%M}")
    muncho = latest.get("muncho")
    skyai = latest.get("skyai")
    lines.extend(
        _component_line("Muncho/Hermes", muncho if isinstance(muncho, dict) else {})
    )
    lines.extend(_component_line("SkyAI", skyai if isinstance(skyai, dict) else {}))
    lines.append(
        "**Safety:** кандидат PR-и само във fork-а; без auto-merge, deploy, "
        "gateway restart, SkyAI runtime, frontend или PBX промени."
    )
    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LENGTH:
        return message[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return message


def delivery_succeeded(payload: object) -> bool:
    return isinstance(payload, dict) and (
        payload.get("ok") is True or payload.get("success") is True
    )


def deliver_once(
    message: str,
    *,
    channel_id: str,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{15,22}", channel_id):
        return {"status": "BLOCKED", "blocker": "invalid_discord_channel_id"}
    try:
        completed = runner(
            (
                sys.executable,
                "-m",
                "hermes_cli.main",
                "send",
                "--to",
                f"discord:{channel_id}",
                "--json",
            ),
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
        payload = json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        return {"status": "BLOCKED", "blocker": "discord_delivery_invalid_result"}
    if not delivery_succeeded(payload):
        return {"status": "BLOCKED", "blocker": "discord_delivery_rejected"}
    result: dict[str, Any] = {"status": "PASS"}
    if isinstance(payload, dict) and payload.get("message_id"):
        result["message_id"] = str(payload["message_id"])
    return result


def write_delivery_receipt(
    state_dir: Path,
    *,
    result: Mapping[str, Any],
    report_count: int,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    payload = {
        "schema": DELIVERY_SCHEMA,
        "created_at_utc": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "status": result.get("status"),
        "blocker": result.get("blocker"),
        "message_id": result.get("message_id"),
        "report_count": report_count,
        "attempts": 1,
        "secret_material_recorded": False,
    }
    target = state_dir / "latest.json"
    temporary = state_dir / f".latest.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-report-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--now")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.window_hours <= 0:
        raise SystemExit("window hours must be positive")
    try:
        ZoneInfo(args.timezone)
    except Exception as exc:
        raise SystemExit("unknown timezone") from exc
    now = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit("invalid --now")
    reports = load_reports(
        args.public_report_dir.resolve(),
        now=now,
        window_hours=args.window_hours,
    )
    message = format_daily_report(
        reports,
        now=now,
        timezone_name=args.timezone,
        window_hours=args.window_hours,
    )
    result = deliver_once(message, channel_id=args.channel_id)
    write_delivery_receipt(
        args.state_dir.resolve(),
        result=result,
        report_count=len(reports),
    )
    print(
        json.dumps(
            {
                "schema": DELIVERY_SCHEMA,
                "status": result["status"],
                "blocker": result.get("blocker"),
                "message_id": result.get("message_id"),
                "report_count": len(reports),
                "attempts": 1,
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
