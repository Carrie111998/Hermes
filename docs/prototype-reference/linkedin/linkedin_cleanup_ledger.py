#!/usr/bin/env python3
"""
linkedin_cleanup_ledger.py — deterministic bookkeeping for the LinkedIn
connection cleanup workflow (LINKEDIN_CLEANUP_WORKFLOW.md).

The agent NEVER edits registry.json by hand; all state changes go through
this script so the ledger stays consistent and resumable.

Commands:
  import <connections.csv>      Merge LinkedIn's official Connections export
  next-batch [N]                Next N unchecked profiles (oldest connection first)
  mark <url> <status> [--last-activity YYYY-MM-DD] [--note TEXT]
                                status: active|inactive|removed|protected|error
  remove-batch [N]              Next N inactive (not yet removed) profiles
  stats                         Counts by status + today's quota usage
"""

import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.expanduser("~/.openclaw/workspace/data/linkedin-cleanup")
REGISTRY = os.path.join(BASE_DIR, "registry.json")
PROTECTED_FILE = os.path.join(BASE_DIR, "protected.txt")

PROTECT_RECENT_MONTHS = 6      # connections newer than this are never culled
ACTIVITY_WINDOW_DAYS = 365     # active = any post/comment/repost within this window
WEEKLY_REMOVAL_TARGET = 100    # mirrors the 100 connection-requests/week budget

# Removed connections are logged to this tab of the customer list spreadsheet
SHEET_ID = "{{SHEET_ID}}"
REMOVED_TAB = "LinkedIn Removed Connections"
GOG_BIN = "/opt/homebrew/bin/gog"
GOG_ACCOUNT = "{{gog_account}}"

VALID_STATUSES = {"unchecked", "active", "inactive", "removed", "protected", "error"}


def norm_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    url = re.sub(r"^https?://(www\.)?", "https://www.", url)
    return url.lower()


def load_registry() -> dict:
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_registry(reg: dict):
    os.makedirs(BASE_DIR, exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, REGISTRY)


def load_protected_terms() -> list:
    try:
        with open(PROTECTED_FILE) as f:
            return [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []


def parse_connected_on(raw: str):
    raw = (raw or "").strip()
    for fmt in ("%d %b %Y", "%d-%b-%y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def is_protected(entry: dict, terms: list) -> bool:
    hay = " ".join([entry.get("name", ""), entry.get("company", ""),
                    entry.get("url", "")]).lower()
    if any(t in hay for t in terms):
        return True
    dt = parse_connected_on(entry.get("connected_on", ""))
    if dt and dt > datetime.now() - timedelta(days=PROTECT_RECENT_MONTHS * 30):
        return True
    return False


def cmd_import(csv_path: str):
    reg = load_registry()
    terms = load_protected_terms()
    added = updated = protected = skipped = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        # LinkedIn export sometimes prefixes note lines before the header
        pos = f.tell()
        while True:
            line = f.readline()
            if not line:
                break
            if line.lower().startswith("first name"):
                f.seek(pos)
                break
            pos = f.tell()
        reader = csv.DictReader(f)
        for row in reader:
            row = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            url = norm_url(row.get("url", ""))
            if not url or "/in/" not in url:
                skipped += 1
                continue
            name = f"{row.get('first name','')} {row.get('last name','')}".strip()
            entry = reg.get(url) or {"status": "unchecked"}
            entry.update({
                "url": url,
                "name": name or entry.get("name", ""),
                "company": row.get("company", "") or entry.get("company", ""),
                "position": row.get("position", "") or entry.get("position", ""),
                "connected_on": row.get("connected on", "") or entry.get("connected_on", ""),
            })
            if url not in reg:
                added += 1
            else:
                updated += 1
            if entry["status"] == "unchecked" and is_protected(entry, terms):
                entry["status"] = "protected"
                entry["note"] = "auto-protected (protected.txt match or connected < %d months)" % PROTECT_RECENT_MONTHS
                protected += 1
            reg[url] = entry

    save_registry(reg)
    print(json.dumps({"added": added, "updated": updated,
                      "auto_protected": protected, "skipped_no_url": skipped,
                      "total": len(reg)}))


def sort_key(e):
    dt = parse_connected_on(e.get("connected_on", ""))
    return dt or datetime.max


def cmd_next_batch(n: int):
    reg = load_registry()
    batch = sorted((e for e in reg.values() if e["status"] == "unchecked"),
                   key=sort_key)[:n]
    print(json.dumps(batch, indent=1, ensure_ascii=False))


def cmd_remove_batch(n: int):
    reg = load_registry()
    batch = sorted((e for e in reg.values() if e["status"] == "inactive"),
                   key=sort_key)[:n]
    print(json.dumps(batch, indent=1, ensure_ascii=False))


def sheet_log_removal(entry: dict) -> bool:
    """Append one removed connection to the 'LinkedIn Removed Connections' tab."""
    row = [[entry.get("name", ""), entry.get("company", ""),
            entry.get("position", ""), entry.get("url", ""),
            entry.get("connected_on", ""), entry.get("last_activity", ""),
            entry.get("removed_on", ""), entry.get("note", "")]]
    try:
        r = subprocess.run(
            [GOG_BIN, "sheets", "append", SHEET_ID, f"'{REMOVED_TAB}'!A1",
             "--values-json", json.dumps(row, ensure_ascii=False),
             "--account", GOG_ACCOUNT, "--no-input"],
            capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0
    except Exception:
        return False


def cmd_mark(url: str, status: str, last_activity: str, note: str):
    if status not in VALID_STATUSES:
        sys.exit(f"invalid status '{status}'; use one of {sorted(VALID_STATUSES)}")
    reg = load_registry()
    url = norm_url(url)
    if url not in reg:
        sys.exit(f"url not in registry: {url}")
    entry = reg[url]
    entry["status"] = status
    entry["checked_on"] = time.strftime("%Y-%m-%d")
    if last_activity:
        entry["last_activity"] = last_activity
    if note:
        entry["note"] = note
    if status == "removed":
        entry["removed_on"] = time.strftime("%Y-%m-%d")
        entry["sheet_logged"] = sheet_log_removal(entry)
    save_registry(reg)
    print(json.dumps(entry, ensure_ascii=False))


def cmd_sync_sheet():
    """Retry sheet logging for removed entries that failed to log earlier."""
    reg = load_registry()
    synced = failed = 0
    for entry in reg.values():
        if entry.get("status") == "removed" and not entry.get("sheet_logged"):
            if sheet_log_removal(entry):
                entry["sheet_logged"] = True
                synced += 1
            else:
                failed += 1
    save_registry(reg)
    print(json.dumps({"synced": synced, "still_failed": failed}))


def cmd_stats():
    reg = load_registry()
    today = time.strftime("%Y-%m-%d")
    this_week = datetime.now().strftime("%G-W%V")
    counts = {}
    checked_today = removed_today = removed_this_week = unlogged = 0
    for e in reg.values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
        if e.get("checked_on") == today:
            checked_today += 1
        ro = e.get("removed_on")
        if ro == today:
            removed_today += 1
        if ro:
            try:
                if datetime.strptime(ro, "%Y-%m-%d").strftime("%G-W%V") == this_week:
                    removed_this_week += 1
            except ValueError:
                pass
        if e.get("status") == "removed" and not e.get("sheet_logged"):
            unlogged += 1
    print(json.dumps({"total": len(reg), "by_status": counts,
                      "checked_today": checked_today,
                      "removed_today": removed_today,
                      "removed_this_week": removed_this_week,
                      "weekly_removal_target": WEEKLY_REMOVAL_TARGET,
                      "weekly_quota_left": max(0, WEEKLY_REMOVAL_TARGET - removed_this_week),
                      "sheet_log_pending": unlogged}, indent=1))


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    cmd = args[0]
    if cmd == "import" and len(args) >= 2:
        cmd_import(args[1])
    elif cmd == "next-batch":
        cmd_next_batch(int(args[1]) if len(args) >= 2 else 60)
    elif cmd == "remove-batch":
        cmd_remove_batch(int(args[1]) if len(args) >= 2 else 25)
    elif cmd == "mark" and len(args) >= 3:
        last_activity = note = ""
        rest = args[3:]
        while rest:
            if rest[0] == "--last-activity" and len(rest) > 1:
                last_activity = rest[1]; rest = rest[2:]
            elif rest[0] == "--note" and len(rest) > 1:
                note = rest[1]; rest = rest[2:]
            else:
                rest = rest[1:]
        cmd_mark(args[1], args[2], last_activity, note)
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "sync-sheet":
        cmd_sync_sheet()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
