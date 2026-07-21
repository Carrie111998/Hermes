"""hermes events doctor — diagnose notification layer health."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from events.paths import (
    audit_log_path, events_db_path, quiet_hours_path,
    telegram_topics_path, telegram_verbosity_path,
)

# telegram-mirror retired 2026-04-28 (scribe_daily topic cutover made it
# duplicate every mailbox_message); checking it produced a permanent false
# FAIL on every doctor run until 2026-07-12.
REQUIRED_SUBSCRIBERS = [
    "audit-logger", "telegram-notifier", "whatsapp-escalator",
    "digest-composer", "memory-writer",
    "mailbox-translator",
]


def _check(name: str, ok: bool, detail: str = "") -> bool:
    marker = "OK" if ok else "FAIL"
    print(f"[{marker}] {name}{' -- ' + detail if detail else ''}")
    return ok


# The gateway's editable install imports the WORKING TREE of this checkout,
# which is deliberately kept on a detached HEAD so worktree agents can land
# commits onto the `main` ref via `git branch -f`.  A commit landed on main
# therefore does NOT run until the detached checkout is fast-forwarded and
# the gateway restarted — on 2026-07-20/21 three restart cycles ran stale
# code while every session believed the fix was live because "main tip
# moved".  This check makes that drift loud.
_AGENT_SRC_DEFAULT = Path.home() / ".hermes" / "agent-src"


def _agent_src_root() -> Path:
    return Path(os.getenv("HERMES_AGENT_SRC") or _AGENT_SRC_DEFAULT)


def _git(repo: Path, *args: str) -> Tuple[int, str]:
    """Run a read-only git command; returns (returncode, stdout)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def check_code_drift(repo_path: Optional[Path] = None) -> int:
    """Compare the deployed checkout's HEAD against the landed `main` ref.

    Read-only — never mutates the repo.  Returns the number of issues found
    (0 or 1).  A missing repo/ref degrades to a skip note, not a failure,
    so the doctor stays usable on boxes without the shared checkout.
    """
    repo = repo_path or _agent_src_root()
    # .git is a directory in a normal checkout and a file in a worktree.
    if not (repo / ".git").exists():
        print(f"[--] code drift -- skipped ({repo} is not a git checkout)")
        return 0

    rc_head, head = _git(repo, "rev-parse", "--verify", "HEAD")
    rc_main, main = _git(repo, "rev-parse", "--verify", "refs/heads/main")
    if rc_head != 0 or rc_main != 0:
        which = "HEAD" if rc_head != 0 else "main ref"
        print(f"[--] code drift -- skipped (cannot resolve {which} in {repo})")
        return 0
    head, main = head.strip(), main.strip()

    dirty = bool(_git(repo, "status", "--porcelain")[1].strip())
    if dirty:
        print(f"[NOTE] code drift: working tree at {repo} is DIRTY "
              "(uncommitted changes -- inspect manually, never auto-fixed)")

    if head == main:
        _check("code drift (HEAD vs main)", True, f"in sync @ {head[:9]}")
        return 0

    head_behind = _git(repo, "merge-base", "--is-ancestor", "HEAD", "refs/heads/main")[0] == 0
    head_ahead = _git(repo, "merge-base", "--is-ancestor", "refs/heads/main", "HEAD")[0] == 0

    if head_behind:
        count = _git(repo, "rev-list", "--count", "HEAD..refs/heads/main")[1].strip()
        subjects = _git(repo, "log", "--format=  missed: %h %s", "-5",
                        "HEAD..refs/heads/main")[1].rstrip()
        print(f"[WARN] code drift: working tree LAGS main by {count} commit(s) "
              "-- landed fixes are NOT running")
        if subjects:
            print(subjects)
        print("  remediation: FF the detached checkout: "
              f"git -C {repo} merge --ff-only main "
              "(safe -- checkout is detached; check for a clean tree first), "
              "then restart the gateway")
        return 1

    if head_ahead:
        count = _git(repo, "rev-list", "--count", "refs/heads/main..HEAD")[1].strip()
        print(f"[WARN] code drift: HEAD is AHEAD of main by {count} commit(s) "
              "(working tree carries unlanded state -- land it on main or "
              "move the checkout back to the main tip)")
        return 1

    print("[WARN] code drift: HEAD has DIVERGED from main "
          f"(HEAD {head[:9]} vs main {main[:9]}, neither is an ancestor "
          "of the other -- reconcile manually)")
    return 1


def run_doctor(check_telegram_api: bool = True) -> int:
    issues = 0

    issues += check_code_drift()

    db = events_db_path()
    if not _check("events db exists", db.exists(), str(db)):
        issues += 1

    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            conn.execute("SELECT 1 FROM events LIMIT 1")
            _check("events db readable", True)

            cursors = {row[0] for row in conn.execute(
                "SELECT subscriber_id FROM subscriber_cursors")}
            for sub in REQUIRED_SUBSCRIBERS:
                if not _check(f"subscriber cursor: {sub}",
                              sub in cursors, "present" if sub in cursors else "missing"):
                    issues += 1

            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM events WHERE timestamp > ?", (since,)
            ).fetchone()[0]
            _check(f"events emitted in last 24h", cnt > 0, f"{cnt} events")
            if cnt == 0:
                issues += 1

            conn.close()
        except sqlite3.Error as e:
            _check("events db readable", False, str(e))
            issues += 1

    for label, p in [
        ("topics.json", telegram_topics_path()),
        ("verbosity.json", telegram_verbosity_path()),
        ("quiet_hours.json", quiet_hours_path()),
        ("audit.jsonl", audit_log_path()),
    ]:
        ok = p.exists()
        detail = str(p) if not ok else ""
        if not _check(f"{label}", ok, detail):
            issues += 1

    if check_telegram_api:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            _check("TELEGRAM_BOT_TOKEN env", False, "unset")
            issues += 1
        else:
            try:
                import urllib.request
                with urllib.request.urlopen(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=5
                ) as r:
                    data = json.loads(r.read().decode())
                    _check("telegram getMe", data.get("ok") is True,
                           data.get("result", {}).get("username", ""))
                    if not data.get("ok"):
                        issues += 1
            except Exception as e:
                _check("telegram getMe", False, str(e))
                issues += 1

    print()
    if issues:
        print(f"events doctor: {issues} issue(s) found")
        return 1
    print("events doctor: all checks passed")
    return 0


def print_dead_letters(
    limit: int = 20,
    since_hours: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Print the most recent dead-letter rows. Returns exit code."""
    db = db_path or events_db_path()
    if not db.exists():
        print(f"events db not found: {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conditions: list = []
        params: list = []
        if since_hours is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conditions.append("dl.failed_at >= ?")
            params.append(cutoff)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(int(limit))

        try:
            rows = conn.execute(
                f"""SELECT dl.event_id, dl.subscriber_id, dl.error, dl.attempts,
                           dl.first_failed_at, dl.failed_at,
                           e.event_type, e.source
                    FROM dead_letters dl
                    LEFT JOIN events e ON e.event_id = dl.event_id
                    {where}
                    ORDER BY dl.failed_at DESC, dl.event_id DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        except sqlite3.OperationalError as e:
            # Old DB without the table — schema gets created on next EventBus
            # instantiation. Surface the diagnosis without crashing.
            print(f"dead_letters table missing ({e}); start the gateway once to migrate schema.")
            return 0

        if not rows:
            where_desc = f" in last {since_hours}h" if since_hours else ""
            print(f"No dead-letter events{where_desc}.")
            return 0

        print(f"Recent dead-letters (showing {len(rows)}):")
        print()
        print(f"  {'failed_at':<20} {'subscriber':<20} {'event_type':<22} {'att':>3}  error")
        print(f"  {'-'*20} {'-'*20} {'-'*22} {'-'*3}  {'-'*40}")
        for r in rows:
            err = (r["error"] or "").replace("\n", " ")
            if len(err) > 60:
                err = err[:57] + "..."
            print(
                f"  {str(r['failed_at'])[:19]:<20} "
                f"{(r['subscriber_id'] or '?')[:20]:<20} "
                f"{(r['event_type'] or '<purged>')[:22]:<22} "
                f"{int(r['attempts']):>3}  {err}"
            )
        return 0
    finally:
        conn.close()


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telegram-api", action="store_true",
                    help="Skip live getMe check")
    ap.add_argument("--dead-letters", action="store_true",
                    help="List recent dead-lettered events instead of running checks")
    ap.add_argument("--limit", type=int, default=20,
                    help="With --dead-letters: max rows to show (default 20)")
    ap.add_argument("--since-hours", type=int, default=None,
                    help="With --dead-letters: only show failures newer than N hours")
    ns = ap.parse_args()
    if ns.dead_letters:
        sys.exit(print_dead_letters(limit=ns.limit, since_hours=ns.since_hours))
    sys.exit(run_doctor(check_telegram_api=not ns.no_telegram_api))


if __name__ == "__main__":
    _cli()
