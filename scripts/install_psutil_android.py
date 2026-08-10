#!/usr/bin/env python3
"""Install psutil on Termux/Android by patching upstream platform detection.

psutil's setup currently gates Linux sources behind
``sys.platform.startswith('linux')``. On Termux, Python reports
``sys.platform == 'android'``, so ``pip install psutil`` aborts with
"platform android is not supported" — even though psutil compiles fine
when the Linux source path is reused.

This script downloads the official psutil sdist, applies a one-line
patch (``LINUX = sys.platform.startswith(("linux", "android"))``), and
installs the patched tree with ``pip install --no-build-isolation``.

Usage:
    python scripts/install_psutil_android.py [--pip "/path/to/pip"] [--uv]

When neither flag is given, the script auto-detects ``uv`` on PATH and
falls back to ``<sys.executable> -m pip``.

This is a stopgap. Remove once psutil upstream merges
https://github.com/giampaolo/psutil/pull/2762 and ships a release.
"""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Keep sibling imports working when invoked as
# ``python scripts/install_psutil_android.py`` from the repo checkout.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli.psutil_android import (
    PSUTIL_URL,
    PsutilAndroidInstallError,
    prepare_patched_psutil_sdist,
)
from hermes_cli import _pip_security
from hermes_cli._subprocess_compat import windows_hide_flags


def _resolve_install_cmd(pip_arg: str | None, prefer_uv: bool) -> list[str]:
    if pip_arg:
        try:
            parts = shlex.split(pip_arg)
        except ValueError as exc:
            raise SystemExit(f"invalid --pip command quoting: {exc}") from exc
        if not parts:
            raise SystemExit("--pip command must not be empty")

        # The shell caller passes the interpreter and ``-m pip`` as one
        # argument.  An unquoted executable path containing spaces is split by
        # shlex before we can inspect it, so reconstruct that path when the
        # prefix clearly begins with an absolute path and ends in Python.
        marker = next(
            (
                index
                for index in range(len(parts) - 1)
                if parts[index : index + 2] == ["-m", "pip"]
            ),
            None,
        )
        if marker is not None and marker > 1:
            first = parts[0]
            last = parts[marker - 1].lower()
            first_is_path = first.startswith(("/", "\\")) or re.match(
                r"^[A-Za-z]:[\\/]", first
            )
            last_is_python = bool(
                re.search(r"(?:^|[\\/])python(?:\d(?:\.\d+)?)?(?:\.exe)?$", last)
            )
            has_option = any(token.startswith("-") for token in parts[:marker])
            if first_is_path and last_is_python and not has_option:
                parts = [" ".join(parts[:marker]), *parts[marker:]]
        return parts
    if prefer_uv:
        uv = shutil.which("uv")
        if not uv:
            sys.exit("--uv requested but no uv on PATH")
        return [uv, "pip"]
    auto_uv = shutil.which("uv")
    if auto_uv:
        return [auto_uv, "pip"]
    return [sys.executable, "-m", "pip"]


def _is_direct_pip_command(command: list[str]) -> bool:
    """Return whether *command* invokes pip rather than uv's pip frontend."""
    launcher = Path(command[0]).name.lower() if command else ""
    if launcher in {"uv", "uv.exe"}:
        return False
    if any(
        command[index : index + 2] == ["-m", "pip"]
        for index in range(len(command) - 1)
    ):
        return True
    return bool(re.fullmatch(r"pip(?:3(?:\.\d+)?)?(?:\.exe)?", launcher))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pip",
        help="Explicit installer command (e.g. '/usr/bin/uv pip' or 'python -m pip')",
    )
    parser.add_argument(
        "--uv",
        action="store_true",
        help="Force using uv (errors out if uv is not on PATH)",
    )
    args = parser.parse_args()

    install_cmd_prefix = _resolve_install_cmd(args.pip, args.uv)
    if _is_direct_pip_command(install_cmd_prefix):
        pip_ok, pip_error = _pip_security.ensure_pip_floor(
            install_cmd_prefix,
            creationflags=windows_hide_flags(),
        )
        if not pip_ok:
            print(
                f"✗ Refusing Android psutil install: pip security floor unavailable: {pip_error}",
                file=sys.stderr,
            )
            return 1

    print(
        "→ Termux/Android: prebuilding psutil with Linux source path "
        "compatibility shim (see psutil#2762)..."
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "psutil.tar.gz"
        urllib.request.urlretrieve(PSUTIL_URL, archive)
        try:
            src_root = prepare_patched_psutil_sdist(archive, tmp_path)
        except PsutilAndroidInstallError as exc:
            sys.exit(str(exc))

        cmd = install_cmd_prefix + ["install", "--no-build-isolation", str(src_root)]
        print(f"  $ {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            creationflags=windows_hide_flags(),
        )
        if result.returncode != 0:
            return result.returncode

    print("✓ psutil installed via Android compatibility shim")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
