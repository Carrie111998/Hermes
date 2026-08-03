"""Behavior tests for the TypeScript decorator interoperability diagnostic."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPO_ROOT
    / "skills"
    / "software-development"
    / "typescript-decorator-interop"
    / "scripts"
    / "inspect_project.py"
)


def _load_script():
    spec = importlib.util.spec_from_file_location("inspect_project", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_reports_compilation_unit_boundary_for_legacy_consumer(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "package.json",
        {
            "type": "commonjs",
            "dependencies": {"@theorvane/type-mcp": "^0.2.2"},
        },
    )
    _write_json(
        tmp_path / "tsconfig.json",
        {
            "compilerOptions": {
                "module": "commonjs",
                "experimentalDecorators": True,
            }
        },
    )

    report = _load_script().inspect_project(tmp_path)

    assert report["project"]["package_type"] == "commonjs"
    assert report["project"]["decorator_mode"] == "legacy"
    assert {finding["code"] for finding in report["findings"]} >= {
        "standard-decorator-dependency-in-legacy-mode",
        "separate-compilation-unit-required",
    }


def test_accepts_standard_decorator_node_next_project(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "package.json",
        {
            "type": "module",
            "dependencies": {"@theorvane/type-chain": "^0.1.1"},
        },
    )
    _write_json(
        tmp_path / "tsconfig.json",
        {
            "compilerOptions": {
                "module": "NodeNext",
                "moduleResolution": "NodeNext",
                "experimentalDecorators": False,
                "lib": ["ES2022", "ESNext.Decorators"],
            }
        },
    )

    report = _load_script().inspect_project(tmp_path)

    assert report["project"]["decorator_mode"] == "standard"
    assert report["project"]["node_aware_module_resolution"] is True
    assert report["findings"] == []


def test_resolves_local_jsonc_extends_before_classifying(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.base.json").write_text(
        """{
          // NestJS legacy decorator configuration
          \"compilerOptions\": {
            \"experimentalDecorators\": true,
          },
        }
        """,
        encoding="utf-8",
    )
    _write_json(tmp_path / "package.json", {"dependencies": {}})
    _write_json(
        tmp_path / "tsconfig.json",
        {"extends": "./tsconfig.base.json", "compilerOptions": {"module": "CommonJS"}},
    )

    report = _load_script().inspect_project(tmp_path)

    assert report["project"]["decorator_mode"] == "legacy"
    assert report["project"]["module"] == "commonjs"
