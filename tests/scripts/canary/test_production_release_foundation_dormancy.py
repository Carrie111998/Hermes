from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
HOST_ACTIONS = ROOT / "scripts/canary/production_release_host_actions.py"
UPDATE_RUNTIME = ROOT / "scripts/canary/production_release_update_runtime.py"
RECOVERY_COORDINATOR = (
    ROOT / "scripts/canary/production_release_update_recovery.py"
)
UPDATE_ENTRYPOINT = ROOT / "scripts/canary/production_release_update_entrypoint.py"
BUILDER_ASSET_ROOT = ROOT / "ops/muncho/release-updater"
BUILDER_UNIT = BUILDER_ASSET_ROOT / "muncho-release-builder@.service"
UPDATE_CALLS = frozenset({"execute_update", "recover_update"})


def _is_update_runtime_module(name: str) -> bool:
    return name == "production_release_update_runtime" or name.endswith(
        ".production_release_update_runtime"
    )


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


def test_production_update_has_only_the_reviewed_dormant_recovery_caller() -> None:
    assert not UPDATE_ENTRYPOINT.exists()

    callers: list[tuple[str, int, str]] = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if (
            path == UPDATE_RUNTIME
            or "tests" in relative.parts
            or any(
                part in {".venv", "venv", "node_modules", "__pycache__"}
                for part in relative.parts
            )
        ):
            continue
        source = path.read_text(encoding="utf-8")
        if "production_release_update_runtime" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        module_aliases: set[str] = set()
        function_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_update_runtime_module(alias.name):
                        module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module in {None, "scripts.canary"}:
                    for alias in node.names:
                        if alias.name == "production_release_update_runtime":
                            module_aliases.add(alias.asname or alias.name)
                elif node.module is not None and _is_update_runtime_module(node.module):
                    for alias in node.names:
                        if alias.name in UPDATE_CALLS:
                            function_aliases[alias.asname or alias.name] = alias.name

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _dotted_name(node.func)
            if target in function_aliases:
                callers.append((str(relative), node.lineno, function_aliases[target]))
                continue
            if isinstance(node.func, ast.Attribute):
                owner = _dotted_name(node.func.value)
                if owner in module_aliases and node.func.attr in UPDATE_CALLS:
                    callers.append((str(relative), node.lineno, node.func.attr))

    assert sorted(
        (path, name)
        for path, _line, name in callers
    ) == [
        (
            str(RECOVERY_COORDINATOR.relative_to(ROOT)),
            "recover_update",
        )
    ]


def test_recovery_coordinator_has_no_fresh_execution_or_activation_surface() -> None:
    tree = ast.parse(
        RECOVERY_COORDINATOR.read_text(encoding="utf-8"),
        filename=str(RECOVERY_COORDINATOR),
    )
    forbidden_imports = {
        "argparse",
        "asyncio",
        "subprocess",
    }
    forbidden_calls = {
        "ReleaseUpdateJournal",
        "active.create_or_replay_active_transaction",
        "runtime.execute_update",
    }
    forbidden: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "main":
                forbidden.append((node.lineno, "main"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".", 1)[0] in forbidden_imports:
                    forbidden.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if (
                node.module is not None
                and node.module.split(".", 1)[0] in forbidden_imports
            ):
                forbidden.append((node.lineno, node.module))
        elif isinstance(node, ast.Call):
            target = _dotted_name(node.func)
            if target in forbidden_calls:
                forbidden.append((node.lineno, target))
        elif isinstance(node, ast.If):
            compared = ast.dump(node.test, include_attributes=False)
            if "__name__" in compared and "__main__" in compared:
                forbidden.append((node.lineno, "__main__"))

    assert forbidden == []
