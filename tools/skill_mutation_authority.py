"""Cross-process mutation authority for Hermes skill writes.

This module owns the filesystem concurrency contract shared by direct skill
edits and approved pending replays: bind the approved pre-image, acquire a
cross-process lease, revalidate at the write boundary, fingerprint the bytes
published, and conditionally roll them back after a rejected security scan.
"""

import contextlib
import contextvars
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from utils import atomic_write_text


logger = logging.getLogger(__name__)

PENDING_SKILL_PRECONDITION_VERSION = 1
SKILL_MUTATION_LOCK_TIMEOUT_SECONDS = 30.0
SKILL_MUTATION_LOCK_POLL_SECONDS = 0.05

_IS_WINDOWS = sys.platform == "win32"
_lease_tls = threading.local()
_scan_hook_guard = threading.local()
_pending_apply_precondition: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = (
    contextvars.ContextVar("pending_apply_precondition", default=None)
)
_pending_apply_payload: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = (
    contextvars.ContextVar("pending_apply_payload", default=None)
)

PendingCapture = Callable[..., Tuple[Optional[Dict[str, Any]], Optional[str]]]
SkillMutator = Callable[..., str]
MutationIdentity = Union[Path, str]
SKILL_WRITE_ACTIONS = {
    "create",
    "edit",
    "patch",
    "delete",
    "write_file",
    "remove_file",
}


def _after_pending_precondition_check() -> None:
    """Test hook after the early approval check and before mutator replay."""


def _during_skill_security_scan() -> None:
    """Test hook after publication and before the security scan."""


def fire_during_skill_security_scan() -> None:
    """Run the scan-window hook once per outer mutation, not nested writers."""
    if getattr(_scan_hook_guard, "active", False):
        return
    _scan_hook_guard.active = True
    try:
        _during_skill_security_scan()
    finally:
        _scan_hook_guard.active = False


def stale_pending_write_error() -> Dict[str, Any]:
    return {
        "success": False,
        "conflict": True,
        "error_code": "stale_pending_write",
        "error": (
            "Pending skill write conflicts with newer on-disk state and was "
            "not applied. Review its diff and recreate the change against the "
            "current skill; the pending record has been preserved."
        ),
    }


def precondition_check_failed_error(detail: str) -> Dict[str, Any]:
    return {
        "success": False,
        "conflict": True,
        "error_code": "precondition_check_failed",
        "error": f"Could not verify the pending skill write safely: {detail}",
    }


def mutation_lock_timeout_error() -> Dict[str, Any]:
    return {
        "success": False,
        "conflict": True,
        "error_code": "mutation_lock_timeout",
        "error": (
            "Could not obtain exclusive mutation authority for this skill; "
            "the pending write was not applied."
        ),
    }


def logical_skill_name_identity(name: str) -> str:
    """Return the global mutation identity for one logical skill name."""
    return f"logical-skill-name:{name}"


def _canonical_identity(identity: MutationIdentity) -> str:
    if isinstance(identity, Path):
        try:
            return f"path:{identity.resolve()}"
        except OSError:
            return f"path:{identity}"
    return identity


def _shared_lock_dir() -> Path:
    """Return one per-user namespace shared by every Hermes profile.

    The namespace cannot live under ``HERMES_HOME`` because profiles aimed at
    the same external skill would then open different lock files. POSIX has a
    stable machine-wide runtime root and uid; Windows uses the user's temp
    root plus a home-derived scope.
    """
    get_uid = getattr(os, "getuid", None)
    uid = get_uid() if get_uid is not None else None
    if os.name == "posix" and uid is not None:
        lock_dir = Path("/tmp") / f"hermes-skill-mutation-locks-{uid}"
    else:
        try:
            user_home = str(Path.home().resolve())
        except OSError:
            user_home = str(Path.home())
        user_scope = hashlib.sha256(
            user_home.encode("utf-8", "surrogateescape")
        ).hexdigest()[:16]
        lock_dir = (
            Path(tempfile.gettempdir())
            / f"hermes-skill-mutation-locks-{user_scope}"
        )

    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_dir.is_symlink() or not lock_dir.is_dir():
        raise OSError(f"unsafe skill mutation lock namespace: {lock_dir}")
    if os.name == "posix" and uid is not None:
        stat_result = lock_dir.stat()
        if stat_result.st_uid != uid:
            raise PermissionError(
                f"skill mutation lock namespace is owned by uid {stat_result.st_uid}"
            )
        lock_dir.chmod(0o700)
    return lock_dir


def _skill_lock_path(identity: MutationIdentity) -> Path:
    ident = _canonical_identity(identity)
    digest = hashlib.sha256(ident.encode("utf-8", "surrogateescape")).hexdigest()
    return _shared_lock_dir() / f"{digest}.lock"


def _held_lease_keys() -> set:
    held = getattr(_lease_tls, "keys", None)
    if held is None:
        held = set()
        _lease_tls.keys = held
    return held


@contextlib.contextmanager
def skill_mutation_lease(identity: MutationIdentity):
    """Yield whether exclusive cross-process authority was established.

    Every failure to derive, create, open, or acquire the lease fails closed.
    A caller must refuse its mutation when this yields ``False``.
    """
    try:
        lock_path = _skill_lock_path(identity)
    except OSError as exc:
        logger.warning(
            "Could not create skill mutation authority for %s (%s); refusing write.",
            identity,
            exc,
        )
        yield False
        return

    key = str(lock_path)
    held = _held_lease_keys()
    if key in held:
        yield True
        return

    try:
        handle = open(lock_path, "a+b")
    except OSError as exc:
        logger.warning(
            "Could not open skill mutation lock %s (%s); refusing write.",
            lock_path,
            exc,
        )
        yield False
        return

    acquired = False
    try:
        deadline = time.monotonic() + SKILL_MUTATION_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    break
                time.sleep(SKILL_MUTATION_LOCK_POLL_SECONDS)
        if not acquired:
            logger.warning(
                "Skill mutation lock %s remained unavailable for %.0fs; refusing write.",
                lock_path,
                SKILL_MUTATION_LOCK_TIMEOUT_SECONDS,
            )
            yield False
            return
        held.add(key)
        try:
            yield True
        finally:
            held.discard(key)
    finally:
        try:
            if acquired:
                if _IS_WINDOWS:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


@contextlib.contextmanager
def bind_pending_apply(precondition: Dict[str, Any], payload: Dict[str, Any]):
    """Bind one approved pre-image to the mutator that will consume it."""
    expected_token = _pending_apply_precondition.set(precondition)
    payload_token = _pending_apply_payload.set(payload)
    try:
        yield
    finally:
        _pending_apply_payload.reset(payload_token)
        _pending_apply_precondition.reset(expected_token)


def _tool_error(message: str, **extra: Any) -> str:
    payload = {"success": False, "error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def stage_or_allow_skill_write(
    action: str,
    name: str,
    payload_kwargs: Dict[str, Any],
    *,
    bypass: bool,
    capture_pending_precondition: PendingCapture,
) -> Optional[str]:
    """Apply the skill approval gate or stage a content-bound proposal."""
    if action not in SKILL_WRITE_ACTIONS or bypass:
        return None
    try:
        from tools import write_approval as wa
    except Exception:
        return None

    decision = wa.evaluate_gate(wa.SKILLS)
    if decision.allow:
        return None
    if decision.blocked:
        return _tool_error(decision.message)

    precondition, error = capture_pending_precondition(
        action, name, **payload_kwargs
    )
    if error or precondition is None:
        return _tool_error(f"Skill write could not be safely staged: {error}")

    payload = {"action": action, "name": name}
    payload.update({key: value for key, value in payload_kwargs.items() if value is not None})
    gist = wa.skill_gist(
        action,
        name,
        content=payload_kwargs.get("content") or "",
        file_path=payload_kwargs.get("file_path") or "",
        old_string=payload_kwargs.get("old_string") or "",
        new_string=payload_kwargs.get("new_string") or "",
    )
    record = wa.stage_write(
        wa.SKILLS,
        payload,
        summary=gist,
        origin=wa.current_origin(),
        precondition=precondition,
    )
    return json.dumps(
        {
            "success": True,
            "staged": True,
            "pending_id": record["id"],
            "gist": gist,
            "message": decision.message,
        },
        ensure_ascii=False,
    )


def apply_pending_skill_write(
    payload: Dict[str, Any],
    precondition: Optional[Dict[str, Any]],
    *,
    capture_pending_precondition: PendingCapture,
    mutate: SkillMutator,
) -> str:
    """Consume a staged pre-image through the real skill mutator."""
    if not precondition:
        return _tool_error(
            "This pending skill write predates conflict protection and cannot "
            "be safely applied. Review its diff, recreate any useful change "
            "against the current skill, then reject this pending record.",
            conflict=True,
            error_code="missing_precondition",
        )

    current, error = capture_pending_precondition(
        payload.get("action", ""),
        payload.get("name", ""),
        content=payload.get("content"),
        category=payload.get("category"),
        file_path=payload.get("file_path"),
    )
    if error or current is None:
        return _tool_error(
            f"Could not verify the pending skill write safely: {error}",
            conflict=True,
            error_code="precondition_check_failed",
        )
    if current != precondition:
        return json.dumps(stale_pending_write_error(), ensure_ascii=False)

    _after_pending_precondition_check()
    with bind_pending_apply(precondition, payload):
        return mutate(
            action=payload.get("action", ""),
            name=payload.get("name", ""),
            content=payload.get("content"),
            category=payload.get("category"),
            file_path=payload.get("file_path"),
            file_content=payload.get("file_content"),
            old_string=payload.get("old_string"),
            new_string=payload.get("new_string"),
            replace_all=payload.get("replace_all", False),
            absorbed_into=payload.get("absorbed_into"),
        )


def refuse_if_pending_precondition_stale(
    capture_pending_precondition: PendingCapture,
) -> Optional[Dict[str, Any]]:
    """Revalidate the approved pre-image at the irreversible write boundary."""
    expected = _pending_apply_precondition.get()
    payload = _pending_apply_payload.get()
    if expected is None or payload is None:
        return None
    current, err = capture_pending_precondition(
        payload.get("action", ""),
        payload.get("name", ""),
        content=payload.get("content"),
        category=payload.get("category"),
        file_path=payload.get("file_path"),
    )
    if err or current is None:
        return precondition_check_failed_error(
            err or "could not recapture target state"
        )
    if current != expected:
        return stale_pending_write_error()
    return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_path_state(path: Path) -> Dict[str, Any]:
    """Return a content fingerprint without following redirected paths."""
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if not path.exists():
        return {"kind": "missing"}
    if path.is_file():
        return {
            "kind": "file",
            "size": path.stat().st_size,
            "sha256": _hash_file(path),
        }
    if not path.is_dir():
        return {"kind": "other"}

    manifest = []
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix()
        if child.is_symlink():
            entry = {"kind": "symlink", "path": relative, "target": os.readlink(child)}
        elif child.is_dir():
            entry = {"kind": "directory", "path": relative}
        elif child.is_file():
            entry = {
                "kind": "file",
                "path": relative,
                "size": child.stat().st_size,
                "sha256": _hash_file(child),
            }
        else:
            entry = {"kind": "other", "path": relative}
        manifest.append(entry)
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "kind": "directory",
        "entries": len(manifest),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def capture_pending_precondition(
    target: Path,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Capture the exact target bytes that can authorize one pending replay."""
    try:
        state = snapshot_path_state(target)
    except OSError as exc:
        return None, f"could not fingerprint '{target}': {exc}"
    return {
        "version": PENDING_SKILL_PRECONDITION_VERSION,
        "target": str(target),
        "state": state,
    }, None


class SettlementStatus(str, Enum):
    RESTORED = "restored"
    SUPERSEDED = "superseded"
    FAILED = "failed"


@dataclass(frozen=True)
class SettlementResult:
    status: SettlementStatus
    detail: str = ""


def security_scan_rejection(
    scan_error: str,
    settlement: SettlementResult,
) -> Dict[str, Any]:
    """Report a rejected scan only with an explicit terminal-state receipt."""
    if settlement.status is SettlementStatus.FAILED:
        return {
            "success": False,
            "error_code": "security_scan_settlement_failed",
            "error": (
                "Security scan rejected the skill mutation, but Hermes could "
                "not prove the terminal filesystem state. Treat the target as "
                "potentially containing rejected bytes and inspect it before "
                "retrying."
            ),
            "security_scan_error": scan_error,
            "settlement": settlement.status.value,
            "settlement_error": settlement.detail,
        }
    return {
        "success": False,
        "error": scan_error,
        "settlement": settlement.status.value,
    }


def rollback_path_if_still_published(
    path: Path,
    published_state: Dict[str, Any],
    original_content: Optional[str],
    original_state: Dict[str, Any],
    *,
    verification_root: Optional[Path] = None,
    original_root_state: Optional[Dict[str, Any]] = None,
) -> SettlementResult:
    """Restore old bytes only while the path still matches our publication."""
    try:
        current = snapshot_path_state(path)
    except OSError as exc:
        return SettlementResult(SettlementStatus.FAILED, f"snapshot failed: {exc}")
    if current != published_state:
        return SettlementResult(SettlementStatus.SUPERSEDED)
    if original_content is None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return SettlementResult(SettlementStatus.FAILED, f"unlink failed: {exc}")
        if verification_root is not None:
            parent = path.parent
            try:
                while parent != verification_root and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except OSError as exc:
                return SettlementResult(
                    SettlementStatus.FAILED,
                    f"supporting-directory restore failed: {exc}",
                )
    else:
        try:
            atomic_write_text(path, original_content, preserve_mode=True)
        except OSError as exc:
            return SettlementResult(SettlementStatus.FAILED, f"restore failed: {exc}")
    verification_path = verification_root or path
    expected_state = original_root_state or original_state
    try:
        restored = snapshot_path_state(verification_path)
    except OSError as exc:
        return SettlementResult(
            SettlementStatus.FAILED,
            f"post-restore verification failed: {exc}",
        )
    if restored != expected_state:
        return SettlementResult(
            SettlementStatus.FAILED,
            "post-restore fingerprint did not match the original state",
        )
    return SettlementResult(SettlementStatus.RESTORED)


def rollback_created_tree_if_still_published(
    skill_dir: Path,
    published_state: Dict[str, Any],
) -> SettlementResult:
    """Remove a new skill tree only while it matches our publication."""
    try:
        current = snapshot_path_state(skill_dir)
    except OSError as exc:
        return SettlementResult(SettlementStatus.FAILED, f"snapshot failed: {exc}")
    if current != published_state:
        return SettlementResult(SettlementStatus.SUPERSEDED)
    try:
        shutil.rmtree(skill_dir)
    except OSError as exc:
        return SettlementResult(SettlementStatus.FAILED, f"tree restore failed: {exc}")
    try:
        restored = snapshot_path_state(skill_dir)
    except OSError as exc:
        return SettlementResult(
            SettlementStatus.FAILED,
            f"post-restore verification failed: {exc}",
        )
    if restored != {"kind": "missing"}:
        return SettlementResult(
            SettlementStatus.FAILED,
            "created tree still exists after rollback",
        )
    return SettlementResult(SettlementStatus.RESTORED)
