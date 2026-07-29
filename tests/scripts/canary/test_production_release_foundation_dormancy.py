from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HOST_ACTIONS = ROOT / "scripts/canary/production_release_host_actions.py"
UPDATE_ENTRYPOINT = ROOT / "scripts/canary/production_release_update_entrypoint.py"
BUILDER_ASSET_ROOT = ROOT / "ops/muncho/release-updater"
BUILDER_UNIT = BUILDER_ASSET_ROOT / "muncho-release-builder@.service"


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


def _docstring_constants(tree: ast.AST) -> set[int]:
    owners = (
        ast.Module,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )
    constants: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            constants.add(id(first.value))
    return constants


def test_production_host_actions_have_no_process_mutation_boundary() -> None:
    tree = ast.parse(HOST_ACTIONS.read_text(encoding="utf-8"))
    docstrings = _docstring_constants(tree)
    forbidden: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    forbidden.append((node.lineno, "import subprocess"))
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            forbidden.append((node.lineno, "from subprocess import"))
        elif isinstance(node, ast.Call):
            target = _dotted_name(node.func)
            if target is not None and (
                target.startswith("subprocess.")
                or target.startswith("asyncio.create_subprocess_")
                or target.startswith("os.exec")
                or target.startswith("os.posix_spawn")
                or target.startswith("os.spawn")
                or target in {"os.popen", "os.system"}
            ):
                forbidden.append((node.lineno, target))
        elif (
            isinstance(node, ast.Constant)
            and id(node) not in docstrings
            and isinstance(node.value, str)
            and "systemctl" in node.value.casefold()
        ):
            forbidden.append((node.lineno, "systemctl literal"))

    assert forbidden == []


def test_builder_assets_have_no_systemd_activation_surface() -> None:
    sections = {
        line[1:-1]
        for raw_line in BUILDER_UNIT.read_text(encoding="ascii").splitlines()
        if (line := raw_line.strip()).startswith("[") and line.endswith("]")
    }
    activation_assets = sorted(
        str(path.relative_to(ROOT))
        for suffix in (".timer", ".target")
        for path in ROOT.rglob(f"*{suffix}")
        if (
            "muncho-release-builder" in path.name
            or "muncho-release-builder@" in path.read_text(encoding="utf-8")
        )
    )

    assert "Install" not in sections
    assert activation_assets == []


def test_production_update_has_no_fresh_execution_entrypoint() -> None:
    assert not UPDATE_ENTRYPOINT.exists()
