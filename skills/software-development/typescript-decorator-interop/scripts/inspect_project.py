#!/usr/bin/env python3
"""Inspect TypeScript decorator and module interoperability boundaries.

This intentionally reads only local project metadata. It does not install
packages, alter tsconfig files, or infer external decorator APIs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_STANDARD_DECORATOR_DEPENDENCIES = {
    "@theorvane/type-mcp",
    "@theorvane/type-chain",
}
_ESM_ONLY_DEPENDENCIES = {"@theorvane/type-chain"}
_NODE_AWARE_MODULE_RESOLUTIONS = {"node16", "nodenext"}


def _strip_jsonc(source: str) -> str:
    """Remove JSONC comments and trailing commas without touching strings."""
    result: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
        elif char == '"':
            in_string = True
            result.append(char)
            index += 1
        elif char == "/" and following == "/":
            index = source.find("\n", index)
            if index == -1:
                break
        elif char == "/" and following == "*":
            end = source.find("*/", index + 2)
            if end == -1:
                raise ValueError("Unclosed JSONC block comment")
            index = end + 2
        else:
            result.append(char)
            index += 1

    uncommented = "".join(result)
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(uncommented):
        char = uncommented[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            next_index = index + 1
            while next_index < len(uncommented) and uncommented[next_index].isspace():
                next_index += 1
            if next_index < len(uncommented) and uncommented[next_index] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _read_jsonc(path: Path) -> dict[str, Any]:
    data = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    if not isinstance(data, dict):
        raise ValueError(f"Expected an object in {path}")
    return data


def _resolve_extends(config_path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    seen = set() if seen is None else seen
    config_path = config_path.resolve()
    if config_path in seen:
        raise ValueError(f"Circular tsconfig extends: {config_path}")
    seen.add(config_path)
    config = _read_jsonc(config_path)
    parent_options: dict[str, Any] = {}
    extends = config.get("extends")
    if isinstance(extends, str) and extends.startswith("."):
        parent_path = (config_path.parent / extends).resolve()
        if parent_path.suffix != ".json":
            parent_path = parent_path.with_suffix(".json")
        parent_options = _resolve_extends(parent_path, seen)
    own_options = config.get("compilerOptions", {})
    if not isinstance(own_options, dict):
        raise ValueError(f"compilerOptions must be an object in {config_path}")
    return {**parent_options, **own_options}


def _dependency_names(package: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        dependencies = package.get(section, {})
        if isinstance(dependencies, dict):
            names.update(name for name in dependencies if isinstance(name, str))
    return names


def _finding(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def inspect_project(project_root: Path, tsconfig: Path | None = None) -> dict[str, Any]:
    """Return deterministic compatibility findings for one TypeScript project."""
    root = project_root.resolve()
    config_path = (tsconfig or root / "tsconfig.json").resolve()
    package_path = root / "package.json"
    package = _read_jsonc(package_path) if package_path.is_file() else {}
    options = _resolve_extends(config_path)
    dependencies = _dependency_names(package)
    standard_dependencies = sorted(dependencies & _STANDARD_DECORATOR_DEPENDENCIES)
    esm_only_dependencies = sorted(dependencies & _ESM_ONLY_DEPENDENCIES)

    experimental = options.get("experimentalDecorators")
    decorator_mode = "legacy" if experimental is True else "standard" if experimental is False else "standard"
    module = str(options.get("module", "")).lower()
    module_resolution = str(options.get("moduleResolution", "")).lower()
    libs = {str(value).lower() for value in options.get("lib", []) if isinstance(value, str)}
    package_type = package.get("type") if package.get("type") in {"module", "commonjs"} else "commonjs"
    node_aware = module_resolution in _NODE_AWARE_MODULE_RESOLUTIONS

    findings: list[dict[str, str]] = []
    if standard_dependencies and decorator_mode == "legacy":
        dependency_list = ", ".join(standard_dependencies)
        findings.append(_finding(
            "standard-decorator-dependency-in-legacy-mode",
            "error",
            f"{dependency_list} requires standard (Stage 3) decorators, but this tsconfig enables legacy experimentalDecorators.",
        ))
        findings.append(_finding(
            "separate-compilation-unit-required",
            "error",
            "TypeScript chooses one decorator calling convention per tsconfig compilation unit; do not compile legacy and standard-decorator source together.",
        ))
    if standard_dependencies and not node_aware:
        findings.append(_finding(
            "node-aware-module-resolution-recommended",
            "warning",
            "Use Node16 or NodeNext moduleResolution when consuming packages with Node exports maps.",
        ))
    if standard_dependencies and "esnext.decorators" not in libs:
        findings.append(_finding(
            "standard-decorator-lib-missing",
            "warning",
            "Add ESNext.Decorators to lib when the configured TypeScript target does not already provide the standard decorator library.",
        ))
    if esm_only_dependencies and package_type == "commonjs":
        dependency_list = ", ".join(esm_only_dependencies)
        findings.append(_finding(
            "esm-only-dependency-in-commonjs-package",
            "warning",
            f"{dependency_list} is ESM-only; keep it behind an async import() runtime boundary in CommonJS code.",
        ))

    return {
        "project": {
            "root": str(root),
            "tsconfig": str(config_path),
            "package_type": package_type,
            "module": module,
            "module_resolution": module_resolution,
            "node_aware_module_resolution": node_aware,
            "decorator_mode": decorator_mode,
            "standard_decorator_dependencies": standard_dependencies,
        },
        "findings": findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, nargs="?", default=Path.cwd())
    parser.add_argument("--tsconfig", type=Path, help="tsconfig path (defaults to <project>/tsconfig.json)")
    args = parser.parse_args()
    try:
        report = inspect_project(args.project, args.tsconfig)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
