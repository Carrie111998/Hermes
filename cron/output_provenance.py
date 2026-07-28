"""Strict local provenance claims for Cron final-result delivery.

This module is deliberately independent from the scheduler transport loop.  It
owns the durable, per-profile state machine that a scheduler must complete
before passing a final result to an installed Kit boundary.
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "cron-output-provenance/v1"
LOCK_NAME = ".output-provenance.lock"
ANCHOR_NAME = "output-provenance-anchor.json"
LEDGER_NAME = "output-provenance-ledger.json"
KEY_NAME = "output-provenance.key"
BOOTSTRAP_LOCK_NAME = ".output-provenance-bootstrap.lock"
TTL_SECONDS = 300
REPAIR_LEASE_SECONDS = 60
SEND_RECOVERY_SECONDS = 60
MAX_BODY_BYTES = 8_000
TERMINAL = {"sent", "indeterminate", "blocked"}


class ProvenanceError(RuntimeError):
    """A provenance invariant failed closed."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _require_private_regular(path: Path, *, expected_inode: tuple[int, int] | None = None) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProvenanceError(f"missing provenance path: {path.name}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ProvenanceError(f"provenance path is not a regular file: {path.name}")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise ProvenanceError(f"provenance file permissions invalid: {path.name}")
    if expected_inode is not None and (info.st_dev, info.st_ino) != expected_inode:
        raise ProvenanceError(f"provenance lock identity changed: {path.name}")
    return info


def _require_private_dir(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ProvenanceError("missing provenance directory") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ProvenanceError("provenance directory is invalid")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ProvenanceError("provenance directory permissions invalid")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class ProvenanceStore:
    """Descriptor-checked, flock-serialized profile-local provenance storage."""

    def __init__(self, home: str | Path):
        self.home = Path(home)
        # Cron already owns jobs.json and .jobs.lock. Provenance must neither
        # require that shared directory to be empty nor change its permissions.
        self.root = self.home / "cron" / "output-provenance"

    @property
    def lock_path(self) -> Path:
        return self.root / LOCK_NAME

    @property
    def anchor_path(self) -> Path:
        return self.root / ANCHOR_NAME

    @property
    def ledger_path(self) -> Path:
        return self.root / LEDGER_NAME

    @property
    def key_path(self) -> Path:
        return self.root / KEY_NAME

    def bootstrap(self) -> dict[str, Any]:
        """Provision the strict store; normal issue/claim never creates it."""
        cron_dir = self.root.parent
        cron_dir.mkdir(parents=True, exist_ok=True)
        bootstrap_fd = os.open(cron_dir / BOOTSTRAP_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(bootstrap_fd, 0o600)
            fcntl.flock(bootstrap_fd, fcntl.LOCK_EX)
            return self._bootstrap_locked()
        finally:
            fcntl.flock(bootstrap_fd, fcntl.LOCK_UN)
            os.close(bootstrap_fd)

    def _bootstrap_locked(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        _require_private_dir(self.root)
        existing = list(self.root.iterdir())
        if existing:
            all_files = {LOCK_NAME, LEDGER_NAME, KEY_NAME, ANCHOR_NAME}
            recoverable = all_files - {ANCHOR_NAME}
            names = {path.name for path in existing}
            if names == all_files:
                for path in existing:
                    _require_private_regular(path)
                lock_dev, lock_ino = self._anchor()
                return {"schema_version": SCHEMA_VERSION, "lock_dev": lock_dev, "lock_ino": lock_ino}
            if ANCHOR_NAME in names or not names <= recoverable:
                raise ProvenanceError("provenance bootstrap requires an empty store")
            for path in existing:
                _require_private_regular(path)
                path.unlink()
            directory_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        for path, payload in (
            (self.lock_path, b"lock\n"),
            (self.ledger_path, _canonical({"schema_version": SCHEMA_VERSION, "occurrences": {}})),
            (self.key_path, secrets.token_bytes(32)),
        ):
            _atomic_write(path, payload)
        lock = _require_private_regular(self.lock_path)
        anchor = {
            "schema_version": SCHEMA_VERSION,
            "lock_dev": lock.st_dev,
            "lock_ino": lock.st_ino,
        }
        _atomic_write(self.anchor_path, _canonical(anchor))
        _require_private_regular(self.anchor_path)
        return anchor

    def _anchor(self) -> tuple[int, int]:
        _require_private_dir(self.root)
        _require_private_regular(self.anchor_path)
        try:
            anchor = json.loads(self.anchor_path.read_text(encoding="utf-8"))
            return int(anchor["lock_dev"]), int(anchor["lock_ino"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProvenanceError("malformed provenance anchor") from exc

    @contextlib.contextmanager
    def _locked(self, *, writeback: bool = True) -> Iterator[dict[str, Any]]:
        expected = self._anchor()
        _require_private_regular(self.lock_path, expected_inode=expected)
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if not getattr(os, "O_NOFOLLOW", 0):
            raise ProvenanceError("O_NOFOLLOW is required for provenance locking")
        descriptor = os.open(self.lock_path, flags)
        try:
            locked = os.fstat(descriptor)
            if (locked.st_dev, locked.st_ino) != expected or not stat.S_ISREG(locked.st_mode):
                raise ProvenanceError("provenance lock changed while opening")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _require_private_regular(self.lock_path, expected_inode=expected)
            _require_private_regular(self.ledger_path)
            try:
                ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProvenanceError("malformed provenance ledger") from exc
            if ledger.get("schema_version") != SCHEMA_VERSION or not isinstance(ledger.get("occurrences"), dict):
                raise ProvenanceError("unexpected provenance ledger schema")
            yield ledger
            if writeback:
                _atomic_write(self.ledger_path, _canonical(ledger))
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def health_check(self) -> None:
        """Verify the provisioned store is readable without mutating its ledger."""
        with self._locked(writeback=False):
            key = _require_private_regular(self.key_path)
            del key
            if len(self.key_path.read_bytes()) != 32:
                raise ProvenanceError("invalid provenance key")

    def issue(
        self,
        *,
        profile_id: str,
        job_id: str,
        occurrence_id: str,
        target_id: str,
        route_digest: str,
        raw_body: bytes,
        template_digest: str,
        producer_class: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Create one occurrence/target proof without exposing a signing key."""
        moment = now or _now()
        if not all(isinstance(value, str) and value for value in (profile_id, job_id, occurrence_id, target_id, route_digest, template_digest, producer_class)):
            raise ProvenanceError("proof identity is incomplete")
        if len(raw_body) > MAX_BODY_BYTES:
            raise ProvenanceError("raw final result exceeds provenance limit")
        occurrence_key = _sha256(
            _canonical({"profile_id": profile_id, "job_id": job_id, "occurrence_id": occurrence_id})
        )
        with self._locked() as ledger:
            occurrences = ledger["occurrences"]
            occurrence = occurrences.setdefault(occurrence_key, {"targets": {}})
            targets = occurrence["targets"]
            prior = targets.get(target_id)
            if prior is not None:
                if prior.get("route_digest") != route_digest:
                    raise ProvenanceError("occurrence target route changed")
                raise ProvenanceError("occurrence target already issued")
            capability_id = "cop-" + secrets.token_hex(16)
            body = {
                "schema_version": SCHEMA_VERSION,
                "capability_id": capability_id,
                "profile_id": profile_id,
                "job_id": job_id,
                "occurrence_id": occurrence_id,
                "target_id": target_id,
                "route_digest": route_digest,
                "raw_sha256": _sha256(raw_body),
                "template_digest": template_digest,
                "producer_class": producer_class,
                "issued_at": moment.isoformat(),
                "expires_at": (moment + timedelta(seconds=TTL_SECONDS)).isoformat(),
            }
            key = _require_private_regular(self.key_path)
            del key
            secret = self.key_path.read_bytes()
            body["mac"] = hmac.new(secret, _canonical(body), hashlib.sha256).hexdigest()
            targets[target_id] = {"route_digest": route_digest, "state": "prepared", "proof": body}
            return {"proof": body, "raw_body_b64": base64.b64encode(raw_body).decode("ascii")}

    @staticmethod
    def _proof_mac_body(proof: dict[str, Any]) -> dict[str, Any]:
        body = dict(proof)
        body.pop("mac", None)
        return body

    @staticmethod
    def _claim(ledger: dict[str, Any], capability_id: str) -> dict[str, Any]:
        for occurrence in ledger["occurrences"].values():
            for target in occurrence.get("targets", {}).values():
                proof = target.get("proof")
                if isinstance(proof, dict) and proof.get("capability_id") == capability_id:
                    return target
        raise ProvenanceError("unknown provenance capability")

    def verify_and_claim(
        self,
        *,
        proof: dict[str, Any],
        raw_body_b64: str,
        decision: str,
        replacement_body_b64: str | None = None,
    ) -> dict[str, Any]:
        """Validate a proof and durably bind one allow/rewrite/deny decision."""
        # Verification owns its security clock.  A caller-provided timestamp
        # would let a stale bearer proof select its own pre-expiry instant.
        moment = _now()
        if decision not in {"allow", "rewrite", "deny"}:
            raise ProvenanceError("invalid provenance decision")
        try:
            raw_body = base64.b64decode(raw_body_b64.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise ProvenanceError("invalid raw proof body encoding") from exc
        if len(raw_body) > MAX_BODY_BYTES:
            raise ProvenanceError("raw proof body exceeds limit")
        with self._locked() as ledger:
            target = self._claim(ledger, str(proof.get("capability_id") or ""))
            canonical_proof = target.get("proof")
            if canonical_proof != proof:
                raise ProvenanceError("proof does not match durable capability")
            secret = self.key_path.read_bytes()
            expected_mac = hmac.new(secret, _canonical(self._proof_mac_body(proof)), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(proof.get("mac") or ""), expected_mac):
                raise ProvenanceError("invalid proof MAC")
            try:
                expiry = datetime.fromisoformat(str(proof["expires_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProvenanceError("invalid proof expiry") from exc
            if expiry <= moment:
                target["state"] = "blocked"
                # The exception skips the context manager's normal writeback;
                # persist this terminal state while the strict lock is held.
                _atomic_write(self.ledger_path, _canonical(ledger))
                raise ProvenanceError("expired provenance proof")
            if _sha256(raw_body) != proof.get("raw_sha256"):
                raise ProvenanceError("raw proof body hash mismatch")
            if target.get("state") != "prepared":
                raise ProvenanceError("provenance capability is not prepared")
            if decision == "allow":
                if replacement_body_b64 not in (None, ""):
                    raise ProvenanceError("allow must not supply replacement body")
                body = raw_body
            elif decision == "deny":
                if replacement_body_b64 not in (None, ""):
                    raise ProvenanceError("deny must not supply replacement body")
                target["state"] = "blocked"
                return {"decision": "deny", "capability_id": proof["capability_id"]}
            else:
                if not isinstance(replacement_body_b64, str):
                    raise ProvenanceError("rewrite requires replacement body")
                try:
                    body = base64.b64decode(replacement_body_b64.encode("ascii"), validate=True)
                except (ValueError, UnicodeError) as exc:
                    raise ProvenanceError("invalid replacement body encoding") from exc
                if not body or len(body) > MAX_BODY_BYTES:
                    raise ProvenanceError("replacement body violates limit")
            claim_id = "coc-" + secrets.token_hex(16)
            target.update({
                "state": "claimed",
                "claim_id": claim_id,
                "body_sha256": _sha256(body),
                "claimed_at": moment.isoformat(),
            })
            return {
                "decision": decision,
                "capability_id": proof["capability_id"],
                "claim_id": claim_id,
                "body_b64": base64.b64encode(body).decode("ascii"),
            }

    def begin_send(
        self,
        *,
        capability_id: str,
        claim_id: str,
        body: bytes,
        rendered_body: bytes,
        route_digest: str,
        post_send_repair_context: dict[str, Any] | None = None,
    ) -> None:
        """Fence a claimed delivery immediately before the adapter is invoked."""
        with self._locked() as ledger:
            target = self._claim(ledger, capability_id)
            if target.get("state") != "claimed" or target.get("claim_id") != claim_id:
                raise ProvenanceError("provenance claim is not sendable")
            if target.get("route_digest") != route_digest:
                raise ProvenanceError("provenance route changed before send")
            if target.get("body_sha256") != _sha256(body):
                raise ProvenanceError("provenance body changed before send")
            try:
                expiry = datetime.fromisoformat(str(target["proof"]["expires_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                target["state"] = "blocked"
                _atomic_write(self.ledger_path, _canonical(ledger))
                raise ProvenanceError("invalid provenance expiry") from exc
            if expiry <= _now():
                target["state"] = "blocked"
                _atomic_write(self.ledger_path, _canonical(ledger))
                raise ProvenanceError("provenance proof expired before send")
            if not rendered_body or len(rendered_body) > MAX_BODY_BYTES * 2:
                raise ProvenanceError("rendered provenance body violates limit")
            target["state"] = "send_started"
            target["rendered_sha256"] = _sha256(rendered_body)
            target["send_started_at"] = _now().isoformat().replace("+00:00", "Z")
            target["post_send_recovery_context"] = dict(post_send_repair_context or {})

    def _recover_stale_send_started(self, target: dict[str, Any], *, capability_id: str) -> bool:
        """Converge an ambiguous dispatch without reopening transport delivery."""
        if target.get("state") != "send_started":
            return False
        try:
            started = datetime.fromisoformat(str(target["send_started_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            started = None
        if started is not None and started.tzinfo is not None and started + timedelta(seconds=SEND_RECOVERY_SECONDS) > _now():
            return False
        claim_id = str(target.get("claim_id") or "")
        if not claim_id or not isinstance(target.get("post_send_recovery_context"), dict):
            raise ProvenanceError("provenance send recovery context is missing")
        event_id = f"after-send:{capability_id}:{claim_id}:indeterminate"
        target["state"] = "indeterminate"
        target["post_send_repair"] = {
            "state": "pending",
            "error": "send_started_recovered",
            "context": {
                **target["post_send_recovery_context"],
                "success": False,
                "send_result": {"error": "send_started_recovered"},
                "observer_event_id": event_id,
            },
            "event_id": event_id,
            "attempts": 0,
        }
        return True

    def block_claim(self, *, capability_id: str, claim_id: str) -> None:
        """Terminally reject a claimed result before crossing transport."""
        with self._locked() as ledger:
            target = self._claim(ledger, capability_id)
            if target.get("state") != "claimed" or target.get("claim_id") != claim_id:
                raise ProvenanceError("provenance claim is not blockable")
            target["state"] = "blocked"

    def complete_claim(
        self, *, capability_id: str, claim_id: str, result: str, post_send_error: str = "",
        post_send_repair_context: dict[str, Any] | None = None,
    ) -> None:
        """Complete a started claim after the transport reports its disposition."""
        if result not in TERMINAL:
            raise ProvenanceError("invalid provenance completion")
        with self._locked() as ledger:
            target = self._claim(ledger, capability_id)
            if target.get("state") != "send_started" or target.get("claim_id") != claim_id:
                raise ProvenanceError("provenance claim is not in send_started")
            target["state"] = result
            if post_send_error and not isinstance(post_send_repair_context, dict):
                raise ProvenanceError("post-send repair context is required")
            if isinstance(post_send_repair_context, dict):
                # Persist the terminal disposition before running an observer.
                # A crash or observer failure can only leave this durable work
                # item pending; recovery never re-enters the transport path.
                target["post_send_repair"] = {
                    "state": "pending",
                    "error": post_send_error,
                    "context": {
                        **post_send_repair_context,
                        "observer_event_id": f"after-send:{capability_id}:{claim_id}:{result}",
                    },
                    "event_id": f"after-send:{capability_id}:{claim_id}:{result}",
                    "attempts": 0,
                }
            if post_send_error:
                target["post_send_error"] = post_send_error

    def claim_post_send_repair(self, *, capability_id: str) -> dict[str, Any]:
        """Lease one durable after-send repair without reopening transport send."""
        with self._locked() as ledger:
            target = self._claim(ledger, capability_id)
            self._recover_stale_send_started(target, capability_id=capability_id)
            repair = target.get("post_send_repair")
            if target.get("state") not in TERMINAL or not isinstance(repair, dict):
                raise ProvenanceError("provenance post-send repair is not pending")
            if repair.get("state") == "claimed":
                try:
                    claimed_at = datetime.fromisoformat(str(repair["claimed_at"]).replace("Z", "+00:00"))
                except (KeyError, TypeError, ValueError):
                    claimed_at = None
                if claimed_at is not None and claimed_at.tzinfo is not None and claimed_at + timedelta(seconds=REPAIR_LEASE_SECONDS) > _now():
                    raise ProvenanceError("provenance post-send repair is not pending")
                repair["state"] = "pending"
                repair.pop("repair_id", None)
                repair.pop("claimed_at", None)
            if repair.get("state") != "pending":
                raise ProvenanceError("provenance post-send repair is not pending")
            repair_id = "cor-" + secrets.token_hex(16)
            repair["state"] = "claimed"
            repair["repair_id"] = repair_id
            repair["claimed_at"] = _now().isoformat().replace("+00:00", "Z")
            repair["attempts"] = int(repair.get("attempts") or 0) + 1
            return {
                "capability_id": capability_id,
                "repair_id": repair_id,
                "event_id": str(repair.get("event_id") or ""),
                "context": dict(repair["context"]),
            }

    def complete_post_send_repair(self, *, capability_id: str, repair_id: str, success: bool, error: str = "") -> None:
        """Persist repair disposition; no branch here can resend the message."""
        with self._locked() as ledger:
            target = self._claim(ledger, capability_id)
            repair = target.get("post_send_repair")
            if target.get("state") not in TERMINAL or not isinstance(repair, dict) or repair.get("state") != "claimed" or repair.get("repair_id") != repair_id:
                raise ProvenanceError("provenance post-send repair is not claimed")
            repair["state"] = "repaired" if success else "pending"
            repair["error"] = "" if success else error
            repair.pop("repair_id", None)
            repair.pop("claimed_at", None)

    def pending_post_send_repairs(self) -> list[dict[str, Any]]:
        """List durable after-send repairs without reopening a sent delivery."""
        with self._locked(writeback=False) as ledger:
            repairs: list[dict[str, str]] = []
            current = _now()
            for occurrence in ledger["occurrences"].values():
                for target in occurrence.get("targets", {}).values():
                    proof = target.get("proof") if isinstance(target.get("proof"), dict) else {}
                    if target.get("state") == "send_started":
                        # Any persisted send_started is an ambiguous transport
                        # outcome. It must hard-gate new protected deliveries
                        # immediately, even though its observer repair cannot be
                        # claimed until the send lease becomes stale.
                        repairs.append({
                            "capability_id": str(proof.get("capability_id") or ""),
                            "error": "send_started_recovery_pending",
                            "context": dict(target.get("post_send_recovery_context") or {}),
                        })
                        continue
                    repair = target.get("post_send_repair")
                    expired_lease = False
                    if isinstance(repair, dict) and repair.get("state") == "claimed":
                        try:
                            claimed_at = datetime.fromisoformat(str(repair["claimed_at"]).replace("Z", "+00:00"))
                            expired_lease = claimed_at.tzinfo is not None and claimed_at + timedelta(seconds=REPAIR_LEASE_SECONDS) <= current
                        except (KeyError, TypeError, ValueError):
                            expired_lease = True
                    if target.get("state") in TERMINAL and isinstance(repair, dict) and (repair.get("state") == "pending" or expired_lease):
                        repairs.append({
                            "capability_id": str(proof.get("capability_id") or ""),
                            "error": str(repair.get("error") or ""),
                            "context": dict(repair.get("context") or {}),
                        })
            return repairs
