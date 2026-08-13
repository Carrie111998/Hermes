"""Fail-closed, host-local workspace lifecycle authority.

V1 records and classifies workspace evidence.  It deliberately has no enabled
removal path: a candidate disposition is not permission to mutate Git state.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from typing import Any, Iterator
import uuid

try:  # The lifecycle manager is host-local; Windows is retain-only until it has a lease backend.
    import fcntl
except ImportError:  # pragma: no cover - exercised by the fail-closed branch below
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 2_000
_MAX_REMOTE_FETCH_AGE_SECONDS = 3_600


def manager_registry_path(home: str | Path | None = None) -> Path:
    """Return the manager-owned host registry, never a repository-local index."""
    root = Path(home).expanduser() if home is not None else Path.home() / ".hermes"
    return root / "workspace-lifecycle" / "registry.sqlite3"


def process_start_identity(pid: int | None = None) -> int | None:
    """Return a PID-reuse-resistant process fingerprint, or None when unknown."""
    target = os.getpid() if pid is None else pid
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(target)
    except Exception:
        return None


class WorkspaceState(str, Enum):
    PREPARING = "preparing"
    ACTIVE = "active"
    TERMINAL_PENDING = "terminal_pending"
    RETAINED_UNREVIEWED = "retained_unreviewed"
    QUARANTINED = "quarantined"
    PRESERVED = "preserved"
    BLOCKED_REVIEW = "blocked_review"
    REMOVED = "removed"


class Disposition(str, Enum):
    RETAIN = "retain"
    REMOVABLE = "removable"
    BLOCKED_REVIEW = "blocked_review"


@dataclass(frozen=True)
class Evidence:
    canonical_path: str
    repo_common_dir: str
    head: str | None = None
    branch: str | None = None
    status: str = "unknown"  # clean, dirty, unreadable, timeout, partial
    reachable: str = "unknown"  # proven, local_only, stale, failed, unknown
    pr: str = "unknown"  # terminal, open, unknown
    manager_created: bool = False
    terminal: bool = False
    live_process: bool = False
    lock: str = "unknown"  # none, live, foreign, stale, unknown
    hold: bool = False
    nested: bool = False
    nested_paths: tuple[str, ...] = ()
    remote_fetch_age_seconds: int | None = None
    process_pid: int | None = None
    process_started_at: int | None = None
    lease_heartbeat_at: int | None = None
    open_handles: tuple[str, ...] = ()
    task_hold: bool = False
    evidence_hold: bool = False
    rollback_hold: bool = False
    mount_state: str = "unknown"
    device_state: str = "unknown"
    observation_provenance: str = "unverified"
    preservation_verified: bool = False
    generated_outputs: tuple[str, ...] = ()
    generated_declaration: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    observed_at: int = 0

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @property
    def observation_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    disposition: Disposition
    state: WorkspaceState
    reasons: tuple[str, ...]
    evidence_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "state": self.state.value,
            "reasons": list(self.reasons),
            "evidence_hash": self.evidence_hash,
        }


def classify(evidence: Evidence) -> Decision:
    """Fail closed from a positive whitelist; arbitrary values can only block."""
    reasons: list[str] = []
    # These are exact whitelists, not "not bad" checks.  A newly introduced
    # status/lock value therefore cannot silently become removal-capable.
    if evidence.status != "clean":
        reasons.append("working_tree_changes" if evidence.status in {"dirty", "untracked", "tracked_dirty"}
                       else "unreadable_status")
    if evidence.lock != "none":
        reasons.append("live_process_or_lock")
    if evidence.live_process or evidence.process_pid is not None or evidence.open_handles:
        reasons.append("live_process_or_open_handle")
    if evidence.generated_outputs and not set(evidence.generated_outputs).issubset(evidence.generated_declaration):
        reasons.append("undeclared_generated_output")
    if evidence.reachable != "proven":
        reasons.append("unproven_reachability")
    if evidence.pr != "terminal":
        reasons.append("pr_not_proven_terminal")
    if (evidence.remote_fetch_age_seconds is None or evidence.remote_fetch_age_seconds < 0
            or evidence.remote_fetch_age_seconds > _MAX_REMOTE_FETCH_AGE_SECONDS):
        reasons.append("remote_fetch_not_verified")
    if evidence.hold or evidence.task_hold or evidence.evidence_hold or evidence.rollback_hold:
        reasons.append("preservation_hold")
    if evidence.nested or evidence.nested_paths:
        reasons.append("nested_workspace")
    if evidence.mount_state != "verified" or evidence.device_state != "verified":
        reasons.append("mount_or_device_unverified")
    if not evidence.manager_created or evidence.observation_provenance != "verified_adapter":
        reasons.append("unmanaged_or_unverified_observation")
    # There is no V1 live adapter/attestation issuer.  A caller constructing
    # friendly booleans must not create removal authority by itself.
    reasons.append("manager_removal_authority_unavailable")
    if not evidence.terminal:
        reasons.append("not_terminal")

    blocked = {"unreadable_status", "live_process_or_lock", "live_process_or_open_handle",
               "unproven_reachability", "pr_not_proven_terminal", "remote_fetch_not_verified",
               "mount_or_device_unverified", "unmanaged_or_unverified_observation", "manager_removal_authority_unavailable"}
    if any(reason in blocked for reason in reasons):
        return Decision(Disposition.BLOCKED_REVIEW, WorkspaceState.BLOCKED_REVIEW,
                        tuple(reasons), evidence.observation_hash)
    if reasons:
        return Decision(Disposition.RETAIN, WorkspaceState.RETAINED_UNREVIEWED,
                        tuple(reasons), evidence.observation_hash)
    return Decision(Disposition.REMOVABLE, WorkspaceState.TERMINAL_PENDING, (), evidence.observation_hash)


def _run_git(repo: Path, args: list[str], timeout: float = 5.0) -> tuple[str, str, str]:
    """Read-only Git probe returning (kind, stdout, stderr), never mutating."""
    try:
        result = subprocess.run(["git", *args], cwd=repo, text=True,
                                capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return "timeout", "", "timeout"
    except (OSError, ValueError) as exc:
        return "unreadable", "", str(exc)
    if result.returncode:
        return "unreadable", result.stdout, result.stderr
    return "ok", result.stdout, result.stderr


def _evidence_from_path(path: Path, repo_common_dir: str, *, porcelain_locked: bool = False) -> Evidence:
    canonical = str(path.resolve(strict=False))
    status_kind, status_out, _ = _run_git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    head_kind, head_out, _ = _run_git(path, ["rev-parse", "HEAD"])
    branch_kind, branch_out, _ = _run_git(path, ["branch", "--show-current"])
    if status_kind == "ok":
        status = "clean" if not status_out else "dirty"
    else:
        status = status_kind
    return Evidence(
        canonical_path=canonical,
        repo_common_dir=repo_common_dir,
        head=head_out.strip() if head_kind == "ok" else None,
        branch=branch_out.strip() if branch_kind == "ok" else None,
        status=status,
        # Inventory does not fetch/query a remote or PR provider, so cannot prove either.
        reachable="unknown",
        pr="unknown",
        manager_created=False,
        terminal=False,
        # ``locked`` is emitted by Git's porcelain worktree listing.  Its
        # absence is meaningful Git metadata (not a guessed process check).
        lock="locked" if porcelain_locked else "none",
        notes=("read_only_inventory",),
        observed_at=int(time.time()),
    )


def collect_inventory(repo: str | Path) -> dict[str, Any]:
    """Read Git registrations without writing registry, refs, paths, or caches."""
    root = Path(repo)
    common_kind, common_out, common_err = _run_git(root, ["rev-parse", "--git-common-dir"])
    if common_kind != "ok":
        evidence = Evidence(str(root.resolve(strict=False)), "", status=common_kind)
        decision = classify(evidence)
        return {"schema_version": SCHEMA_VERSION, "repo": str(root), "workspaces": [],
                "disposition": decision.disposition.value, "state": decision.state.value,
                "reasons": list(decision.reasons), "evidence_hash": decision.evidence_hash,
                "error": common_err}
    common_dir = str((root / common_out.strip()).resolve(strict=False)) if not Path(common_out.strip()).is_absolute() else str(Path(common_out.strip()).resolve(strict=False))
    list_kind, listing, list_err = _run_git(root, ["worktree", "list", "--porcelain", "-z"])
    if list_kind != "ok":
        evidence = Evidence(str(root.resolve(strict=False)), common_dir, status=list_kind)
        decision = classify(evidence)
        return {"schema_version": SCHEMA_VERSION, "repo": str(root.resolve(strict=False)), "workspaces": [],
                **decision.as_dict(), "error": list_err}
    records: list[dict[str, Any]] = []
    for block in (item for item in listing.split("\0\0") if item):
        fields: dict[str, str] = {}
        for line in block.split("\0"):
            if not line:
                continue
            key, separator, value = line.partition(" ")
            fields[key] = value if separator else ""
        raw_path = fields.get("worktree")
        if not raw_path:
            continue
        evidence = _evidence_from_path(Path(raw_path), common_dir, porcelain_locked="locked" in fields)
        decision = classify(evidence)
        records.append({"evidence": json.loads(evidence.canonical_json()), **decision.as_dict()})
    # An inventory of legacy registrations is inherently retain/block-only.
    return {"schema_version": SCHEMA_VERSION, "repo": str(root.resolve(strict=False)),
            "workspaces": records, "dry_run": True}


def import_dry_run(repo: str | Path) -> dict[str, Any]:
    """Return the exact import observations without opening or changing a registry."""
    report = collect_inventory(repo)
    report["dry_run"] = True
    report["operation"] = "import"
    if "disposition" not in report:
        report["disposition"] = "blocked_review" if not report["workspaces"] else "retain"
    return report


def build_closeout_manifest(repo: str | Path) -> dict[str, Any]:
    """Bind an owner-review packet to the exact observed paths and evidence.

    This remains a read-only report.  Its hash is a review identity, not removal
    authority; a later apply phase must re-observe every predicate and require an
    exact hash match before it can even consider a mutation.
    """
    inventory = collect_inventory(repo)
    entries = [
        {
            "canonical_path": row["evidence"]["canonical_path"],
            "repo_common_dir": row["evidence"]["repo_common_dir"],
            "head": row["evidence"]["head"],
            "branch": row["evidence"]["branch"],
            "evidence_hash": row["evidence_hash"],
            "disposition": row["disposition"],
            "state": row["state"],
            "reasons": row["reasons"],
        }
        for row in inventory.get("workspaces", [])
    ]
    entries.sort(key=lambda item: item["canonical_path"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repo": inventory["repo"],
        "entries": entries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        **payload,
        "operation": "closeout_manifest",
        "dry_run": True,
        "manifest_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "apply_available": False,
        "disposition": Disposition.BLOCKED_REVIEW.value,
        "state": WorkspaceState.BLOCKED_REVIEW.value,
        "reasons": ["report_only", "owner_review_required", "apply_path_unavailable"],
    }


def normalized_idempotency_key(repo_common_dir: str, base: str, intent: str) -> str:
    normalized = " ".join(intent.split())
    payload = f"{Path(repo_common_dir).resolve(strict=False)}\0{base}\0{normalized}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Registry:
    """SQLite registry. Unavailable/corrupt registry never receives a fallback index."""

    _TRANSITIONS: dict[WorkspaceState, set[WorkspaceState]] = {
        WorkspaceState.PREPARING: {WorkspaceState.ACTIVE, WorkspaceState.BLOCKED_REVIEW},
        WorkspaceState.ACTIVE: {WorkspaceState.TERMINAL_PENDING, WorkspaceState.PRESERVED, WorkspaceState.BLOCKED_REVIEW},
        WorkspaceState.TERMINAL_PENDING: {WorkspaceState.QUARANTINED, WorkspaceState.BLOCKED_REVIEW},
        WorkspaceState.RETAINED_UNREVIEWED: {WorkspaceState.PRESERVED, WorkspaceState.BLOCKED_REVIEW},
        WorkspaceState.QUARANTINED: {WorkspaceState.BLOCKED_REVIEW},  # V1 removal is hard-disabled
        WorkspaceState.PRESERVED: {WorkspaceState.BLOCKED_REVIEW},
        WorkspaceState.BLOCKED_REVIEW: {WorkspaceState.PRESERVED},
        WorkspaceState.REMOVED: set(),
    }

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else manager_registry_path()
        self.lease_path = self.path.with_suffix(self.path.suffix + ".lease")

    def open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("CREATE TABLE IF NOT EXISTS workspace_schema (version INTEGER NOT NULL)")
            row = conn.execute("SELECT version FROM workspace_schema").fetchone()
            if row is None:
                conn.execute("INSERT INTO workspace_schema VALUES (?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise RuntimeError("unsupported workspace registry schema")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                canonical_path TEXT NOT NULL UNIQUE, repo_common_dir TEXT NOT NULL,
                state TEXT NOT NULL, disposition TEXT NOT NULL, reasons TEXT NOT NULL,
                evidence_hash TEXT NOT NULL, record TEXT NOT NULL DEFAULT '{}'
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_observations (
                evidence_hash TEXT PRIMARY KEY, workspace_id TEXT REFERENCES workspaces(id), payload TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_receipts (
                receipt_hash TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), operation TEXT NOT NULL,
                payload TEXT NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS workspace_leases (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id), nonce TEXT NOT NULL, pid INTEGER NOT NULL,
                process_started_at INTEGER, heartbeat_at INTEGER NOT NULL, reason TEXT NOT NULL
            )""")
            # Ordinary opens and reads are non-mutating. Crash recovery is an
            # explicit manager operation under an OS lease (reconcile_preparing).
            return conn
        except Exception:
            conn.close()
            raise

    @contextmanager
    def held_lease(self, workspace_id: str, *, reason: str) -> Iterator[dict[str, Any]]:
        """Hold an OS lock and nonce-bound registry lease for one critical section."""
        if fcntl is None:
            raise RuntimeError("OS lease backend unavailable; retain-only")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        nonce = ""
        handle = self.lease_path.open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("workspace lease held by another manager; retain-only") from exc
            nonce = uuid.uuid4().hex
            now, pid = int(time.time()), os.getpid()
            started = process_start_identity(pid)
            conn = self.open()
            try:
                existing = conn.execute("SELECT nonce, pid, process_started_at FROM workspace_leases WHERE workspace_id=?", (workspace_id,)).fetchone()
                if existing and existing[1] != pid:
                    current = process_start_identity(int(existing[1]))
                    if current is not None and current == existing[2]:
                        raise RuntimeError("foreign live workspace lease; retain-only")
                    conn.execute("UPDATE workspaces SET state=?, disposition=?, reasons=? WHERE id=?",
                                 (WorkspaceState.BLOCKED_REVIEW.value, Disposition.BLOCKED_REVIEW.value,
                                  json.dumps(["stale_or_foreign_lease"]), workspace_id))
                    raise RuntimeError("stale or foreign lease requires review")
                conn.execute("INSERT OR REPLACE INTO workspace_leases VALUES (?, ?, ?, ?, ?, ?)",
                             (workspace_id, nonce, pid, started, now, reason))
            finally:
                conn.close()
            yield {"workspace_id": workspace_id, "nonce": nonce, "pid": pid,
                   "process_started_at": started, "heartbeat_at": now, "reason": reason}
        finally:
            try:
                conn = self.open()
                try:
                    conn.execute("DELETE FROM workspace_leases WHERE workspace_id=? AND nonce=?", (workspace_id, nonce))
                finally:
                    conn.close()
            except Exception:
                pass
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def heartbeat_lease(self, workspace_id: str, nonce: str) -> None:
        conn = self.open()
        try:
            changed = conn.execute("UPDATE workspace_leases SET heartbeat_at=? WHERE workspace_id=? AND nonce=?",
                                   (int(time.time()), workspace_id, nonce)).rowcount
            if changed != 1:
                raise RuntimeError("lease nonce mismatch; retain-only")
        finally:
            conn.close()

    def transition(self, workspace_id: str, target: WorkspaceState, *, reason: str) -> dict[str, Any]:
        """Validate explicit, conservative state transitions and append the reason."""
        conn = self.open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state, reasons FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row:
                raise RuntimeError("unknown workspace transition")
            current = WorkspaceState(row[0])
            if target not in self._TRANSITIONS[current]:
                raise RuntimeError(f"invalid workspace state transition: {current.value}->{target.value}")
            reasons = json.loads(row[1])
            if reason not in reasons:
                reasons.append(reason)
            disposition = Disposition.BLOCKED_REVIEW if target is WorkspaceState.BLOCKED_REVIEW else Disposition.RETAIN
            conn.execute("UPDATE workspaces SET state=?, disposition=?, reasons=? WHERE id=?",
                         (target.value, disposition.value, json.dumps(reasons), workspace_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return self.get(workspace_id) or {}

    def _row(self, row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ("id", "canonical_path", "state", "disposition", "reasons", "evidence_hash", "record")
        data = dict(zip(keys, row))
        data["reasons"] = json.loads(data["reasons"])
        data["record"] = json.loads(data["record"])
        return data

    def get(self, workspace_id: str) -> dict[str, Any] | None:
        conn = self.open()
        try:
            row = conn.execute("SELECT id, canonical_path, state, disposition, reasons, evidence_hash, record FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    def backup_to(self, destination: Path) -> Path:
        """Create a consistent SQLite backup without changing registry authority."""
        destination = Path(destination)
        if destination.resolve(strict=False) == self.path.resolve(strict=False):
            raise RuntimeError("registry backup destination must differ from authority")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = self.open()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def restore_from_backup(self, backup_path: Path) -> None:
        """Restore only into a fresh authority path; never overwrite live state."""
        backup_path = Path(backup_path)
        if not backup_path.is_file():
            raise RuntimeError("registry backup unavailable; retain-only")
        if self.path.exists() and self.path.stat().st_size:
            raise RuntimeError("refusing to overwrite existing registry authority")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        source = sqlite3.connect(backup_path)
        target = sqlite3.connect(self.path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        conn = self.open()
        conn.close()

    def reserve(self, *, workspace_id: str, idempotency_key: str, evidence: Evidence) -> dict[str, Any]:
        """Atomically reserve identity before future Git materialization begins."""
        try:
            conn = self.open()
        except (OSError, sqlite3.Error) as exc:
            raise RuntimeError("registry unavailable; retain-only") from exc
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id, canonical_path, state, disposition, reasons, evidence_hash, record FROM workspaces WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing:
                prior = self._row(existing)
                requested = json.loads(evidence.canonical_json())
                identity_fields = ("canonical_path", "repo_common_dir", "head", "branch")
                if any(prior["record"].get("evidence", {}).get(name) != requested.get(name) for name in identity_fields):
                    raise RuntimeError("idempotency conflict: key maps to different workspace identity")
                conn.execute("COMMIT")
                return prior
            canonical_path = Path(evidence.canonical_path).resolve(strict=False)
            if canonical_path.exists():
                raise RuntimeError("unmanaged pre-existing workspace collision; retain-only")
            record = json.dumps({"schema_version": SCHEMA_VERSION, "evidence": json.loads(evidence.canonical_json()),
                                 "reservation": {"pid": os.getpid(), "process_started_at": process_start_identity(), "reserved_at": int(time.time()),
                                                 "recovery_statement": "interrupted reservation blocks review; no Git mutation resumed"}},
                                sort_keys=True, separators=(",", ":"))
            conn.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (workspace_id, idempotency_key, str(canonical_path),
                          evidence.repo_common_dir, WorkspaceState.PREPARING.value,
                          Disposition.BLOCKED_REVIEW.value, json.dumps(["preparing"]), evidence.observation_hash, record))
            self._append_observation(conn, workspace_id, evidence)
            conn.execute("COMMIT")
            return {"id": workspace_id, "canonical_path": str(canonical_path),
                    "state": WorkspaceState.PREPARING.value, "disposition": Disposition.BLOCKED_REVIEW.value,
                    "reasons": ["preparing"], "evidence_hash": evidence.observation_hash}
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _append_observation(conn: sqlite3.Connection, workspace_id: str, evidence: Evidence) -> None:
        existing = conn.execute("SELECT workspace_id, payload FROM workspace_observations WHERE evidence_hash=?", (evidence.observation_hash,)).fetchone()
        if existing and existing != (workspace_id, evidence.canonical_json()):
            raise RuntimeError("append-only observation conflict")
        conn.execute("INSERT OR IGNORE INTO workspace_observations VALUES (?, ?, ?)",
                     (evidence.observation_hash, workspace_id, evidence.canonical_json()))

    def materialization_result(self, workspace_id: str, evidence: Evidence, *, recovery_statement: str) -> dict[str, Any]:
        """Record the post-reservation observation; failure stays blocked, never retries Git."""
        decision = classify(evidence)
        conn = self.open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
            if not row or WorkspaceState(row[0]) is not WorkspaceState.PREPARING:
                raise RuntimeError("materialization result requires a preparing reservation")
            self._append_observation(conn, workspace_id, evidence)
            payload = {"canonical_path": evidence.canonical_path, "repo_common_dir": evidence.repo_common_dir,
                       "head": evidence.head, "physical_before_bytes": None, "physical_after_bytes": None,
                       "predicate_evidence_hashes": [evidence.observation_hash], "recovery_statement": recovery_statement,
                       "outcome": "blocked" if decision.disposition is Disposition.BLOCKED_REVIEW else "retained",
                       "error": None}
            receipt_hash = self._receipt_in_conn(conn, workspace_id, "materialization", payload)
            conn.execute("UPDATE workspaces SET state=?, disposition=?, reasons=?, evidence_hash=? WHERE id=?",
                         (decision.state.value, decision.disposition.value, json.dumps(decision.reasons), evidence.observation_hash, workspace_id))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        result = self.get(workspace_id) or {}
        result["receipt_hash"] = receipt_hash
        return result

    def create_or_get(self, *, workspace_id: str, idempotency_key: str, evidence: Evidence) -> dict[str, Any]:
        reserved = self.reserve(workspace_id=workspace_id, idempotency_key=idempotency_key, evidence=evidence)
        if reserved.get("state") != WorkspaceState.PREPARING.value:
            return reserved
        return self.materialization_result(workspace_id, evidence, recovery_statement="materialization not invoked by registry; V1 retains outcome")

    def reconcile_preparing(self) -> int:
        """Explicitly fail-close only reservations whose owner is provably gone.

        This is deliberately never called by open() or get(): an observer must
        not convert another manager's live reservation into a terminal state.
        """
        conn = self.open()
        try:
            rows = conn.execute("SELECT id FROM workspaces WHERE state=?", (WorkspaceState.PREPARING.value,)).fetchall()
        finally:
            conn.close()
        reconciled = 0
        for (workspace_id,) in rows:
            with self.held_lease(workspace_id, reason="reconcile_preparing"):
                conn = self.open()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    row = conn.execute("SELECT state, reasons, record FROM workspaces WHERE id=?", (workspace_id,)).fetchone()
                    if not row or WorkspaceState(row[0]) is not WorkspaceState.PREPARING:
                        conn.execute("COMMIT")
                        continue
                    reservation = json.loads(row[2]).get("reservation", {})
                    pid, started = reservation.get("pid"), reservation.get("process_started_at")
                    observed_start = process_start_identity(pid) if isinstance(pid, int) else None
                    age = int(time.time()) - int(reservation.get("reserved_at", 0))
                    interrupted = started is not None and observed_start != started
                    unknown_and_expired = started is None and age >= 300
                    if not (interrupted or unknown_and_expired):
                        conn.execute("COMMIT")
                        continue
                    reasons = json.loads(row[1])
                    if "interrupted_preparing" not in reasons:
                        reasons.append("interrupted_preparing")
                    conn.execute("UPDATE workspaces SET state=?, disposition=?, reasons=? WHERE id=?",
                                 (WorkspaceState.BLOCKED_REVIEW.value, Disposition.BLOCKED_REVIEW.value,
                                  json.dumps(reasons), workspace_id))
                    conn.execute("COMMIT")
                    reconciled += 1
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
        return reconciled

    @staticmethod
    def _validate_receipt_payload(payload: dict[str, Any]) -> None:
        required = {"canonical_path", "repo_common_dir", "head", "physical_before_bytes", "physical_after_bytes",
                    "predicate_evidence_hashes", "recovery_statement", "outcome", "error"}
        missing = sorted(required - set(payload))
        if missing:
            raise RuntimeError(f"receipt missing required immutable fields: {', '.join(missing)}")
        if not isinstance(payload["predicate_evidence_hashes"], list) or not payload["recovery_statement"]:
            raise RuntimeError("receipt predicate evidence and recovery statement are required")

    def _receipt_in_conn(self, conn: sqlite3.Connection, workspace_id: str, operation: str,
                         payload: dict[str, Any], *, receipt_hash: str | None = None) -> str:
        self._validate_receipt_payload(payload)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(f"{workspace_id}\0{operation}\0{encoded}".encode("utf-8")).hexdigest()
        if receipt_hash is not None and receipt_hash != digest:
            raise RuntimeError("immutable receipt hash does not match payload")
        if not conn.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
            raise RuntimeError("orphan receipt rejected: workspace is not registered")
        existing = conn.execute("SELECT workspace_id, operation, payload FROM workspace_receipts WHERE receipt_hash=?", (digest,)).fetchone()
        if existing and existing != (workspace_id, operation, encoded):
            raise RuntimeError("immutable receipt collision")
        conn.execute("INSERT OR IGNORE INTO workspace_receipts VALUES (?, ?, ?, ?)", (digest, workspace_id, operation, encoded))
        return digest

    def receipt(self, workspace_id: str, operation: str, payload: dict[str, Any], *, receipt_hash: str | None = None) -> str:
        conn = self.open()
        try:
            return self._receipt_in_conn(conn, workspace_id, operation, payload, receipt_hash=receipt_hash)
        finally:
            conn.close()

    def show_receipt(self, receipt_hash: str) -> dict[str, Any] | None:
        conn = self.open()
        try:
            row = conn.execute("SELECT receipt_hash, workspace_id, operation, payload FROM workspace_receipts WHERE receipt_hash=?", (receipt_hash,)).fetchone()
            if not row:
                return None
            return {"receipt_hash": row[0], "workspace_id": row[1], "operation": row[2], "payload": json.loads(row[3])}
        finally:
            conn.close()


def closeout_dry_run(registry: Registry, workspace_id: str, evidence: Evidence) -> dict[str, Any]:
    """Classify a terminal transition without changing Git or removing anything."""
    decision = classify(evidence)
    return {"dry_run": True, "workspace_id": workspace_id, **decision.as_dict()}


def remove_exact_path(*_args: Any, **_kwargs: Any) -> None:
    """Future boundary only: V1 never authorizes or invokes removal."""
    raise RuntimeError("workspace removal is disabled in V1; owner receipt required")
