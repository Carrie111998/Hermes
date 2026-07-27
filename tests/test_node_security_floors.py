"""Regression guards for the audited root npm runtime graph."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"}
EXCLUDED_PARTS = {"node_modules", "dist", "build", "release"}


def _manifest(relative: str) -> dict:
    return json.loads((ROOT / relative / "package.json").read_text(encoding="utf-8"))


def _version(spec: str) -> tuple[int, ...]:
    return tuple(int(part) for part in spec.lstrip("^~>=<").split("."))


def test_frontend_router_and_sanitizer_security_floors() -> None:
    desktop = _manifest("apps/desktop")
    web = _manifest("web")

    for manifest in (desktop, web):
        assert manifest["dependencies"]["react-router"] == "^8.3.0"
        assert "react-router-dom" not in manifest["dependencies"]

    assert _version(desktop["dependencies"]["dompurify"]) >= (3, 4, 12)


def test_concurrently_uses_patched_shell_quote() -> None:
    root = _manifest(".")
    assert root["overrides"]["concurrently"]["shell-quote"] == "1.10.0"


def test_runtime_sources_do_not_import_removed_router_reexport() -> None:
    offenders: list[str] = []
    for source_root in (ROOT / "web", ROOT / "apps" / "desktop"):
        for path in source_root.rglob("*"):
            if (
                not path.is_file()
                or path.suffix not in SOURCE_SUFFIXES
                or any(part in EXCLUDED_PARTS for part in path.parts)
            ):
                continue
            if "react-router-dom" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
