#!/usr/bin/env python3
"""Validate a gateway runtime before a supervisor starts Hermes.

This script deliberately uses only the standard library and is invoked by the
systemd unit as a file, rather than through the editable Hermes package. That
lets it diagnose a stale editable-install finder before importing Hermes would
fail.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlparse


class RuntimePreflightError(RuntimeError):
    """A structural runtime problem that must block gateway startup."""


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _text_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def find_forbidden_references(
    venv_root: Path,
    forbidden_roots: tuple[Path, ...],
    extra_paths: tuple[Path, ...] = (),
) -> list[Path]:
    """Return runtime metadata/scripts that retain a forbidden source path."""
    roots = [venv_root / "bin", venv_root / "Scripts", venv_root / "lib", *extra_paths]
    matches: list[Path] = []
    needles = tuple(str(path.resolve()) for path in forbidden_roots)
    for root in roots:
        paths = root.rglob("*") if root.is_dir() else (root,)
        for path in paths:
            if not path.is_file():
                continue
            if (
                root == venv_root / "lib"
                and path.name != "direct_url.json"
                and path.suffix not in {".pth", ".py"}
            ):
                continue
            if any(_text_contains(path, needle) for needle in needles):
                matches.append(path)
    return matches


def validate_console_scripts(venv_root: Path) -> None:
    """Reject console scripts whose Python shebang still names another venv."""
    scripts_dir = venv_root / ("Scripts" if os.name == "nt" else "bin")
    if not scripts_dir.is_dir():
        raise RuntimePreflightError("virtualenv scripts directory is missing")
    expected = str(scripts_dir.resolve())
    stale: list[Path] = []
    for path in scripts_dir.iterdir():
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                first_line = handle.readline().strip()
        except OSError:
            continue
        if not first_line.startswith("#!") or "python" not in first_line.lower():
            continue
        if first_line.startswith("#!/usr/bin/env "):
            continue  # explicitly relocatable form
        if expected not in first_line:
            stale.append(path)
    if stale:
        raise RuntimePreflightError(
            "console-script shebang does not target this virtualenv: "
            + ", ".join(map(str, stale[:5]))
        )


def validate_hermes_direct_url(runtime_root: Path, venv_root: Path) -> None:
    """Require Hermes editable metadata to point at the selected runtime root."""
    direct_urls = list(
        (venv_root / "lib").glob(
            "python*/site-packages/hermes_agent-*.dist-info/direct_url.json"
        )
    )
    if not direct_urls:
        raise RuntimePreflightError(
            "Hermes editable-install direct_url.json is missing"
        )
    for direct_url in direct_urls:
        try:
            payload = json.loads(direct_url.read_text(encoding="utf-8"))
            source = Path(unquote(urlparse(str(payload.get("url", ""))).path)).resolve()
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimePreflightError(
                f"invalid Hermes editable-install metadata: {direct_url}"
            ) from exc
        if source != runtime_root.resolve():
            raise RuntimePreflightError(
                f"Hermes editable-install source is not the selected runtime: {direct_url}"
            )


def validate_imports(runtime_root: Path, service_workdir: Path) -> None:
    """Import the gateway's core modules from the real service working directory."""
    probe = """
import importlib
import importlib.metadata as metadata
import json
mods = ('hermes_cli', 'gateway', 'gateway.cgroup_cleanup')
origins = {name: importlib.import_module(name).__file__ for name in mods}
print(json.dumps({'origins': origins, 'version': metadata.version('hermes-agent')}))
"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=service_workdir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise RuntimePreflightError("could not execute core import probe") from exc
    if not payload:
        raise RuntimePreflightError("core import probe failed")
    for name, origin in payload["origins"].items():
        if not origin or not _under(Path(origin), runtime_root):
            raise RuntimePreflightError(f"{name} resolves outside the selected runtime")


def validate_runtime(
    runtime_root: Path,
    venv_root: Path,
    service_workdir: Path,
    forbidden_roots: tuple[Path, ...],
    extra_paths: tuple[Path, ...],
) -> None:
    if not runtime_root.is_dir() or not venv_root.is_dir():
        raise RuntimePreflightError("runtime root or virtualenv is missing")
    stale = find_forbidden_references(venv_root, forbidden_roots, extra_paths)
    if stale:
        raise RuntimePreflightError(
            "forbidden runtime reference: " + ", ".join(map(str, stale[:5]))
        )
    validate_console_scripts(venv_root)
    validate_hermes_direct_url(runtime_root, venv_root)
    validate_imports(runtime_root, service_workdir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--venv-root", type=Path, required=True)
    parser.add_argument("--service-workdir", type=Path, default=Path.cwd())
    parser.add_argument("--forbid-path", type=Path, action="append", default=[])
    parser.add_argument("--extra-path", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        validate_runtime(
            args.runtime_root,
            args.venv_root,
            args.service_workdir,
            tuple(args.forbid_path),
            tuple(args.extra_path),
        )
    except RuntimePreflightError as exc:
        print(f"runtime preflight failed: {exc}", file=sys.stderr)
        return 1
    print("runtime preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
