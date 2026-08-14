#!/usr/bin/env python3
"""Fail CI when a known dispatcher mutation seam is absent/unclassified."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/contracts/kanban_mutating_entrypoints.json"
ALLOWED_POLICIES = {
    "GUARDED_DISPATCH_MUTATION",
    "TOKEN_GUARDED_DELIVERY_COMPLETION",
    "DISABLED_NONZERO",
    "PROVEN_READ_ONLY",
}
TARGETS = {
    "hermes_cli/kanban.py": {"_cmd_dispatcher", "_cmd_dispatch", "_cmd_daemon"},
    "hermes_cli/kanban_dispatcher.py": {"run_foreground_dispatcher", "run_dispatcher_tick"},
    "hermes_cli/kanban_db.py": {
        "dispatch_once_authorized",
        "dispatch_once",
        "_dispatch_once",
        "_dispatch_once_locked",
        "_default_spawn",
        "remove_notify_sub",
        "advance_notify_cursor",
    },
    "plugins/kanban/dashboard/plugin_api.py": {"dispatch"},
}


def module_name(path: str) -> str:
    return path[:-3].replace("/", ".")


def main() -> int:
    rows = json.loads(MANIFEST.read_text(encoding="utf-8"))
    by_symbol = {row["symbol"]: row for row in rows}
    if len(by_symbol) != len(rows):
        raise SystemExit("duplicate dispatcher manifest symbol")
    for symbol, row in by_symbol.items():
        if row.get("authority_policy") not in ALLOWED_POLICIES:
            raise SystemExit(f"unclassified authority policy: {symbol}")
        if not row.get("test_id"):
            raise SystemExit(f"missing executable test id: {symbol}")

    discovered: set[str] = set()
    for relative, names in TARGETS.items():
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        present = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = names - present
        if missing:
            raise SystemExit(f"expected dispatcher seams missing from {relative}: {sorted(missing)}")
        discovered.update(f"{module_name(relative)}.{name}" for name in names)

    gateway = ROOT / "gateway/kanban_watchers.py"
    tree = ast.parse(gateway.read_text(encoding="utf-8"), filename=str(gateway))
    found_gateway = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "GatewayKanbanWatchersMixin":
            found_gateway = any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "_kanban_dispatcher_watcher"
                for item in node.body
            )
    if not found_gateway:
        raise SystemExit("embedded dispatcher adapter missing")
    discovered.add(
        "gateway.kanban_watchers.GatewayKanbanWatchersMixin._kanban_dispatcher_watcher"
    )

    absent = discovered - set(by_symbol)
    if absent:
        raise SystemExit(f"unclassified dispatcher entrypoints: {sorted(absent)}")
    print(f"dispatcher manifest OK: {len(discovered)} discovered, {len(rows)} classified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
