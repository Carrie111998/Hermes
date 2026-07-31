"""Make the optional gateway hygiene watchdog respect an explicit owner stop.

Some installs have an older, locally-created watchdog outside the Hermes
repository.  Its service can pull ``hermes-gateway.service`` in as a dependency
and its script can schedule a delayed restart whenever the gateway is inactive.
That behavior defeats an intentional operator stop.

This module performs one narrow, idempotent migration:

* remove dependency directives that implicitly start the gateway;
* add a durable owner-hold check before the watchdog evaluates or schedules;
* preserve byte-for-byte backups with hash/readback proof.

It does not introduce another supervisor or change normal watchdog thresholds.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


_PULL_DEPENDENCY_DIRECTIVES = frozenset({"Wants", "Requires", "BindsTo", "Upholds"})
_SCRIPT_DECLARATION_MARKER = "# direct-ops-owner-hold-path"
_SCRIPT_EARLY_GUARD_MARKER = "# direct-ops-owner-hold-early-guard"
_SCRIPT_SCHEDULE_GUARD_MARKER = "# direct-ops-owner-hold-schedule-guard"
_BACKUP_DIRNAME = "gateway-hygiene-pre-direct-ops"


@dataclass(frozen=True)
class HygieneMigrationReceipt:
    changed_paths: tuple[Path, ...]
    backup_paths: tuple[Path, ...]
    before_sha256: dict[str, str]
    after_sha256: dict[str, str]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _backup_name(path: Path) -> str:
    return f"original-{path.name}"


def _backup_with_readback(path: Path, backup_root: Path) -> Path:
    """Preserve the first unmanaged source exactly and verify its bytes."""

    source = _read_bytes(path)
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / _backup_name(path)
    if not backup.exists():
        shutil.copy2(path, backup)
    readback = _read_bytes(backup)
    if readback != source:
        raise RuntimeError(
            "Gateway hygiene backup does not match the source being migrated: "
            f"{path} -> {backup}"
        )
    return backup


def _atomic_replace_text(path: Path, content: str) -> None:
    """Replace one text file in-place with mode preservation and readback."""

    previous_mode = path.stat().st_mode
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.direct-ops-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path = Path(temp_name)
        os.chmod(temp_path, previous_mode)
        os.replace(temp_path, path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    if path.read_bytes() != content.encode("utf-8"):
        raise RuntimeError(f"Gateway hygiene migration readback failed: {path}")


def harden_hygiene_unit_definition(
    definition: str,
    *,
    gateway_service: str = "hermes-gateway.service",
) -> str:
    """Remove only dependency edges that can implicitly start the gateway."""

    kept: list[str] = []
    for line in definition.splitlines(keepends=True):
        match = re.match(r"^(\s*)([A-Za-z]+)\s*=\s*(.*?)(\r?\n)?$", line)
        if match is None or match.group(2) not in _PULL_DEPENDENCY_DIRECTIVES:
            kept.append(line)
            continue
        dependencies = match.group(3).split()
        remaining = [item for item in dependencies if item != gateway_service]
        if len(remaining) == len(dependencies):
            kept.append(line)
            continue
        if remaining:
            ending = match.group(4) or ""
            kept.append(
                f"{match.group(1)}{match.group(2)}={' '.join(remaining)}{ending}"
            )
    return "".join(kept)


def harden_hygiene_watchdog_script(script: str) -> str:
    """Add hold checks at intake and immediately before delayed restart."""

    updated = script
    if _SCRIPT_DECLARATION_MARKER not in updated:
        service_line = re.search(r"^SERVICE\s*=\s*.+$", updated, flags=re.MULTILINE)
        if service_line is None:
            raise ValueError("Watchdog script has no SERVICE declaration anchor")
        declaration = (
            f"{service_line.group(0)}\n"
            'OWNER_HOLD = HOME / ".gateway-owner-hold.json"  '
            f"{_SCRIPT_DECLARATION_MARKER}"
        )
        updated = (
            updated[: service_line.start()]
            + declaration
            + updated[service_line.end() :]
        )

    if _SCRIPT_SCHEDULE_GUARD_MARKER not in updated:
        schedule_line = re.search(
            r"^def schedule_restart\((.*?)\)\s*->\s*tuple\[bool,\s*str\]:\r?\n",
            updated,
            flags=re.MULTILINE,
        )
        if schedule_line is None:
            raise ValueError("Watchdog script has no schedule_restart anchor")
        guard = (
            f"    {_SCRIPT_SCHEDULE_GUARD_MARKER}\n"
            "    if OWNER_HOLD.exists():\n"
            '        return False, "owner hold active; restart suppressed"\n'
        )
        updated = (
            updated[: schedule_line.end()] + guard + updated[schedule_line.end() :]
        )

    if _SCRIPT_EARLY_GUARD_MARKER not in updated:
        parse_line = re.search(
            r"^(\s*)args\s*=\s*parser\.parse_args\(\)\r?\n",
            updated,
            flags=re.MULTILINE,
        )
        if parse_line is None:
            raise ValueError("Watchdog script has no argparse intake anchor")
        indent = parse_line.group(1)
        guard = (
            f"{indent}{_SCRIPT_EARLY_GUARD_MARKER}\n"
            f"{indent}if OWNER_HOLD.exists():\n"
            f"{indent}    if args.status:\n"
            f'{indent}        print("Gateway Hygiene Watchdog status\\n'
            'What matters: explicit owner hold is active; no restart evaluated.")\n'
            f"{indent}    return 0\n"
        )
        updated = updated[: parse_line.end()] + guard + updated[parse_line.end() :]

    return updated


def migrate_gateway_hygiene_hold_support(
    *,
    unit_path: Path,
    script_path: Path,
    backup_root: Path,
    gateway_service: str = "hermes-gateway.service",
    daemon_reload: Optional[Callable[[], None]] = None,
) -> HygieneMigrationReceipt:
    """Migrate the optional watchdog as one protected filesystem transaction."""

    changed: list[Path] = []
    backups: list[Path] = []
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    planned: list[tuple[Path, bytes, bytes]] = []

    transforms = (
        (
            unit_path,
            lambda text: harden_hygiene_unit_definition(
                text,
                gateway_service=gateway_service,
            ),
        ),
        (script_path, harden_hygiene_watchdog_script),
    )
    # Compute and validate every transformation before the first filesystem
    # effect. A malformed second file must not leave a hardened unit paired
    # with an unhardened watchdog (or vice versa).
    for path, transform in transforms:
        if not path.exists():
            continue
        original_bytes = _read_bytes(path)
        original = original_bytes.decode("utf-8")
        transformed = transform(original)
        transformed_bytes = transformed.encode("utf-8")
        before[str(path)] = _sha256_bytes(original_bytes)
        planned.append((path, original_bytes, transformed_bytes))
        if transformed_bytes != original_bytes:
            changed.append(path)

    # Preserve every original before writing any target.
    for path, original_bytes, transformed_bytes in planned:
        if transformed_bytes != original_bytes:
            backup = _backup_with_readback(path, backup_root)
            backups.append(backup)

    try:
        for path, original_bytes, transformed_bytes in planned:
            if transformed_bytes == original_bytes:
                continue
            _atomic_replace_text(path, transformed_bytes.decode("utf-8"))

        if unit_path in changed and daemon_reload is not None:
            daemon_reload()
    except Exception as exc:
        compensation_issues: list[str] = []
        # Inspect every planned target because a replace can succeed and then
        # fail during its own readback. Restore only exact states produced by
        # this migration; refuse to overwrite any concurrent third state.
        for path, original_bytes, transformed_bytes in reversed(planned):
            try:
                observed = _read_bytes(path)
                if observed == original_bytes:
                    continue
                if observed != transformed_bytes:
                    compensation_issues.append(
                        f"{path}: protected state changed during migration"
                    )
                    continue
                _atomic_replace_text(path, original_bytes.decode("utf-8"))
                if _read_bytes(path) != original_bytes:
                    compensation_issues.append(
                        f"{path}: compensation readback mismatch"
                    )
            except Exception as rollback_exc:
                compensation_issues.append(
                    f"{path}: compensation failed: "
                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                )

        if unit_path in changed and daemon_reload is not None:
            try:
                # If the first reload partially took effect, reload the exact
                # compensated unit. A second failure leaves an explicit
                # external blocker instead of pretending the loaded state is
                # known.
                daemon_reload()
            except Exception as reload_exc:
                compensation_issues.append(
                    "systemd reload after compensation failed: "
                    f"{type(reload_exc).__name__}: {reload_exc}"
                )

        if compensation_issues:
            raise RuntimeError(
                "Gateway hygiene migration failed and protected compensation "
                "was incomplete: "
                + "; ".join(compensation_issues)
            ) from exc
        raise

    for path, _original_bytes, transformed_bytes in planned:
        after_bytes = _read_bytes(path)
        after[str(path)] = _sha256_bytes(after_bytes)
        if transformed_bytes != after_bytes:
            raise RuntimeError(f"Gateway hygiene source readback mismatch: {path}")

    return HygieneMigrationReceipt(
        changed_paths=tuple(changed),
        backup_paths=tuple(backups),
        before_sha256=before,
        after_sha256=after,
    )


def rollback_gateway_hygiene_migration(
    *,
    unit_path: Path,
    script_path: Path,
    backup_root: Path,
    receipt: HygieneMigrationReceipt,
    daemon_reload: Optional[Callable[[], None]] = None,
) -> tuple[Path, ...]:
    """Restore exact backups only while protected post-migration state matches."""

    restore_plan: list[tuple[Path, bytes, bytes]] = []
    for path in (unit_path, script_path):
        backup = backup_root / _backup_name(path)
        if not backup.exists():
            continue
        current = _read_bytes(path)
        expected_after = receipt.after_sha256.get(str(path))
        if expected_after is None or _sha256_bytes(current) != expected_after:
            raise RuntimeError(
                "Gateway hygiene rollback refused because protected state changed "
                f"after migration: {path}"
            )
        original = _read_bytes(backup)
        expected_before = receipt.before_sha256.get(str(path))
        if expected_before is None or _sha256_bytes(original) != expected_before:
            raise RuntimeError(
                f"Gateway hygiene rollback backup hash mismatch: {backup}"
            )
        restore_plan.append((path, current, original))

    restored: list[Path] = []
    try:
        for path, _current, original in restore_plan:
            _atomic_replace_text(path, original.decode("utf-8"))
            if _read_bytes(path) != original:
                raise RuntimeError(
                    f"Gateway hygiene rollback readback failed: {path}"
                )
            restored.append(path)
    except Exception:
        # Compensate any file that reached the intended backup bytes; leave an
        # untouched file alone. Any third state is a concurrent edit and must
        # never be overwritten blindly.
        for path, current, original in reversed(restore_plan):
            observed = _read_bytes(path)
            if observed == current:
                continue
            if observed != original:
                raise RuntimeError(
                    "Gateway hygiene rollback compensation refused because "
                    f"protected state changed: {path}"
                )
            _atomic_replace_text(path, current.decode("utf-8"))
        raise
    if unit_path in restored and daemon_reload is not None:
        daemon_reload()
    return tuple(restored)


def default_hygiene_backup_root(hermes_home: Path) -> Path:
    return hermes_home / "quarantine" / _BACKUP_DIRNAME


__all__ = [
    "HygieneMigrationReceipt",
    "default_hygiene_backup_root",
    "harden_hygiene_unit_definition",
    "harden_hygiene_watchdog_script",
    "migrate_gateway_hygiene_hold_support",
    "rollback_gateway_hygiene_migration",
]
