#!/usr/bin/env python3
"""Collect bounded raw SkyVision cPanel backup facts without interpreting them."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


SSH_HELPER = Path("/opt/adventico-ai-platform/hermes-home/bin/ssh-alwyzon-phoenix")
MAX_CAPTURE = 256 * 1024
MAX_LOG_BYTES = 48 * 1024

PROBE_COMMAND = "set -eu; printf 'remote_probe=ok\\n'"
COLLECT_COMMAND = r"""set -eu
export LC_ALL=C
remote_date="$(date +%F)"
printf 'remote_epoch=%s\n' "$(date +%s)"
printf 'remote_date=%s\n' "$remote_date"
printf 'remote_weekday=%s\n' "$(date +%u)"
printf 'remote_hhmm=%s\n' "$(date +%H%M)"
df -Pk / | awk 'NR==2 {gsub("%", "", $5); print "root_available_kib=" $4; print "root_used_pct=" $5}'
for service in httpd mysql exim; do
  printf 'service_%s=' "$service"
  systemctl is-active "$service" 2>/dev/null || true
done
latest_archive="$(sudo -n find /backup -maxdepth 5 -type f -name 'skyvisio.tar.gz' -printf '%T@ %s\n' 2>/dev/null | sort -nr | head -n 1 || true)"
if [ -n "$latest_archive" ]; then
  archive_mtime="$(printf '%s\n' "$latest_archive" | awk '{printf "%.0f", $1}')"
  printf 'archive_present=1\n'
  printf 'archive_mtime_epoch=%s\n' "$archive_mtime"
  printf 'archive_date=%s\n' "$(date -d "@$archive_mtime" +%F)"
  printf 'archive_bytes=%s\n' "$(printf '%s\n' "$latest_archive" | awk '{print $2}')"
else
  printf 'archive_present=0\narchive_mtime_epoch=0\narchive_date=none\narchive_bytes=0\n'
fi
printf 'process_rows_b64='
ps -eo pid=,etimes=,comm=,args= | awk '$3 == "backup" || $3 == "pkgacct" || $3 == "cpbackup_transporter" || index($0, "/usr/local/cpanel/bin/backup") || index($0, "/usr/local/cpanel/scripts/pkgacct") || index($0, "/usr/local/cpanel/bin/cpbackup_transporter")' | head -n 80 | base64 -w0
printf '\n'
latest_backup_log="$(sudo -n find /usr/local/cpanel/logs/cpbackup -maxdepth 1 -type f -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 1 | cut -d" " -f2- || true)"
printf 'account_log_path_present=%s\n' "$([ -n "$latest_backup_log" ] && printf 1 || printf 0)"
printf 'account_log_rows_b64='
if [ -n "$latest_backup_log" ] && sudo -n test -r "$latest_backup_log"; then
  sudo -n tail -n 400 "$latest_backup_log" | grep -F -i 'skyvisio' | tail -n 80 | base64 -w0 || true
fi
printf '\n'
printf 'transporter_log_rows_b64='
transporter_dir=/usr/local/cpanel/logs/cpbackup_transporter
if sudo -n test -d "$transporter_dir"; then
  next_date="$(date -d "$remote_date +1 day" +%F)"
  sudo -n find "$transporter_dir" -maxdepth 1 -type f -newermt "$remote_date 00:00:00" ! -newermt "$next_date 00:00:00" -exec cat {} + 2>/dev/null | grep -F -i 'skyvisio.tar.gz' | tail -n 80 | base64 -w0 || true
fi
printf '\n'
printf 'exclude_exact_rows_b64='
sudo -n grep -F -x 'mail_offline_archive' /home/skyvisio/cpbackup-exclude.conf 2>/dev/null | head -n 20 | base64 -w0 || true
printf '\n'
printf 'backup_config_rows_b64='
sudo -n awk -F: '$1 == "BACKUPENABLE" || $1 == "BACKUP_DAILY_ENABLE" || $1 == "BACKUPDAYS" {print $0}' /var/cpanel/backups/config 2>/dev/null | head -n 20 | base64 -w0 || true
printf '\n'
"""


def _run_remote(command: str) -> str:
    completed = subprocess.run(
        [str(SSH_HELPER), command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=150,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_CAPTURE
        or len(completed.stderr) > MAX_CAPTURE
    ):
        raise RuntimeError("backup_raw_remote_collection_failed")
    return completed.stdout.decode("utf-8", errors="strict")


def _decode_rows(value: str) -> list[str]:
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError("backup_raw_evidence_invalid") from exc
    if len(raw) > MAX_LOG_BYTES:
        raise RuntimeError("backup_raw_evidence_oversized")
    return raw.decode("utf-8", errors="replace").splitlines()


def collect() -> dict[str, object]:
    facts: dict[str, str] = {}
    evidence: dict[str, list[str]] = {}
    for line in _run_remote(COLLECT_COMMAND).splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in facts or key in evidence:
            raise RuntimeError("backup_raw_projection_invalid")
        if key.endswith("_b64"):
            evidence[key.removesuffix("_b64")] = _decode_rows(value)
        else:
            facts[key] = value
    return {
        "schema": "skyvision-backup-raw-facts.v1",
        "ok": True,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
        "raw_evidence": evidence,
        "semantic_judgment_performed": False,
        "delivery_attempted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("probe", "collect"))
    args = parser.parse_args(argv)
    if args.action == "probe":
        value = _run_remote(PROBE_COMMAND).strip()
        output = {
            "schema": "skyvision-backup-raw-probe.v1",
            "ok": value == "remote_probe=ok",
        }
    else:
        output = collect()
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
