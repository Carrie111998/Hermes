"""hermes events doctor — diagnose notification layer health."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from events.paths import (
    audit_log_path, events_db_path, quiet_hours_path,
    telegram_topics_path, telegram_verbosity_path,
)
from events.producers.code_drift_monitor import (
    DEFAULT_TRUNK_REF, MISCONFIG_HEAD_UNRESOLVED, _agent_src_root,
    sample_code_drift, watched_repos,
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


# Two checkouts on this box ARE production and can silently run stale code:
#
#   ~/.hermes/agent-src (trunk `main`)   — the gateway's editable install
#     imports its WORKING TREE, which is kept on a detached HEAD so worktree
#     agents can land commits onto `main` via `git branch -f`.  A commit
#     landed on main does NOT run until the checkout is fast-forwarded and
#     the gateway restarted — on 2026-07-20/21 three restart cycles ran
#     stale code because every session believed "main tip moved" meant live.
#
#   ~/.hermes (trunk `master`)           — cron script slots and Windows
#     Scheduled Tasks resolve ABSOLUTE paths under ~/.hermes/scripts/,
#     ~/.hermes/profiles/*/scripts/ and ~/.hermes/ops/, so only the working
#     tree runs.  On 2026-07-28, 62 commits sat undeployed for three days
#     behind a clean `git status`.
#
# Trunk ref names differ per repo and ~/.hermes has NO `main` branch, so the
# trunk is configured alongside the path — see the loud-vs-skip note in
# check_code_drift().
#
# Both the watched-repo registry AND the git probe itself come from the
# CODE_DRIFT producer: this file renders, it does not probe.  The duplicate
# probe it used to carry is what let the unresolvable-trunk blind spot exist
# in two places at once.
def check_code_drift(
    repo_path: Optional[Path] = None,
    trunk_ref: str = DEFAULT_TRUNK_REF,
    *,
    label: str = "",
) -> int:
    """Compare a deployed checkout's HEAD against its configured trunk ref.

    Read-only -- never mutates the repo.  Returns the number of issues found
    (0 or 1).

    RENDERS the CODE_DRIFT producer's ``sample_code_drift()``; it does not
    probe git itself.  Until 2026-07-28 this function carried its own
    near-duplicate copy of that probe (`_git`, `_agent_src_root`, a
    hardcoded `refs/heads/main`), and BOTH copies independently degraded an
    unresolvable trunk ref to a quiet skip.  That is the failure mode worth
    designing against: a blind spot fixed in one surface and left open in
    the other is worse than one fixed nowhere, because the surface that got
    fixed is then cited as proof the box is clean.  One probe, two
    renderings -- so the hole cannot be reopened by halves.

    Deliberately NOT gated by WatchedRepo.executed_dirs, unlike the
    event-bus producer -- note ``executed_dirs=()`` below.  The producer
    narrows ALERTS so a phone does not buzz for inert docs churn; this is a
    diagnostic the operator ran on purpose, so it reports every divergence
    and lets them judge.  Sharing the probe does not mean sharing the alert
    policy: the gate is a parameter of the probe, so this surface simply
    declines it.

    Skip vs FAIL, the 2026-07-28 lesson: an ABSENT repo (no .git) degrades
    to a skip note so the doctor stays usable on boxes without the shared
    checkout -- that is the ONLY case the sampler returns None for.  A repo
    that is PRESENT but cannot be evaluated comes back state="misconfigured"
    and is a FAILURE here, because reporting it quietly is how a watcher
    ends up certifying an unwatched repo as clean.  ~/.hermes has no `main`
    branch, so under the old hardcoded ref that was the guaranteed outcome.
    """
    repo = Path(repo_path) if repo_path else _agent_src_root()
    trunk_name = trunk_ref.rsplit("/", 1)[-1]
    tag = f"code drift [{label}]" if label else "code drift"

    sample = sample_code_drift(
        repo, trunk_ref,
        repo_name=label or "agent-src",
        executed_dirs=(),   # diagnostic: report every divergence, gate nothing
    )
    # None means the repo is genuinely absent (no .git) -- the one case that
    # is a skip rather than a finding.
    if sample is None:
        print(f"[--] {tag} -- skipped ({repo} is not a git checkout)")
        return 0

    if sample.state == "misconfigured":
        print(f"[FAIL] {tag}: {sample.detail} in {repo} -- drift CANNOT be "
              "evaluated, so this repo is effectively UNMONITORED (not clean)")
        if sample.detail != MISCONFIG_HEAD_UNRESOLVED:
            print(f"  remediation: check the real trunk name (git -C {repo} "
                  "branch --list) and fix the watched-repo entry's trunk_ref")
        return 1

    if sample.dirty:
        print(f"[NOTE] {tag}: working tree at {repo} is DIRTY "
              "(uncommitted changes -- inspect manually, never auto-fixed)")

    if sample.state == "in_sync":
        _check(f"{tag} (HEAD vs {trunk_name})", True,
               f"in sync @ {sample.head[:9]}")
        return 0

    if sample.state == "behind":
        print(f"[WARN] {tag}: working tree LAGS {trunk_name} by "
              f"{sample.behind_count} commit(s) -- landed fixes are NOT running")
        for subject in sample.missed_subjects:
            print(f"  missed: {subject}")
        print("  remediation: FF the checkout: "
              f"git -C {repo} merge --ff-only {trunk_name} "
              "(check for a clean tree first), then restart the gateway")
        return 1

    if sample.state == "ahead":
        print(f"[WARN] {tag}: HEAD is AHEAD of {trunk_name} by "
              f"{sample.ahead_count} commit(s) (working tree carries unlanded "
              f"state -- land it on {trunk_name} or move the checkout back to "
              f"the {trunk_name} tip)")
        return 1

    print(f"[WARN] {tag}: HEAD has DIVERGED from {trunk_name} "
          f"(HEAD {sample.head[:9]} vs {trunk_name} {sample.trunk[:9]}, "
          "neither is an ancestor of the other -- reconcile manually)")
    return 1


def run_doctor(check_telegram_api: bool = True) -> int:
    issues = 0

    # Every repo whose WORKING TREE is deployed code, each with its own
    # trunk ref (agent-src `main`, ~/.hermes `master`). Shared source of
    # truth with the CODE_DRIFT producer so the two layers cannot disagree
    # about what is watched.
    for watched in watched_repos():
        issues += check_code_drift(watched.path, watched.trunk_ref,
                                   label=watched.name)

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
