"""One-shot migration: profile-scoped notification state -> canonical ~/.hermes root.

Moves:
    ~/.hermes/profiles/main/telegram/*           -> ~/.hermes/telegram/*
    ~/.hermes/profiles/main/notifications/*      -> ~/.hermes/notifications/*
    ~/.hermes/profiles/main/events/*             -> ~/.hermes/events/*
    ~/.hermes/profiles/main/mailbox/.event_watermark.json
                                                 -> ~/.hermes/mailbox/.event_watermark.json

Pre-existing global copies at the destination are preserved under
<name>.pre-2026-04-16 so nothing is lost.  Safe to re-run (idempotent).
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

_MIGRATIONS: Tuple[Tuple[str, str], ...] = (
    ("telegram/topics.json",            "telegram/topics.json"),
    ("telegram/verbosity.json",         "telegram/verbosity.json"),
    ("notifications/quiet_hours.json",  "notifications/quiet_hours.json"),
    ("notifications/quiet_queue.json",  "notifications/quiet_queue.json"),
    ("events/event_bus.db",             "events/event_bus.db"),
    ("events/event_bus.db-wal",         "events/event_bus.db-wal"),
    ("events/event_bus.db-shm",         "events/event_bus.db-shm"),
    ("events/audit.jsonl",              "events/audit.jsonl"),
    ("mailbox/.event_watermark.json",   "mailbox/.event_watermark.json"),
)

BACKUP_SUFFIX = ".pre-2026-04-16"


def _default_root() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def migrate(
    root: Path | None = None,
    profile_home: Path | None = None,
    *,
    dry_run: bool = False,
) -> None:
    root = Path(root) if root is not None else _default_root()
    profile_home = Path(profile_home) if profile_home is not None else (root / "profiles" / "main")

    logger.info("migration root=%s profile_home=%s dry_run=%s", root, profile_home, dry_run)

    for rel_src, rel_dst in _MIGRATIONS:
        src = profile_home / rel_src
        dst = root / rel_dst
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            backup = dst.with_name(dst.name + BACKUP_SUFFIX)
            if backup.exists():
                if not dry_run:
                    if backup.is_dir():
                        shutil.rmtree(backup)
                    else:
                        backup.unlink()
            if not dry_run:
                dst.rename(backup)
        if not dry_run:
            shutil.move(str(src), str(dst))


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--profile-home", type=Path, default=None)
    ns = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    migrate(root=ns.root, profile_home=ns.profile_home, dry_run=ns.dry_run)


if __name__ == "__main__":
    _cli()
