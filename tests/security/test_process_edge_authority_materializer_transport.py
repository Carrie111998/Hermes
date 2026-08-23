"""Temporary deterministic transport for the Phase-G product object.

The fork's Actions executor is not producing observable runs. The upstream PR
lane is authoritative and already executes PR-controlled Python tests, so this
test applies the materializer's literal transformation table in an isolated
temporary tree and emits a content-addressed archive of only the final product
paths. It never mutates the checked-out repository.

This file is transport scaffolding. It must not exist in the final PR.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MATERIALIZER = REPO_ROOT / ".github" / "materialize-process-edge-authority.py"
ARCHIVE_PATH = REPO_ROOT / "phase-g-product.tar.gz"
CHUNK_CHARS = 3000


def _literal_assignment(module: ast.Module, name: str):
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"materializer assignment {name!r} not found")


def _materializer_data(source: str) -> tuple[dict, tuple, tuple]:
    module = ast.parse(source, filename=str(MATERIALIZER))
    new_files = _literal_assignment(module, "NEW_FILES")
    exact = _literal_assignment(module, "EXACT_REPLACEMENTS")
    regex = _literal_assignment(module, "REGEX_REPLACEMENTS")
    assert isinstance(new_files, dict)
    assert isinstance(exact, (list, tuple))
    assert isinstance(regex, (list, tuple))
    return new_files, tuple(exact), tuple(regex)


def _copy_existing(paths: set[str], destination: Path) -> None:
    for rel in sorted(paths):
        source = REPO_ROOT / rel
        assert source.is_file(), f"materializer source path missing: {rel}"
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _write(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _replace_cli_exec_import(content: str, old: str, new: str) -> str:
    """Retarget the old shared anchor to the current split cli.exec handler.

    Current main has the same ``windows_hide_flags`` + ``subprocess.run``
    sequence in both ``cli.exec`` and ``shell.exec``. Only ``cli.exec`` receives
    the trusted-Hermes-child policy in this replacement; shell execution keeps
    its separately sanitized model-authored-command contract.
    """

    assert content.count(old) == 2, (
        "expected the current-main methods_tools split to expose two generic "
        f"spawn anchors, found {content.count(old)}"
    )
    marker = '@method("cli.exec")'
    start = content.index(marker)
    end = content.find("\n@method(", start + len(marker))
    assert end != -1, "cli.exec handler has no following method boundary"
    handler = content[start:end]
    assert handler.count(old) == 1, (
        "cli.exec must own exactly one generic spawn anchor, found "
        f"{handler.count(old)}"
    )
    handler = handler.replace(old, new, 1)
    return content[:start] + handler + content[end:]


def _apply_materializer(
    root: Path,
    new_files: dict,
    exact: tuple,
    regex: tuple,
) -> list[str]:
    adaptations: list[str] = []

    for rel, content in new_files.items():
        assert not (root / rel).exists(), f"new materializer path already exists: {rel}"
        _write(root, str(rel), str(content))

    for path, old, new in exact:
        path = str(path)
        content = _read(root, path)
        count = content.count(old)
        if (
            path == "tui_gateway/methods_tools.py"
            and "from hermes_cli._subprocess_compat import windows_hide_flags" in old
            and count == 2
        ):
            content = _replace_cli_exec_import(content, old, new)
            adaptations.append(
                "tui_gateway/methods_tools.py: scope shared import anchor to cli.exec"
            )
        else:
            assert count == 1, (
                f"{path}: expected one exact replacement, found {count}"
            )
            content = content.replace(old, new, 1)
        _write(root, path, content)

    for path, pattern, replacement, flags in regex:
        path = str(path)
        content = _read(root, path)
        updated, count = re.subn(
            pattern,
            replacement,
            content,
            count=1,
            flags=flags,
        )
        assert count == 1, (
            f"{path}: expected one regex replacement, found {count}"
        )
        _write(root, path, updated)

    return adaptations


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_product(root: Path, paths: set[str], receipt: dict) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        receipt_bytes = (
            json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
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
    ARCHIVE_PATH.write_bytes(archive)
    encoded = base64.b64encode(archive).decode("ascii")
    print("PHASE_G_MATERIALIZED_ARCHIVE_BEGIN")
    print(f"archive_path={ARCHIVE_PATH}")
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
    new_files, exact, regex = _materializer_data(source)
    existing = {str(item[0]) for item in (*exact, *regex)}
    generated = {str(path) for path in new_files}
    product_paths = existing | generated
    assert existing and generated

    checkout_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="phase-g-materialize-") as tmp:
        root = Path(tmp)
        _copy_existing(existing, root)
        adaptations = _apply_materializer(root, new_files, exact, regex)

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
            "current_main_adaptations": adaptations,
            "files": files,
        }
        archive = _archive_product(root, product_paths, receipt)
        _emit_archive(archive, receipt)

    pytest.fail(
        "deterministic Phase-G product archive emitted above; transport PR closes unmerged",
        pytrace=False,
    )
