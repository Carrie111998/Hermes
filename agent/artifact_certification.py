"""Deterministic artifact writing and certification.

The content generator supplies draft bytes only. This wrapper owns the output
path, evaluates an immutable snapshot of exact acceptance criteria, and records
an append-only PASS/FAIL outcome. Agent prose is data, never certification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_PASS_CLAIM_RE = re.compile(r"\bpass(?:ed|es|ing)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class ExactCountCriterion:
    """Require an exact literal string to occur exactly ``expected_count`` times."""

    name: str
    text: str
    expected_count: int

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("criterion name must not be empty")
        if not self.text:
            raise ValueError("criterion text must not be empty")
        if self.expected_count < 0:
            raise ValueError("expected_count must be non-negative")


@dataclass(frozen=True)
class ArtifactContract:
    """Authoritative deliverable, certified snapshot, and deterministic criteria."""

    output_path: Path
    workspace_root: Path
    artifact_path: Path
    criteria: tuple[ExactCountCriterion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_path", Path(self.output_path).expanduser().resolve())
        workspace_root = Path(self.workspace_root).expanduser().resolve(strict=True)
        if not workspace_root.is_dir():
            raise ValueError("workspace_root must be an existing directory")
        artifact_path = Path(self.artifact_path)
        if artifact_path.is_absolute() or not artifact_path.parts:
            raise ValueError("artifact_path must be workspace-relative")
        if any(part in {"", ".", ".."} for part in artifact_path.parts):
            raise ValueError("artifact_path must not contain traversal components")
        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "artifact_path", artifact_path)
        object.__setattr__(self, "criteria", tuple(self.criteria))
        if not self.criteria:
            raise ValueError("at least one deterministic acceptance criterion is required")


@dataclass(frozen=True)
class CriterionResult:
    name: str
    text: str
    expected_count: int
    actual_count: int
    passed: bool


@dataclass(frozen=True)
class CertificationResult:
    run_id: str
    status: str
    output_path: str
    artifact_path: str
    contract_hash: str
    artifact_hash: str
    checks: tuple[CriterionResult, ...]
    agent_draft_claimed_pass: bool
    recorded_at: str


@dataclass(frozen=True)
class _ContractSnapshot:
    output_path: str
    workspace_root: str
    artifact_path: str
    criteria: tuple[ExactCountCriterion, ...]
    contract_hash: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_contract(contract: ArtifactContract) -> _ContractSnapshot:
    """Copy and hash the contract before any generator code is allowed to run."""
    criteria = tuple(
        ExactCountCriterion(item.name, item.text, item.expected_count)
        for item in contract.criteria
    )
    payload = {
        "output_path": str(Path(contract.output_path).expanduser().resolve()),
        "workspace_root": str(contract.workspace_root),
        "artifact_path": contract.artifact_path.as_posix(),
        "criteria": [asdict(item) for item in criteria],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return _ContractSnapshot(
        output_path=payload["output_path"],
        workspace_root=payload["workspace_root"],
        artifact_path=payload["artifact_path"],
        criteria=criteria,
        contract_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _ensure_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_certifications (
            run_id TEXT PRIMARY KEY,
            recorded_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
            output_path TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            agent_draft_claimed_pass INTEGER NOT NULL CHECK(agent_draft_claimed_pass IN (0, 1))
        )
        """
    )
    certification_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(artifact_certifications)")
    }
    if "artifact_path" not in certification_columns:
        conn.execute(
            "ALTER TABLE artifact_certifications "
            "ADD COLUMN artifact_path TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS artifact_certifications_no_update
        BEFORE UPDATE ON artifact_certifications
        BEGIN
            SELECT RAISE(ABORT, 'certification outcomes are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS artifact_certifications_no_delete
        BEFORE DELETE ON artifact_certifications
        BEGIN
            SELECT RAISE(ABORT, 'certification outcomes are immutable');
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_certification_reservations (
            run_id TEXT PRIMARY KEY,
            reserved_at TEXT NOT NULL,
            contract_hash TEXT NOT NULL
        )
        """
    )
    reservation_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(artifact_certification_reservations)")
    }
    if "contract_hash" not in reservation_columns:
        conn.execute(
            "ALTER TABLE artifact_certification_reservations "
            "ADD COLUMN contract_hash TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS artifact_certification_reservations_no_update
        BEFORE UPDATE ON artifact_certification_reservations
        BEGIN
            SELECT RAISE(ABORT, 'certification reservations are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS artifact_certification_reservations_no_delete
        BEFORE DELETE ON artifact_certification_reservations
        BEGIN
            SELECT RAISE(ABORT, 'certification reservations are immutable');
        END
        """
    )

    # Durable intent bridges the filesystem replace and SQLite commit. A retry
    # after process death completes the original staged bytes instead of leaving
    # an immutable reservation or accepting a replacement draft.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact_certification_pending (
            run_id TEXT PRIMARY KEY,
            staged_path TEXT NOT NULL,
            artifact_bytes BLOB,
            recorded_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('PASS', 'FAIL')),
            output_path TEXT NOT NULL,
            artifact_path TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            checks_json TEXT NOT NULL,
            agent_draft_claimed_pass INTEGER NOT NULL CHECK(agent_draft_claimed_pass IN (0, 1))
        )
        """
    )
    pending_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(artifact_certification_pending)")
    }
    if "artifact_path" not in pending_columns:
        conn.execute(
            "ALTER TABLE artifact_certification_pending "
            "ADD COLUMN artifact_path TEXT NOT NULL DEFAULT ''"
        )
    if "artifact_bytes" not in pending_columns:
        conn.execute(
            "ALTER TABLE artifact_certification_pending ADD COLUMN artifact_bytes BLOB"
        )


def _reserve_run_id(ledger_path: Path, run_id: str, contract_hash: str) -> None:
    """Claim a run ID before untrusted generation or artifact mutation."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ledger(conn)
        recoverable = conn.execute(
            """
            SELECT contract_hash FROM artifact_certifications WHERE run_id = ?
            UNION ALL
            SELECT contract_hash FROM artifact_certification_pending WHERE run_id = ?
            LIMIT 1
            """,
            (run_id, run_id),
        ).fetchone()
        if recoverable is not None:
            if recoverable[0] != contract_hash:
                raise sqlite3.IntegrityError(
                    f"run_id belongs to a different contract: {run_id}"
                )
            return
        reservation = conn.execute(
            "SELECT contract_hash FROM artifact_certification_reservations WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if reservation is not None:
            raise sqlite3.IntegrityError(f"run_id already reserved: {run_id}")
        conn.execute(
            """
            INSERT INTO artifact_certification_reservations(
                run_id, reserved_at, contract_hash
            ) VALUES (?, ?, ?)
            """,
            (run_id, _utc_now(), contract_hash),
        )


def _stage_exact_path(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".hermes-certification.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return Path(temporary)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_workspace_file(root_fd: int, relative_path: str) -> bytes:
    """Read a regular workspace file through symlink-safe openat traversal."""
    parts = Path(relative_path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact_path must remain workspace-relative")
    directory_fd = os.dup(root_fd)
    file_fd: int | None = None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        for component in parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise OSError(f"authoritative artifact is not a regular file: {relative_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _read_regular_file_nofollow(path: Path) -> bytes:
    """Read a named regular file without following a replacement symlink."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"path is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _publish_exact_output(path: Path, content: bytes) -> None:
    """Create the output relative to a pinned parent directory descriptor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.open(path.parent, parent_flags)
    parent_identity = os.fstat(parent_fd)
    name = path.name
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

    def _parent_is_attached() -> bool:
        try:
            current = os.lstat(path.parent)
        except OSError:
            return False
        return (
            stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino)
            == (parent_identity.st_dev, parent_identity.st_ino)
        )

    try:
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            try:
                existing_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                try:
                    opened = os.fstat(existing_fd)
                    if not stat.S_ISREG(opened.st_mode):
                        raise OSError("output is not a regular file")
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(existing_fd, 1024 * 1024)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    existing = b"".join(chunks)
                finally:
                    os.close(existing_fd)
            except OSError as exc:
                raise RuntimeError(f"certification output path is unsafe: {path}") from exc
            if existing != content:
                raise RuntimeError(
                    f"certification output already contains different bytes: {path}"
                )
            if not _parent_is_attached():
                raise RuntimeError(f"certification output parent identity changed: {path}")
            return
        try:
            view = memoryview(content)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            committed: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                committed.append(chunk)
            if b"".join(committed) != content:
                raise RuntimeError(f"certification output write mismatch: {path}")
            opened = os.fstat(fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or not _parent_is_attached()
            ):
                raise RuntimeError(f"certification output identity changed: {path}")
            os.fsync(parent_fd)
        except BaseException:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(fd)
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _evaluate(content: str, criteria: Iterable[ExactCountCriterion]) -> tuple[CriterionResult, ...]:
    return tuple(
        CriterionResult(
            name=criterion.name,
            text=criterion.text,
            expected_count=criterion.expected_count,
            actual_count=content.count(criterion.text),
            passed=content.count(criterion.text) == criterion.expected_count,
        )
        for criterion in criteria
    )


def _row_to_result(row: sqlite3.Row) -> CertificationResult:
    checks = json.loads(row["checks_json"])
    return CertificationResult(
        run_id=row["run_id"],
        status=row["status"],
        output_path=row["output_path"],
        artifact_path=row["artifact_path"],
        contract_hash=row["contract_hash"],
        artifact_hash=row["artifact_hash"],
        checks=tuple(CriterionResult(**{"text": "", **item}) for item in checks),
        agent_draft_claimed_pass=bool(row["agent_draft_claimed_pass"]),
        recorded_at=row["recorded_at"],
    )


def _minimized_checks_json(checks: Iterable[CriterionResult]) -> str:
    """Serialize deterministic counts without retaining private criterion text."""
    return json.dumps(
        [
            {
                "name": check.name,
                "expected_count": check.expected_count,
                "actual_count": check.actual_count,
                "passed": check.passed,
            }
            for check in checks
        ],
        sort_keys=True,
    )


def _insert_result(conn: sqlite3.Connection, result: CertificationResult) -> None:
    conn.execute(
        """
        INSERT INTO artifact_certifications(
            run_id, recorded_at, status, output_path, artifact_path, contract_hash,
            artifact_hash, checks_json, agent_draft_claimed_pass
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.run_id,
            result.recorded_at,
            result.status,
            "",
            "",
            result.contract_hash,
            result.artifact_hash,
            _minimized_checks_json(result.checks),
            int(result.agent_draft_claimed_pass),
        ),
    )


def _hydrate_result(
    result: CertificationResult, snapshot: _ContractSnapshot
) -> CertificationResult:
    """Restore runtime-only data from the wrapper's pinned contract."""
    criterion_text = {criterion.name: criterion.text for criterion in snapshot.criteria}
    return replace(
        result,
        output_path=snapshot.output_path,
        artifact_path=str(Path(snapshot.workspace_root) / snapshot.artifact_path),
        checks=tuple(
            replace(check, text=criterion_text.get(check.name, ""))
            for check in result.checks
        ),
    )


def _finalize_pending(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    output_path: Path,
) -> CertificationResult:
    result = _row_to_result(row)
    staged_path_text = row["staged_path"] or ""
    staged_path = None
    if staged_path_text:
        stored_staged_path = Path(staged_path_text)
        # New rows retain only the private temporary basename. Accept an
        # absolute value solely for recovery of ledgers written by older code.
        if stored_staged_path.is_absolute():
            staged_path = stored_staged_path
        elif stored_staged_path.name == staged_path_text:
            staged_path = output_path.parent / stored_staged_path
        else:
            raise RuntimeError(
                f"pending certification {result.run_id} has an unsafe staged path"
            )
    journaled = row["artifact_bytes"]
    if journaled is None:
        if staged_path is None:
            raise RuntimeError(
                f"pending certification {result.run_id} has no journaled bytes"
            )
        try:
            artifact_bytes = _read_regular_file_nofollow(staged_path)
        except OSError as exc:
            raise RuntimeError(
                f"pending certification {result.run_id} staged bytes changed"
            ) from exc
    else:
        artifact_bytes = bytes(journaled)
    if hashlib.sha256(artifact_bytes).hexdigest() != result.artifact_hash:
        raise RuntimeError(f"pending certification {result.run_id} journaled bytes changed")
    if result.status == "PASS":
        # Publication owns the output descriptor from its first path lookup
        # through the write and verification. Do not preflight the path here
        # and reopen it later, because the directory entry can change between
        # those operations.
        _publish_exact_output(output_path, artifact_bytes)
    if staged_path is not None and staged_path.exists():
        staged_path.unlink()

    _insert_result(conn, result)
    conn.execute(
        "DELETE FROM artifact_certification_pending WHERE run_id = ?",
        (result.run_id,),
    )
    return result


def _commit_failure(
    ledger_path: Path,
    result: CertificationResult,
) -> CertificationResult:
    """Record a deterministic FAIL without staging or retaining artifact bytes."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ledger(conn)
        recorded = conn.execute(
            "SELECT * FROM artifact_certifications WHERE run_id = ?", (result.run_id,)
        ).fetchone()
        if recorded is not None:
            return _row_to_result(recorded)
        pending = conn.execute(
            "SELECT * FROM artifact_certification_pending WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        if pending is not None:
            # A prior PASS owns this run and must complete crash recovery.
            # Legacy FAIL rows are finalized without publishing their bytes.
            return _finalize_pending(
                conn,
                pending,
                output_path=Path(result.output_path),
            )
        _insert_result(conn, result)
    return result


def _commit_recoverable(
    ledger_path: Path,
    result: CertificationResult,
    staged_path: Path,
    artifact_bytes: bytes,
) -> CertificationResult:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    pending_result: CertificationResult | None = None
    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ledger(conn)
        recorded = conn.execute(
            "SELECT * FROM artifact_certifications WHERE run_id = ?", (result.run_id,)
        ).fetchone()
        if recorded is not None:
            # The ledger may commit before the ACP transcript does. A retry of
            # the same stable turn identity must recover that immutable first
            # result, never replace it with newly generated bytes.
            staged_path.unlink(missing_ok=True)
            return _row_to_result(recorded)
        pending = conn.execute(
            "SELECT * FROM artifact_certification_pending WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        if pending is None:
            conn.execute(
                """
                INSERT INTO artifact_certification_pending(
                    run_id, staged_path, artifact_bytes, recorded_at, status, output_path,
                    artifact_path, contract_hash, artifact_hash, checks_json,
                    agent_draft_claimed_pass
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    staged_path.name,
                    artifact_bytes,
                    result.recorded_at,
                    result.status,
                    "",
                    "",
                    result.contract_hash,
                    result.artifact_hash,
                    _minimized_checks_json(result.checks),
                    int(result.agent_draft_claimed_pass),
                ),
            )
        else:
            # The immutable staged draft from the first attempt owns this run.
            staged_path.unlink(missing_ok=True)
            pending_result = _row_to_result(pending)

    with sqlite3.connect(ledger_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ledger(conn)
        pending = conn.execute(
            "SELECT * FROM artifact_certification_pending WHERE run_id = ?",
            (result.run_id,),
        ).fetchone()
        if pending is None:
            recorded = conn.execute(
                "SELECT * FROM artifact_certifications WHERE run_id = ?",
                (result.run_id,),
            ).fetchone()
            if recorded is None:
                raise RuntimeError(f"certification commit state disappeared: {result.run_id}")
            return _row_to_result(recorded)
        committed = _finalize_pending(
            conn,
            pending,
            output_path=Path(result.output_path),
        )
    return pending_result or committed


def read_certification(ledger_path: str | Path, run_id: str) -> CertificationResult | None:
    path = Path(ledger_path).expanduser().resolve()
    if not path.exists():
        return None
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_ledger(conn)
        row = conn.execute(
            "SELECT * FROM artifact_certifications WHERE run_id = ?", (run_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_result(row)


class CertifiedArtifactWrapper:
    """Write and certify untrusted draft text under deterministic ownership."""

    def __init__(self, *, contract: ArtifactContract, ledger_path: str | Path):
        self._snapshot = _snapshot_contract(contract)
        self._ledger_path = Path(ledger_path).expanduser().resolve()
        self._reserved_run_ids: set[str] = set()
        root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self._workspace_fd: int | None = os.open(
            self._snapshot.workspace_root, root_flags
        )

    def __del__(self) -> None:
        workspace_fd = getattr(self, "_workspace_fd", None)
        if workspace_fd is not None:
            try:
                os.close(workspace_fd)
            except OSError:
                pass
            self._workspace_fd = None

    def reserve(self, run_id: str) -> None:
        """Durably bind a run identity to this contract before model execution."""
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        _reserve_run_id(self._ledger_path, run_id, self._snapshot.contract_hash)
        self._reserved_run_ids.add(run_id)

    def run(self, *, run_id: str, draft: str) -> CertificationResult:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if not isinstance(draft, str):
            raise TypeError("draft must be str content")

        if run_id not in self._reserved_run_ids:
            self.reserve(run_id)

        artifact_path = Path(self._snapshot.workspace_root) / self._snapshot.artifact_path
        workspace_fd = self._workspace_fd
        if workspace_fd is None:
            raise RuntimeError("artifact workspace descriptor is closed")
        artifact_bytes = _read_workspace_file(workspace_fd, self._snapshot.artifact_path)
        artifact = artifact_bytes.decode("utf-8")
        output_path = Path(self._snapshot.output_path)
        # Evaluate and hash only the bytes read from the pinned workspace
        # descriptor. Failed bytes are never staged or published.
        checks = _evaluate(artifact, self._snapshot.criteria)
        status = "PASS" if all(check.passed for check in checks) else "FAIL"
        result = CertificationResult(
            run_id=run_id,
            status=status,
            output_path=str(output_path),
            artifact_path=str(artifact_path),
            contract_hash=self._snapshot.contract_hash,
            artifact_hash=hashlib.sha256(artifact_bytes).hexdigest(),
            checks=checks,
            agent_draft_claimed_pass=bool(_PASS_CLAIM_RE.search(draft)),
            recorded_at=_utc_now(),
        )
        try:
            if status == "FAIL":
                committed = _commit_failure(self._ledger_path, result)
            else:
                staged_path = _stage_exact_path(output_path, artifact_bytes)
                committed = _commit_recoverable(
                    self._ledger_path, result, staged_path, artifact_bytes
                )
            return _hydrate_result(committed, self._snapshot)
        except BaseException:
            self._reserved_run_ids.discard(run_id)
            raise


__all__ = [
    "ArtifactContract",
    "CertificationResult",
    "CertifiedArtifactWrapper",
    "CriterionResult",
    "ExactCountCriterion",
    "read_certification",
]
