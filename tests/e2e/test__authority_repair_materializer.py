"""Temporary read-only compiler for one authority-repair branch.

The upstream PR e2e checkout is the available complete repository. This test
executes the branch's sole ``one-shot-fix-*.yml`` carrier with every push
intercepted, then emits the exact tested product diff as a gzip/base64 failure
artifact. The compiler and workflow carriers are excluded from that artifact.
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
SELF = "tests/e2e/test__authority_repair_materializer.py"
BRANCH_BY_CARRIER = {
    "one-shot-fix-86354.yml": "fix/email-app-password-normalization",
    "one-shot-fix-88796.yml": "fix/memory-prefetch-cancel",
    "one-shot-fix-85644.yml": "campaign/webhook-delivery-callbacks",
    "one-shot-fix-89252.yml": "fix/88715-canonical-multiplex-identity",
}


def _carrier() -> Path:
    candidates = sorted(Path(".github/workflows").glob("one-shot-fix-*.yml"))
    assert len(candidates) == 1, [str(path) for path in candidates]
    assert candidates[0].name in BRANCH_BY_CARRIER, candidates[0].name
    return candidates[0]


def _run_block(path: Path) -> str:
    """Extract the carrier's sole final run block without parsing heredocs.

    Carrier payloads intentionally contain raw heredoc/triple-quoted lines
    that are not valid YAML indentation. The run block is the final field in
    every one-shot carrier, so consume to EOF and remove the YAML content
    indent only where it is actually present.
    """
    lines = path.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "run: |"]
    assert len(starts) == 1
    start = starts[0]
    key_indent = len(lines[start]) - len(lines[start].lstrip())
    prefix = " " * (key_indent + 2)
    body = [line[len(prefix) :] if line.startswith(prefix) else line for line in lines[start + 1 :]]
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
        "if [[ ${args[0]:-} == push ]]; then echo '[materializer] blocked git push' >&2; exit 0; fi\n"
        "for i in \"${!args[@]}\"; do\n"
        "  if [[ ${args[$i]} == origin ]]; then args[$i]=https://github.com/andrexibiza/hermes-agent.git; fi\n"
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
        if path == SELF or path.startswith("tests/test__authority_repair_materializer.py"):
            continue
        if path.startswith(".github/workflows/") or path.startswith("contributors/emails/"):
            continue
        entries.append((status, path))

    manifest: dict[str, object] = {
        "base": CURRENT_MAIN,
        "entries": entries,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    }
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        data = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(data)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(data))
        for status, path in entries:
            if not status.startswith("D"):
                archive.add(path, arcname=path, recursive=False)
    return payload.getvalue(), manifest


def test_materialize_exact_authority_repair_tree(tmp_path: Path) -> None:
    carrier = _carrier()
    script = tmp_path / "carrier.sh"
    script.write_text(_run_block(carrier))
    subprocess.run(["bash", "-n", str(script)], check=True)

    env = os.environ.copy()
    env["PATH"] = f"{_install_read_only_git(tmp_path)}:{env['PATH']}"
    env["CURRENT_MAIN"] = CURRENT_MAIN
    env["BRANCH"] = BRANCH_BY_CARRIER[carrier.name]
    subprocess.run(["bash", str(script)], check=True, env=env, timeout=1800)

    artifact, manifest = _product_diff()
    digest = hashlib.sha256(artifact).hexdigest()
    encoded = base64.b64encode(artifact).decode()
    wrapped = "\n".join(encoded[i : i + 120] for i in range(0, len(encoded), 120))
    pytest.fail(
        "HERMES_MATERIALIZED_TREE_BEGIN\n"
        f"sha256={digest}\nmanifest={json.dumps(manifest, sort_keys=True)}\n"
        f"{wrapped}\nHERMES_MATERIALIZED_TREE_END",
        pytrace=False,
    )
