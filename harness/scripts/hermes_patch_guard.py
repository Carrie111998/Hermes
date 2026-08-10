#!/usr/bin/env python3
"""Verifica integridade dos patches Hermes One no checkout local.

Somente leitura — não altera arquivos. Saída JSON no stdout.
Exit 0 se todos os checks passarem; exit 1 caso contrário.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CHECKS: list[dict] = []


def _record(name: str, status: str, evidence: str) -> None:
    CHECKS.append({"name": name, "status": status, "evidence": evidence})


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _record(f"read:{path.name}", "fail", str(exc))
        return ""


def check_hermes_one_block(repo: Path) -> None:
    path = repo / "hermes_cli" / "web_server.py"
    text = _read(path)
    if not text:
        return

    markers = [
        "HERMES_ONE_MODEL_LIBRARY_COMPAT_V1",
        "def _hermes_one_model_library_path",
        "def _hermes_one_read_model_library",
        '@app.get("/api/model/library")',
        '@app.post("/api/model/library")',
        '@app.patch("/api/model/library/{model_id:path}")',
        '@app.delete("/api/model/library/{model_id:path}")',
        "get_hermes_home() / \"models.json\"",
    ]
    missing = [m for m in markers if m not in text]
    if missing:
        _record(
            "hermes_one_web_server",
            "fail",
            f"{path}: missing {len(missing)} marker(s): {missing[:3]}{'...' if len(missing) > 3 else ''}",
        )
    else:
        _record("hermes_one_web_server", "pass", f"{path}: all {len(markers)} markers present")


def check_openrouter_prune(repo: Path) -> None:
    path = repo / "agent" / "credential_pool.py"
    text = _read(path)
    if not text:
        return

    markers = [
        'source = "env:OPENROUTER_API_KEY"',
        "INCIDENTE-AUTH-JSON-REWRITE",
        "entries[:] = [e for e in entries if e.source != source]",
    ]
    missing = [m for m in markers if m not in text]
    if missing:
        _record(
            "openrouter_prune",
            "fail",
            f"{path}: missing prune markers: {missing}",
        )
    else:
        _record("openrouter_prune", "pass", f"{path}: OpenRouter stale-entry prune intact")


def check_files_exist(repo: Path) -> None:
    for rel in ("hermes_cli/web_server.py", "agent/credential_pool.py", "AGENTS.md"):
        path = repo / rel
        if path.is_file():
            _record(f"exists:{rel}", "pass", str(path))
        else:
            _record(f"exists:{rel}", "fail", f"missing {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes One patch integrity guard")
    parser.add_argument(
        "repo_path",
        nargs="?",
        default=str(Path(__file__).resolve().parents[2]),
        help="Root of hermes-agent checkout",
    )
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    repo = Path(args.repo_path).resolve()

    if not repo.is_dir():
        out = {"ok": False, "checks": [{"name": "repo", "status": "fail", "evidence": f"not a directory: {repo}"}]}
        print(json.dumps(out, indent=2))
        return 1

    check_files_exist(repo)
    check_hermes_one_block(repo)
    check_openrouter_prune(repo)

    ok = all(c["status"] == "pass" for c in CHECKS)
    out = {"ok": ok, "repo_path": str(repo), "checks": CHECKS}
    print(json.dumps(out, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
