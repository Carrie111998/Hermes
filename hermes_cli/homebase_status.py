"""Read-only Wizard Tower status report used by ``/homebase-status``.

This deliberately has no gateway/session dependency so the CLI and gateway
render the same live snapshot. Every probe is bounded; an unavailable optional
component is reported rather than failing the entire dashboard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", HOME / ".hermes"))
UPS_NAME = "wizardtowerups@localhost"


def _run(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _service_state(unit: str, user: bool = False) -> str:
    args = ["systemctl"] + (["--user"] if user else []) + ["is-active", unit]
    rc, output = _run(args)
    return "✅ active" if rc == 0 and output == "active" else f"⚠️ {output or 'unavailable'}"


def _age(path: Path) -> str:
    try:
        seconds = max(0, (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime))
    except OSError:
        return "⚠️ unavailable"
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def _ups() -> str:
    rc, output = _run(["upsc", UPS_NAME])
    if rc:
        return f"⚠️ unavailable ({output})"
    fields = {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in output.splitlines() if ":" in line)
    }
    status = fields.get("ups.status", "unknown")
    battery = fields.get("battery.charge", "?")
    runtime = fields.get("battery.runtime", "?")
    prefix = "✅" if "OL" in status.split() else "⚠️"
    return f"{prefix} {status}; battery {battery}%; runtime {runtime}s"


def _disk() -> str:
    usage = shutil.disk_usage("/")
    pct = usage.used / usage.total * 100
    return f"{'✅' if pct < 85 else '⚠️'} /: {usage.free / 2**30:.1f} GiB free ({pct:.0f}% used)"


def _thermal() -> str:
    parts: list[str] = []
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000
        parts.append(f"{temp:.1f}°C")
    except (OSError, ValueError):
        parts.append("temp unavailable")
    rc, output = _run(["vcgencmd", "get_throttled"])
    throttle = output.split("=")[-1] if rc == 0 and "=" in output else "unavailable"
    prefix = "✅" if throttle == "0x0" else "⚠️"
    return f"{prefix} {', '.join(parts)}; throttling {throttle}"


def _backup() -> str:
    backup_dir = HERMES_HOME / "backups"
    try:
        newest = max(backup_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    except (ValueError, OSError):
        return "⚠️ no local backup found"
    age = _age(newest)
    stale = newest.stat().st_mtime < datetime.now().timestamp() - 36 * 3600
    return f"{'⚠️' if stale else '✅'} {newest.name} ({age})"


def _mainframe() -> str:
    rc, _ = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "mainframe", "true"], timeout=6)
    return "✅ reachable" if rc == 0 else "⚠️ unreachable"


def _maintenance() -> str:
    unit = "hermes-daily-self-maintenance.service"
    rc, output = _run(["systemctl", "--user", "show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p", "ExecMainExitTimestamp"])
    if rc:
        return "⚠️ no maintenance result available"
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    result = values.get("Result", "unknown")
    when = values.get("ExecMainExitTimestamp", "") or "no completed run"
    prefix = "✅" if result == "success" else "⚠️"
    return f"{prefix} {result}; {when}"


def format_homebase_status() -> str:
    """Return a compact, bounded, read-only status report."""
    lines = [
        "**Wizard Tower Homebase Status**",
        f"Gateway: {_service_state('hermes-gateway.service', user=True)}",
        f"UPS: {_ups()}",
        f"Disk: {_disk()}",
        f"Thermal: {_thermal()}",
        f"Backup: {_backup()}",
        f"Mainframe: {_mainframe()}",
        f"Last maintenance: {_maintenance()}",
    ]
    return "\n".join(lines)
