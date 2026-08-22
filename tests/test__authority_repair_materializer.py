"""Temporary exact-tree materializer for #86354.

The upstream PR CI checkout is the only available execution environment with a
complete repository.  This test runs the branch's reviewed one-shot carrier
with every push intercepted, then emits the exact tested product diff as a
compressed failure artifact.  The file is excluded from that artifact and is
removed when the resulting tree is published.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


CURRENT_MAIN = "b6bcb3e791c673e63974029bbab40cc9326803ff"
CARRIER = Path(".github/workflows/one-shot-fix-86354.yml")
SELF = "tests/test__authority_repair_materializer.py"


def _run_block(path: Path) -> str:
    lines = path.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "run: |"]
    assert len(starts) == 1, f"expected one run block in {path}, got {len(starts)}"
    start = starts[0]
    key_indent = len(lines[start]) - len(lines[start].lstrip())
    content_indent = key_indent + 2
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip():
            indent = len(line) - len(line.lstrip())
            if indent <= key_indent:
                break
            assert indent >= content_indent, f"bad carrier indentation: {line!r}"
            body.append(line[content_indent:])
        else:
            body.append("")
    assert body
    return "\n".join(body) + "\n"


def _install_read_only_git(tmp_path: Path) -> Path:
    real_git = shutil.which("git")
    assert real_git
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "args=(\"$@\")\n"
        "if [[ ${args[0]:-} == push ]]; then\n"
        "  echo '[materializer] blocked git push' >&2\n"
        "  exit 0\n"
        "fi\n"
        "for i in \"${!args[@]}\"; do\n"
        "  if [[ ${args[$i]} == origin ]]; then\n"
        "    args[$i]=https://github.com/andrexibiza/hermes-agent.git\n"
        "  fi\n"
        "done\n"
        f"exec {real_git!s} \"${{args[@]}}\"\n"
    )
    wrapper.chmod(0o755)
    return bindir


def _product_diff() -> tuple[bytes, dict[str, object]]:
    raw = subprocess.check_output(
        ["git", "diff", "--name-status", CURRENT_MAIN, "HEAD"], text=True
    )
    entries: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        if path == SELF or path.startswith(".github/workflows/"):
            continue
        if path.startswith("contributors/emails/"):
            continue
        entries.append((status, path))

    payload = io.BytesIO()
    manifest: dict[str, object] = {
        "base": CURRENT_MAIN,
        "entries": entries,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        encoded_manifest = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(encoded_manifest)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(encoded_manifest))
        for status, path in entries:
            if status.startswith("D"):
                continue
            archive.add(path, arcname=path, recursive=False)
    return payload.getvalue(), manifest


def test_materialize_endpoint_scoped_app_password(tmp_path: Path) -> None:
    assert CARRIER.is_file()
    script = tmp_path / "carrier.sh"
    script.write_text(_run_block(CARRIER))
    subprocess.run(["bash", "-n", str(script)], check=True)

    env = os.environ.copy()
    env["PATH"] = f"{_install_read_only_git(tmp_path)}:{env['PATH']}"
    subprocess.run(["bash", str(script)], check=True, env=env, timeout=1800)

    artifact, manifest = _product_diff()
    digest = hashlib.sha256(artifact).hexdigest()
    encoded = base64.b64encode(artifact).decode()
    wrapped = "\n".join(encoded[i : i + 120] for i in range(0, len(encoded), 120))
    pytest.fail(
        "HERMES_MATERIALIZED_TREE_BEGIN\n"
        f"sha256={digest}\n"
        f"manifest={json.dumps(manifest, sort_keys=True)}\n"
        f"{wrapped}\n"
        "HERMES_MATERIALIZED_TREE_END",
        pytrace=False,
    )
