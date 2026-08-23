"""Fail-closed cross-process exclusion for cron and JobFlow dispatch.

Every acting boundary takes a slot in the same kernel-backed admission lock
before it captures or claims work and keeps it through submission.  An incident
barrier takes every slot, so acquiring it both stops future actors and drains
actors already between observation and submission.  The durable fence and wake
queue live in a dedicated control database and change only under that barrier.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - platform selected at import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - platform selected at import
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]


_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS quarantine_control_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    database_device TEXT NOT NULL,
    database_inode TEXT NOT NULL,
    lock_device TEXT NOT NULL,
    lock_inode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_dispatch_fence (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    fenced INTEGER NOT NULL CHECK(fenced IN (0, 1)),
    generation INTEGER NOT NULL,
    fence_token TEXT,
    authorization_request_id TEXT,
    changed_at TEXT NOT NULL
);
INSERT OR IGNORE INTO quarantine_dispatch_fence
    (singleton, fenced, generation, fence_token, authorization_request_id, changed_at)
VALUES (1, 0, 0, NULL, NULL, '1970-01-01T00:00:00Z');
CREATE TABLE IF NOT EXISTS quarantine_wakes (
    job_id TEXT PRIMARY KEY,
    wake_token TEXT NOT NULL UNIQUE,
    caller TEXT NOT NULL,
    reason TEXT,
    requested_at TEXT NOT NULL
);
"""

_PROCESS_BARRIERS: dict[str, "DispatchBarrier"] = {}
_PROCESS_BARRIERS_LOCK = threading.Lock()
_DISPATCH_DEPTH = threading.local()
MAX_PENDING_WAKES = 512
_ADMISSION_SLOTS = 128
_LOCK_FILE_SIZE = _ADMISSION_SLOTS + 1
_LOCK_IDENTITY_MARKER_OFFSET = _LOCK_FILE_SIZE
_LOCK_IDENTITY_MARKER_SIZE = 64
_CONTROL_LOCK_FILE_SIZE = _LOCK_FILE_SIZE + _LOCK_IDENTITY_MARKER_SIZE
_EMPTY_LOCK_IDENTITY_MARKER = b"0" * _LOCK_IDENTITY_MARKER_SIZE
_DEFAULT_CONTROL_STORE: "QuarantineControlStore | None" = None
_DEFAULT_CONTROL_STORE_KEY: tuple[str, str] | None = None
_DEFAULT_CONTROL_STORE_LOCK = threading.Lock()


class WakeQueueFullError(RuntimeError):
    """A distinct wake could not fit in the bounded durable queue."""


def _canonical_hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def default_control_path() -> Path:
    """Return the canonical cross-profile quarantine-control database path."""
    return _canonical_hermes_root() / "telemetry" / "jobflow_quarantine_fence.db"


def _normalize_st_dev(st_dev: int) -> int:
    """Collapse interpreter-dependent st_dev width to one stable value.

    CPython 3.12 on Windows reports the full 64-bit NTFS volume id while 3.11
    reports its low 32 bits; the durable identity marker must not flip when the
    gateway's interpreter changes.
    """
    return int(st_dev) & 0xFFFFFFFF


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_digest(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


class _KernelLock:
    def __init__(
        self,
        path: Path,
        *,
        timeout: float,
        poll_interval: float,
        exclusive: bool,
        offsets: tuple[int, ...] | None = None,
    ):
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self.exclusive = bool(exclusive)
        self.offsets = offsets
        self.handle = None
        self.held = False
        self.offset: int | None = None

    def _open(self):
        handle = open(self.path, "r+b")
        size = os.fstat(handle.fileno()).st_size
        if size < _LOCK_FILE_SIZE:
            # Another opener can observe the file between O_EXCL creation and the
            # initializer's durable write. Give that bounded creation window a
            # chance to close; a genuinely truncated established file remains a
            # hard refusal.
            deadline = time.monotonic() + min(self.timeout, 0.25)
            while size < _LOCK_FILE_SIZE and time.monotonic() < deadline:
                time.sleep(self.poll_interval)
                size = os.fstat(handle.fileno()).st_size
            if size < _LOCK_FILE_SIZE:
                handle.close()
                raise RuntimeError("dispatch lock file is truncated or corrupt")
        return handle

    @staticmethod
    def _try_lock(handle, offset: int) -> None:
        handle.seek(offset)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # never silently degrade to a process-local lock
            raise RuntimeError("cross-process dispatch locking unavailable")

    @staticmethod
    def _unlock(handle, offset: int) -> None:
        handle.seek(offset)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def acquire(self) -> "_KernelLock":
        handle = self._open()
        deadline = time.monotonic() + self.timeout
        if fcntl is not None:
            operation = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                    self.offset = 0
                    self.handle = handle
                    self.held = True
                    return self
                except (OSError, IOError):
                    if time.monotonic() >= deadline:
                        handle.close()
                        detail = (
                            "active dispatch sections to drain"
                            if self.exclusive
                            else "dispatch admission"
                        )
                        raise TimeoutError(f"timed out waiting for {detail}")
                    time.sleep(self.poll_interval)
        if msvcrt is None:
            handle.close()
            raise RuntimeError("cross-process dispatch locking unavailable")
        if self.exclusive:
            acquired: list[int] = []
            offsets = self.offsets or tuple(range(_LOCK_FILE_SIZE))
            while True:
                try:
                    for offset in offsets:
                        self._try_lock(handle, offset)
                        acquired.append(offset)
                    self.offset = offsets[0]
                    self.handle = handle
                    self.held = True
                    return self
                except (OSError, IOError):
                    for offset in reversed(acquired):
                        self._unlock(handle, offset)
                    acquired.clear()
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise TimeoutError(
                            "timed out waiting for active dispatch sections to drain"
                        )
                    time.sleep(self.poll_interval)

        while True:
            for offset in range(1, _LOCK_FILE_SIZE):
                try:
                    self._try_lock(handle, offset)
                except (OSError, IOError):
                    continue
                self.offset = offset
                self.handle = handle
                self.held = True
                return self
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError("timed out waiting for a dispatch admission slot")
            time.sleep(self.poll_interval)

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if self.held:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif self.exclusive:
                    offsets = self.offsets or tuple(range(_LOCK_FILE_SIZE))
                    for offset in reversed(offsets):
                        self._unlock(handle, offset)
                elif self.offset is not None:
                    self._unlock(handle, self.offset)
        finally:
            self.offset = None
            self.held = False
            handle.close()


class DispatchBarrier:
    """Retained capability handle proving the dispatch exclusion is still held."""

    def __init__(self, store: "QuarantineControlStore", reason: str, lock: _KernelLock):
        self.store = store
        self.reason = reason
        self._lock = lock
        self.token = uuid.uuid4().hex
        self._entered = False

    def __enter__(self) -> "DispatchBarrier":
        if self._entered:
            raise RuntimeError("dispatch barrier cannot be re-entered")
        self._lock.acquire()
        self._entered = True
        with _PROCESS_BARRIERS_LOCK:
            _PROCESS_BARRIERS[self.token] = self
        return self

    def assert_held(self) -> dict[str, Any]:
        if not self._entered or not self._lock.held or self._lock.handle is None:
            raise RuntimeError("dispatch barrier is not held")
        return {
            "schema_version": 1,
            "complete": True,
            "source": "kernel-byte-lock-dispatch-barrier",
            "barrier_token": self.token,
            "coverage": "due_row_capture_through_submission",
        }

    def __exit__(self, _typ, _value, _traceback) -> None:
        with _PROCESS_BARRIERS_LOCK:
            _PROCESS_BARRIERS.pop(self.token, None)
        self._entered = False
        self._lock.release()


class QuarantineControlStore:
    """Canonical durable fence/wake state plus the shared dispatch lock."""

    def __init__(
        self,
        db_path: Path,
        *,
        lock_path: Path | None = None,
        timeout: float = 30.0,
        poll_interval: float = 0.02,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(lock_path or self.db_path.with_suffix(".dispatch.lock"))
        self.store_key = (
            os.path.normcase(str(self.db_path.resolve())),
            os.path.normcase(str(self.lock_path.resolve())),
        )
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        database_existed = self.db_path.exists()
        lock_existed = self.lock_path.exists()
        if database_existed != lock_existed:
            deadline = time.monotonic() + min(self.timeout, 0.25)
            while database_existed != lock_existed and time.monotonic() < deadline:
                time.sleep(self.poll_interval)
                database_existed = self.db_path.exists()
                lock_existed = self.lock_path.exists()
        if database_existed != lock_existed:
            raise RuntimeError(
                "canonical dispatch control database/lock pair is incomplete"
            )
        if not database_existed:
            self._initialize_lock_file()
            self._initialize_database_file()
        self._wait_for_control_pair()

        initialization_lock = self._kernel_lock(
            exclusive=True, offsets=(0,)
        ).acquire()
        try:
            if initialization_lock.handle is None:
                raise RuntimeError("dispatch initialization lock is not held")
            self._upgrade_legacy_lock_file()
            self._initialize_or_verify_store(initialization_lock.handle)
        finally:
            initialization_lock.release()

    def _wait_for_control_pair(self) -> None:
        deadline = time.monotonic() + min(self.timeout, 0.25)
        while not self.db_path.exists() or not self.lock_path.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "canonical dispatch control database/lock pair is incomplete"
                )
            time.sleep(self.poll_interval)

    def _upgrade_legacy_lock_file(self) -> None:
        size = self.lock_path.stat().st_size
        if size == _LOCK_FILE_SIZE:
            with open(self.lock_path, "r+b") as handle:
                handle.seek(_LOCK_FILE_SIZE)
                if not handle.read(1):
                    handle.write(_EMPTY_LOCK_IDENTITY_MARKER)
                    handle.flush()
                    os.fsync(handle.fileno())
            size = self.lock_path.stat().st_size
        if size < _CONTROL_LOCK_FILE_SIZE:
            raise RuntimeError("dispatch lock file is truncated or corrupt")

    @staticmethod
    def _path_identity(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return (_normalize_st_dev(stat.st_dev), int(stat.st_ino))

    @staticmethod
    def _identity_marker(identity: tuple[int, int]) -> bytes:
        marker = f"{identity[0]}:{identity[1]}".encode("ascii")
        if len(marker) > _LOCK_IDENTITY_MARKER_SIZE:
            raise RuntimeError("dispatch database identity is too large")
        return marker.ljust(_LOCK_IDENTITY_MARKER_SIZE, b"0")

    @staticmethod
    def _read_lock_identity_marker(handle: Any) -> bytes:
        handle.seek(_LOCK_IDENTITY_MARKER_OFFSET)
        marker = handle.read(_LOCK_IDENTITY_MARKER_SIZE)
        if len(marker) != _LOCK_IDENTITY_MARKER_SIZE:
            raise RuntimeError("dispatch lock file is truncated or corrupt")
        return marker

    def _write_lock_identity_marker(
        self, handle: Any, identity: tuple[int, int]
    ) -> None:
        marker = self._identity_marker(identity)
        handle.seek(_LOCK_IDENTITY_MARKER_OFFSET)
        handle.write(marker)
        handle.flush()
        os.fsync(handle.fileno())

    def _initialize_or_verify_store(self, lock_handle: Any) -> None:
        database_identity_before = self._current_database_identity()
        lock_stat = os.fstat(lock_handle.fileno())
        lock_identity = (_normalize_st_dev(lock_stat.st_dev), int(lock_stat.st_ino))
        if lock_identity != self._path_identity(self.lock_path):
            raise RuntimeError("canonical dispatch lock file identity changed")
        marker_before = self._read_lock_identity_marker(lock_handle)
        expected_marker = self._identity_marker(database_identity_before)
        if marker_before not in (_EMPTY_LOCK_IDENTITY_MARKER, expected_marker):
            raise RuntimeError("canonical dispatch control database identity changed")

        with self._connect_unchecked() as conn:
            existing_tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            established = bool(existing_tables)
            conn.executescript(_CONTROL_SCHEMA)
            wake_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(quarantine_wakes)")
            }
            if "wake_token" not in wake_columns:
                conn.execute("ALTER TABLE quarantine_wakes ADD COLUMN wake_token TEXT")
                for row in conn.execute(
                    "SELECT job_id FROM quarantine_wakes WHERE wake_token IS NULL"
                ).fetchall():
                    conn.execute(
                        "UPDATE quarantine_wakes SET wake_token=? WHERE job_id=?",
                        (uuid.uuid4().hex, row["job_id"]),
                    )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS quarantine_wakes_token "
                    "ON quarantine_wakes(wake_token)"
                )

            self._database_identity = self._current_database_identity()
            self._lock_identity = lock_identity
            identity_row = conn.execute(
                "SELECT * FROM quarantine_control_identity WHERE singleton=1"
            ).fetchone()
            if identity_row is None:
                legacy_marker = marker_before == _EMPTY_LOCK_IDENTITY_MARKER
                legacy_tables = {
                    "quarantine_dispatch_fence",
                    "quarantine_wakes",
                }.issubset(existing_tables)
                if established and not (legacy_marker and legacy_tables):
                    raise RuntimeError("durable dispatch control identity row is missing")
                conn.execute(
                    "INSERT INTO quarantine_control_identity "
                    "(singleton, database_device, database_inode, lock_device, lock_inode) "
                    "VALUES (1, ?, ?, ?, ?)",
                    tuple(
                        str(value)
                        for value in (*self._database_identity, *self._lock_identity)
                    ),
                )
            else:
                expected_database = (
                    int(identity_row["database_device"]),
                    int(identity_row["database_inode"]),
                )
                expected_lock = (
                    int(identity_row["lock_device"]),
                    int(identity_row["lock_inode"]),
                )
                if (
                    expected_database != self._database_identity
                    or expected_lock != self._lock_identity
                ):
                    raise RuntimeError("canonical dispatch control identity changed")
            conn.commit()

        if marker_before == _EMPTY_LOCK_IDENTITY_MARKER:
            self._write_lock_identity_marker(lock_handle, self._database_identity)
        elif marker_before != self._identity_marker(self._database_identity):
            raise RuntimeError("canonical dispatch control database identity changed")

    def _initialize_lock_file(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.lock_path, "xb") as handle:
                handle.write(
                    b"0" * _LOCK_FILE_SIZE
                    + _EMPTY_LOCK_IDENTITY_MARKER
                )
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
        deadline = time.monotonic() + min(self.timeout, 0.25)
        size = self.lock_path.stat().st_size
        while size < _CONTROL_LOCK_FILE_SIZE and time.monotonic() < deadline:
            time.sleep(self.poll_interval)
            size = self.lock_path.stat().st_size
        if size < _CONTROL_LOCK_FILE_SIZE:
            raise RuntimeError("dispatch lock file is truncated or corrupt")

    def _initialize_database_file(self) -> None:
        try:
            descriptor = os.open(
                self.db_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            return
        os.close(descriptor)

    def _current_database_identity(self) -> tuple[int, int]:
        return self._path_identity(self.db_path)

    def _verify_database_identity(self) -> tuple[int, int]:
        try:
            current = self._current_database_identity()
        except FileNotFoundError as exc:
            raise RuntimeError("canonical dispatch control database disappeared") from exc
        if current != self._database_identity:
            raise RuntimeError("canonical dispatch control database identity changed")
        return current

    def _record_or_verify_lock_identity(self) -> tuple[int, int]:
        try:
            current = self._path_identity(self.lock_path)
        except FileNotFoundError as exc:
            raise RuntimeError("canonical dispatch lock file disappeared") from exc
        if current != self._lock_identity:
            raise RuntimeError("canonical dispatch lock file identity changed")
        return current

    def _connect_unchecked(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _connect(self) -> sqlite3.Connection:
        self._verify_database_identity()
        self._record_or_verify_lock_identity()
        return self._connect_unchecked()

    def _kernel_lock(
        self,
        timeout: float | None = None,
        *,
        exclusive: bool,
        offsets: tuple[int, ...] | None = None,
    ) -> _KernelLock:
        return _KernelLock(
            self.lock_path,
            timeout=self.timeout if timeout is None else float(timeout),
            poll_interval=self.poll_interval,
            exclusive=exclusive,
            offsets=offsets,
        )

    @contextlib.contextmanager
    def dispatch_section(self, *, boundary: str) -> Iterator[None]:
        _identity(boundary, "boundary")
        admissions = getattr(_DISPATCH_DEPTH, "admissions", None)
        if admissions is None:
            admissions = {}
            _DISPATCH_DEPTH.admissions = admissions
        depth = admissions.get(self.store_key, 0)
        if depth:
            admissions[self.store_key] = depth + 1
            try:
                yield
            finally:
                admissions[self.store_key] -= 1
            return

        lock = self._kernel_lock(exclusive=False).acquire()
        try:
            self._record_or_verify_lock_identity()
        except BaseException:
            lock.release()
            raise
        admissions[self.store_key] = 1
        try:
            state = self.fence_state()
            if state["fenced"]:
                raise RuntimeError(
                    f"dispatch fenced by generation {state['generation']} at {boundary}"
                )
            yield
        finally:
            admissions.pop(self.store_key, None)
            lock.release()

    @contextlib.contextmanager
    def acquire_dispatch_barrier(
        self, *, reason: str, timeout: float | None = None
    ) -> Iterator[DispatchBarrier]:
        barrier = DispatchBarrier(
            self,
            _identity(reason, "reason"),
            self._kernel_lock(timeout, exclusive=True),
        )
        with barrier:
            self._record_or_verify_lock_identity()
            yield barrier

    def _held_barrier(self, token: str) -> DispatchBarrier:
        with _PROCESS_BARRIERS_LOCK:
            barrier = _PROCESS_BARRIERS.get(token)
        if barrier is None:
            raise RuntimeError("exact held dispatch barrier token is required")
        barrier.assert_held()
        if barrier.store.store_key != self.store_key:
            raise RuntimeError("dispatch barrier must belong to the same control store")
        return barrier

    @staticmethod
    def _validate_fence_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise RuntimeError("durable dispatch fence row is missing")
        fenced = row["fenced"]
        generation = row["generation"]
        token = row["fence_token"]
        authorization = row["authorization_request_id"]
        changed_at = row["changed_at"]
        if (
            fenced not in (0, 1)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not isinstance(changed_at, str)
            or not changed_at
            or (
                bool(fenced)
                and (
                    generation < 1
                    or not isinstance(token, str)
                    or not token
                    or not isinstance(authorization, str)
                    or not authorization
                )
            )
            or (not bool(fenced) and (token is not None or authorization is not None))
        ):
            raise RuntimeError("durable dispatch fence row is semantically invalid")
        return {
            "fenced": bool(fenced),
            "generation": generation,
            "fence_token": token,
            "authorization_request_id": authorization,
            "changed_at": changed_at,
        }

    def fence_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM quarantine_dispatch_fence WHERE singleton=1"
            ).fetchone()
        return self._validate_fence_row(row)

    def pending_wakes(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id, wake_token, caller, reason, requested_at FROM quarantine_wakes "
                "ORDER BY job_id"
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def request_wake(self, job_id: Any, *, caller: Any, reason: Any = None) -> bool:
        job = _identity(job_id, "job_id")
        owner = _identity(caller, "caller")
        detail = None if reason is None else str(reason)
        with self.dispatch_section(boundary="wake-request"):
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                duplicate = conn.execute(
                    "SELECT 1 FROM quarantine_wakes WHERE job_id=?", (job,)
                ).fetchone()
                if duplicate is not None:
                    conn.commit()
                    return False
                count = conn.execute(
                    "SELECT COUNT(*) AS count FROM quarantine_wakes"
                ).fetchone()
                if count is None:
                    conn.rollback()
                    raise RuntimeError("durable wake queue count is unavailable")
                if int(count["count"]) >= MAX_PENDING_WAKES:
                    conn.rollback()
                    raise WakeQueueFullError(
                        f"durable wake queue reached capacity {MAX_PENDING_WAKES}"
                    )
                conn.execute(
                    "INSERT INTO quarantine_wakes "
                    "(job_id, wake_token, caller, reason, requested_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job, uuid.uuid4().hex, owner, detail, _now()),
                )
                conn.commit()
                return True

    def ack_wake(self, wake: dict[str, Any]) -> bool:
        job = _identity(wake.get("job_id"), "wake.job_id")
        token = _identity(wake.get("wake_token"), "wake.wake_token")
        with self.dispatch_section(boundary="wake-ack"):
            with self._connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM quarantine_wakes WHERE job_id=? AND wake_token=?",
                    (job, token),
                )
                conn.commit()
                return cursor.rowcount == 1

    def drain_wakes(self) -> tuple[str, ...]:
        with self.dispatch_section(boundary="wake-drain"):
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                wakes = conn.execute(
                    "SELECT job_id FROM quarantine_wakes ORDER BY job_id"
                ).fetchall()
                conn.execute("DELETE FROM quarantine_wakes")
                conn.commit()
        return tuple(str(row["job_id"]) for row in wakes)

    def clear_wakes(self) -> None:
        with self.dispatch_section(boundary="wake-clear"):
            with self._connect() as conn:
                conn.execute("DELETE FROM quarantine_wakes")
                conn.commit()


    def activate_fence(
        self,
        *,
        barrier_token: str,
        authorization_request_id: str,
        required: bool,
    ) -> dict[str, Any]:
        self._held_barrier(barrier_token)
        auth = _identity(authorization_request_id, "authorization_request_id")
        queried_at = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM quarantine_dispatch_fence WHERE singleton=1"
            ).fetchone()
            current_state = self._validate_fence_row(current)
            wakes = [dict(row) for row in conn.execute(
                "SELECT job_id, wake_token, caller, reason, requested_at FROM quarantine_wakes "
                "ORDER BY job_id"
            ).fetchall()]
            pre = {
                "fenced": current_state["fenced"],
                "generation": current_state["generation"],
                "fence_token": current_state["fence_token"],
                # The kernel barrier proves there is no claim-through-wake actor
                # still in flight. The separate activation ledger is censused by
                # QuarantineSettlementControl, never by this control database.
                "claims": [],
                "wakes": wakes,
            }
            if pre["fenced"]:
                conn.rollback()
                raise RuntimeError("dispatcher fence is already active")
            if wakes and not required:
                conn.rollback()
                raise RuntimeError("pending wake drain requires an authorized transition")
            if required:
                conn.execute("DELETE FROM quarantine_wakes")
            generation = pre["generation"] + 1
            token = uuid.uuid4().hex
            conn.execute(
                "UPDATE quarantine_dispatch_fence SET fenced=1, generation=?, "
                "fence_token=?, authorization_request_id=?, changed_at=? WHERE singleton=1",
                (generation, token, auth, queried_at),
            )
            conn.commit()
        post = {
            "fenced": True,
            "generation": generation,
            "fence_token": token,
            "authorization_request_id": auth,
            "claims": [],
            "wakes": [],
        }
        return {
            "schema_version": 1,
            "complete": True,
            "source": "durable-jobflow-dispatch-control",
            "queried_at": queried_at,
            "required": required,
            "authorization_request_id": auth,
            "pre": pre,
            "post": post,
        }

    def verify_fence(self, expected_fence_token: str) -> dict[str, Any]:
        token = _identity(expected_fence_token, "expected_fence_token")
        state = self.fence_state()
        if not state["fenced"] or state["fence_token"] != token:
            raise RuntimeError("live fence token does not match the expected fence token")
        wakes = list(self.pending_wakes())
        if wakes:
            raise RuntimeError("live fence is not drained")
        proof = {
            "schema_version": 1,
            "complete": True,
            "source": "durable-jobflow-dispatch-control",
            "verified_at": _now(),
            **state,
            "claims": [],
            "wakes": wakes,
        }
        proof["proof_digest"] = _canonical_digest(proof)
        return proof

    def release_fence(
        self, *, barrier_token: str, expected_fence_token: str
    ) -> dict[str, Any]:
        self._held_barrier(barrier_token)
        token = _identity(expected_fence_token, "expected_fence_token")
        changed_at = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM quarantine_dispatch_fence WHERE singleton=1"
            ).fetchone()
            state = self._validate_fence_row(row)
            if not state["fenced"] or state["fence_token"] != token:
                conn.rollback()
                raise RuntimeError("live fence token does not match the expected fence token")
            conn.execute(
                "UPDATE quarantine_dispatch_fence SET fenced=0, fence_token=NULL, "
                "authorization_request_id=NULL, changed_at=? WHERE singleton=1",
                (changed_at,),
            )
            conn.commit()
        return {
            "schema_version": 1,
            "complete": True,
            "source": "durable-jobflow-dispatch-control",
            "fenced": False,
            "generation": state["generation"],
            "released_fence_token": token,
            "changed_at": changed_at,
        }


def default_control_store() -> QuarantineControlStore:
    """Return the process-cached canonical dispatch control capability."""
    global _DEFAULT_CONTROL_STORE, _DEFAULT_CONTROL_STORE_KEY

    db_path = default_control_path()
    lock_path = db_path.with_suffix(".dispatch.lock")
    key = (
        os.path.normcase(str(db_path.resolve())),
        os.path.normcase(str(lock_path.resolve())),
    )
    with _DEFAULT_CONTROL_STORE_LOCK:
        if _DEFAULT_CONTROL_STORE is not None and _DEFAULT_CONTROL_STORE_KEY == key:
            _DEFAULT_CONTROL_STORE._verify_database_identity()
            _DEFAULT_CONTROL_STORE._record_or_verify_lock_identity()
            # Validate semantic state without treating corruption or identity
            # failures as a cache-lifecycle event. Those must remain visible and
            # fail closed rather than being healed by schema bootstrap.
            _DEFAULT_CONTROL_STORE.fence_state()
        if _DEFAULT_CONTROL_STORE is None or _DEFAULT_CONTROL_STORE_KEY != key:
            _DEFAULT_CONTROL_STORE = QuarantineControlStore(
                db_path, lock_path=lock_path
            )
            _DEFAULT_CONTROL_STORE_KEY = key
        return _DEFAULT_CONTROL_STORE


def request_wake(job_id: Any, *, caller: Any, reason: Any = None) -> bool:
    return default_control_store().request_wake(job_id, caller=caller, reason=reason)


def peek_wakes() -> tuple[dict[str, Any], ...]:
    return default_control_store().pending_wakes()


def ack_wake(wake: dict[str, Any]) -> bool:
    return default_control_store().ack_wake(wake)


def drain_wakes() -> set[str]:
    return set(default_control_store().drain_wakes())


def pending_wakes() -> frozenset[str]:
    return frozenset(row["job_id"] for row in default_control_store().pending_wakes())


def clear_wakes() -> None:
    default_control_store().clear_wakes()
