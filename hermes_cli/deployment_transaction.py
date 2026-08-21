"""Generation-fenced mutation authority for one Hermes deployment update.

This module is intentionally deployment-specific.  It does not try to wrap every
mutation in Hermes.  It binds the Phase-2 updater mutation boundary to the exact
DeploymentPlan, canonical checkout, pre-mutation source generation, and one
durable operation owner.  Callers may only report source mutation success after
the same authority observes the committed generation.

Architecture interlocks: #88683, #90049, #90144, #90145, #90150, #90866,
#91277.  The admission model lives in ``deployment_plan.py``; this module is the
physical ownership/fencing layer immediately below it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.deployment_plan import (
    DeploymentMutationDenied,
    DeploymentPlan,
    DeploymentPlanError,
)

TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_LOCK_NAME = "deployment-update.lock"
TRANSACTION_RECEIPT_NAME = "deployment-update-receipt.json"


class DeploymentTransactionError(DeploymentPlanError):
    """Base error for deployment mutation ownership/proof failures."""

    code = "DEPLOYMENT_TRANSACTION_ERROR"


class DeploymentTransactionBusy(DeploymentTransactionError):
    """Another live or unreconciled update transaction owns this deployment."""

    code = "DEPLOYMENT_TRANSACTION_BUSY"


class DeploymentTransactionStale(DeploymentTransactionError):
    """The transaction's durable ownership token no longer matches this caller."""

    code = "DEPLOYMENT_TRANSACTION_STALE"


class DeploymentGenerationMismatch(DeploymentTransactionError):
    """The committed source generation is not the generation this plan allowed."""

    code = "DEPLOYMENT_GENERATION_MISMATCH"


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _git_generation(checkout: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise DeploymentTransactionError(
            f"cannot inspect deployment generation at {checkout}: {exc}",
            remediation="repair the canonical checkout before retrying the update",
        ) from exc
    generation = result.stdout.strip()
    if result.returncode != 0 or len(generation) != 40:
        detail = (result.stderr or result.stdout).strip()
        raise DeploymentTransactionError(
            f"cannot resolve deployment generation at {checkout}: {detail or 'git rev-parse failed'}",
            remediation="repair the canonical checkout before retrying the update",
        )
    return generation


def _write_durable_json(path: Path, payload: dict[str, Any], *, exclusive: bool = False) -> None:
    """Write JSON and fsync both the file and containing directory when possible."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentTransactionStale(
            f"deployment transaction authority at {path} is unreadable",
            remediation=(
                "do not continue mutation; reconcile the existing update transaction "
                "and its deployment-update.lock first"
            ),
        ) from exc
    if not isinstance(raw, dict):
        raise DeploymentTransactionStale(
            f"deployment transaction authority at {path} is not an object"
        )
    return raw


@dataclass(frozen=True, slots=True)
class DeploymentMutationPermit:
    """Exact immutable authority carried from admission to commit verification."""

    operation_id: str
    plan_digest: str
    canonical_checkout: str
    admitted_generation: str
    target_generation: str | None
    issued_at_ns: int

    @property
    def digest(self) -> str:
        payload = {
            "operation_id": self.operation_id,
            "plan_digest": self.plan_digest,
            "canonical_checkout": self.canonical_checkout,
            "admitted_generation": self.admitted_generation,
            "target_generation": self.target_generation,
            "issued_at_ns": self.issued_at_ns,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "plan_digest": self.plan_digest,
            "canonical_checkout": self.canonical_checkout,
            "admitted_generation": self.admitted_generation,
            "target_generation": self.target_generation,
            "issued_at_ns": self.issued_at_ns,
            "permit_digest": self.digest,
        }


class DeploymentUpdateTransaction:
    """Exclusive generation-fenced authority for one in-place deployment update.

    Acquisition is fail-closed and durable: the lock is created with O_EXCL and
    contains the exact permit.  Every later transition rereads that token before
    trusting ownership, so replacing the lock after admission cannot self-certify
    a stale transaction.  The lock is only removed by the authority that created
    it, after either a verified commit or a recorded abort.
    """

    def __init__(self, plan: DeploymentPlan) -> None:
        if not plan.in_place_mutation_allowed:
            raise DeploymentMutationDenied(
                f"deployment kind {plan.kind.value!r} does not permit in-place mutation"
            )
        if plan.canonical_checkout is None:
            raise DeploymentMutationDenied(
                "deployment plan does not identify a canonical checkout"
            )
        checkout = _resolved(plan.canonical_checkout)
        if checkout != _resolved(Path.cwd()) and checkout != _resolved(plan.canonical_checkout):
            # Defensive shape guard; admission remains the owner of cwd policy.
            raise DeploymentMutationDenied("canonical checkout identity changed during admission")

        self.plan = plan
        self.checkout = checkout
        self.lock_path = _resolved(plan.hermes_home) / TRANSACTION_LOCK_NAME
        self.receipt_path = _resolved(plan.hermes_home) / TRANSACTION_RECEIPT_NAME
        self.permit = DeploymentMutationPermit(
            operation_id=uuid.uuid4().hex,
            plan_digest=plan.digest,
            canonical_checkout=str(checkout),
            admitted_generation=_git_generation(checkout),
            target_generation=plan.target_generation,
            issued_at_ns=time.time_ns(),
        )
        self._acquired = False
        self._settled = False

    def acquire(self) -> "DeploymentUpdateTransaction":
        if self._acquired:
            return self
        try:
            _write_durable_json(self.lock_path, self.permit.to_dict(), exclusive=True)
        except FileExistsError as exc:
            existing = None
            try:
                existing = _read_json(self.lock_path)
            except DeploymentTransactionStale:
                pass
            owner = existing.get("operation_id") if existing else "unknown"
            raise DeploymentTransactionBusy(
                f"deployment update authority is already held by operation {owner}",
                remediation=(
                    f"reconcile {self.lock_path}; Hermes will not overlap deployment mutations"
                ),
            ) from exc
        self._acquired = True
        return self

    def assert_current(self) -> None:
        if not self._acquired or self._settled:
            raise DeploymentTransactionStale("deployment transaction is not active")
        durable = _read_json(self.lock_path)
        expected = self.permit.to_dict()
        for key in (
            "schema_version",
            "operation_id",
            "plan_digest",
            "canonical_checkout",
            "admitted_generation",
            "target_generation",
            "issued_at_ns",
            "permit_digest",
        ):
            if durable.get(key) != expected.get(key):
                raise DeploymentTransactionStale(
                    f"deployment transaction authority changed after admission ({key})",
                    remediation="stop mutation and reconcile the durable transaction owner",
                )
        if _resolved(Path(str(durable["canonical_checkout"]))) != self.checkout:
            raise DeploymentTransactionStale("canonical checkout identity changed after admission")
        if self.plan.digest != self.permit.plan_digest:
            raise DeploymentTransactionStale("deployment-plan authority changed after admission")

    def verify_committed_generation(self) -> str:
        """Bind update completion to the generation now present at the admitted root."""

        self.assert_current()
        observed = _git_generation(self.checkout)
        target = self.permit.target_generation
        if target is not None and observed != target:
            raise DeploymentGenerationMismatch(
                f"deployment committed generation {observed} but authority required {target}",
                remediation="do not report update success; restore or advance to the authorized generation",
            )
        return observed

    def settle_verified(self, committed_generation: str) -> None:
        self.assert_current()
        receipt = {
            **self.permit.to_dict(),
            "status": "verified",
            "committed_generation": committed_generation,
            "verified_at_ns": time.time_ns(),
        }
        _write_durable_json(self.receipt_path, receipt)
        self._release_owned_lock()
        self._settled = True

    def abort(self, reason: str) -> None:
        if self._settled:
            return
        if self._acquired:
            self.assert_current()
            receipt = {
                **self.permit.to_dict(),
                "status": "aborted",
                "reason": reason,
                "observed_generation": _git_generation(self.checkout),
                "aborted_at_ns": time.time_ns(),
            }
            _write_durable_json(self.receipt_path, receipt)
            self._release_owned_lock()
        self._settled = True

    def _release_owned_lock(self) -> None:
        # Re-read immediately before unlink: authority cannot replace itself and
        # then use an old in-memory permit to delete the successor's lock.
        self.assert_current()
        try:
            self.lock_path.unlink()
        except OSError as exc:
            raise DeploymentTransactionError(
                f"cannot release deployment update authority at {self.lock_path}: {exc}",
                remediation="reconcile the durable lock before starting another update",
            ) from exc
        try:
            directory_fd = os.open(self.lock_path.parent, os.O_RDONLY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

    def __enter__(self) -> "DeploymentUpdateTransaction":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._settled:
            return False
        if exc is None:
            # A caller that exits without proving/settling the committed
            # generation has not completed the transaction.
            self.abort("mutation scope exited without verified settlement")
        else:
            self.abort(f"{exc_type.__name__ if exc_type else 'error'}: {exc}")
        return False


def run_under_deployment_authority(plan: DeploymentPlan, mutation) -> Any:
    """Execute one legacy update under exact deployment authority.

    This is deliberately the only compatibility wrapper: it fences the existing
    monolithic updater without claiming stage-level proof it does not possess.
    Stage-aware mutation can consume ``DeploymentMutationPermit`` directly as
    #91277 Phase 2 replaces the legacy path.
    """

    tx = DeploymentUpdateTransaction(plan)
    with tx:
        result = mutation()
        committed = tx.verify_committed_generation()
        tx.settle_verified(committed)
        return result
