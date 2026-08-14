"""Flush pending messages and agent transcripts to disk before shutdown to prevent data loss.

When FTS5 index corruption prevents ``INSERT INTO messages``, the gateway
accumulates messages in ``_pending_messages`` (memory-only) and the live
``agent._session_messages`` cannot be flushed via ``_flush_messages_to_session_db``.
On shutdown, ``.clear()`` discards the only surviving copy — permanent user data loss.

This module provides three hooks:

1. ``flush_pending_to_file()`` — called BEFORE ``_pending_messages.clear()``
   during shutdown.  Serialises any non-empty pending slots to a JSON file
   under ``<hermes_home>/pending_messages/``.

2. ``recover_pending_to_db()`` — called AFTER ``runner.start()`` on startup.
   Reads flush files, inserts messages into state.db via ``SessionDB.append_message``
   (so FTS indexing, session metadata, and display_kind are handled correctly),
   then deletes the flush file on success.

3. ``flush_agent_history_to_file()`` — called from ``_finalize_shutdown_agents``
   when ``_flush_messages_to_session_db`` raises.  Dumps the live
   ``agent._session_messages`` to the same atomic JSON recovery directory.

See issue #72680 for the full incident report.
"""

from __future__ import annotations

import itertools
import hashlib
import inspect
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


_PENDING_SPOOL_WRITER_ID = uuid.uuid4().hex
_PENDING_SPOOL_SEQ = itertools.count()
_PENDING_SPOOL_ORDER_LOCK = threading.RLock()
_PENDING_SPOOL_LOCK_STATE = threading.local()
_PENDING_SPOOL_LAST_CREATED_NS = 0

# File locks serialize allocation, publication, scanning, durable append, and
# cleanup across gateway processes that share one HERMES_HOME. The in-process
# RLock is still required because POSIX flock semantics alone are not a thread
# mutex. Unsupported platforms fail closed instead of risking reordering.
_fcntl = None
_msvcrt = None
if os.name == "posix":  # pragma: no branch - exactly one platform path runs
    try:  # pragma: no cover - import outcome is platform-specific
        import fcntl as _fcntl
    except ImportError:
        pass
elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
    try:
        import msvcrt as _msvcrt
    except ImportError:
        pass

_PENDING_SPOOL_LOCK_NAME = ".spool.lock"
_PENDING_SPOOL_ORDER_STATE_NAME = ".spool-order"
_PENDING_SPOOL_FORMAT_STATE_NAME = ".spool-format"
_PENDING_SPOOL_FORMAT_VERSION = 4


def _get_flush_dir():
    """Return the pending-messages flush directory under the active HERMES_HOME."""
    from hermes_constants import get_hermes_home

    flush_dir = get_hermes_home() / "pending_messages"
    flush_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(flush_dir, 0o700)
    return flush_dir


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry on platforms that support directory fsync."""
    if os.name != "posix":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def _pending_spool_lock(flush_dir: Path):
    """Hold the shared spool transaction lock across process and thread peers."""
    if _fcntl is None and _msvcrt is None:
        raise RuntimeError("cross-process pending-spool locking is unavailable")

    lock_path = flush_dir / _PENDING_SPOOL_LOCK_NAME
    with _PENDING_SPOOL_ORDER_LOCK:
        lock_key = str(lock_path.absolute())
        held_paths = getattr(_PENDING_SPOOL_LOCK_STATE, "held_paths", None)
        if held_paths is None:
            held_paths = set()
            _PENDING_SPOOL_LOCK_STATE.held_paths = held_paths
        if lock_key in held_paths:
            # The outer entry already owns both the process RLock and the OS
            # lock. Opening and flocking a second descriptor self-deadlocks.
            yield
            return

        if _msvcrt is not None and (
            not lock_path.exists() or lock_path.stat().st_size == 0
        ):
            lock_path.write_text(" ", encoding="utf-8")
        lock_file = open(
            lock_path,
            "r+" if _msvcrt is not None else "a+",
            encoding="utf-8",
        )
        try:
            if os.name == "posix":
                os.chmod(lock_path, 0o600)
            if _fcntl is not None:
                _fcntl.flock(lock_file, _fcntl.LOCK_EX)
            else:
                assert _msvcrt is not None
                lock_file.seek(0)
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_LOCK, 1)
            held_paths.add(lock_key)
            yield
        finally:
            held_paths.discard(lock_key)
            try:
                if _fcntl is not None:
                    _fcntl.flock(lock_file, _fcntl.LOCK_UN)
                else:
                    assert _msvcrt is not None
                    lock_file.seek(0)
                    _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)
            except (OSError, IOError):
                pass
            lock_file.close()


def _pending_publication_in_progress(flush_dir: Path) -> bool:
    """Whether an older atomic writer has a not-yet-published payload."""
    # atomic_json_write(pending-<id>.json, ...) reserves a visible
    # .pending-<id>_<random>.tmp in this directory. Previous versions do not
    # acquire _pending_spool_lock, so this is the cross-version handoff fence.
    return any(flush_dir.glob(".pending-*.tmp"))


def _pending_payload_digest(payload: Dict[str, Any]) -> str:
    """Canonical semantic digest for a durable pending payload."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def _legacy_migration_spool_id(filename: str, digest: str) -> str:
    return hashlib.sha256(
        f"legacy-migration\0{filename}\0{digest}".encode("utf-8")
    ).hexdigest()


def _legacy_receipt_name(spool_id: str) -> str:
    return f".legacy-receipt-{spool_id}"


def _pending_path_identity(path: Path) -> Dict[str, int]:
    """Filesystem identity used to distinguish pathname reuse from the source."""
    stat_result = path.stat()
    return {
        "device": int(stat_result.st_dev),
        "inode": int(stat_result.st_ino),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "ctime_ns": int(stat_result.st_ctime_ns),
    }


def _pending_path_matches_source_identity(
    path: Path,
    source_identity: Dict[str, int],
    *,
    quarantined: bool = False,
) -> bool:
    current = _pending_path_identity(path)
    fields = (
        ("device", "inode", "size", "mtime_ns")
        if quarantined
        else ("device", "inode", "size", "mtime_ns", "ctime_ns")
    )
    return all(current[field] == source_identity[field] for field in fields)


def _read_stable_legacy_payload(path: Path) -> tuple[Dict[str, Any], Dict[str, int]]:
    """Read one legacy object only when its filesystem identity stays stable."""
    before = _pending_path_identity(path)
    payload = _read_pending_json(path)
    after = _pending_path_identity(path)
    if before != after:
        raise PendingSpoolOrderError(
            f"legacy payload {path.name!r} changed while establishing cutover"
        )
    return payload, before


def _read_pending_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PendingSpoolOrderError(
            f"cannot parse pending payload {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise PendingSpoolOrderError(f"pending payload {path} is not an object")
    return payload


def _write_pending_spool_format_state(flush_dir: Path, state: Dict[str, Any]) -> None:
    """Durably publish the monotonic legacy-to-current migration ledger."""
    from utils import atomic_json_write

    atomic_json_write(
        flush_dir / _PENDING_SPOOL_FORMAT_STATE_NAME,
        state,
        mode=0o600,
        default=str,
    )
    _fsync_directory(flush_dir)


def _write_pending_cutover_tombstone(flush_dir: Path) -> None:
    """Replicate cutover establishment into the independent order sidecar."""
    from utils import atomic_json_write

    order_path = flush_dir / _PENDING_SPOOL_ORDER_STATE_NAME
    last_order = 0
    try:
        order_state = json.loads(order_path.read_text(encoding="utf-8"))
        if not isinstance(order_state, dict):
            raise ValueError("order state is not an object")
        last_order = int(order_state.get("last_order", 0))
        if last_order < 0:
            raise ValueError("negative last_order")
    except FileNotFoundError:
        pass
    except Exception as exc:
        raise PendingSpoolOrderError(
            "cannot establish pending-spool cutover tombstone"
        ) from exc
    atomic_json_write(
        order_path,
        {
            "last_order": last_order,
            "format_cutover": _PENDING_SPOOL_FORMAT_VERSION,
        },
        mode=0o600,
        default=str,
    )
    _fsync_directory(flush_dir)


def _validate_pending_spool_format_state(state: Any) -> Dict[str, Any]:
    if not isinstance(state, dict) or state.get("format") != (
        _PENDING_SPOOL_FORMAT_VERSION
    ):
        raise PendingSpoolOrderError("invalid pending-spool format state")
    legacy_files = state.get("legacy_files")
    if not isinstance(legacy_files, dict):
        raise PendingSpoolOrderError("invalid legacy migration ledger")
    spool_ids: set[str] = set()
    for name, record in legacy_files.items():
        if (
            not isinstance(name, str)
            or not name.endswith(".json")
            or Path(name).name != name
            or not isinstance(record, dict)
        ):
            raise PendingSpoolOrderError("invalid legacy migration record")
        digest = record.get("digest")
        spool_id = record.get("spool_id")
        state_name = record.get("state")
        receipt = record.get("receipt")
        source_identity = record.get("source_identity")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or not isinstance(spool_id, str)
            or len(spool_id) != 64
            or any(character not in "0123456789abcdef" for character in spool_id)
            or spool_id in spool_ids
            or state_name not in {"pending", "committed"}
            or receipt != _legacy_receipt_name(spool_id)
            or not isinstance(source_identity, dict)
            or set(source_identity)
            != {"device", "inode", "size", "mtime_ns", "ctime_ns"}
            or any(
                not isinstance(source_identity[field], int)
                or isinstance(source_identity[field], bool)
                or source_identity[field] < 0
                for field in source_identity
            )
        ):
            raise PendingSpoolOrderError("invalid legacy migration record")
        spool_ids.add(spool_id)
    return state


def _load_pending_spool_format_state(flush_dir: Path) -> Dict[str, Any]:
    state_path = flush_dir / _PENDING_SPOOL_FORMAT_STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PendingSpoolOrderError(
            "cannot read pending-spool format state"
        ) from exc
    return _validate_pending_spool_format_state(state)


def _validate_legacy_migration_payload(
    payload: Dict[str, Any],
    filename: str,
    record: Dict[str, Any],
) -> None:
    spool_meta = payload.get("_spool")
    if not isinstance(spool_meta, dict) or (
        str(spool_meta.get("id") or "") != record["spool_id"]
        or str(spool_meta.get("legacy_source") or "") != filename
        or str(spool_meta.get("legacy_digest") or "") != record["digest"]
    ):
        raise PendingSpoolOrderError("invalid legacy migration payload identity")
    legacy_payload = dict(payload)
    legacy_payload.pop("_spool", None)
    if _pending_payload_digest(legacy_payload) != record["digest"]:
        raise PendingSpoolOrderError("legacy migration payload digest mismatch")


def _publish_legacy_migration_payload(
    flush_dir: Path,
    filename: str,
    record: Dict[str, Any],
    legacy_payload: Dict[str, Any],
) -> Path:
    """Durably copy one captured legacy payload into the current format."""
    from utils import atomic_json_write

    order = _allocate_pending_spool_order(flush_dir)
    migrated = dict(legacy_payload)
    migrated["_spool"] = {
        "id": record["spool_id"],
        "order": order,
        "created_ns": time.time_ns(),
        "writer_id": _PENDING_SPOOL_WRITER_ID,
        "writer_seq": next(_PENDING_SPOOL_SEQ),
        "legacy_source": filename,
        "legacy_digest": record["digest"],
    }
    migration_path = flush_dir / f"pending-{record['spool_id']}.json"
    atomic_json_write(
        migration_path,
        migrated,
        mode=0o600,
        default=str,
    )
    _fsync_directory(flush_dir)
    return migration_path


def _prepare_pending_legacy_migrations(
    flush_dir: Path,
    state: Dict[str, Any],
) -> None:
    """Quarantine each old pathname behind a deterministic current spool.

    The copied current-format file is durable before ``os.replace`` moves the
    legacy pathname. An old writer that publishes the same pathname later can
    therefore never be unlinked as cleanup for the selected older payload.
    """
    legacy_files = state["legacy_files"]
    pending_records = [
        name
        for name, record in legacy_files.items()
        if record["state"] == "pending"
    ]
    if len(pending_records) > 1:
        raise PendingSpoolOrderError(
            "multiple legacy pending payloads have no cross-process ordering "
            "identity; preserving every file"
        )

    for filename, record in legacy_files.items():
        original_path = flush_dir / filename
        migration_path = flush_dir / f"pending-{record['spool_id']}.json"
        receipt_path = flush_dir / record["receipt"]

        if record["state"] == "committed":
            if migration_path.exists():
                migrated = _read_pending_json(migration_path)
                _validate_legacy_migration_payload(migrated, filename, record)
                migration_path.unlink()
                _fsync_directory(flush_dir)
            if receipt_path.exists():
                if not _pending_path_matches_source_identity(
                    receipt_path,
                    record["source_identity"],
                    quarantined=True,
                ):
                    raise PendingSpoolOrderError(
                        "legacy migration receipt filesystem identity changed"
                    )
                receipt = _read_pending_json(receipt_path)
                if _pending_payload_digest(receipt) != record["digest"]:
                    raise PendingSpoolOrderError(
                        "late legacy replacement is preserved in migration receipt"
                    )
                receipt_path.unlink()
                _fsync_directory(flush_dir)
            if original_path.exists():
                raise PendingSpoolOrderError(
                    f"legacy payload {filename!r} appeared after committed cutover"
                )
            continue

        if migration_path.exists():
            migrated = _read_pending_json(migration_path)
            _validate_legacy_migration_payload(migrated, filename, record)
        else:
            source_path = receipt_path if receipt_path.exists() else original_path
            if not source_path.exists():
                raise PendingSpoolOrderError(
                    f"pending legacy migration source {filename!r} is missing"
                )
            if not _pending_path_matches_source_identity(
                source_path,
                record["source_identity"],
                quarantined=source_path == receipt_path,
            ):
                raise PendingSpoolOrderError(
                    f"legacy payload {filename!r} was replaced after cutover"
                )
            legacy_payload = _read_pending_json(source_path)
            if _pending_payload_digest(legacy_payload) != record["digest"]:
                raise PendingSpoolOrderError(
                    f"legacy payload {filename!r} changed after cutover"
                )
            _publish_legacy_migration_payload(
                flush_dir,
                filename,
                record,
                legacy_payload,
            )

        if original_path.exists() and not receipt_path.exists():
            if _pending_path_matches_source_identity(
                original_path,
                record["source_identity"],
            ):
                os.replace(original_path, receipt_path)
                _fsync_directory(flush_dir)
                if not _pending_path_matches_source_identity(
                    receipt_path,
                    record["source_identity"],
                    quarantined=True,
                ):
                    raise PendingSpoolOrderError(
                        "legacy migration receipt identity changed during quarantine"
                    )


def _ensure_pending_spool_format_state(flush_dir: Path) -> Dict[str, Any]:
    """Load or establish the durable legacy-to-current cutover boundary.

    Legacy writers do not acquire ``_pending_spool_lock``. The first current
    operation snapshots and fingerprints the legacy files already visible,
    persists an independent tombstone, and migrates one unambiguous payload to
    a deterministic current-format spool before replay or new publication.
    """
    state_path = flush_dir / _PENDING_SPOOL_FORMAT_STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        order_path = flush_dir / _PENDING_SPOOL_ORDER_STATE_NAME
        if order_path.exists():
            raise PendingSpoolOrderError(
                "pending-spool format state is missing after cutover"
            )
        legacy_files: Dict[str, Any] = {}
        captured_legacy_payloads: Dict[str, Dict[str, Any]] = {}
        current_files = []
        for path in flush_dir.glob("*.json"):
            payload = _read_pending_json(path)
            if payload.get("reason") == "shutdown-with-unpersisted-agent-history":
                # Manual operator snapshots are not automatic replay rows and
                # must remain at their original visible path.
                continue
            if isinstance(payload.get("_spool"), dict):
                current_files.append(path.name)
                continue
            payload, source_identity = _read_stable_legacy_payload(path)
            digest = _pending_payload_digest(payload)
            spool_id = _legacy_migration_spool_id(path.name, digest)
            legacy_files[path.name] = {
                "digest": digest,
                "spool_id": spool_id,
                "state": "pending",
                "receipt": _legacy_receipt_name(spool_id),
                "source_identity": source_identity,
            }
            captured_legacy_payloads[path.name] = payload
        if legacy_files and current_files:
            raise PendingSpoolOrderError(
                "legacy and current payloads predate the cutover marker; "
                "preserving every file"
            )
        state = {
            "format": _PENDING_SPOOL_FORMAT_VERSION,
            "legacy_files": legacy_files,
        }
        if len(legacy_files) == 1:
            filename, record = next(iter(legacy_files.items()))
            # Publish the immutable replay copy before the marker. If the
            # marker write crashes, the order tombstone and copied payload make
            # the partial cutover deletion-detectable and preserve the bytes.
            _publish_legacy_migration_payload(
                flush_dir,
                filename,
                record,
                captured_legacy_payloads[filename],
            )
        _write_pending_spool_format_state(flush_dir, state)
        _write_pending_cutover_tombstone(flush_dir)
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        if isinstance(exc, PendingSpoolOrderError):
            raise
        raise PendingSpoolOrderError(
            "cannot read pending-spool format state"
        ) from exc
    state = _validate_pending_spool_format_state(state)
    order_path = flush_dir / _PENDING_SPOOL_ORDER_STATE_NAME
    if not order_path.exists():
        _write_pending_cutover_tombstone(flush_dir)
    else:
        try:
            order_state = json.loads(order_path.read_text(encoding="utf-8"))
            if (
                not isinstance(order_state, dict)
                or order_state.get("format_cutover")
                != _PENDING_SPOOL_FORMAT_VERSION
            ):
                raise ValueError("missing cutover generation")
        except Exception as exc:
            raise PendingSpoolOrderError(
                "invalid pending-spool cutover tombstone"
            ) from exc
    _prepare_pending_legacy_migrations(flush_dir, state)
    return state


def _mark_pending_legacy_migration_committed(
    flush_dir: Path,
    payload: Dict[str, Any],
) -> None:
    spool_meta = payload.get("_spool")
    if not isinstance(spool_meta, dict) or not spool_meta.get("legacy_source"):
        return
    filename = str(spool_meta["legacy_source"])
    state = _load_pending_spool_format_state(flush_dir)
    record = state["legacy_files"].get(filename)
    if not isinstance(record, dict):
        raise PendingSpoolOrderError("legacy migration ledger entry is missing")
    _validate_legacy_migration_payload(payload, filename, record)
    if record["state"] != "committed":
        record["state"] = "committed"
        _write_pending_spool_format_state(flush_dir, state)


def _finalize_pending_legacy_migration(
    flush_dir: Path,
    payload: Dict[str, Any],
) -> None:
    """Delete only the private receipt; never touch a reused legacy pathname."""
    spool_meta = payload.get("_spool")
    if not isinstance(spool_meta, dict) or not spool_meta.get("legacy_source"):
        return
    filename = str(spool_meta["legacy_source"])
    state = _load_pending_spool_format_state(flush_dir)
    record = state["legacy_files"].get(filename)
    if not isinstance(record, dict) or record["state"] != "committed":
        raise PendingSpoolOrderError("legacy migration is not durably committed")
    receipt_path = flush_dir / record["receipt"]
    if receipt_path.exists():
        if not _pending_path_matches_source_identity(
            receipt_path,
            record["source_identity"],
            quarantined=True,
        ):
            raise PendingSpoolOrderError(
                "legacy migration receipt filesystem identity changed"
            )
        receipt = _read_pending_json(receipt_path)
        if _pending_payload_digest(receipt) != record["digest"]:
            raise PendingSpoolOrderError(
                "late legacy replacement is preserved in migration receipt"
            )
        receipt_path.unlink()
        _fsync_directory(flush_dir)


def _pending_requires_idempotency(payload: Dict[str, Any]) -> bool:
    spool_meta = payload.get("_spool")
    if not isinstance(spool_meta, dict):
        return False
    return (
        not spool_meta.get("legacy_source")
        or payload.get("reason") == TRANSCRIPT_CAP_DROP_REASON
    )


def _allocate_pending_spool_order(flush_dir: Path) -> int:
    """Allocate a durable order value while ``_pending_spool_lock`` is held."""
    from utils import atomic_json_write

    state_path = flush_dir / _PENDING_SPOOL_ORDER_STATE_NAME
    last_order = 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        last_order = int(state.get("last_order", 0))
        if last_order < 0:
            raise ValueError("negative last_order")
    except FileNotFoundError:
        pass
    except Exception as exc:
        # Rebuild from every still-pending payload. Gaps are harmless; reuse is
        # not. A corrupt sidecar must never force a lower order than a file.
        logger.warning("Rebuilding corrupt pending-spool order state: %s", exc)
        last_order = 0

    for path in flush_dir.glob("pending-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            spool_meta = payload.get("_spool")
            if isinstance(spool_meta, dict):
                value = spool_meta.get("order", spool_meta.get("created_ns", 0))
                observed = int(value)
            else:
                observed = int(float(payload.get("ts", 0)) * 1_000_000_000)
            last_order = max(last_order, observed)
        except Exception:
            # Recovery will preserve malformed files. They provide no trusted
            # numeric floor, so the persisted state and wall clock remain the
            # allocator authority for the new payload.
            continue

    order = max(last_order + 1, time.time_ns())
    atomic_json_write(
        state_path,
        {
            "last_order": order,
            "format_cutover": _PENDING_SPOOL_FORMAT_VERSION,
        },
        mode=0o600,
        default=str,
    )
    return order


def _write_payload(flush_dir: Path, payload: Dict[str, Any]) -> Path:
    """Atomically write one private, uniquely named recovery payload.

    Returns the path of the published payload file.
    """
    from utils import atomic_json_write

    global _PENDING_SPOOL_LAST_CREATED_NS

    with _pending_spool_lock(flush_dir):
        try:
            _ensure_pending_spool_format_state(flush_dir)
        except PendingSpoolOrderError as exc:
            # A fail-closed replay barrier must not turn a runtime cap eviction
            # into data loss. Publish the younger current-format payload behind
            # the barrier; recovery will continue to preserve every file until
            # the older ambiguity is resolved.
            logger.warning(
                "Publishing pending payload behind an existing recovery "
                "barrier: %s",
                exc,
            )
        order = _allocate_pending_spool_order(flush_dir)
        created_ns = time.time_ns()
        _PENDING_SPOOL_LAST_CREATED_NS = max(
            _PENDING_SPOOL_LAST_CREATED_NS,
            created_ns,
        )
        writer_seq = next(_PENDING_SPOOL_SEQ)
        file_id = hashlib.sha256(
            (
                f"current-spool\0{uuid.uuid4().hex}\0{order}\0"
                f"{_PENDING_SPOOL_WRITER_ID}\0{writer_seq}"
            ).encode("utf-8")
        ).hexdigest()
        payload = dict(payload)
        payload["_spool"] = {
            "id": file_id,
            "order": order,
            "created_ns": created_ns,
            "writer_id": _PENDING_SPOOL_WRITER_ID,
            "writer_seq": writer_seq,
        }
        final_path = flush_dir / f"pending-{file_id}.json"
        if final_path.exists():
            raise PendingSpoolOrderError(
                "allocated pending-message replay identity already exists"
            )
        atomic_json_write(
            final_path,
            payload,
            mode=0o600,
            default=str,
        )

        try:
            _fsync_directory(flush_dir)
        except OSError as exc:
            # The atomically published file is still the only recovery copy.
            # Keep it even if this filesystem cannot persist directory entries.
            logger.debug("Failed to fsync pending-message directory: %s", exc)
        return final_path


def flush_pending_to_file(
    pending: Dict[str, Any],
    *,
    reason: str = "shutdown",
) -> int:
    """Serialise non-empty ``_pending_messages`` slots to disk.

    Parameters
    ----------
    pending:
        The adapter or runner ``_pending_messages`` dict.  Values may be
        ``MessageEvent`` objects (adapter) or plain strings (runner).
    reason:
        Logged context (``shutdown``, ``restart``, etc.).

    Returns
    -------
    int
        Number of sessions flushed.
    """
    if not pending:
        return 0

    flush_dir = _get_flush_dir()
    ts = int(time.time())
    flushed = 0

    for session_key, value in list(pending.items()):
        if value is None:
            continue
        try:
            serialised = _serialise_value(value)
            if serialised is None:
                continue
            _write_payload(
                flush_dir,
                {
                    "session_key": session_key,
                    "reason": reason,
                    "ts": ts,
                    "data": serialised,
                },
            )
            flushed += 1
        except Exception as exc:
            logger.debug(
                "Failed to flush pending message for %s: %s",
                session_key, exc,
            )

    if flushed:
        logger.info(
            "Flushed %d pending message(s) to %s (reason=%s)",
            flushed, flush_dir, reason,
        )
    return flushed


# Reason tag for transcript messages dropped by the in-memory pending cap
# during live operation (#78182). These payloads carry the full transcript
# message dict so they can be replayed verbatim once the DB recovers.
TRANSCRIPT_CAP_DROP_REASON = "transcript_cap_drop"


def spool_dropped_transcript_message(
    session_id: str,
    message: Dict[str, Any],
) -> Optional[Path]:
    """Spool a transcript message evicted by the runtime pending cap.

    Uses the same on-disk pending spool as :func:`flush_pending_to_file`
    (one atomic JSON payload per message under
    ``<hermes_home>/pending_messages/``), so a runtime cap rotation no
    longer silently discards user data while the process stays up
    (#78182).

    Returns the written spool path, or ``None`` when spooling failed —
    callers must degrade to the previous drop-and-log behaviour.
    """
    try:
        flush_dir = _get_flush_dir()
        return _write_payload(
            flush_dir,
            {
                "session_key": session_id,
                "reason": TRANSCRIPT_CAP_DROP_REASON,
                "ts": int(time.time()),
                "seq": next(_TRANSCRIPT_SPOOL_SEQ),
                "data": {
                    "session_id": session_id,
                    "message": message,
                },
            },
        )
    except Exception as exc:
        logger.debug(
            "Failed to spool cap-dropped transcript message for %s: %s",
            session_id, exc,
        )
        return None


# Monotonic tiebreaker so same-second spool files replay in drop order.
_TRANSCRIPT_SPOOL_SEQ = itertools.count()


class PendingSpoolOrderError(RuntimeError):
    """Pending payloads cannot be placed in one trustworthy total order."""


class PendingSpoolTargetError(RuntimeError):
    """A failed row's source/live-tip equivalence class is unknowable."""


def _pending_payload_order(payload: Dict[str, Any]) -> tuple[int, str, str, int]:
    """Return durable ordering metadata, including legacy payload support."""
    spool_meta = payload.get("_spool")
    if isinstance(spool_meta, dict):
        spool_id = str(spool_meta.get("id") or "")
        writer_id = str(spool_meta.get("writer_id") or "")
        try:
            writer_seq = int(spool_meta["writer_seq"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PendingSpoolOrderError("invalid _spool ordering metadata") from exc
        if writer_seq < 0 or not writer_id or not spool_id:
            raise PendingSpoolOrderError("invalid _spool ordering metadata")

        if "order" in spool_meta:
            try:
                durable_order = int(spool_meta["order"])
            except (TypeError, ValueError) as exc:
                raise PendingSpoolOrderError(
                    "invalid durable _spool order"
                ) from exc
            if durable_order <= 0:
                raise PendingSpoolOrderError("invalid durable _spool order")
            return durable_order, "current", writer_id, writer_seq

        # Transitional payloads from the first chronology fix predate the
        # durable allocator. Their order is trustworthy only within one writer
        # lifetime; _order_pending_entries enforces that restriction.
        try:
            created_ns = int(spool_meta["created_ns"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PendingSpoolOrderError("invalid _spool ordering metadata") from exc
        if created_ns < 0:
            raise PendingSpoolOrderError("invalid _spool ordering metadata")
        return created_ns, "transitional", writer_id, writer_seq

    # Pre-fix files had only second-resolution time plus a process-local
    # counter. Only one legacy file can be replayed automatically: once more
    # than one process lifetime may be represented, neither timestamp nor seq
    # proves their relative creation order.
    try:
        created_ns = int(float(payload.get("ts", 0)) * 1_000_000_000)
        writer_seq = int(payload.get("seq", 0))
    except (TypeError, ValueError) as exc:
        raise PendingSpoolOrderError("invalid legacy spool ordering metadata") from exc
    return created_ns, "legacy", "", writer_seq


def _order_pending_entries(entries):
    """Sort ``(path, payload)`` entries or fail closed on an ordering tie."""
    decorated = []
    by_created_ns: Dict[int, list] = {}
    spool_ids: set[str] = set()
    for path, payload in entries:
        order = _pending_payload_order(payload)
        spool_meta = payload.get("_spool")
        if isinstance(spool_meta, dict):
            spool_id = str(spool_meta.get("id") or "")
            if spool_id in spool_ids:
                raise PendingSpoolOrderError(
                    f"duplicate pending spool id {spool_id!r}; preserving every file"
                )
            spool_ids.add(spool_id)
        item = (order, path, payload)
        decorated.append(item)
        by_created_ns.setdefault(order[0], []).append(item)

    kinds = {item[0][1] for item in decorated}
    if "legacy" in kinds and len(decorated) > 1:
        raise PendingSpoolOrderError(
            "multiple legacy pending payloads have no cross-process ordering "
            "identity; preserving every file"
        )
    if "transitional" in kinds:
        transitional_writers = {
            item[0][2] for item in decorated if item[0][1] == "transitional"
        }
        if kinds != {"transitional"} or len(transitional_writers) != 1:
            raise PendingSpoolOrderError(
                "transitional pending payload order crosses writer lifetimes; "
                "preserving every file"
            )

    for created_ns, group in by_created_ns.items():
        if len(group) < 2:
            continue
        group_kinds = {item[0][1] for item in group}
        writers = {item[0][2] for item in group}
        sequences = [item[0][3] for item in group]
        if (
            group_kinds == {"transitional"}
            and len(writers) == 1
            and len(sequences) == len(set(sequences))
        ):
            continue
        raise PendingSpoolOrderError(
            "ambiguous pending payload order at created_ns="
            f"{created_ns}; preserving every file"
        )

    decorated.sort(key=lambda item: (item[0][0], item[0][3]))
    return [(path, payload) for _order, path, payload in decorated]


def _read_ordered_pending_entries(paths):
    entries = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PendingSpoolOrderError(
                f"cannot parse pending payload {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise PendingSpoolOrderError(
                f"pending payload {path} is not an object"
            )
        # Agent-history snapshots are manual operator artifacts, not rows for
        # automatic insertion, so they do not participate in replay ordering.
        if payload.get("reason") == "shutdown-with-unpersisted-agent-history":
            continue
        entries.append((path, payload))
    if entries:
        format_state = _ensure_pending_spool_format_state(entries[0][0].parent)
        legacy_records = format_state["legacy_files"]
        current_spool_ids = {
            str(payload["_spool"].get("id") or "")
            for _path, payload in entries
            if isinstance(payload.get("_spool"), dict)
        }
        filtered_entries = []
        late_legacy = []
        for path, payload in entries:
            if isinstance(payload.get("_spool"), dict):
                filtered_entries.append((path, payload))
                continue
            record = legacy_records.get(path.name)
            if (
                isinstance(record, dict)
                and record["state"] == "pending"
                and record["spool_id"] in current_spool_ids
            ):
                # The immutable migration copy is the replay authority. A
                # legacy writer may have reused the old pathname after it was
                # quarantined; preserve that file, replay the older copy first,
                # then let the committed-state check block the late payload.
                continue
            late_legacy.append(path.name)
        if late_legacy:
            raise PendingSpoolOrderError(
                "legacy payload appeared after the current-format cutover: "
                + ", ".join(sorted(late_legacy))
            )
        entries = filtered_entries
    return _order_pending_entries(entries)


def _assert_pending_replay_barrier_clear(flush_dir: Path) -> None:
    """Re-scan while locked so a late old writer cannot bypass a static scan."""
    try:
        if _pending_publication_in_progress(flush_dir):
            raise PendingSpoolOrderError(
                "a pending payload publication appeared during recovery"
            )
        _read_ordered_pending_entries(list(flush_dir.glob("*.json")))
    except PendingSpoolOrderError:
        raise
    except Exception as exc:
        raise PendingSpoolOrderError(
            "cannot revalidate the pending-message ordering barrier"
        ) from exc


def _pending_replay_key(path: Path, payload: Dict[str, Any]) -> str:
    """Stable identity used to collapse append-success/unlink-failure retries."""
    spool_meta = payload.get("_spool")
    spool_id = (
        str(spool_meta.get("id") or "")
        if isinstance(spool_meta, dict)
        else ""
    )
    if spool_id:
        return f"pending-spool:{spool_id}"
    return f"pending-legacy:{_pending_payload_digest(payload)}"


def _drain_transcript_spools_unlocked(
    session_ids,
    replay,
    *,
    target_session_id: Optional[str] = None,
    resolve_target=None,
) -> tuple[int, int, set[str]]:
    """Globally merge and replay cap-dropped rows for several source sessions.

    ``replay(source_session_id, message_dict, replay_key)`` must make the
    append idempotent with ``replay_key``. The file remains the durable retry
    record until unlink succeeds.
    """
    source_ids = {str(session_id) for session_id in session_ids if session_id}
    target_session_id = str(target_session_id or "")
    if not source_ids and not (target_session_id and callable(resolve_target)):
        return 0, 0, set()
    flush_dir = _get_flush_dir()
    if _pending_publication_in_progress(flush_dir):
        logger.warning(
            "A pending payload publication is still in progress; preserving "
            "all transcript spool files"
        )
        return 0, max(1, len(source_ids)), set(source_ids)
    try:
        candidates = list(flush_dir.glob("pending-*.json"))
        all_entries = _read_ordered_pending_entries(candidates)
    except Exception as exc:
        logger.warning(
            "Cannot establish transcript spool order; preserving files: %s",
            exc,
        )
        return 0, max(1, len(source_ids)), set(source_ids)

    resolved_targets: dict[int, str] = {}
    resolution_errors: set[int] = set()

    def payload_source_id(payload: Dict[str, Any]) -> str:
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        if payload.get("reason") == TRANSCRIPT_CAP_DROP_REASON:
            return str(data.get("session_id") or payload.get("session_key") or "")
        return str(data.get("session_id") or "")

    if target_session_id and callable(resolve_target):
        for index, (_path, payload) in enumerate(all_entries):
            source_session_id = payload_source_id(payload)
            if not source_session_id:
                resolution_errors.add(index)
                continue
            try:
                resolved_target = str(resolve_target(source_session_id) or "")
            except Exception:
                resolution_errors.add(index)
                continue
            if resolved_target:
                resolved_targets[index] = resolved_target
            else:
                resolution_errors.add(index)

    selected_positions = []
    for index, (_path, payload) in enumerate(all_entries):
        if payload.get("reason") != TRANSCRIPT_CAP_DROP_REASON:
            continue
        literal_source = str(payload.get("session_key") or "")
        if literal_source in source_ids or (
            resolved_targets.get(index) == target_session_id
        ):
            selected_positions.append(index)
    if not selected_positions:
        return 0, 0, set()
    last_selected = selected_positions[-1]
    selected_position_set = set(selected_positions)
    selected_sources = {
        str(all_entries[index][1].get("session_key") or "")
        for index in selected_positions
    }
    selected_sources.discard("")
    if any(index <= last_selected for index in resolution_errors):
        logger.warning(
            "Cannot prove the durable target of an older pending payload; "
            "preserving all selected transcript spools"
        )
        return 0, len(selected_positions), selected_sources | source_ids

    guarded_session_ids = set(source_ids)
    if target_session_id:
        guarded_session_ids.add(target_session_id)

    def belongs_to_guarded_session(
        index: int, payload: Dict[str, Any]
    ) -> bool:
        if resolved_targets.get(index) == target_session_id:
            return True
        payload_ids = {str(payload.get("session_key") or "")}
        data = payload.get("data")
        if isinstance(data, dict) and data.get("session_id"):
            payload_ids.add(str(data["session_id"]))
        payload_ids.discard("")
        return bool(payload_ids & guarded_session_ids)

    if any(
        index not in selected_position_set
        and belongs_to_guarded_session(index, all_entries[index][1])
        for index in range(last_selected + 1)
    ):
        logger.warning(
            "Cannot replay selected transcript spools past an older pending "
            "payload owned by another format or source; preserving files"
        )
        return 0, len(selected_positions), selected_sources | source_ids

    entries = []
    for index in selected_positions:
        path, payload = all_entries[index]
        source_session_id = str(payload.get("session_key") or "")
        message = (payload.get("data") or {}).get("message")
        if not isinstance(message, dict):
            logger.warning(
                "Cannot replay structurally invalid transcript spool file %s; "
                "preserving all target files",
                path,
            )
            remaining_sources = {
                str(all_entries[item_index][1].get("session_key") or "")
                for item_index in selected_positions
            }
            remaining_sources.discard("")
            return 0, len(remaining_sources), remaining_sources
        entries.append((path, payload, source_session_id, message))

    replayed = 0
    for idx, (path, payload, source_session_id, message) in enumerate(entries):
        try:
            _assert_pending_replay_barrier_clear(flush_dir)
        except Exception as exc:
            logger.warning(
                "A new pending-message ordering barrier appeared before %s; "
                "preserving this and every younger spool: %s",
                path,
                exc,
            )
            remaining_entries = entries[idx:]
            return (
                replayed,
                max(1, len(remaining_entries)),
                {entry[2] for entry in remaining_entries} | source_ids,
            )
        try:
            replay(
                source_session_id,
                message,
                _pending_replay_key(path, payload),
            )
            _mark_pending_legacy_migration_committed(flush_dir, payload)
            path.unlink(missing_ok=True)
            _finalize_pending_legacy_migration(flush_dir, payload)
        except Exception as exc:
            logger.warning(
                "Replay or cleanup of spooled transcript message %s for %s "
                "failed; keeping spool file for idempotent retry: %s",
                path,
                source_session_id,
                exc,
            )
            remaining_entries = entries[idx:]
            return (
                replayed,
                len(remaining_entries),
                {entry[2] for entry in remaining_entries},
            )
        replayed += 1
        try:
            _assert_pending_replay_barrier_clear(flush_dir)
        except Exception as exc:
            logger.warning(
                "A new pending-message ordering barrier appeared after %s; "
                "preserving every younger spool: %s",
                path,
                exc,
            )
            remaining_entries = entries[idx + 1 :]
            return (
                replayed,
                max(1, len(remaining_entries)),
                {entry[2] for entry in remaining_entries} | source_ids,
            )

    if replayed:
        logger.info(
            "Replayed %d globally ordered transcript spool message(s) after "
            "DB recovery",
            replayed,
        )
    return replayed, 0, set()


def drain_transcript_spools(
    session_ids,
    replay,
    *,
    target_session_id: Optional[str] = None,
    resolve_target=None,
) -> tuple[int, int, set[str]]:
    """Replay transcript spools under the shared publication/recovery lock."""
    source_ids = {str(session_id) for session_id in session_ids if session_id}
    if not source_ids and not (target_session_id and callable(resolve_target)):
        return 0, 0, set()
    try:
        flush_dir = _get_flush_dir()
    except Exception as exc:
        if not source_ids:
            # Preserve the pre-existing ordinary-write compatibility path when
            # this process never successfully published a spool and even the
            # spool directory itself is unavailable.
            logger.debug("Cannot inspect transcript spool directory: %s", exc)
            return 0, 0, set()
        logger.warning(
            "Cannot inspect transcript spool directory; preserving known "
            "source files: %s",
            exc,
        )
        return 0, len(source_ids), set(source_ids)
    try:
        with _pending_spool_lock(flush_dir):
            _ensure_pending_spool_format_state(flush_dir)
            return _drain_transcript_spools_unlocked(
                source_ids,
                replay,
                target_session_id=target_session_id,
                resolve_target=resolve_target,
            )
    except Exception as exc:
        logger.warning(
            "Cannot lock or drain transcript spool; preserving files: %s",
            exc,
        )
        return 0, max(1, len(source_ids)), set(source_ids)


def drain_transcript_spool(session_id: str, replay) -> tuple[int, int]:
    """Replay cap-dropped transcript messages spooled for *session_id*.

    ``replay(message_dict, replay_key)`` is invoked for each spooled message
    in drop order. The callback must bind ``replay_key`` to its durable write
    transaction; a legacy one-argument callback fails closed and keeps the
    file instead of risking a duplicate after cleanup failure.

    Returns ``(replayed, remaining)`` — messages replayed and spool files
    left behind for a later retry.
    """
    replayed, remaining, _remaining_sources = drain_transcript_spools(
        {session_id},
        lambda _source_session_id, message, replay_key: replay(
            message, replay_key
        ),
        target_session_id=session_id,
    )
    return replayed, remaining


def _serialise_value(value: Any) -> Optional[dict]:
    """Convert a pending message value to a JSON-serialisable dict."""
    # MessageEvent objects have a .text attribute and other fields
    if hasattr(value, "text"):
        result: Dict[str, Any] = {"text": getattr(value, "text", "")}
        # Preserve additional fields if present
        for attr in ("session_id", "platform", "sender_id", "sender_name",
                      "reply_to", "media", "raw_event"):
            val = getattr(value, attr, None)
            if val is not None:
                try:
                    json.dumps(val)
                    result[attr] = val
                except (TypeError, ValueError):
                    result[attr] = str(val)
        return result
    # Plain string (runner-level _pending_messages)
    if isinstance(value, str):
        return {"text": value}
    # Dict — try direct serialisation
    if isinstance(value, dict):
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return {"text": str(value)}
    return {"text": str(value)}


def _append_recovered_message(
    session_db,
    replay_key: str,
    *,
    require_idempotency: bool,
    **append_kwargs,
):
    """Use transactional replay dedupe when the concrete DB supports it."""
    append_message = session_db.append_message
    try:
        signature_target = type(session_db).append_message
    except AttributeError:
        signature_target = append_message
    try:
        parameters = inspect.signature(signature_target).parameters
    except (TypeError, ValueError, AttributeError):
        parameters = {}
    explicit_idempotency = "idempotency_key" in parameters
    variadic_keywords = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if explicit_idempotency or (require_idempotency and variadic_keywords):
        append_kwargs["idempotency_key"] = replay_key
    elif require_idempotency:
        raise RuntimeError(
            "Session DB append_message override lacks idempotency_key; "
            "preserving pending payload"
        )
    return append_message(**append_kwargs)


def _pending_payload_session_ids(payload: Dict[str, Any], session_db) -> set[str]:
    """Return source/live-tip aliases used to scope startup failure barriers."""
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    session_key = str(payload.get("session_key") or "")
    data_session_id = str(data.get("session_id") or "")
    session_ids = {session_key, data_session_id}
    session_ids.discard("")

    lineage_source = data_session_id
    if not lineage_source and payload.get("reason") == TRANSCRIPT_CAP_DROP_REASON:
        lineage_source = session_key
    resolver = getattr(session_db, "find_live_compression_child", None)
    if lineage_source and callable(resolver):
        child = resolver(lineage_source)
        child_id = (
            str(child.get("id") or "") if isinstance(child, dict) else ""
        )
        if child_id:
            session_ids.add(child_id)
    return session_ids


def _recover_pending_to_db_unlocked(
    session_db,
    flush_dir: Path,
) -> int:
    """Recover flushed pending messages into state.db via SessionDB.

    Reads all ``*.json`` files from the flush directory, inserts messages
    using ``SessionDB.append_message`` (so FTS indexing, session metadata
    updates, and all required columns are handled correctly), and deletes
    the flush file on success.

    Parameters
    ----------
    session_db:
        An existing ``SessionDB`` instance.  If ``None``, a new one is
        opened on the default ``state.db`` path.

    Returns
    -------
    int
        Number of messages recovered.
    """
    if _pending_publication_in_progress(flush_dir):
        logger.warning(
            "A pending payload publication is still in progress; preserving "
            "all startup recovery files"
        )
        return 0
    flush_files = list(flush_dir.glob("*.json"))
    if not flush_files:
        return 0
    try:
        ordered_entries = _read_ordered_pending_entries(flush_files)
    except PendingSpoolOrderError as exc:
        logger.warning(
            "Cannot establish pending-message recovery order; preserving all "
            "files: %s",
            exc,
        )
        return 0

    # Use the provided SessionDB or open one on the default path.
    own_db = False
    if session_db is None:
        from hermes_state import SessionDB
        session_db = SessionDB()
        own_db = True

    recovered = 0
    blocked_session_ids: set[str] = set()
    for path, payload in ordered_entries:
        try:
            _assert_pending_replay_barrier_clear(flush_dir)
        except PendingSpoolOrderError as exc:
            logger.warning(
                "A new pending-message ordering barrier appeared before %s; "
                "preserving this and every younger file: %s",
                path,
                exc,
            )
            break
        try:
            payload_session_ids = _pending_payload_session_ids(payload, session_db)
        except Exception as exc:
            # If lineage resolution itself fails, the target equivalence class
            # is unknowable. Continuing could append a younger alias first.
            logger.warning(
                "Cannot resolve pending-message recovery target for %s; "
                "preserving this and all younger files: %s",
                path,
                exc,
            )
            break

        if payload_session_ids & blocked_session_ids:
            continue

        durable = False
        try:
            # Cap-dropped transcript payloads carry the full message dict
            # keyed by session_id. Replay directly while it is writable, or
            # follow its unique compression lineage with the same transactional
            # root guard as the live gateway path (#78182). This handles spool
            # files that were never drained before a restart.
            if payload.get("reason") == TRANSCRIPT_CAP_DROP_REASON:
                data = payload.get("data")
                data = data if isinstance(data, dict) else {}
                spooled_sid = str(data.get("session_id") or "")
                message = data.get("message")
                if not spooled_sid or not isinstance(message, dict):
                    logger.warning(
                        "Cannot recover structurally invalid transcript spool "
                        "file %s; preserved for manual inspection",
                        path,
                    )
                    if payload_session_ids:
                        blocked_session_ids.update(payload_session_ids)
                        continue
                    break
                append_kwargs = {
                    "role": message.get("role", "unknown"),
                    "content": message.get("content") or "",
                    "timestamp": message.get("timestamp") or payload.get("ts"),
                }
                replay_key = _pending_replay_key(path, payload)
                try:
                    _append_recovered_message(
                        session_db,
                        replay_key,
                        require_idempotency=_pending_requires_idempotency(payload),
                        session_id=spooled_sid,
                        **append_kwargs,
                    )
                except Exception as append_exc:
                    from hermes_state import CompressionSessionClosedError

                    if not isinstance(
                        append_exc, CompressionSessionClosedError
                    ):
                        raise
                    try:
                        child = session_db.find_live_compression_child(spooled_sid)
                    except Exception as exc:
                        raise PendingSpoolTargetError(
                            "compression continuation lookup failed"
                        ) from exc
                    child_id = (
                        str(child.get("id") or "")
                        if isinstance(child, dict)
                        else ""
                    )
                    if not child_id:
                        raise PendingSpoolTargetError(
                            "compression continuation is ambiguous or missing"
                        )
                    payload_session_ids.add(child_id)
                    _append_recovered_message(
                        session_db,
                        replay_key,
                        require_idempotency=_pending_requires_idempotency(payload),
                        session_id=child_id,
                        compression_lineage_root=spooled_sid,
                        **append_kwargs,
                    )
                recovered += 1
                durable = True
                _mark_pending_legacy_migration_committed(flush_dir, payload)
                path.unlink(missing_ok=True)
                _finalize_pending_legacy_migration(flush_dir, payload)
                _assert_pending_replay_barrier_clear(flush_dir)
                continue

            session_key = str(payload.get("session_key") or "")
            data = payload.get("data")
            data = data if isinstance(data, dict) else {}
            session_id = str(data.get("session_id") or "")
            if not session_id:
                logger.warning(
                    "Cannot recover pending message for %s: no session_id "
                    "in flush file and session_key-to-id resolution is not "
                    "available at this recovery stage. The message text is "
                    "preserved in %s",
                    session_key,
                    path,
                )
                # A routing key does not prove the durable target, including
                # when text/data validation also fails. Stop globally.
                break

            text = data.get("text", "")
            if not text or not session_key:
                logger.warning(
                    "Cannot recover structurally invalid pending message from %s; "
                    "the flush file has been preserved",
                    path,
                )
                if payload_session_ids:
                    blocked_session_ids.update(payload_session_ids)
                    continue
                break

            replay_key = _pending_replay_key(path, payload)
            append_kwargs = {
                "role": "user",
                "content": text,
                "timestamp": payload.get("ts", int(time.time())),
            }
            try:
                _append_recovered_message(
                    session_db,
                    replay_key,
                    require_idempotency=_pending_requires_idempotency(payload),
                    session_id=session_id,
                    **append_kwargs,
                )
            except Exception as append_exc:
                from hermes_state import CompressionSessionClosedError

                if not isinstance(append_exc, CompressionSessionClosedError):
                    raise
                try:
                    child = session_db.find_live_compression_child(session_id)
                except Exception as exc:
                    raise PendingSpoolTargetError(
                        "compression continuation lookup failed"
                    ) from exc
                child_id = (
                    str(child.get("id") or "")
                    if isinstance(child, dict)
                    else ""
                )
                if not child_id:
                    raise PendingSpoolTargetError(
                        "compression continuation is ambiguous or missing"
                    )
                payload_session_ids.add(child_id)
                _append_recovered_message(
                    session_db,
                    replay_key,
                    require_idempotency=_pending_requires_idempotency(payload),
                    session_id=child_id,
                    compression_lineage_root=session_id,
                    **append_kwargs,
                )
            recovered += 1
            durable = True
            _mark_pending_legacy_migration_committed(flush_dir, payload)
            path.unlink(missing_ok=True)
            _finalize_pending_legacy_migration(flush_dir, payload)
            _assert_pending_replay_barrier_clear(flush_dir)
        except Exception as exc:
            logger.warning(
                "Failed to recover pending message from %s: %s",
                path, exc,
            )
            # Append failure blocks only this source/live-tip equivalence class.
            # If unlink failed after a durable append, idempotency makes the
            # retained file safe and younger rows can continue in order. A new
            # ordering barrier is global even when the selected row is durable.
            if isinstance(exc, PendingSpoolOrderError):
                break
            if not durable:
                if isinstance(exc, PendingSpoolTargetError):
                    break
                if payload_session_ids:
                    blocked_session_ids.update(payload_session_ids)
                else:
                    break

    if own_db:
        try:
            session_db.close()
        except Exception:
            pass

    if recovered:
        logger.info(
            "Recovered %d pending message(s) from shutdown flush", recovered,
        )
    return recovered


def recover_pending_to_db(session_db=None) -> int:
    """Recover pending payloads under one publication/replay transaction lock."""
    try:
        flush_dir = _get_flush_dir()
        with _pending_spool_lock(flush_dir):
            _ensure_pending_spool_format_state(flush_dir)
            return _recover_pending_to_db_unlocked(session_db, flush_dir)
    except Exception as exc:
        logger.warning(
            "Cannot lock pending-message recovery; preserving files: %s",
            exc,
        )
        return 0


def flush_agent_history_to_file(
    session_id: Optional[str],
    history: list,
) -> None:
    """Best-effort dump of an agent's in-memory transcript before teardown.

    Used when ``_flush_messages_to_session_db`` raises (e.g. FTS/SQLite
    index corruption, #72680): the live ``agent._session_messages`` could
    not be written to disk, and a plain debug log would lose it permanently
    when the process exits. Serialize to an atomic JSON file outside the
    broken DB so an operator can salvage the conversation after repairing
    state.db.

    Failures are swallowed — shutdown must never block on a best-effort
    backup.
    """
    if not history:
        return
    try:
        flush_dir = _get_flush_dir()
        snapshot = []
        for _m in history:
            try:
                snapshot.append(
                    _m if isinstance(_m, (dict, list, str, int, float, bool, type(None)))
                    else str(_m)
                )
            except Exception:
                continue
        _write_payload(
            flush_dir,
            {
                "reason": "shutdown-with-unpersisted-agent-history",
                "issue": "#72680",
                "session_id": session_id,
                "count": len(snapshot),
                "messages": snapshot,
            },
        )
        logger.warning(
            "Preserved %d in-memory message(s) for session %s "
            "(possible FTS corruption — recover after repairing state.db)",
            len(snapshot),
            session_id,
        )
    except Exception as _e:
        logger.warning(
            "Agent-history shutdown preservation failed for session %s: %s",
            session_id, _e,
        )
