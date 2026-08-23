#!/usr/bin/env python3
"""Apply final readable hardening to the materialized Phase-G product tree.

This is temporary transport scaffolding.  It runs after the generated
materializer/finalizer and is removed before the clean product commit.
Every edit is exact-count guarded so upstream drift fails closed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _replace_once(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    count = content.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path}: expected exactly one hardening anchor, found {count}"
        )
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


def _assert_no_broker_adjacent_ambient_reads(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    findings: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls_broker = any(
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "build_child_process_env"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "build_child_process_env"
            )
            for node in ast.walk(function)
        )
        if not calls_broker:
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Attribute)
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id == "os"
                and func.value.attr == "environ"
            ):
                findings.append(f"{function.name}:{node.lineno}: os.environ.{func.attr}")
    if findings:
        raise RuntimeError(
            f"{path}: broker-adjacent ambient environment reads remain: {findings}"
        )


def main(root: Path) -> None:
    host = root / "tui_gateway" / "host_supervisor.py"
    guard = root / "tests" / "security" / "test_child_process_authority_guard.py"

    _replace_once(
        host,
        '''        overrides = dict(self.env or {})
        overrides["HERMES_COMPUTE_HOST_HEARTBEAT_SECS"] = str(self.heartbeat_secs)
        inherited_pythonpath = str(
            overrides.get("PYTHONPATH") or os.environ.get("PYTHONPATH") or ""
        )
        repo_root = str(_repo_root())
        pythonpath_parts = [
            part for part in inherited_pythonpath.split(os.pathsep) if part
        ]
        if repo_root not in pythonpath_parts:
            pythonpath_parts.insert(0, repo_root)
        overrides["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env = build_child_process_env(
            trusted_hermes_child_spec(source="tui_gateway.host_supervisor"),
            overrides=overrides,
        )
''',
        '''        overrides = dict(self.env or {})
        overrides["HERMES_COMPUTE_HOST_HEARTBEAT_SECS"] = str(self.heartbeat_secs)
        env = build_child_process_env(
            trusted_hermes_child_spec(source="tui_gateway.host_supervisor"),
            overrides=overrides,
        )
        inherited_pythonpath = str(env.get("PYTHONPATH") or "")
        repo_root = str(_repo_root())
        pythonpath_parts = [
            part for part in inherited_pythonpath.split(os.pathsep) if part
        ]
        if repo_root not in pythonpath_parts:
            pythonpath_parts.insert(0, repo_root)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
''',
    )

    _replace_once(
        guard,
        '''def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _find_escape_hatches(path: Path) -> list[str]:
''',
        '''def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_os_environ_method(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Attribute)
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "os"
        and func.value.attr == "environ"
    )


def _function_calls_child_env_broker(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and _call_name(child) == "build_child_process_env"
        for child in ast.walk(node)
    )


def _find_escape_hatches(path: Path) -> list[str]:
''',
    )

    _replace_once(
        guard,
        '''    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
''',
        '''    findings: list[str] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _function_calls_child_env_broker(function):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and _is_os_environ_method(node):
                findings.append(
                    f"{rel}:{node.lineno}: ambient os.environ read beside child-env broker"
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
''',
    )

    _assert_no_broker_adjacent_ambient_reads(host)
    compile(guard.read_text(encoding="utf-8"), str(guard), "exec")
    print("phase_g_hardening=host-pythonpath-through-broker,broker-adjacent-env-guard")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: harden-process-edge-authority.py PRODUCT_ROOT")
    main(Path(sys.argv[1]).resolve())
