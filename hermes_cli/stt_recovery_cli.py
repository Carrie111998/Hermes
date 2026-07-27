"""Built-in ``hermes stt recovery`` commands."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Optional

from agent.redact import redact_sensitive_text
from hermes_cli.config import load_config
from hermes_cli.stt_recovery import (
    RecoveryRecord,
    SttRecoveryCache,
    recovery_expiry_iso,
)


def register_cli(subparsers) -> None:
    """Attach the STT recovery command tree to the top-level parser."""
    stt_parser = subparsers.add_parser(
        "stt",
        help="Manage speech-to-text configuration and recovery",
    )
    stt_subparsers = stt_parser.add_subparsers(dest="stt_command")
    recovery_parser = stt_subparsers.add_parser(
        "recovery",
        help="Recover desktop recordings retained after STT failures",
    )
    recovery_subparsers = recovery_parser.add_subparsers(dest="recovery_command")

    list_parser = recovery_subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="List retained recordings",
    )
    list_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    list_parser.set_defaults(func=_cmd_list)

    retry_parser = recovery_subparsers.add_parser(
        "retry",
        help="Retry transcription and remove the recording after output is delivered",
    )
    retry_parser.add_argument(
        "recovery_id", help="Opaque recovery ID from the STT error"
    )
    retry_parser.set_defaults(func=_cmd_retry)

    save_parser = recovery_subparsers.add_parser(
        "save",
        help="Export the original recording without changing the retained copy",
    )
    save_parser.add_argument(
        "recovery_id", help="Opaque recovery ID from the STT error"
    )
    save_parser.add_argument("output", help="Destination file or existing directory")
    save_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing destination file atomically",
    )
    save_parser.set_defaults(func=_cmd_save)

    discard_parser = recovery_subparsers.add_parser(
        "discard",
        aliases=["rm"],
        help="Permanently delete one retained recording",
    )
    discard_parser.add_argument(
        "recovery_id", help="Opaque recovery ID from the STT error"
    )
    discard_parser.set_defaults(func=_cmd_discard)

    def _show_help(_args: argparse.Namespace) -> int:
        recovery_parser.print_help()
        return 0

    recovery_parser.set_defaults(func=_show_help)
    stt_parser.set_defaults(func=_show_help)


def _cache() -> SttRecoveryCache:
    return SttRecoveryCache.from_config(load_config())


def _record_payload(record: RecoveryRecord) -> dict[str, object]:
    return {
        "recovery_id": record.recovery_id,
        "status": record.status,
        "created_at": datetime
        .fromtimestamp(
            record.created_at,
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "expires_at": recovery_expiry_iso(record),
        "byte_size": record.byte_size,
        "mime_type": record.mime_type,
        "attempts": record.attempts,
        "provider": record.provider,
        "failure_code": record.failure_code,
    }


def _cmd_list(args: argparse.Namespace) -> int:
    records = _cache().list_records()
    if args.json:
        print(json.dumps([_record_payload(record) for record in records], indent=2))
        return 0
    if not records:
        print("No retained STT recordings.")
        return 0

    print("RECOVERY ID                      STATUS        SIZE       EXPIRES (UTC)")
    for record in records:
        size = _human_bytes(record.byte_size)
        print(
            f"{record.recovery_id}  {record.status:<12}  {size:>9}  "
            f"{recovery_expiry_iso(record)}"
        )
    return 0


def _cmd_retry(args: argparse.Namespace) -> int:
    cache = _cache()
    record = cache.claim_retry(args.recovery_id)
    if record is None:
        print(
            "Recovery entry was not found, expired, or is already being retried.",
            file=sys.stderr,
        )
        return 1

    try:
        from tools.voice_mode import transcribe_recording

        result = transcribe_recording(str(record.audio_path))
        if not isinstance(result, Mapping):
            raise TypeError("STT provider returned an invalid result")
        result = dict(result)
        raw_transcript = result.get("transcript")
        if raw_transcript is not None and not isinstance(raw_transcript, str):
            raise TypeError("STT provider returned an invalid transcript")
        raw_provider = result.get("provider")
        provider = (
            raw_provider
            if isinstance(raw_provider, str)
            and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", raw_provider)
            else None
        )
    except (KeyboardInterrupt, SystemExit):
        cache.mark_failed_attempt(
            record.recovery_id,
            attempts=record.attempts,
            failure_code="retry_interrupted",
        )
        raise
    except Exception as exc:
        cache.mark_failed_attempt(
            record.recovery_id,
            attempts=record.attempts,
            failure_code="unexpected_error",
        )
        safe_error = redact_sensitive_text(str(exc), force=True)
        print(f"Transcription retry failed: {safe_error}", file=sys.stderr)
        print("The original recording is still retained.", file=sys.stderr)
        return 1

    if result.get("success") is not True:
        cache.mark_failed_attempt(
            record.recovery_id,
            attempts=record.attempts,
            failure_code="provider_error",
            provider=provider,
        )
        raw_error = result.get("error")
        safe_error = redact_sensitive_text(
            raw_error if isinstance(raw_error, str) else "Transcription failed",
            force=True,
        )
        print(f"Transcription retry failed: {safe_error}", file=sys.stderr)
        print("The original recording is still retained.", file=sys.stderr)
        return 1

    transcript = (result.get("transcript") or "").strip()
    if not transcript:
        cache.mark_failed_attempt(
            record.recovery_id,
            attempts=record.attempts,
            failure_code="no_speech",
            provider=provider,
        )
        print(
            "The retry returned no speech; the original recording is still retained.",
            file=sys.stderr,
        )
        return 1

    # Keep the immutable input in a non-evictable, non-claimable state while
    # stdout receives the only transcript copy. A hard process crash is
    # detected by the released OS lease; ordinary delivery failures become
    # immediately retryable below.
    delivery = cache.mark_delivering_attempt(
        record.recovery_id,
        attempts=record.attempts,
    )
    delivery_status = "delivering" if delivery is not None else "transcribing"
    try:
        print(transcript)
        # Deletion is the commit boundary: if stdout is a broken pipe or
        # otherwise cannot deliver the transcript, flush raises and the
        # original survives in a retryable state.
        sys.stdout.flush()
    except BaseException:
        cache.mark_failed_attempt(
            record.recovery_id,
            attempts=record.attempts,
            expected_status=delivery_status,
            failure_code="delivery_interrupted",
            provider=provider,
        )
        raise
    if not cache.complete_attempt(
        record.recovery_id,
        attempts=record.attempts,
        expected_status=delivery_status,
    ):
        cache.mark_failed_attempt(
            record.recovery_id,
            attempts=record.attempts,
            expected_status=delivery_status,
            failure_code="cleanup_error",
            provider=provider,
        )
        print(
            "Warning: transcription succeeded, but cleanup could not be committed; "
            "the recording remains retained.",
            file=sys.stderr,
        )
    return 0


def _cmd_save(args: argparse.Namespace) -> int:
    record = _cache().get_record(args.recovery_id)
    if record is None:
        print("Recovery entry was not found or has expired.", file=sys.stderr)
        return 1

    destination = Path(args.output).expanduser()
    if destination.is_dir():
        destination = (
            destination / f"stt-{record.recovery_id}{record.audio_path.suffix}"
        )
    try:
        _copy_private_file(record.audio_path, destination, force=bool(args.force))
    except FileExistsError:
        print(f"Destination already exists: {destination}", file=sys.stderr)
        print("Pass --force to replace it atomically.", file=sys.stderr)
        return 1
    except OSError as exc:
        safe_error = redact_sensitive_text(str(exc), force=True)
        print(f"Could not save recording: {safe_error}", file=sys.stderr)
        return 1

    print(f"Saved recording to {destination}")
    return 0


def _cmd_discard(args: argparse.Namespace) -> int:
    if not _cache().discard_failed(args.recovery_id):
        print("Recovery entry was not found or has expired.", file=sys.stderr)
        return 1
    print(f"Discarded STT recovery {args.recovery_id}.")
    return 0


def _copy_private_file(source: Path, destination: Path, *, force: bool) -> None:
    """Copy a validated recovery audio file with no world-readable window."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise FileExistsError(destination)

    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    temp_path: Optional[Path] = None
    output_fd = -1
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise OSError("Recovery audio is not a regular file")

        if force:
            output_fd, raw_temp_path = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=str(destination.parent),
            )
            temp_path = Path(raw_temp_path)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            output_fd = os.open(destination, flags, 0o600)

        if hasattr(os, "fchmod"):
            os.fchmod(output_fd, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                view = view[written:]
        os.fsync(output_fd)
        os.close(output_fd)
        output_fd = -1
        if temp_path is not None:
            os.replace(temp_path, destination)
            temp_path = None
    finally:
        os.close(source_fd)
        if output_fd >= 0:
            os.close(output_fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"
