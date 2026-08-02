#!/usr/bin/env python3
"""Nightly Hermes maintenance check — silent when healthy, reports when action is needed.

Checks:
  1. Update availability  — hermes update --check
  2. Gateway health       — process alive check (cross-platform)
  3. Cron job failures    — enabled jobs with last_status='error'

Zero LLM cost. No imports from hermes_cli. Uses only stdlib + the Hermes CLI.
Works on Linux, macOS, and Windows.

Environment variables:
  MAINTENANCE_REPORT_ALL    =1 to always print (even when healthy)
  MAINTENANCE_SKIP_UPDATE   =1 to skip update check
  MAINTENANCE_SKIP_GATEWAY  =1 to skip gateway health check
  MAINTENANCE_SKIP_CRON     =1 to skip cron failure scan
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPORT_ALL = os.environ.get("MAINTENANCE_REPORT_ALL") == "1"
SKIP_UPDATE = os.environ.get("MAINTENANCE_SKIP_UPDATE") == "1"
SKIP_GATEWAY = os.environ.get("MAINTENANCE_SKIP_GATEWAY") == "1"
SKIP_CRON = os.environ.get("MAINTENANCE_SKIP_CRON") == "1"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -2, "", str(e)


def _find_hermes_exe() -> str | None:
    """Locate the hermes executable."""
    # 1. HERMES_HOME env
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates = [
            Path(home) / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            Path(home) / "hermes-agent" / "venv" / "bin" / "hermes",
        ]
        for c in candidates:
            if c.exists():
                return str(c)
    # 2. On PATH
    for name in ("hermes", "hermes.exe"):
        rc, out, _ = _run(["which", name] if sys.platform != "win32" else ["where", name], timeout=5)
        if rc == 0 and out:
            return out.splitlines()[0].strip()
    # 3. Common Windows location
    win_path = Path(os.path.expanduser("~")) / "AppData" / "Local" / "hermes" / "hermes-agent" / "venv" / "Scripts" / "hermes.exe"
    if win_path.exists():
        return str(win_path)
    # 4. Common Unix location
    unix_path = Path(os.path.expanduser("~")) / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes"
    if unix_path.exists():
        return str(unix_path)
    return None


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

def check_updates(hermes_exe: str | None) -> dict:
    """Run hermes update --check and parse the result."""
    if SKIP_UPDATE:
        return {"skipped": True}
    if not hermes_exe:
        return {"error": "hermes executable not found"}
    rc, out, err = _run([hermes_exe, "update", "--check"], timeout=45)
    if rc != 0 and not out:
        return {"error": f"hermes update --check failed: {err or 'unknown'}"}
    combined = f"{out}\n{err}"
    # Parse: "Update available: N commits behind upstream/main"
    m = re.search(r"(\d+)\s+commits?\s+behind", combined, re.IGNORECASE)
    if m:
        count = int(m.group(1))
        # Try to extract latest commit info
        latest = None
        for line in combined.splitlines():
            if "behind" in line.lower():
                continue
            stripped = line.strip()
            if stripped and not stripped.startswith(("→", "⚕", "Run ")):
                latest = stripped
                break
        return {"available": True, "commits_behind": count, "latest": latest}
    # "Already up to date" or similar
    if "up to date" in combined.lower() or "up-to-date" in combined.lower():
        return {"available": False, "commits_behind": 0}
    # Ambiguous — treat as unknown
    return {"available": False, "commits_behind": 0, "raw": combined[:200]}


# ---------------------------------------------------------------------------
# Gateway health
# ---------------------------------------------------------------------------

def check_gateway() -> dict:
    """Check if the Hermes gateway process is alive."""
    if SKIP_GATEWAY:
        return {"skipped": True}
    # Try psutil first (cleanest cross-platform)
    try:
        import psutil
        hermes_procs = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (p.info.get("name") or "").lower()
                cmdline = " ".join(p.info.get("cmdline") or [])
                if "python" in name and ("hermes" in cmdline or "hermes_cli" in cmdline):
                    hermes_procs.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"healthy": len(hermes_procs) > 0, "processes": len(hermes_procs)}
    except ImportError:
        pass
    # Fallback: platform-specific
    if sys.platform == "win32":
        rc, out, _ = _run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"], timeout=10)
        if rc != 0:
            return {"error": f"tasklist failed: {rc}"}
        count = sum(1 for line in out.splitlines() if "python" in line.lower())
        return {"healthy": count > 0, "processes": count}
    else:
        rc, out, _ = _run(["pgrep", "-f", "hermes"], timeout=10)
        if rc == 0 and out:
            count = len(out.splitlines())
            return {"healthy": count > 0, "processes": count}
        # pgrep might not exist; try ps
        rc, out, _ = _run(["ps", "aux"], timeout=10)
        if rc == 0:
            count = sum(1 for line in out.splitlines() if "hermes" in line.lower() and "grep" not in line.lower())
            return {"healthy": count > 0, "processes": count}
        return {"error": "could not determine process status"}


# ---------------------------------------------------------------------------
# Cron failure scan
# ---------------------------------------------------------------------------

def _find_cron_db() -> Path | None:
    """Locate the Hermes cron state SQLite database."""
    home = os.environ.get("HERMES_HOME")
    if home:
        candidates = [
            Path(home) / "cron" / "cron.db",
            Path(home) / "cron" / "cron_state.db",
            Path(home) / "cron.db",
        ]
        for c in candidates:
            if c.exists():
                return c
    # Default locations
    default_home = Path(os.path.expanduser("~")) / ".hermes"
    candidates = [
        default_home / "cron" / "cron.db",
        default_home / "cron" / "cron_state.db",
        default_home / "cron.db",
        # Windows native
        Path(os.path.expanduser("~")) / "AppData" / "Local" / "hermes" / "cron" / "cron.db",
        Path(os.path.expanduser("~")) / "AppData" / "Local" / "hermes" / "cron" / "cron_state.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def check_cron_failures(hermes_exe: str | None) -> dict:
    """Scan enabled cron jobs for last_status='error'."""
    if SKIP_CRON:
        return {"skipped": True}
    db_path = _find_cron_db()
    if db_path:
        # Read directly from SQLite — no Hermes import needed
        for attempt in range(3):
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                # Find the right table name
                tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                cron_table = None
                for t in ("cron_jobs", "jobs", "cron"):
                    if t in tables:
                        cron_table = t
                        break
                if not cron_table:
                    conn.close()
                    # Fallback to CLI
                    return _check_cron_via_cli(hermes_exe)
                # Query for failed enabled jobs
                rows = cur.execute(
                    f"SELECT * FROM {cron_table} WHERE enabled = 1 AND last_status = 'error'"
                ).fetchall()
                conn.close()
                failed = []
                for row in rows:
                    name = dict(row).get("name") or dict(row).get("job_id", "?")
                    failed.append(str(name))
                return {"failed_count": len(failed), "failed_jobs": failed}
            except sqlite3.OperationalError:
                time.sleep(0.5 * (attempt + 1))
                continue
            except Exception as e:
                return {"error": f"sqlite read failed: {e}"}
        return {"error": "cron database locked after retries"}
    # No DB found — try the CLI
    return _check_cron_via_cli(hermes_exe)


def _check_cron_via_cli(hermes_exe: str | None) -> dict:
    """Fallback: use hermes cron list to detect failures."""
    if not hermes_exe:
        return {"error": "cron DB not found and hermes executable unavailable"}
    rc, out, err = _run([hermes_exe, "cron", "list"], timeout=30)
    if rc != 0:
        return {"error": f"hermes cron list failed: {err[:100]}"}
    # Parse text output for error lines
    failed = []
    for line in out.splitlines():
        if "error" in line.lower() and ("last_status" in line.lower() or "│ error" in line.lower()):
            # Try to extract job name
            parts = line.split("│")
            if len(parts) >= 2:
                name = parts[1].strip()
                if name and name != "Name":
                    failed.append(name)
    return {"failed_count": len(failed), "failed_jobs": failed, "method": "cli"}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main() -> int:
    now = _now()
    hermes_exe = _find_hermes_exe()

    updates = check_updates(hermes_exe)
    gateway = check_gateway()
    cron = check_cron_failures(hermes_exe)

    # Determine if anything needs attention
    update_available = updates.get("available", False)
    gateway_down = not gateway.get("healthy", True) and "error" not in gateway
    cron_failures = cron.get("failed_count", 0)
    has_errors = "error" in updates or "error" in gateway or "error" in cron

    needs_attention = update_available or gateway_down or cron_failures > 0 or has_errors

    if not needs_attention and not REPORT_ALL:
        # Silent — all healthy
        return 0

    # Build report
    lines = [f"⚠ Hermes Maintenance Report — {now}", ""]

    # Updates
    if updates.get("skipped"):
        if REPORT_ALL:
            lines.append("⏭ Update check: skipped")
            lines.append("")
    elif updates.get("error"):
        lines.append(f"❓ Update check: {updates['error']}")
        lines.append("")
    elif updates.get("available"):
        lines.append(f"📦 Update available: {updates['commits_behind']} commits behind upstream")
        if updates.get("latest"):
            lines.append(f"   Latest: {updates['latest']}")
        lines.append("   Run: hermes update")
        lines.append("")
    elif REPORT_ALL:
        lines.append("✅ Updates: up to date")
        lines.append("")

    # Gateway
    if gateway.get("skipped"):
        if REPORT_ALL:
            lines.append("⏭ Gateway: skipped")
            lines.append("")
    elif gateway.get("error"):
        lines.append(f"❓ Gateway: {gateway['error']}")
        lines.append("")
    elif gateway.get("healthy"):
        if REPORT_ALL or needs_attention:
            lines.append(f"✅ Gateway: {gateway.get('processes', 0)} process(es) running")
            lines.append("")
    else:
        lines.append("🔴 Gateway: no Hermes processes detected — gateway may be down")
        lines.append("   Manual restart may be needed")
        lines.append("")

    # Cron
    if cron.get("skipped"):
        if REPORT_ALL:
            lines.append("⏭ Cron: skipped")
            lines.append("")
    elif cron.get("error"):
        lines.append(f"❓ Cron: {cron['error']}")
        lines.append("")
    elif cron.get("failed_count", 0) > 0:
        failed = cron.get("failed_jobs", [])
        lines.append(f"⚠ Cron: {cron['failed_count']} failed job(s)")
        for name in failed[:10]:
            lines.append(f"   — {name}")
        if len(failed) > 10:
            lines.append(f"   ... and {len(failed) - 10} more")
        lines.append("")
    elif REPORT_ALL:
        lines.append("✅ Cron: no failures")
        lines.append("")

    report = "\n".join(lines).rstrip() + "\n"
    print(report, end="")
    return 0 if not needs_attention else 1


if __name__ == "__main__":
    sys.exit(main())