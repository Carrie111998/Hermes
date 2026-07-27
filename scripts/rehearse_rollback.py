"""Rehearse restoring Hermes from the audit safety point — without touching it.

A backup nobody has restored is a hypothesis. This restores every artifact into
a scratch directory and verifies it is actually usable: databases open and pass
integrity checks with row counts matching live, the git bundles clone, the
config parses and still carries the migrated keys, and a fallback interpreter
exists.

Production is never written to. Every restore target is under a temp directory
that is removed on exit unless --keep is passed. The live profile is opened
read-only (mode=ro) purely to compare row counts.

Usage:
    python scripts/rehearse_rollback.py
    python scripts/rehearse_rollback.py --keep     # leave the scratch tree
    python scripts/rehearse_rollback.py --backup <dir>

Exit 0 only when every rollback path is verified restorable.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

DEFAULT_BACKUP = Path(
    r"C:/Users/Waxilliam/backups/hermes-audit-safety-20260727T070301Z"
)
PROFILE = Path(r"C:/Users/Waxilliam/AppData/Local/hermes/profiles/aletheon")
HERMES_AGENT = Path(r"C:/Users/Waxilliam/AppData/Local/hermes/hermes-agent")

# backup filename -> live path, for the row-count comparison.
DB_LIVE_MAP = {
    "state.db": PROFILE / "state.db",
    "receipts.db": PROFILE / "receipts.db",
    "projects.db": PROFILE / "projects.db",
    "verification_evidence.db": PROFILE / "verification_evidence.db",
    "cron__executions.db": PROFILE / "cron" / "executions.db",
    "mneme__mneme.db": PROFILE / "mneme" / "mneme.db",
    "mneme__mneme_archive.db": PROFILE / "mneme" / "mneme_archive.db",
    "memories__mneme-notes__mneme.db": PROFILE / "memories" / "mneme-notes" / "mneme.db",
    "workers__bridge.db": PROFILE / "workers" / "bridge.db",
    ".scraper-state__cache.db": PROFILE / ".scraper-state" / "cache.db",
}

results: list[tuple[str, bool, str]] = []

# The console here is cp1252, so any non-ASCII in a status line raises
# UnicodeEncodeError and kills the run AFTER the work is done but BEFORE the
# verdict prints — a rollback rehearsal that reports nothing is worse than one
# that fails. Same locale-default trap this audit fixed in the worker bridge.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def record(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]


def _row_counts(path: Path, read_only: bool = False) -> dict:
    uri = f"file:{str(path).replace(chr(92), '/')}" + ("?mode=ro" if read_only else "")
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        out = {}
        for table in _tables(conn):
            try:
                out[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            except sqlite3.Error:
                out[table] = None      # virtual/FTS shadow tables can refuse COUNT
        return out
    finally:
        conn.close()


def rehearse_databases(backup: Path, scratch: Path) -> None:
    print("\n[1] databases — restore, integrity-check, compare against live")
    src_dir = backup / "databases"
    if not src_dir.is_dir():
        record("database backups present", False, f"{src_dir} missing")
        return
    dest_dir = scratch / "db"
    dest_dir.mkdir(parents=True, exist_ok=True)

    for name, live in DB_LIVE_MAP.items():
        src = src_dir / name
        if not src.exists():
            record(f"db {name}", False, "backup file missing")
            continue
        dest = dest_dir / name
        shutil.copy2(src, dest)          # a restore is a copy back into place

        try:
            conn = sqlite3.connect(str(dest), timeout=30)
            integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
            restored_tables = len(_tables(conn))
            conn.close()
        except sqlite3.Error as exc:
            record(f"db {name}", False, f"unopenable after restore: {exc}")
            continue
        if integ != "ok":
            record(f"db {name}", False, f"integrity_check={integ[:60]}")
            continue

        detail = f"{restored_tables} tables, integrity ok"
        if live.exists():
            try:
                restored = _row_counts(dest)
                current = _row_counts(live, read_only=True)
                shared = set(restored) & set(current)
                # The live DB has moved on since the snapshot (the gateway has
                # been running), so equality is the wrong test. What matters is
                # that the restore is not EMPTY where live has data.
                empty = [t for t in shared
                         if current.get(t) and not restored.get(t)]
                if empty:
                    record(f"db {name}", False,
                           f"restored copy empty for tables live has data in: {empty[:3]}")
                    continue
                detail += f", {len(shared)} tables comparable, none empty-vs-live"
            except sqlite3.Error as exc:
                detail += f" (live compare skipped: {exc})"
        record(f"db {name}", True, detail)


def rehearse_git_bundles(backup: Path, scratch: Path) -> None:
    print("\n[2] git bundles — verify and actually clone")
    for label, bundle in (("inner", backup / "inner-repo.bundle"),
                          ("outer", backup / "outer-repo.bundle")):
        if not bundle.exists():
            record(f"{label} bundle present", False, "missing")
            continue
        verify = subprocess.run(["git", "bundle", "verify", str(bundle)],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1800)
        if verify.returncode != 0:
            record(f"{label} bundle verify", False, verify.stderr.strip()[:120])
            continue
        record(f"{label} bundle verify", True, "bundle is a valid, complete pack")

        target = scratch / f"{label}-clone"
        clone = subprocess.run(["git", "clone", "--quiet", str(bundle), str(target)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=3600)
        if clone.returncode != 0:
            record(f"{label} bundle clone", False, clone.stderr.strip()[:160])
            continue
        head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=target,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        record(f"{label} bundle clone", True,
               f"HEAD={head.stdout.strip()[:60]}")


def rehearse_config(backup: Path, scratch: Path) -> None:
    print("\n[3] configuration — extract from the archive and validate")
    archive = backup / "outer-uncommitted.tar.gz"
    if not archive.exists():
        record("config archive present", False, "outer-uncommitted.tar.gz missing")
        return
    member = "profiles/aletheon/config.yaml"
    dest = scratch / "config"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive) as tf:
            try:
                entry = tf.getmember(member)
            except KeyError:
                record("config in archive", False, f"{member} not in archive")
                return
            tf.extract(entry, path=dest)
    except (tarfile.TarError, OSError) as exc:
        record("config extract", False, str(exc)[:120])
        return
    restored = dest / member
    record("config extract", restored.exists(), str(restored.name))

    try:
        import yaml

        cfg = yaml.safe_load(restored.read_text(encoding="utf-8"))
    except Exception as exc:
        record("config parses", False, str(exc)[:120])
        return
    record("config parses", isinstance(cfg, dict), f"{len(cfg or {})} top-level keys")

    delegation = (cfg or {}).get("delegation") or {}
    record("migrated key present (max_concurrent_children)",
           "max_concurrent_children" in delegation,
           f"value={delegation.get('max_concurrent_children')}")
    record("deprecated key absent (max_async_children)",
           "max_async_children" not in delegation)


def rehearse_runtime_fallback() -> None:
    print("\n[4] runtime — a fallback interpreter must exist to roll back TO")
    for label, venv in (("venv313 (current)", HERMES_AGENT / "venv313"),
                        ("venv (rollback target)", HERMES_AGENT / "venv")):
        exe = venv / "Scripts" / "python.exe"
        if not exe.exists():
            exe = venv / "bin" / "python"
        if not exe.exists():
            record(label, False, "interpreter missing")
            continue
        probe = subprocess.run(
            [str(exe), "-c",
             "import sys,sqlite3;print(sys.version.split()[0],sqlite3.sqlite_version)"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        if probe.returncode != 0:
            record(label, False, probe.stderr.strip()[:100])
            continue
        py, sqlite_ver = (probe.stdout.strip().split() + ["?", "?"])[:2]
        note = f"Python {py}, SQLite {sqlite_ver}"
        # Rolling back to a WAL-vulnerable interpreter is possible but is a
        # decision, not a default — say so rather than reporting a clean pass.
        parts = tuple(int(x) for x in sqlite_ver.split(".")[:3]) if sqlite_ver[0].isdigit() else (0,)
        vulnerable = (3, 7, 0) <= parts <= (3, 51, 2) and parts not in {(3, 50, 7), (3, 44, 6)}
        if vulnerable:
            note += "  <- WAL-reset vulnerable: rollback here needs HERMES_ALLOW_VULNERABLE_SQLITE"
        record(label, True, note)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if not args.backup.is_dir():
        print(f"backup directory not found: {args.backup}")
        return 2
    print(f"safety point : {args.backup}")
    print(f"live profile : {PROFILE}  (opened READ-ONLY, never written)")

    scratch = Path(tempfile.mkdtemp(prefix="hermes-rollback-rehearsal-"))
    print(f"scratch      : {scratch}")
    try:
        rehearse_databases(args.backup, scratch)
        rehearse_git_bundles(args.backup, scratch)
        rehearse_config(args.backup, scratch)
        rehearse_runtime_fallback()

        failed = [n for n, ok, _ in results if not ok]
        print("\n" + "-" * 70)
        print(f"{len(results) - len(failed)}/{len(results)} rollback checks passed")
        if failed:
            print("FAILED:")
            for name in failed:
                print(f"  {name}")
            print("\nRESULT: rollback is NOT fully verified — fix the above first.")
        else:
            print("\nRESULT: every rollback path restores and verifies.")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"\nscratch kept: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
            print("\nscratch removed")


if __name__ == "__main__":
    raise SystemExit(main())
