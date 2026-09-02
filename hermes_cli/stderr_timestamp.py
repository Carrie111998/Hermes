"""Run a child process while prefixing each stderr line with a timestamp."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Sequence, TextIO

EXTERNAL_SUPERVISOR_FLAG = "--external-supervisor"


_TIMESTAMP_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}(?:\s|$)"
)


def _timestamp() -> str:
    """Match logging.Formatter's default ``%(asctime)s`` timestamp shape."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:23]


def _write_timestamped_line(log_file: TextIO, line: str) -> None:
    rendered = line.rstrip("\r\n")
    prefix = "" if _TIMESTAMP_PREFIX.match(rendered) else f"{_timestamp()} "
    log_file.write(f"{prefix}{rendered}\n")
    log_file.flush()


# Size-capped rotation for gateway.error.log (t_57aac3e7 fix 1).
# launchd's StandardErrorPath writes without rotation; if the same bind
# error repeats ~3/min (24k lines observed), the file grows without bound.
# These defaults match the 5 MiB / 3-backup shape used by hermes_logging
# for agent.log, scaled slightly for the lower-volume error log.
_ERROR_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_ERROR_LOG_BACKUP_COUNT = 3
# oMLX Application Support log (t_57aac3e7 fix 1) — same treatment for the
# 1.4 GB unrotated oMLX server.log.
_OMLX_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_OMLX_LOG_BACKUP_COUNT = 3


def _rotate_error_log_if_needed(
    log_path: Path,
    max_bytes: int = _ERROR_LOG_MAX_BYTES,
    backup_count: int = _ERROR_LOG_BACKUP_COUNT,
    force: bool = False,
) -> None:
    """Rotate *log_path* when it exceeds *max_bytes* (best-effort, no throw).

    ``force=True`` rotates unconditionally (used by the one-shot
    ``hermes doctor logs`` rotator); the default size-gated behaviour is
    unchanged for the live wrapper path.
    """
    try:
        if not log_path.exists():
            return
        if not force and log_path.stat().st_size <= max_bytes:
            return
        if backup_count <= 0:
            log_path.unlink()
            return
        oldest = log_path.with_suffix(log_path.suffix + f".{backup_count}")
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for gen in range(backup_count - 1, 0, -1):
            src = log_path.with_suffix(log_path.suffix + f".{gen}")
            if not src.exists():
                continue
            try:
                src.rename(log_path.with_suffix(log_path.suffix + f".{gen + 1}"))
            except OSError:
                pass
        log_path.rename(log_path.with_suffix(log_path.suffix + ".1"))
    except OSError:
        pass


def _get_external_log_rotation_config(log_path: Path) -> tuple[int, int]:
    """Return (max_bytes, backup_count) for *log_path*.

    Recognizes the oMLX Application Support log by path substring and
    applies its larger cap; all other external logs (gateway.error.log,
    dashboard.error.log) use the error-log defaults. Future external logs
    get error-log defaults — the right failure mode is conservative caps,
    not unbounded growth.
    """
    p = str(log_path)
    if "oMLX" in p or "omlx" in p:
        return _OMLX_LOG_MAX_BYTES, _OMLX_LOG_BACKUP_COUNT
    return _ERROR_LOG_MAX_BYTES, _ERROR_LOG_BACKUP_COUNT


def _copy_stderr_with_timestamps(stderr: BinaryIO, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes, backup_count = _get_external_log_rotation_config(log_path)
    # Check once at open time — if the file is already oversized (leftover
    # from a previous run that never rotated), rotate before appending.
    _rotate_error_log_if_needed(log_path, max_bytes, backup_count)
    log_file: TextIO | None = None
    try:
        log_file = log_path.open("a", encoding="utf-8", buffering=1)
        for raw_line in iter(stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace")
            _write_timestamped_line(log_file, line)
            # Size check after each line — cheap (buffered tell) and catches
            # the tight bind-error loop (24k lines) within one rotation window.
            try:
                if log_file.tell() > max_bytes:
                    log_file.flush()
                    log_file.close()
                    log_file = None
                    _rotate_error_log_if_needed(log_path, max_bytes, backup_count)
                    log_file = log_path.open("a", encoding="utf-8", buffering=1)
            except OSError:
                break
    finally:
        if log_file is not None:
            try:
                log_file.close()
            except Exception:
                pass


def _command_exit_code(returncode: int) -> int:
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def _install_signal_forwarders(proc: subprocess.Popen[bytes]) -> dict[int, object]:
    def _forward(signum: int, _frame: object) -> None:
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    previous: dict[int, object] = {}
    for signum in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
        if signum is not None:
            try:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, _forward)
            except (OSError, RuntimeError, ValueError):
                previous.pop(signum, None)
    return previous


def _restore_signal_handlers(previous: dict[int, object]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _is_launchd_supervised(environ: Mapping[str, str] | None = None) -> bool:
    """True when this process is launchd's direct child (not an interactive shell)."""
    env = os.environ if environ is None else environ
    xpc_service = str(env.get("XPC_SERVICE_NAME", "")).strip()
    return bool(xpc_service and xpc_service != "0")


def _is_hermes_gateway_run_argv(command: Sequence[str]) -> bool:
    """True for Hermes ``gateway run`` argv this wrapper is allowed to upgrade.

    The wrapper is generic. Only historical/current Hermes gateway shapes
    get ``--external-supervisor``; an arbitrary launchd child must not be
    marked as gateway-supervised (#87005).
    """
    try:
        from gateway.status import looks_like_gateway_command_line
    except Exception:
        return False
    return bool(looks_like_gateway_command_line(" ".join(str(part) for part in command)))


def _with_external_supervisor_flag(command: Sequence[str]) -> list[str]:
    argv = [str(part) for part in command]
    if EXTERNAL_SUPERVISOR_FLAG not in argv:
        argv.append(EXTERNAL_SUPERVISOR_FLAG)
    return argv


def _prepare_child_command(
    command: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Return the argv to exec, upgrading stale launchd-wrapped gateway commands.

    launchd stamps ``XPC_SERVICE_NAME=<job label>`` only on this wrapper.
    The grandchild sees ``XPC_SERVICE_NAME=0``. Newly generated plists put
    ``--external-supervisor`` on the inner ``gateway run`` so ``hermes update``
    can see the flag on the live process argv. Stale plists still wrap the
    historical ``gateway run --replace`` shape without that flag; append it
    here, and only for that shape.
    """
    argv = [str(part) for part in command]
    if not _is_launchd_supervised(environ):
        return argv
    if not _is_hermes_gateway_run_argv(argv):
        return argv
    return _with_external_supervisor_flag(argv)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command and timestamp each stderr line into a log file."
    )
    parser.add_argument("--error-log", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing command after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    log_path: Path = args.error_log

    try:
        proc = subprocess.Popen(
            _prepare_child_command(args.command),
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
            _write_timestamped_line(
                log_file,
                f"failed to start stderr-timestamped command: {exc}",
            )
        return 127

    assert proc.stderr is not None
    previous_handlers = _install_signal_forwarders(proc)
    try:
        _copy_stderr_with_timestamps(proc.stderr, log_path)
    finally:
        proc.stderr.close()
        _restore_signal_handlers(previous_handlers)
    return _command_exit_code(proc.wait())


if __name__ == "__main__":
    sys.exit(main())
