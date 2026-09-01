"""Stable macOS Hermes Desktop installation contract.

Builds remain disposable artifacts under the source checkout.  The only
launchable/updateable product install is ``~/Applications/Hermes.app``.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BUNDLE_ID = "com.nousresearch.hermes"


class DesktopInstallError(RuntimeError):
    """Raised when the stable install cannot be proven safe and complete."""


@dataclass(frozen=True)
class DesktopInstallResult:
    source: Path
    target: Path
    executable: Path
    replaced_existing: bool


def canonical_macos_app(home: Path | None = None) -> Path:
    """Return the one supported per-user macOS installation path."""
    resolved_home = Path.home() if home is None else Path(home)
    return resolved_home.expanduser() / "Applications" / "Hermes.app"


def macos_app_executable(app: Path) -> Path:
    return Path(app) / "Contents" / "MacOS" / "Hermes"


def _validate_bundle(app: Path) -> Path:
    app = Path(app)
    if app.is_symlink() or not app.is_dir():
        raise DesktopInstallError(f"Hermes app bundle is missing or is a symlink: {app}")
    info_path = app / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as stream:
            info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise DesktopInstallError(f"Hermes app Info.plist is unreadable: {info_path}") from exc
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise DesktopInstallError(
            f"unexpected bundle identifier in {info_path}: {info.get('CFBundleIdentifier')!r}"
        )
    executable_name = info.get("CFBundleExecutable")
    if executable_name != "Hermes":
        raise DesktopInstallError(f"unexpected Hermes executable name: {executable_name!r}")
    executable = macos_app_executable(app)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DesktopInstallError(f"Hermes app executable is missing or not executable: {executable}")
    return executable


def _production_copy(source: Path, destination: Path) -> None:
    ditto = Path("/usr/bin/ditto")
    if ditto.is_file():
        subprocess.run([str(ditto), str(source), str(destination)], check=True)
    else:
        shutil.copytree(source, destination, copy_function=shutil.copy2)


def _remove_owned_bundle(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    # These are exact transaction-owned names (installing / rollback, or the
    # canonical target after a failed final rename). Removing a symlink here
    # unlinks only the directory entry; it never follows or deletes its target.
    # This is also how a legacy versioned-app symlink is migrated safely.
    if path.is_symlink():
        path.unlink()
        return
    if not path.is_dir():
        raise DesktopInstallError(f"refusing to remove unexpected updater path: {path}")
    shutil.rmtree(path)


def install_macos_app(
    source: Path,
    *,
    home: Path | None = None,
    target: Path | None = None,
    copy_bundle: Callable[[Path, Path], None] = _production_copy,
    move: Callable[[Path, Path], None] = lambda source, destination: source.rename(destination),
) -> DesktopInstallResult:
    """Atomically replace the canonical app bundle, rolling back on failure."""
    source = Path(source).expanduser()
    canonical = canonical_macos_app(home)
    requested_target = canonical if target is None else Path(target).expanduser()
    if requested_target != canonical:
        raise DesktopInstallError(f"refusing noncanonical Hermes install target: {requested_target}; expected {canonical}")
    if canonical.parent.is_symlink():
        raise DesktopInstallError(f"canonical Applications directory must not be a symlink: {canonical.parent}")

    source_executable = _validate_bundle(source)
    del source_executable
    try:
        if source.resolve(strict=True) == canonical.resolve(strict=False):
            raise DesktopInstallError("the canonical installed app cannot be used as its own artifact source")
    except OSError as exc:
        raise DesktopInstallError(f"cannot resolve Hermes artifact source: {source}") from exc

    canonical.parent.mkdir(parents=True, exist_ok=True)
    stage = canonical.with_name("Hermes.app.installing")
    rollback = canonical.with_name("Hermes.app.rollback")

    # Recover a transaction interrupted after the old app moved aside.
    if rollback.exists() and not canonical.exists():
        if not rollback.is_symlink() and not rollback.is_dir():
            raise DesktopInstallError(f"refusing unexpected rollback path: {rollback}")
        move(rollback, canonical)
    elif rollback.exists() or rollback.is_symlink():
        _remove_owned_bundle(rollback)
    _remove_owned_bundle(stage)

    try:
        copy_bundle(source, stage)
        _validate_bundle(stage)
    except Exception as exc:
        try:
            _remove_owned_bundle(stage)
        except Exception:
            pass
        if isinstance(exc, DesktopInstallError):
            raise
        raise DesktopInstallError(f"could not stage Hermes app from {source}") from exc

    replaced_existing = canonical.exists()
    if replaced_existing or canonical.is_symlink():
        if not canonical.is_symlink() and not canonical.is_dir():
            _remove_owned_bundle(stage)
            raise DesktopInstallError(f"canonical Hermes install is not a real app directory: {canonical}")
        move(canonical, rollback)
        replaced_existing = True

    try:
        move(stage, canonical)
        executable = _validate_bundle(canonical)
    except Exception as exc:
        try:
            if canonical.exists() or canonical.is_symlink():
                _remove_owned_bundle(canonical)
            if replaced_existing and rollback.exists():
                move(rollback, canonical)
                _validate_bundle(canonical)
        except Exception as rollback_exc:
            raise DesktopInstallError(
                f"install failed and rollback failed; staged artifact remains at {stage}"
            ) from rollback_exc
        try:
            _remove_owned_bundle(stage)
        except Exception:
            pass
        if replaced_existing:
            raise DesktopInstallError("install failed; the previous app was restored") from exc
        raise DesktopInstallError("install failed before the canonical app could be created") from exc

    _remove_owned_bundle(rollback)
    return DesktopInstallResult(
        source=source,
        target=canonical,
        executable=executable,
        replaced_existing=replaced_existing,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install a Hermes.app artifact at its stable per-user path")
    parser.add_argument("--source", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = install_macos_app(args.source)
    except DesktopInstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "source": str(result.source),
                "target": str(result.target),
                "executable": str(result.executable),
                "replaced_existing": result.replaced_existing,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
