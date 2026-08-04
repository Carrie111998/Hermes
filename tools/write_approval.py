#!/usr/bin/env python3
"""Write-approval gate + pending store for memory and skill writes.

Background
----------
The agent writes to two persistent stores that survive across sessions:

  * **memory** — MEMORY.md / USER.md, small (~200 char) declarative entries
  * **skills** — SKILL.md + supporting files, potentially huge (10-100 KB)

Both stores are written from two origins:

  * **foreground** — a normal agent turn (user is present / chatting)
  * **background_review** — the self-improvement review fork that runs after a
    turn and autonomously decides what to save (the source of the
    "wrong assumptions" users complained about)

This module lets the user gate those writes per-subsystem with a boolean
``write_approval``:

  * ``false`` (default) — write freely (the pre-gate behaviour)
  * ``true``            — require approval: do not commit the write; either
    prompt inline (memory, interactive CLI only) or **stage** it to a pending
    store and surface it for the user to approve or reject out-of-band

The size asymmetry between memory and skills is real and unavoidable: a memory
entry can be reviewed inline in a chat bubble; a 100 KB SKILL.md cannot. So
the gate stages BOTH to disk, but review affordances differ by subsystem
(see ``hermes_cli`` slash handlers): memory shows full content, skills show
metadata + a one-line gist + a ``diff`` escape hatch (CLI/dashboard/file).

Staging is mandatory for background-origin writes (a daemon thread cannot
block on an interactive prompt) and for gateway sessions (no inline prompt
channel — review happens via ``/memory pending``). Foreground CLI memory
writes prompt inline via the dangerous-command approval callback; skill
writes always stage (too big to eyeball mid-loop).

Pending records live under ``<HERMES_HOME>/pending/{memory,skills}/<id>.json``
so they survive process restarts and can be reviewed from CLI, gateway, or the
web dashboard.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _atomic_pending_json_write(path: Path, data: Mapping[str, Any], *, create: bool = False) -> None:
    """Write pending JSON without ever following the destination symlink."""
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("pending directory is not a regular local directory")
    if path.is_symlink() or (create and os.path.lexists(path)):
        raise FileExistsError("pending record path already exists or is a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("pending record path is not a regular file")
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.stem}_", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink():
            raise FileExistsError("pending record path became a symlink")
        # Replace the directory entry itself; never redirect through a symlink.
        os.replace(tmp_path, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

# Subsystem identifiers
MEMORY = "memory"
SKILLS = "skills"
_SUBSYSTEMS = (MEMORY, SKILLS)
# Pending IDs may be namespaced when records are migrated from another
# profile (for example ``pastoral:<uuid>``). Keep the separator explicit.
_ID_RE = re.compile(r"^(?:[A-Za-z0-9_-]+:)?[A-Za-z0-9_-]{1,64}$")

# Config key (per subsystem). A single boolean: the approval gate is OFF by
# default (writes flow freely, the pre-gate behaviour), and ON means stage /
# prompt every write for the user's approval. There is intentionally no third
# "block all writes" state — to disable a subsystem entirely use its own
# enable flag (e.g. ``memory.memory_enabled: false``).
CONFIG_KEY = "write_approval"


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def write_approval_enabled(subsystem: str) -> bool:
    """Return whether the approval gate is enabled for ``subsystem``.

    Reads ``<subsystem>.write_approval`` from config.yaml. Defaults to
    ``False`` (gate off — writes flow freely) for any unset / invalid value so
    existing installs keep their current behaviour until the user opts in.
    """
    if subsystem not in _SUBSYSTEMS:
        return False
    try:
        from hermes_cli.config import load_config, cfg_get
        cfg = load_config()
        raw = cfg_get(cfg, subsystem, CONFIG_KEY, default=False)
    except Exception:
        return False
    return _normalize_enabled(raw)


def _normalize_enabled(value: Any) -> bool:
    """Coerce a config value to a bool. Default (unknown) is False (gate off).

    Accepts real bools and the usual truthy/falsey strings. YAML 1.1 parses
    bare ``on``/``off``/``yes``/``no`` as bools already, so the string branch
    is mostly for hand-edited configs.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"on", "true", "yes", "1", "approve", "enabled"}
    return False


# ---------------------------------------------------------------------------
# Pending store (file-backed)
# ---------------------------------------------------------------------------

def _pending_dir(subsystem: str) -> Path:
    if subsystem not in _SUBSYSTEMS:
        raise ValueError(f"invalid pending subsystem: {subsystem!r}")
    root = get_hermes_home() / "pending"
    directory = root / subsystem
    if (root.exists() and root.is_symlink()) or (
        directory.exists() and directory.is_symlink()
    ):
        raise RuntimeError("pending write directory must not be a symlink")
    return directory


def _validate_pending_id(pending_id: str) -> str:
    value = str(pending_id or "")
    if not _ID_RE.fullmatch(value):
        raise ValueError("pending id contains invalid characters")
    return value


def _pending_filename_id(pending_id: str) -> str:
    """Encode a logical ID for a safe pending filename.

    Migrated records historically encoded the single namespace separator as a
    hyphen in filenames while retaining ``namespace:<id>`` in the envelope.
    Preserve that on-disk compatibility without weakening ID validation.
    """
    return pending_id.replace(":", "-", 1)


def _pending_path(subsystem: str, pending_id: str) -> Path:
    return _pending_dir(subsystem) / f"{_pending_filename_id(pending_id)}.json"


def _risk_for(subsystem: str, action: str) -> str:
    if action in {"delete", "remove", "remove_file"}:
        return "high"
    if subsystem == SKILLS and action in {"edit", "patch", "write_file"}:
        return "medium"
    if subsystem == MEMORY and action in {"replace", "batch"}:
        return "medium"
    return "low"


def _capture_skill_precondition(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Fingerprint the exact existing skill target without changing it."""
    try:
        from tools.skill_manager_tool import _find_skill

        found = _find_skill(str(payload.get("name") or ""))
        action = str(payload.get("action") or "")
        if found is None:
            return {"target_exists": False}
        skill_dir = Path(found["path"])
        skill_root = skill_dir.resolve()
        if action in {"write_file", "remove_file"}:
            target = skill_dir / str(payload.get("file_path") or "")
        else:
            target = skill_dir / "SKILL.md"
        try:
            target.resolve(strict=False).relative_to(skill_root)
        except (OSError, ValueError):
            return {"capture_failed": True}
        if not target.exists() or target.is_symlink() or not target.is_file():
            return {"target_exists": False}
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"target_exists": True, "target_sha256": digest}
    except Exception:
        return {"capture_failed": True}


def verify_reviewed_payload(
    subsystem: str,
    record: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    memory_store=None,
) -> tuple[bool, str]:
    """Reject changed replay payloads or targets after human review."""
    from agent import learning_ledger

    record_id = str(record.get("id") or "")
    candidate_id = str(record.get("candidate_id") or "")
    ledger_id = str(candidate.get("candidate_id") or "")
    if not record_id or record_id != candidate_id or record_id != ledger_id:
        return False, "pending record is not linked to its reviewed candidate"
    if str(record.get("subsystem") or "") != subsystem or str(candidate.get("subsystem") or "") != subsystem:
        return False, "pending subsystem does not match its reviewed candidate"
    payload = dict(record.get("payload") or {})
    fingerprint = learning_ledger.canonical_payload_fingerprint(subsystem, payload)
    if fingerprint != str(candidate.get("payload_fingerprint") or ""):
        return False, "reviewed payload changed after staging"
    precondition = dict(record.get("precondition") or candidate.get("precondition") or {})
    if not precondition:
        return True, ""
    if precondition.get("capture_failed"):
        return False, "target precondition could not be captured"
    if subsystem == MEMORY:
        if memory_store is None:
            return False, "memory store unavailable"
        entries = memory_store._entries_for(str(payload.get("target") or "memory"))
        target_fingerprint = precondition.get("target_fingerprint")
        if target_fingerprint:
            actual = hashlib.sha256(
                json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if actual != target_fingerprint:
                return False, "memory target changed after staging"
            return True, ""
        for old_text in precondition.get("old_texts", []):
            if not any(str(old_text) in entry for entry in entries):
                return False, "memory target changed after staging"
        return True, ""
    current = _capture_skill_precondition(payload)
    if current != precondition:
        return False, "skill target changed after staging"
    return True, ""


def stage_write(
    subsystem: str,
    payload: Dict[str, Any],
    *,
    summary: str,
    origin: str,
    metadata: Optional[Mapping[str, Any]] = None,
    candidate_id: Optional[str] = None,
    dedup_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a pending write and return a short record describing it.

    Args:
        subsystem: ``memory`` or ``skills``.
        payload: the exact kwargs needed to replay the write when approved
            (e.g. ``{"action": "add", "target": "user", "content": "..."}``
            for memory, or the full ``skill_manage`` kwargs for skills).
        summary: a one-line human-readable description shown in pending lists.
            For skills this is the LLM/heuristic gist; for memory it can be the
            entry text itself.
        origin: ``foreground`` or ``background_review`` — recorded for audit.

    Returns a dict with ``id`` and metadata. Staging fails closed unless both
    the exact replay payload and its ledger lifecycle row are durable.
    """
    from agent import learning_ledger

    if subsystem not in _SUBSYSTEMS:
        raise ValueError(f"invalid pending subsystem: {subsystem!r}")
    pid = _validate_pending_id(candidate_id or uuid.uuid4().hex)
    meta: Dict[str, Any] = {}
    if (origin or "") == "background_review":
        try:
            from agent.learning_context import current_learning_metadata

            meta = current_learning_metadata()
        except Exception:
            meta = {}
    meta.update(dict(metadata or {}))
    if subsystem == SKILLS and not meta.get("precondition"):
        meta["precondition"] = _capture_skill_precondition(payload)
    payload_fingerprint = learning_ledger.canonical_payload_fingerprint(subsystem, payload)
    resolved_dedup_key = dedup_key or learning_ledger.candidate_dedup_key(subsystem, payload)
    if (origin or "") == "background_review":
        existing = learning_ledger.find_candidate_by_dedup(
            resolved_dedup_key,
            statuses={"pending", "applying", "active", "validated", "rejected", "rolled_back"},
        )
        if existing is not None:
            return {
                "id": existing["candidate_id"],
                "candidate_id": existing["candidate_id"],
                "subsystem": subsystem,
                "action": payload.get("action", ""),
                "summary": (summary or "").strip(),
                "origin": origin,
                "ledger_recorded": True,
                "suppressed": True,
                "deduplicated": True,
                "existing_status": existing["status"],
            }
    evidence = dict(meta.get("evidence") or {})
    if str(evidence.get("risk") or "unknown") == "unknown":
        evidence["risk"] = _risk_for(subsystem, str(payload.get("action") or ""))
    record = {
        "id": pid,
        "candidate_id": pid,
        "schema_version": 2,
        "subsystem": subsystem,
        "action": payload.get("action", ""),
        "summary": (summary or "").strip(),
        "origin": origin or "foreground",
        "created_at": time.time(),
        "payload": payload,
        "payload_fingerprint": payload_fingerprint,
        "dedup_key": resolved_dedup_key,
        "precondition": dict(meta.get("precondition") or {}),
        "ledger_recorded": False,
        "success": True,
        "staged": True,
    }
    try:
        d = _pending_dir(subsystem)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = d / f"{pid}.json"
        _atomic_pending_json_write(path, record, create=True)
    except Exception as e:  # pragma: no cover - disk failure path
        logger.error("Failed to stage pending %s write: %s", subsystem, e, exc_info=True)
        record["success"] = False
        record["staged"] = False
        return record

    try:
        source = {"origin": record["origin"], **dict(meta.get("source") or {})}
        learning_ledger.create_candidate(
            {
                "candidate_id": pid,
                "subsystem": subsystem,
                "action": record["action"],
                "status": "pending",
                "payload_fingerprint": payload_fingerprint,
                "dedup_key": resolved_dedup_key,
                "pending_relpath": f"pending/{subsystem}/{pid}.json",
                "proposal": {
                    # Pending JSON is the exact, reviewable replay envelope.
                    # The long-lived ledger keeps only a non-content-bearing gist.
                    "summary": f"{subsystem} {record['action']} candidate",
                    "target": payload.get("target"),
                    "name": payload.get("name"),
                    "file_path": payload.get("file_path"),
                },
                "source": source,
                "evidence": evidence,
                "precondition": record["precondition"],
            }
        )
        record["ledger_recorded"] = True
        _atomic_pending_json_write(path, record)
    except Exception as e:
        # A competing process may have latched the same autonomous proposal
        # after our optimistic pre-check.  The SQLite latch is authoritative;
        # remove this orphan replay payload and report the existing candidate.
        if (origin or "") == "background_review":
            try:
                existing = learning_ledger.find_candidate_by_dedup(resolved_dedup_key)
                if existing is not None and existing["candidate_id"] != pid:
                    path.unlink(missing_ok=True)
                    return {
                        "id": existing["candidate_id"],
                        "candidate_id": existing["candidate_id"],
                        "subsystem": subsystem,
                        "action": payload.get("action", ""),
                        "summary": (summary or "").strip(),
                        "origin": origin,
                        "ledger_recorded": True,
                        "success": True,
                        "staged": False,
                        "suppressed": True,
                        "deduplicated": True,
                        "existing_status": existing["status"],
                    }
            except Exception:
                pass
        logger.error("Failed to record learning candidate %s: %s", pid, e, exc_info=True)
        path.unlink(missing_ok=True)
        record["success"] = False
        record["staged"] = False
    return record


def _read_pending_record(
    path: Path,
    *,
    subsystem: str,
    expected_id: str,
) -> Optional[Dict[str, Any]]:
    """Read one pending record only when its envelope matches its path."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return None
        record_id = _validate_pending_id(str(record.get("id") or ""))
        if record_id != expected_id and _pending_filename_id(record_id) != expected_id:
            return None
        raw_candidate_id = record.get("candidate_id")
        if raw_candidate_id in {None, ""}:
            # Pre-ledger records had no candidate_id. Bind them to their
            # immutable filename/id so migration never trusts a mutable link.
            candidate_id = record_id
            record["candidate_id"] = record_id
        else:
            candidate_id = _validate_pending_id(str(raw_candidate_id))
            if candidate_id != record_id:
                return None
        record_subsystem = str(record.get("subsystem") or subsystem)
        if record_subsystem != subsystem:
            return None
        return record
    except Exception:
        return None


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """Return all pending records for ``subsystem``, oldest first."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        expected_id = p.name.removesuffix(".json")
        try:
            expected_id = _validate_pending_id(expected_id)
        except ValueError:
            continue
        record = _read_pending_record(p, subsystem=subsystem, expected_id=expected_id)
        if record is not None:
            records.append(record)
        else:
            logger.warning("Skipping unreadable pending record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """Return a single pending record by id, or None."""
    pending_id = _validate_pending_id(pending_id)
    path = _pending_path(subsystem, pending_id)
    if not path.exists():
        return None
    expected_id = _pending_filename_id(pending_id)
    return _read_pending_record(path, subsystem=subsystem, expected_id=expected_id)


def discard_pending(subsystem: str, pending_id: str) -> bool:
    """Delete a pending record. Returns True if it existed."""
    pending_id = _validate_pending_id(pending_id)
    path = _pending_path(subsystem, pending_id)
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to discard pending %s/%s: %s", subsystem, pending_id, e)
    return False


def claim_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim one pending payload for apply/reject."""
    pending_id = _validate_pending_id(pending_id)
    path = _pending_path(subsystem, pending_id)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        return None
    claim_id = uuid.uuid4().hex
    claim_path = path.with_name(f"{_pending_filename_id(pending_id)}.json.applying.{claim_id}")
    try:
        os.replace(path, claim_path)
        record = _read_pending_record(
            claim_path,
            subsystem=subsystem,
            expected_id=pending_id,
        )
        if record is None:
            claim_path.unlink(missing_ok=True)
            return None
        record["_claim_path"] = str(claim_path)
        record["_claim_id"] = claim_id
        return record
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error("Failed to claim pending %s/%s: %s", subsystem, pending_id, e)
        return None


def release_claim(subsystem: str, claim: Mapping[str, Any], *, restore: bool) -> bool:
    """Restore or delete a claimed payload after a decision."""
    pending_id = _validate_pending_id(str(claim.get("id") or ""))
    claim_path = Path(str(claim.get("_claim_path") or ""))
    expected_parent = _pending_dir(subsystem).resolve()
    if (
        not claim_path.exists()
        or claim_path.is_symlink()
        or not claim_path.is_file()
        or claim_path.parent.resolve() != expected_parent
        or not any(
            claim_path.name.startswith(f"{prefix}.json.applying.")
            for prefix in (_pending_filename_id(pending_id), pending_id)
        )
    ):
        return False
    try:
        if restore:
            canonical = _pending_path(subsystem, pending_id)
            # Never overwrite a fresh canonical proposal created while this
            # claim was in flight.  A failed restore remains an explicit claim
            # for reconciliation instead of losing either payload.
            os.link(claim_path, canonical, follow_symlinks=False)
            if canonical.is_symlink() or not canonical.is_file():
                canonical.unlink(missing_ok=True)
                return False
            claim_path.unlink()
        else:
            claim_path.unlink()
        return True
    except Exception as e:
        logger.error("Failed to release pending claim %s/%s: %s", subsystem, pending_id, e)
        return False


def list_claims(subsystem: str) -> List[Dict[str, Any]]:
    """List interrupted claims without replaying or resolving them."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    claims: List[Dict[str, Any]] = []
    for path in d.glob("*.json.applying.*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            pending_id = _validate_pending_id(str(record.get("id") or ""))
            if not any(
                path.name.startswith(f"{prefix}.json.applying.")
                for prefix in (_pending_filename_id(pending_id), pending_id)
            ):
                continue
            record["_claim_path"] = str(path)
            record["_claim_id"] = path.name.rsplit(".", 1)[-1]
            record["_claim_age_seconds"] = max(0.0, time.time() - path.stat().st_mtime)
            claims.append(record)
        except Exception:
            logger.warning("Skipping unreadable pending claim: %s", path)
    claims.sort(key=lambda item: (-float(item.get("_claim_age_seconds", 0)), str(item.get("id", ""))))
    return claims


def ensure_candidate_for_record(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Return or create a ledger candidate for a legacy pending record."""
    from agent import learning_ledger

    candidate_id = str(record.get("candidate_id") or record.get("id") or "")
    if not candidate_id:
        return None
    existing = learning_ledger.get_candidate(candidate_id)
    if existing is not None:
        return existing
    subsystem = str(record.get("subsystem") or "")
    payload = dict(record.get("payload") or {})
    try:
        return learning_ledger.create_candidate(
            {
                "candidate_id": candidate_id,
                "subsystem": subsystem,
                "action": str(record.get("action") or payload.get("action") or "legacy"),
                "status": "pending",
                "payload_fingerprint": learning_ledger.canonical_payload_fingerprint(subsystem, payload),
                "dedup_key": learning_ledger.candidate_dedup_key(subsystem, payload),
                "pending_relpath": f"pending/{subsystem}/{candidate_id}.json",
                "proposal": {
                    "summary": f"{subsystem} {str(record.get('action') or payload.get('action') or 'legacy')} candidate"
                },
                "source": {"origin": str(record.get("origin") or "legacy")},
                "evidence": {"status": "legacy_missing"},
                "precondition": {},
            }
        )
    except Exception as e:
        logger.error("Failed to create ledger entry for legacy pending %s: %s", candidate_id, e)
        return None


def pending_count(subsystem: str) -> int:
    """Cheap count of pending records (for notification badges)."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Write origin
# ---------------------------------------------------------------------------

def current_origin() -> str:
    """Return the active write origin: ``foreground`` or ``background_review``.

    Reuses the skill-provenance ContextVar, which the background review fork
    already sets (see ``agent.background_review`` /
    ``AIAgent._spawn_background_review``). Foreground agent turns leave it at
    the default ``foreground``.
    """
    try:
        from tools.skill_provenance import get_current_write_origin
        return get_current_write_origin()
    except Exception:
        return "foreground"


def is_background() -> bool:
    return current_origin() == "background_review"


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------

class GateDecision:
    """Result of evaluating the write gate for a single write attempt.

    Exactly one of the boolean flags is True:
      * ``allow``  — proceed with the real write (gate off, or an inline
        approval was granted).
      * ``blocked`` — refuse the write (the user denied an inline approval
        prompt). ``message`` explains why; surface it to the agent.
      * ``stage``  — do not write; the caller should stage the payload via
        ``stage_write`` (gate on, and no inline prompt is available — gateway,
        background review, script, or any skill write). ``message`` is the
        user-facing "staged for approval" note.
    """

    __slots__ = ("allow", "blocked", "stage", "message")

    def __init__(self, *, allow=False, blocked=False, stage=False, message=""):
        self.allow = allow
        self.blocked = blocked
        self.stage = stage
        self.message = message


def evaluate_gate(subsystem: str, *, inline_summary: str = "",
                  inline_detail: str = "") -> GateDecision:
    """Decide what to do with a pending write for ``subsystem``.

    Args:
        subsystem: ``memory`` or ``skills``.
        inline_summary: short description used as the inline approval prompt
            header (memory foreground path only).
        inline_detail: full content shown in the inline prompt (memory entries
            are small; skills never take the inline path).

    Decision matrix:
        gate off (default)                    → allow (writes flow freely)
        gate on, memory + interactive CLI     → inline approve/deny prompt
        gate on, memory + gateway/script/bg   → stage
        gate on, skills (any origin)          → stage (too big to review inline)

    Note: there is no config-driven "blocked" outcome — the gate only ever
    delays a write for approval, never silently refuses it. ``blocked`` is
    still produced when the user *actively denies* an inline prompt.
    """
    background = is_background()

    # A real background-review run binds a trusted metadata envelope.  Missing
    # context here means a legacy/internal caller, whose existing gate-off
    # behavior remains unchanged for compatibility.
    if background:
        try:
            from agent.learning_context import current_learning_metadata

            evidence = dict(current_learning_metadata().get("evidence") or {})
        except Exception:
            evidence = {}
        if evidence:
            trust = str(evidence.get("source_trust") or "unknown")
            status = str(evidence.get("status") or "missing")
            risk = str(evidence.get("risk") or "unknown")
            if (
                trust in {"untrusted_external", "user_supplied_unverified", "unknown"}
                or status in {"missing", "malformed", "unverified"}
                or risk == "high"
            ):
                where = "/skills pending" if subsystem == SKILLS else "/memory pending"
                return GateDecision(
                    stage=True,
                    message=f"Untrusted or high-risk learning candidate staged for review with {where}.",
                )

    if not write_approval_enabled(subsystem):
        return GateDecision(allow=True)

    # Skills always stage — a SKILL.md is too large to review inline, and a
    # background skill write happens in a daemon thread with no user present.
    if subsystem == SKILLS or background:
        where = "/skills pending" if subsystem == SKILLS else "/memory pending"
        return GateDecision(
            stage=True,
            message=(
                f"Staged for approval ({subsystem}.write_approval is on). "
                f"Not yet saved — review with {where}."
            ),
        )

    # Memory + foreground: if an interactive approval channel exists (a CLI
    # approval callback registered on this thread), prompt inline — entries
    # are small enough to show in full. Otherwise (gateway, script, batch,
    # no listener) stage instead of forcing a blind deny.
    if _interactive_approval_available():
        granted = _prompt_inline_memory_approval(inline_summary, inline_detail)
        if granted is True:
            return GateDecision(allow=True)
        if granted is False:
            return GateDecision(
                blocked=True,
                message="Memory write denied by user. The change was not saved.",
            )
        # granted is None → prompt failed; fall through to staging.

    return GateDecision(
        stage=True,
        message=(
            "Staged for approval (memory.write_approval is on). "
            "Not yet saved — review with /memory pending."
        ),
    )


def _interactive_approval_available() -> bool:
    """True when a foreground memory write can be approved inline.

    Inline prompting requires a per-thread approval callback registered by the
    interactive CLI (``tools.terminal_tool.set_approval_callback``). Every
    other surface stages instead:

    * **Gateway/API sessions** — the dangerous-command ``/approve`` round-trip
      lives in the pending-approval queue (``submit_pending`` +
      ``_await_gateway_decision``), which ``prompt_dangerous_approval`` never
      reaches; trying to prompt from a gateway session would hit the
      ``input()`` fallback and silently deny. Staging gives the user a real
      review affordance (``/memory pending``) instead.
    * Scripts, cron, and background threads — no user present.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
        return _get_approval_callback() is not None
    except Exception:
        return False


def _prompt_inline_memory_approval(summary: str, detail: str) -> Optional[bool]:
    """Prompt the user inline to approve a memory write.

    Returns True (approved), False (denied), or None (no interactive prompt
    available / prompt failed → caller should stage instead).

    Reuses the per-thread CLI approval callback registered for dangerous
    commands (``tools.terminal_tool.set_approval_callback``). The callback is
    invoked directly — NOT via ``prompt_dangerous_approval`` — because that
    wrapper falls back to ``input()`` (deadlock-prone under prompt_toolkit,
    see #15216) and converts callback errors into a silent deny; here a
    failed prompt must stage the write instead.
    """
    try:
        from tools.terminal_tool import _get_approval_callback
    except Exception:
        return None

    callback = _get_approval_callback()
    if callback is None:
        # No interactive channel on this thread — stage rather than risk the
        # input() fallback (deadlock under prompt_toolkit, EOF-deny in tests).
        return None

    header = summary.strip() or "Save to memory?"
    body = detail.strip()
    description = f"Save to memory: {header}"
    command = body if body else header
    # Invoke the callback directly instead of via prompt_dangerous_approval:
    # that wrapper swallows callback exceptions into "deny", which would
    # silently refuse the write. Direct invocation lets a crashed prompt fall
    # back to staging (the gate only ever delays a write, never drops it).
    try:
        choice = callback(command, description, allow_permanent=False)
    except Exception as e:
        logger.error("Inline memory approval prompt failed: %s", e)
        return None

    if choice in {"once", "session"}:
        return True
    if choice == "deny":
        return False
    # Any other outcome (e.g. timeout that returns "deny" already handled) →
    # treat unknown as no-decision so we stage rather than silently drop.
    return None


# ---------------------------------------------------------------------------
# Skill-specific helpers (gist + diff for the review affordances)
# ---------------------------------------------------------------------------

def skill_gist(action: str, name: str, *, content: str = "",
               file_path: str = "", old_string: str = "",
               new_string: str = "") -> str:
    """Build a one-line human gist for a pending skill write.

    Heuristic, no model call — the gist surfaces enough to decide approve/reject
    in a chat bubble, while the full diff stays behind /skills diff (CLI/
    dashboard/file). For create/edit it pulls the frontmatter ``description:``;
    for patch/write_file it describes the size of the change.
    """
    if action in {"create", "edit"} and content:
        desc = _frontmatter_description(content)
        size = f"{len(content) // 1024 + 1} KB" if len(content) >= 1024 else f"{len(content)} chars"
        verb = "create" if action == "create" else "rewrite"
        if desc:
            return f"{verb} '{name}' — {desc} ({size})"
        return f"{verb} '{name}' ({size})"
    if action == "patch":
        target = file_path or "SKILL.md"
        removed = old_string.count("\n") + 1 if old_string else 0
        added = new_string.count("\n") + 1 if new_string else 0
        return f"patch '{name}' {target} (+{added}/-{removed} lines)"
    if action == "write_file":
        return f"write {file_path} in '{name}'"
    if action == "remove_file":
        return f"remove {file_path} from '{name}'"
    if action == "delete":
        return f"delete skill '{name}'"
    return f"{action} '{name}'"


def _frontmatter_description(content: str) -> str:
    """Extract the ``description:`` value from SKILL.md YAML frontmatter."""
    import re
    m = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    if not m:
        return ""
    desc = m.group(1).strip().strip("'\"")
    return desc[:140]


def skill_pending_diff(record: Dict[str, Any]) -> str:
    """Build a full unified diff (or full content) for a staged skill write.

    Used by /skills diff <id> on a surface that can render it (CLI pager, web
    dashboard, or by opening the pending JSON file). For create this is the new
    file content; for edit/patch it is a unified diff against the current
    on-disk skill.
    """
    import difflib
    payload = record.get("payload", {})
    action = payload.get("action", "")
    name = payload.get("name", "")

    if action == "create":
        return (payload.get("content") or "")

    # Resolve current on-disk content for diffable actions.
    try:
        from tools.skill_manager_tool import _find_skill, _validate_file_path
    except Exception:
        _find_skill = None  # type: ignore
        _validate_file_path = None  # type: ignore

    current = ""
    target_label = "SKILL.md"
    if _find_skill is not None:
        found = _find_skill(name)
        if found:
            base = found["path"]
            if action == "edit":
                p = base / "SKILL.md"
            elif action in {"patch", "write_file"}:
                rel = payload.get("file_path") or "SKILL.md"
                path_error = _validate_file_path(str(rel)) if _validate_file_path is not None else "validator unavailable"
                if path_error:
                    return f"invalid staged skill path: {path_error}"
                p = base / rel
                target_label = rel
            else:
                p = base / "SKILL.md"
            try:
                base_resolved = Path(base).resolve()
                p.resolve(strict=False).relative_to(base_resolved)
                if p.exists() and not p.is_symlink() and p.is_file():
                    current = p.read_text(encoding="utf-8")
            except (OSError, ValueError):
                current = ""

    if action == "edit":
        new = payload.get("content") or ""
    elif action == "patch":
        old_s = payload.get("old_string") or ""
        new_s = payload.get("new_string") or ""
        new = current.replace(old_s, new_s) if current else f"(patch {old_s!r} → {new_s!r})"
    elif action == "write_file":
        new = payload.get("file_content") or ""
    elif action == "remove_file":
        return f"remove file: {payload.get('file_path')} from skill '{name}'"
    elif action == "delete":
        return f"delete skill '{name}'"
    else:
        return f"({action} on '{name}')"

    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{target_label}",
        tofile=f"b/{target_label}",
    )
    text = "".join(diff)
    return text or "(no textual change)"
