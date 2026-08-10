"""Plugin-native diagnostics for Memory Duo."""

from __future__ import annotations

import json
from pathlib import Path


COMMANDS = ("status", "doctor", "rebuild-index", "reconcile", "pending", "conflicts", "stats")


def _paths():
    from hermes_constants import get_hermes_home
    from .config import ObsidianDuoConfig

    home = Path(get_hermes_home())
    config = ObsidianDuoConfig.load(home)
    return home, config


def run_diagnostics() -> dict:
    try:
        home, config = _paths()
    except Exception as exc:
        return {"config": "unavailable", "message": type(exc).__name__}
    vault = Path(config.vault_path)
    db_path = home / "obsidian_duo" / "memory.db"
    result = {
        "vault_reachable": vault.is_dir(),
        "managed_folder": (vault / config.managed_folder).is_dir(),
        "database": db_path.exists(),
    }
    if db_path.exists():
        import sqlite3
        try:
            with sqlite3.connect(db_path) as conn:
                result["sqlite_integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                result["incomplete_journal"] = conn.execute(
                    "SELECT COUNT(*) FROM journal WHERE state != 'committed'"
                ).fetchone()[0]
        except sqlite3.Error:
            result["sqlite_integrity"] = False
    return result


def obsidian_duo_command(args) -> None:
    command = getattr(args, "obsidian_duo_command", "status")
    if command == "doctor":
        result = run_diagnostics()
    else:
        result = {"command": command, **run_diagnostics()}
    print(json.dumps(result, sort_keys=True, default=str))


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="obsidian_duo_command")
    rebuild = subs.add_parser("rebuild-index", help="Rebuild the managed-memory index")
    rebuild.add_argument("--full", action="store_true")
    for command in ("status", "doctor", "reconcile", "pending", "conflicts", "stats"):
        subs.add_parser(command)
    subparser.set_defaults(handler_fn=obsidian_duo_command)
