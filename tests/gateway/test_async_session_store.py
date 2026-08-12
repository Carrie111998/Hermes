"""Async SessionStore boundary for gateway event-loop safety."""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from gateway.session import AsyncSessionStore


class _SpyStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.label = "store"

    def read(self, value: str) -> str:
        self.calls.append((value, threading.get_ident()))
        return value


def _nearest_function(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def _is_awaited(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Await):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return False


def test_gateway_async_code_uses_one_awaited_session_store_boundary() -> None:
    """Loop-side store calls must use the facade; raw store remains sync-only."""
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for rel in ("gateway/run.py", "gateway/slash_commands.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for owner in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
            raw_aliases = {
                target.id
                for node in ast.walk(owner)
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in {"self", "_self"}
                and node.value.attr == "session_store"
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(owner):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if _nearest_function(node, parents) is not owner:
                    # A nested sync helper (for example run_sync) executes off-loop.
                    continue
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in raw_aliases:
                    violations.append(
                        f"{rel}:{node.lineno} raw alias {receiver.id}.{node.func.attr}() in async {owner.name}"
                    )
                    continue
                if not (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id in {"self", "_self"}
                ):
                    continue
                if receiver.attr == "session_store":
                    violations.append(
                        f"{rel}:{node.lineno} raw session_store.{node.func.attr}() in async {owner.name}"
                    )
                elif receiver.attr == "async_session_store" and not _is_awaited(
                    node, parents
                ):
                    violations.append(
                        f"{rel}:{node.lineno} unawaited async_session_store.{node.func.attr}()"
                    )
    assert not violations, "\n".join(violations)


def test_no_repository_local_claude_permissions_file() -> None:
    """Tracked ``.claude/settings.json`` carries ``hooks`` and nothing else.

    Two things must never land in it (AGENTS.md, *MCP servers for Claude
    Code*): a ``permissions`` block, which would grant tool access to every
    agent that opens the repo, and an ``mcpServers`` block. The latter is not
    cosmetic — Claude Code documents ``${VAR}`` expansion for ``.mcp.json``
    and ``~/.claude.json`` but *not* for ``settings.json``, so a server
    declared here needs a literal absolute path and then starts on exactly
    one machine, failing silently everywhere else. That is what happened:
    the block was removed deliberately on 2026-07-28 in favour of
    ``.mcp.json.example``, and an unrelated dependabot bump (7e38fa5)
    restored a copy carrying a hardcoded ``/Users/<name>/`` path.

    The file itself must stay — ``.claude/hooks/session-start.sh`` is tracked
    and reached only from here via ``$CLAUDE_PROJECT_DIR``, which *does*
    expand in hook commands.
    """
    import json

    root = Path(__file__).resolve().parents[2]
    settings_path = root / ".claude" / "settings.json"
    if not settings_path.exists():
        return

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    forbidden = {"permissions", "mcpServers"}.intersection(settings)
    assert not forbidden, (
        f".claude/settings.json declares {sorted(forbidden)}; it may carry "
        "`hooks` only. Move MCP servers to .mcp.json (template: "
        ".mcp.json.example) — settings.json does not expand ${VAR}, so a "
        "server declared here silently fails to start off the machine its "
        "absolute path was written on."
    )
