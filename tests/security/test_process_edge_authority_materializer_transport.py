"""Temporary deterministic transport for the Phase-G product object.

The fork's Actions executor is not producing observable runs.  The upstream PR
lane *is* authoritative and already executes PR-controlled Python tests, so this
test reconstructs the materializer in an isolated temporary tree and emits a
content-addressed archive of only the final product paths.  It never mutates the
checked-out repository.

This file is transport scaffolding.  It must not exist in the final PR.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MATERIALIZER = REPO_ROOT / ".github" / "materialize-process-edge-authority.py"
CHUNK_CHARS = 3000


def _literal_assignment(module: ast.Module, name: str):
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"materializer assignment {name!r} not found")


def _materializer_paths(source: str) -> tuple[set[str], set[str]]:
    module = ast.parse(source, filename=str(MATERIALIZER))
    new_files = _literal_assignment(module, "NEW_FILES")
    exact = _literal_assignment(module, "EXACT_REPLACEMENTS")
    regex = _literal_assignment(module, "REGEX_REPLACEMENTS")

    assert isinstance(new_files, dict)
    existing = {str(item[0]) for item in (*exact, *regex)}
    generated = {str(path) for path in new_files}
    assert existing
    assert generated
    return existing, generated


def _copy_existing(paths: set[str], destination: Path) -> None:
    for rel in sorted(paths):
        source = REPO_ROOT / rel
        assert source.is_file(), f"materializer source path missing: {rel}"
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_product(root: Path, paths: set[str], receipt: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        receipt_bytes = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        receipt_info = tarfile.TarInfo("phase-g-materialization-receipt.json")
        receipt_info.size = len(receipt_bytes)
        receipt_info.mode = 0o644
        receipt_info.mtime = 0
        receipt_info.uid = receipt_info.gid = 0
        receipt_info.uname = receipt_info.gname = ""
        archive.addfile(receipt_info, io.BytesIO(receipt_bytes))

        for rel in sorted(paths):
            data = (root / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _emit_archive(archive: bytes, receipt: dict) -> None:
    encoded = base64.b64encode(archive).decode("ascii")
    print("PHASE_G_MATERIALIZED_ARCHIVE_BEGIN")
    print(f"archive_sha256={_sha256(archive)}")
    print(f"archive_bytes={len(archive)}")
    print(f"encoded_chars={len(encoded)}")
    print(f"checkout_sha={receipt['checkout_sha']}")
    print(f"materializer_sha256={receipt['materializer_sha256']}")
    for index, start in enumerate(range(0, len(encoded), CHUNK_CHARS)):
        print(f"chunk={index:06d}:{encoded[start:start + CHUNK_CHARS]}")
    print("PHASE_G_MATERIALIZED_ARCHIVE_END")


def test_export_assertion_guarded_phase_g_product_object():
    source = MATERIALIZER.read_text(encoding="utf-8")
    existing, generated = _materializer_paths(source)
    product_paths = existing | generated

    checkout_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="phase-g-materialize-") as tmp:
        root = Path(tmp)
        _copy_existing(existing, root)
        copied_materializer = root / ".github" / MATERIALIZER.name
        copied_materializer.parent.mkdir(parents=True, exist_ok=True)
        copied_materializer.write_text(source, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(copied_materializer)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        assert result.returncode == 0, (
            "materializer failed against the upstream synthetic-merge checkout\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        missing = [rel for rel in sorted(product_paths) if not (root / rel).is_file()]
        assert not missing, f"materializer did not produce: {missing}"

        files = {
            rel: {
                "bytes": (root / rel).stat().st_size,
                "sha256": _sha256((root / rel).read_bytes()),
            }
            for rel in sorted(product_paths)
        }
        receipt = {
            "schema": "hermes.phase-g-materialized-object.v1",
            "checkout_sha": checkout_sha,
            "materializer_sha256": _sha256(source.encode("utf-8")),
            "product_file_count": len(files),
            "files": files,
        }
        archive = _archive_product(root, product_paths, receipt)
        _emit_archive(archive, receipt)

    pytest.fail(
        "deterministic Phase-G product archive emitted above; transport PR closes unmerged",
        pytrace=False,
    )
