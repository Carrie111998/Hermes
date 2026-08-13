"""Native process identity primitives used by the Kanban attempt fence."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROC_PIDTBSDINFO = 3
MAXCOMLEN = 16
MAX_CANONICAL_BOARD_DBS = 256


class AttemptFenceCapabilityError(RuntimeError):
    """The host cannot provide the native attempt-fence identity contract."""


class AttemptFenceInventoryOverflow(RuntimeError):
    """The canonical board inventory exceeded the fail-closed scan bound."""


class StaleAttemptError(RuntimeError):
    """A worker registration is ambiguous, stale, or internally inconsistent."""


class ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class DarwinProcessIdentity:
    pid: int
    pgid: int
    start_tvsec: int
    start_tvusec: int

    @property
    def token(self) -> str:
        return f"darwin:{self.pid}:{self.start_tvsec}:{self.start_tvusec}"


@dataclass(frozen=True)
class ProcessProvenance:
    caller_pid: int
    caller_pgid: int
    caller_identity: DarwinProcessIdentity
    leader_identity: DarwinProcessIdentity
    board_db_path: str
    task_id: str
    run_id: int
    claim_lock: str
    raw_fence: str


def _darwin_process_identity(pid: int) -> DarwinProcessIdentity | None:
    """Return a PID identity tied to its microsecond process start time."""
    if sys.platform != "darwin":
        return None
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        info = ProcBSDInfo()
        info_size = ctypes.sizeof(info)
        returned = proc_pidinfo(
            int(pid),
            PROC_PIDTBSDINFO,
            0,
            ctypes.byref(info),
            info_size,
        )
    except Exception:
        # Identity discovery is a fail-closed probe. Every binding/kernel
        # failure means "identity unavailable"; callers decide whether that
        # is a no-registration result or a hard capability rejection.
        return None
    if returned != info_size or int(info.pbi_pid) != int(pid):
        return None
    return DarwinProcessIdentity(
        pid=int(info.pbi_pid),
        pgid=int(info.pbi_pgid),
        start_tvsec=int(info.pbi_start_tvsec),
        start_tvusec=int(info.pbi_start_tvusec),
    )


def _host_id() -> str | None:
    """Return the host identifier persisted in a worker fence."""
    try:
        host = socket.gethostname().strip()
    except OSError:
        return None
    if not host or host.casefold() == "unknown":
        return None
    return host


def _require_attempt_fence_platform() -> None:
    """Fail before worker dispatch when native identity is unavailable."""
    if sys.platform != "darwin":
        raise AttemptFenceCapabilityError(
            "worker attempt fencing requires macOS libproc"
        )


def _canonical_board_db_paths() -> list[Path]:
    """Enumerate canonical board databases without consulting routing env."""
    from hermes_cli import kanban_db as kb

    root = Path(kb.kanban_home())
    try:
        resolved_root = Path(str(root.resolve()))
    except OSError as exc:
        raise StaleAttemptError("canonical board root cannot be resolved") from exc
    candidates: list[Path] = [Path(str(root / "kanban.db"))]
    boards = root / "kanban" / "boards"
    try:
        boards_mode = boards.stat().st_mode
    except FileNotFoundError:
        boards_mode = None
    except OSError as exc:
        raise StaleAttemptError("canonical board directory is unreadable") from exc
    if boards_mode is not None:
        if not stat.S_ISDIR(boards_mode):
            raise StaleAttemptError("canonical board path is not a directory")
        try:
            children = sorted(boards.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise StaleAttemptError("canonical board directory listing failed") from exc
        for child in children:
            try:
                child_mode = child.stat().st_mode
            except OSError as exc:
                raise StaleAttemptError("canonical board entry is unreadable") from exc
            if not stat.S_ISDIR(child_mode):
                continue
            try:
                slug = kb._normalize_board_slug(child.name)
            except ValueError:
                continue
            if slug:
                candidates.append(Path(str(child / "kanban.db")))
    resolved_paths: set[Path] = set()
    for path in candidates:
        try:
            resolved = Path(str(path.resolve()))
        except OSError as exc:
            raise StaleAttemptError("canonical board DB cannot be resolved") from exc
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            continue
        try:
            resolved_mode = resolved.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StaleAttemptError("canonical board DB is unreadable") from exc
        if not stat.S_ISREG(resolved_mode):
            raise StaleAttemptError("canonical board DB is not a regular file")
        resolved_paths.add(resolved)
    ordered_paths: list[Path] = []
    for path in resolved_paths:
        ordered_paths.append(path)
    ordered_paths.sort(key=lambda path: path.as_posix())
    return ordered_paths


def _read_registration_rows(path: Path, caller_pgid: int) -> list[sqlite3.Row]:
    uri = f"{path.as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT t.id AS task_id,
                   t.status AS task_status,
                   t.current_run_id AS task_run_id,
                   t.claim_lock AS task_claim_lock,
                   t.worker_pid AS task_worker_pid,
                   t.worker_pgid AS task_worker_pgid,
                   t.worker_identity AS task_worker_identity,
                   t.worker_fence AS task_worker_fence,
                   r.id AS run_id,
                   r.task_id AS run_task_id,
                   r.status AS run_status,
                   r.claim_lock AS run_claim_lock,
                   r.worker_pid AS run_worker_pid,
                   r.worker_pgid AS run_worker_pgid,
                   r.worker_identity AS run_worker_identity,
                   r.worker_fence AS run_worker_fence
              FROM tasks AS t
              LEFT JOIN task_runs AS r ON r.id = t.current_run_id
             WHERE t.worker_pgid = ? AND t.worker_fence IS NOT NULL
            """,
            (caller_pgid,),
        ).fetchall()
    finally:
        conn.close()


def _is_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _is_non_empty_str(value: object) -> bool:
    return type(value) is str and bool(value)


def _validated_provenance(
    path: Path,
    row: sqlite3.Row,
    caller_identity: DarwinProcessIdentity,
) -> ProcessProvenance:
    required_fence_keys = {
        "run_id",
        "claim_lock",
        "host",
        "leader_pid",
        "worker_pgid",
        "worker_identity",
        "reason",
        "created_at",
    }
    required_row_keys = {
        "task_id",
        "task_status",
        "task_run_id",
        "task_claim_lock",
        "task_worker_pid",
        "task_worker_pgid",
        "task_worker_identity",
        "task_worker_fence",
        "run_id",
        "run_task_id",
        "run_status",
        "run_claim_lock",
        "run_worker_pid",
        "run_worker_pgid",
        "run_worker_identity",
        "run_worker_fence",
    }
    try:
        values = {key: row[key] for key in required_row_keys}
    except (IndexError, KeyError, TypeError) as exc:
        raise StaleAttemptError("worker registration row has an invalid shape") from exc

    task_id = values["task_id"]
    task_status = values["task_status"]
    task_run_id = values["task_run_id"]
    task_claim_lock = values["task_claim_lock"]
    task_worker_pid = values["task_worker_pid"]
    task_worker_pgid = values["task_worker_pgid"]
    task_worker_identity = values["task_worker_identity"]
    raw_fence = values["task_worker_fence"]
    run_id = values["run_id"]
    run_task_id = values["run_task_id"]
    run_status = values["run_status"]
    run_claim_lock = values["run_claim_lock"]
    run_worker_pid = values["run_worker_pid"]
    run_worker_pgid = values["run_worker_pgid"]
    run_worker_identity = values["run_worker_identity"]
    run_worker_fence = values["run_worker_fence"]
    row_is_valid = (
        _is_non_empty_str(task_id)
        and _is_non_empty_str(run_task_id)
        and _is_non_empty_str(task_status)
        and _is_non_empty_str(run_status)
        and _is_positive_int(task_run_id)
        and _is_positive_int(run_id)
        and _is_non_empty_str(task_claim_lock)
        and _is_non_empty_str(run_claim_lock)
        and _is_positive_int(task_worker_pid)
        and _is_positive_int(run_worker_pid)
        and _is_positive_int(task_worker_pgid)
        and _is_positive_int(run_worker_pgid)
        and _is_non_empty_str(task_worker_identity)
        and _is_non_empty_str(run_worker_identity)
        and _is_non_empty_str(raw_fence)
        and _is_non_empty_str(run_worker_fence)
    )
    if not row_is_valid:
        raise StaleAttemptError("worker registration row has invalid field types")

    try:
        fence = json.loads(raw_fence)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StaleAttemptError("worker fence is not valid JSON") from exc
    if type(fence) is not dict or set(fence) != required_fence_keys:
        raise StaleAttemptError("worker fence has an invalid shape")
    fence_is_valid = (
        _is_positive_int(fence["run_id"])
        and _is_non_empty_str(fence["claim_lock"])
        and _is_non_empty_str(fence["host"])
        and _is_positive_int(fence["leader_pid"])
        and _is_positive_int(fence["worker_pgid"])
        and _is_non_empty_str(fence["worker_identity"])
        and _is_non_empty_str(fence["reason"])
        and _is_positive_int(fence["created_at"])
    )
    if not fence_is_valid:
        raise StaleAttemptError("worker fence has invalid field types")
    canonical = json.dumps(fence, sort_keys=True, separators=(",", ":"))
    if canonical != raw_fence:
        raise StaleAttemptError("worker fence is not canonical JSON")

    copies_match = (
        task_run_id == run_id
        and run_task_id == task_id
        and task_claim_lock == run_claim_lock
        and task_worker_pid == run_worker_pid
        and task_worker_pgid == run_worker_pgid
        and task_worker_identity == run_worker_identity
        and raw_fence == run_worker_fence
        and task_status == "running"
        and run_status == "running"
    )
    if not copies_match:
        raise StaleAttemptError("task and run worker registrations differ")
    current_host = _host_id()
    if current_host is None:
        raise StaleAttemptError("host identity is unavailable")
    fence_matches = (
        fence["run_id"] == task_run_id
        and fence["claim_lock"] == task_claim_lock
        and fence["host"] == current_host
        and fence["leader_pid"] == task_worker_pid
        and fence["worker_pgid"] == task_worker_pgid
        and fence["worker_identity"] == task_worker_identity
    )
    if not fence_matches:
        raise StaleAttemptError("worker fence does not match the active run")
    leader_identity = _darwin_process_identity(task_worker_pid)
    if (
        leader_identity is None
        or leader_identity.pgid != task_worker_pgid
        or leader_identity.token != task_worker_identity
    ):
        raise StaleAttemptError("worker process identity is no longer current")
    return ProcessProvenance(
        caller_pid=caller_identity.pid,
        caller_pgid=caller_identity.pgid,
        caller_identity=caller_identity,
        leader_identity=leader_identity,
        board_db_path=str(path),
        task_id=task_id,
        run_id=task_run_id,
        claim_lock=task_claim_lock,
        raw_fence=raw_fence,
    )


def _discover_current_worker_registration() -> ProcessProvenance | None:
    """Scan canonical boards read-only; zero match means manual operator."""
    _require_attempt_fence_platform()
    caller_pid = os.getpid()
    caller_pgid = os.getpgid(0)
    caller_identity = _darwin_process_identity(caller_pid)
    if caller_identity is None or caller_identity.pgid != caller_pgid:
        raise StaleAttemptError("caller process identity is unavailable")
    paths = _canonical_board_db_paths()
    if len(paths) > MAX_CANONICAL_BOARD_DBS:
        raise AttemptFenceInventoryOverflow(
            f"canonical board inventory has {len(paths)} databases; "
            f"maximum is {MAX_CANONICAL_BOARD_DBS}"
        )
    matches: list[tuple[Path, sqlite3.Row]] = []
    try:
        for path in paths:
            matches.extend(
                (path, row) for row in _read_registration_rows(path, caller_pgid)
            )
    except sqlite3.Error as exc:
        raise StaleAttemptError("canonical board registration scan failed") from exc
    if not matches:
        return None
    if len(matches) != 1:
        raise StaleAttemptError(
            "multiple worker registrations match this process group"
        )
    path, row = matches[0]
    return _validated_provenance(path, row, caller_identity)


def main(argv: Sequence[str] | None = None) -> int:
    """Wait for the dispatcher bind token, then replace this bootstrap."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-fd", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        return 64
    token = os.read(args.gate_fd, 1)
    os.close(args.gate_fd)
    if token != b"1":
        return 125
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())
