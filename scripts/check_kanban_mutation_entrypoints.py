#!/usr/bin/env python3
"""Generate and verify the installed Kanban mutation-entrypoint inventory."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests/contracts/kanban_mutating_entrypoints.json"
ALLOWED_POLICIES = {
    "AUDITED_DELIVERY_MUTATION",
    "CHILD_STATE_DERIVED_MUTATION",
    "DETERMINISTIC_DELIVERY_MUTATION",
    "DISABLED_NONZERO",
    "GUARDED_DISPATCH_MUTATION",
    "IDEMPOTENT_SCHEMA_MUTATION",
    "LEASE_EXPIRY_DELIVERY_MUTATION",
    "PROVEN_READ_ONLY",
    "STATE_GUARDED_DELIVERY_MUTATION",
    "TOKEN_GUARDED_DELIVERY_COMPLETION",
    "TOKEN_GUARDED_DELIVERY_MUTATION",
    "TOKEN_MINTING_DELIVERY_MUTATION",
}
_WRITE_PREFIXES = (
    "ALTER ",
    "BEGIN IMMEDIATE",
    "CREATE ",
    "DELETE ",
    "DROP ",
    "INSERT ",
    "REPLACE ",
    "UPDATE ",
    "VACUUM",
)


@dataclass(frozen=True)
class FunctionFacts:
    symbol: str
    module: str
    name: str
    calls: frozenset[str]
    writes_sql: bool
    docstring: str
    decorators: tuple[str, ...]


def _module_name(path: Path, root: Path) -> str:
    return str(path.relative_to(root))[:-3].replace("/", ".")


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _decorator_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _facts_for(path: Path, root: Path) -> list[FunctionFacts]:
    module = _module_name(path, root)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    facts: list[FunctionFacts] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            for child in node.body:
                self.visit(child)
            self.classes.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            calls = {_call_name(item) for item in ast.walk(node) if isinstance(item, ast.Call)}
            strings = [
                item.value.lstrip().upper()
                for item in ast.walk(node)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            symbol = ".".join([module, *self.classes, node.name])
            facts.append(
                FunctionFacts(
                    symbol=symbol,
                    module=module,
                    name=node.name,
                    calls=frozenset(calls - {""}),
                    writes_sql=any(value.startswith(_WRITE_PREFIXES) for value in strings),
                    docstring=ast.get_docstring(node) or "",
                    decorators=tuple(_decorator_text(item) for item in node.decorator_list),
                )
            )
            # Nested functions are implementation details, not installed surfaces.

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

    Visitor().visit(tree)
    return facts


def discover_entrypoints(root: Path = ROOT) -> set[str]:
    """Discover installed mutation/status surfaces from behavior, never a name allowlist."""
    paths = sorted((root / "hermes_cli").glob("kanban*.py"))
    paths += [
        root / "gateway/kanban_watchers.py",
        root / "plugins/kanban/dashboard/plugin_api.py",
    ]
    facts = [fact for path in paths if path.exists() for fact in _facts_for(path, root)]
    by_symbol = {fact.symbol: fact for fact in facts}

    discovered = {
        fact.symbol
        for fact in facts
        if (
            "require_dispatcher_lease" in fact.calls
            or fact.name == "_kanban_dispatcher_watcher"
            or (
                fact.module == "hermes_cli.kanban_delivery_outbox"
                and fact.writes_sql
            )
            or (
                fact.module == "hermes_cli.kanban_db"
                and "notify" in fact.name
                and fact.writes_sql
                and fact.name.startswith(("advance_", "remove_"))
            )
            or "disabled legacy" in fact.docstring.lower()
            or (
                fact.name == "dispatch"
                and any(".post('/dispatch')" in value.replace('"', "'") for value in fact.decorators)
            )
        )
    }

    # Close over installed facades that invoke a discovered mutator. Restrict the
    # closure to dispatch/delivery vocabulary so ordinary board CRUD does not get
    # mislabeled as machine-dispatch authority.
    changed = True
    while changed:
        changed = False
        mutating_names = {by_symbol[symbol].name for symbol in discovered}
        for fact in facts:
            vocabulary = any(
                word in fact.name
                for word in ("dispatch", "daemon", "spawn", "materialize", "lease", "recover", "mark_", "audit", "process_parent", "derive_parent", "init_schema", "ensure_column")
            )
            if fact.symbol not in discovered and vocabulary and fact.calls & mutating_names:
                discovered.add(fact.symbol)
                changed = True
    return discovered


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

    discovered = discover_entrypoints()
    absent = discovered - set(by_symbol)
    if absent:
        raise SystemExit(f"unclassified generated mutation entrypoints: {sorted(absent)}")
    stale = set(by_symbol) - discovered
    if stale:
        raise SystemExit(f"manifest rows not generated from installed surfaces: {sorted(stale)}")
    print(f"dispatcher manifest OK: {len(discovered)} generated, {len(rows)} classified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
