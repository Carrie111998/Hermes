#!/usr/bin/env python3
"""Transactional runner for safe Hermes changes.

This script turns a change into a small deployment-style transaction:
preflight -> snapshot -> apply -> verify -> rollback-or-finalize -> report.

It deliberately lives as a CLI/script rather than a model tool, preserving
Hermes's narrow core tool surface while giving agents and maintainers a
repeatable way to apply risky local changes with evidence and rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


class SafeChangeError(Exception):
    """Raised for preflight/policy failures."""


@dataclass
class AttemptReport:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    delay_before_next_seconds: float | None = None


@dataclass
class CommandReport:
    argv: list[str]
    attempts: list[AttemptReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].exit_code == 0


@dataclass
class SnapshotItem:
    rel_path: str
    existed: bool
    kind: str
    backup_rel_path: str | None
    before_sha256: str | None


@dataclass
class SnapshotReport:
    root: str
    items: list[SnapshotItem]


@dataclass
class TransactionReport:
    name: str
    status: str
    started_at: str
    finished_at: str | None = None
    workdir: str = ""
    failure_phase: str | None = None
    error: str | None = None
    rollback_performed: bool = False
    snapshot: SnapshotReport | None = None
    commands: dict[str, list[CommandReport]] = field(default_factory=lambda: {"apply": [], "verify": []})

    def to_jsonable(self) -> dict:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(sha256_file(child).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def parse_json_argv(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafeChangeError(f"command is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise SafeChangeError("commands must be non-empty JSON argv lists, not shell strings")
    return value


def parse_retry_delays(raw: str) -> list[float]:
    delays: list[float] = []
    for part in raw.split(","):
        stripped = part.strip()
        if not stripped:
            continue
        try:
            delay = float(stripped)
        except ValueError as exc:
            raise SafeChangeError(f"invalid retry delay {stripped!r}") from exc
        if delay < 0:
            raise SafeChangeError("retry delays must be >= 0")
        delays.append(delay)
    return delays or [0.0]


def resolve_snapshot_path(workdir: Path, raw_path: str) -> tuple[str, Path]:
    candidate = Path(raw_path)
    absolute = candidate if candidate.is_absolute() else workdir / candidate
    resolved = absolute.resolve(strict=False)
    resolved_workdir = workdir.resolve(strict=True)
    try:
        rel = resolved.relative_to(resolved_workdir)
    except ValueError as exc:
        raise SafeChangeError(f"snapshot path {raw_path!r} is outside workdir") from exc
    if ".git" in rel.parts:
        raise SafeChangeError(f"snapshot path {raw_path!r} targets .git, which is not allowed")
    if not rel.parts:
        raise SafeChangeError("snapshot path cannot be the workdir root")
    return rel.as_posix(), resolved


def ensure_report_parent(report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)


def write_report(report_path: Path, report: TransactionReport) -> None:
    ensure_report_parent(report_path)
    report_path.write_text(json.dumps(report.to_jsonable(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_snapshot_paths(workdir: Path, snapshot_paths: Sequence[str]) -> None:
    if not snapshot_paths:
        raise SafeChangeError("at least one --snapshot path is required")
    for raw_path in snapshot_paths:
        resolve_snapshot_path(workdir, raw_path)


def create_snapshot(workdir: Path, name: str, snapshot_paths: Sequence[str]) -> SnapshotReport:
    validate_snapshot_paths(workdir, snapshot_paths)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in name).strip("-") or "change"
    snapshot_root = workdir / ".safe-change" / f"{timestamp}-{safe_name}"
    snapshot_root.mkdir(parents=True, exist_ok=False)
    items: list[SnapshotItem] = []
    for raw_path in snapshot_paths:
        rel_path, source = resolve_snapshot_path(workdir, raw_path)
        backup = snapshot_root / rel_path
        if source.is_dir():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, backup, symlinks=True)
            items.append(SnapshotItem(rel_path, True, "dir", backup.relative_to(snapshot_root).as_posix(), sha256_tree(source)))
        elif source.is_file():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            items.append(SnapshotItem(rel_path, True, "file", backup.relative_to(snapshot_root).as_posix(), sha256_file(source)))
        elif source.exists():
            raise SafeChangeError(f"snapshot path {raw_path!r} is neither file nor directory")
        else:
            items.append(SnapshotItem(rel_path, False, "missing", None, None))
    manifest = {"created_at": utc_now(), "items": [asdict(item) for item in items]}
    (snapshot_root / "snapshot-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return SnapshotReport(root=snapshot_root.as_posix(), items=items)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def rollback(workdir: Path, snapshot: SnapshotReport) -> None:
    snapshot_root = Path(snapshot.root)
    for item in snapshot.items:
        target = workdir / item.rel_path
        if target.exists() or target.is_symlink():
            remove_path(target)
        if item.existed:
            assert item.backup_rel_path is not None
            backup = snapshot_root / item.backup_rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.kind == "dir":
                shutil.copytree(backup, target, symlinks=True)
            elif item.kind == "file":
                shutil.copy2(backup, target)
            else:
                raise SafeChangeError(f"cannot restore unsupported snapshot kind {item.kind!r}")


def _terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows fallback
            proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - Windows fallback
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            proc.kill()


def run_command(argv: Sequence[str], workdir: Path, timeout: float, retry_delays: Sequence[float]) -> CommandReport:
    command_report = CommandReport(argv=list(argv))
    for index, delay in enumerate(retry_delays):
        start = time.monotonic()
        proc = subprocess.Popen(
            list(argv),
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(proc)
            stdout, stderr = proc.communicate()
            if exc.stdout and isinstance(exc.stdout, str):
                stdout = f"{exc.stdout}{stdout or ''}"
            if exc.stderr and isinstance(exc.stderr, str):
                stderr = f"{exc.stderr}{stderr or ''}"
            exit_code = 124
            stderr = f"{stderr or ''}\ncommand timed out after {timeout}s; process tree terminated".strip()
        duration = round(time.monotonic() - start, 4)
        should_retry = exit_code != 0 and index < len(retry_delays) - 1
        command_report.attempts.append(AttemptReport(exit_code, stdout or "", stderr or "", duration, delay if should_retry else None))
        if exit_code == 0:
            return command_report
        if should_retry and delay:
            time.sleep(delay)
    return command_report


def run_phase(
    phase: str,
    commands: Sequence[Sequence[str]],
    workdir: Path,
    timeout: float,
    retry_delays: Sequence[float],
    report: TransactionReport,
) -> bool:
    for argv in commands:
        command_report = run_command(argv, workdir, timeout, retry_delays)
        report.commands[phase].append(command_report)
        if not command_report.ok:
            return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local change as a verified rollback-capable transaction.")
    parser.add_argument("--workdir", default=".", help="Repository/work directory. Snapshot paths must stay under it.")
    parser.add_argument("--name", required=True, help="Human-readable transaction name.")
    parser.add_argument("--snapshot", action="append", default=[], help="File or directory to snapshot before applying changes. Repeatable.")
    parser.add_argument("--apply-json", action="append", default=[], help="Apply command as JSON argv list, e.g. '[\"python\", \"script.py\"]'. Repeatable.")
    parser.add_argument("--verify-json", action="append", default=[], help="Verification command as JSON argv list. Repeatable.")
    parser.add_argument("--report", required=True, help="Path to write JSON transaction report.")
    parser.add_argument("--retry-delays", default="0,5,15", help="Comma-separated seconds for bounded retries/backoff.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-command timeout in seconds.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report_path = Path(args.report)
    report = TransactionReport(
        name=args.name,
        status="started",
        started_at=utc_now(),
        workdir=str(Path(args.workdir).resolve(strict=False)),
    )
    try:
        workdir = Path(args.workdir).resolve(strict=True)
        report.workdir = workdir.as_posix()
        validate_snapshot_paths(workdir, args.snapshot)
        apply_commands = [parse_json_argv(raw) for raw in args.apply_json]
        verify_commands = [parse_json_argv(raw) for raw in args.verify_json]
        if not apply_commands:
            raise SafeChangeError("at least one --apply-json command is required")
        if not verify_commands:
            raise SafeChangeError("at least one --verify-json command is required")
        retry_delays = parse_retry_delays(args.retry_delays)
        snapshot = create_snapshot(workdir, args.name, args.snapshot)
        report.snapshot = snapshot
        report.status = "applying"
        if not run_phase("apply", apply_commands, workdir, args.timeout, retry_delays, report):
            report.failure_phase = "apply"
            rollback(workdir, snapshot)
            report.rollback_performed = True
            report.status = "rolled_back"
            return_code = 1
        else:
            report.status = "verifying"
            if not run_phase("verify", verify_commands, workdir, args.timeout, retry_delays, report):
                report.failure_phase = "verify"
                rollback(workdir, snapshot)
                report.rollback_performed = True
                report.status = "rolled_back"
                return_code = 1
            else:
                report.status = "verified"
                return_code = 0
    except SafeChangeError as exc:
        report.status = "preflight_failed"
        report.failure_phase = "preflight"
        report.error = str(exc)
        return_code = 2
    except Exception as exc:  # pragma: no cover - defensive fail-closed path
        report.status = "error"
        report.failure_phase = report.failure_phase or "unknown"
        report.error = f"{type(exc).__name__}: {exc}"
        if report.snapshot is not None:
            try:
                rollback(Path(report.workdir), report.snapshot)
                report.rollback_performed = True
                report.status = "rolled_back"
                return_code = 1
            except Exception as rollback_exc:
                report.error = f"{report.error}; rollback failed: {type(rollback_exc).__name__}: {rollback_exc}"
                return_code = 3
        else:
            return_code = 3
    finally:
        report.finished_at = utc_now()
        write_report(report_path, report)
    return return_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
