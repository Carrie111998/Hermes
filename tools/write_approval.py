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

import difflib
import hashlib
import json
import logging
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# Subsystem identifiers
MEMORY = "memory"
SKILLS = "skills"
_SUBSYSTEMS = (MEMORY, SKILLS)

# Config key (per subsystem). A single boolean: the approval gate is OFF by
# default (writes flow freely, the pre-gate behaviour), and ON means stage /
# prompt every write for the user's approval. There is intentionally no third
# "block all writes" state — to disable a subsystem entirely use its own
# enable flag (e.g. ``memory.memory_enabled: false``).
CONFIG_KEY = "write_approval"

# Hard safety bounds for repeated background-review proposals. These are
# intentionally fixed rather than configurable in the first slice: changing
# policy must be an explicit code review, not an autonomous config mutation.
REFINEMENT_COOLDOWN_SECONDS = 24 * 60 * 60
REFINEMENT_ATTEMPT_WINDOW_SECONDS = 7 * 24 * 60 * 60
REFINEMENT_MAX_ATTEMPTS = 3
_refinement_guard_lock = threading.RLock()
_refinement_guard_state = threading.local()


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
    return get_hermes_home() / "pending" / subsystem


def stage_write(subsystem: str, payload: Dict[str, Any],
                *, summary: str, origin: str,
                refinement_candidate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        refinement_candidate: optional immutable proposal envelope for a
            background-review mutation of an existing skill.

    Returns a dict with ``id`` and metadata. Best-effort: on disk failure it
    logs and still returns a record (the write is simply lost, which is the
    safe failure for an approval gate — nothing is silently committed).
    """
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": subsystem,
        "action": payload.get("action", ""),
        "summary": (summary or "").strip(),
        "origin": origin or "foreground",
        "created_at": time.time(),
        "payload": payload,
    }
    if refinement_candidate is not None:
        record["refinement_candidate"] = refinement_candidate
    try:
        replace_pending_record(subsystem, record)
    except Exception as e:  # pragma: no cover - disk failure path
        logger.error("Failed to stage pending %s write: %s", subsystem, e, exc_info=True)
    return record


def replace_pending_record(subsystem: str, record: Dict[str, Any]) -> None:
    """Atomically replace one pending record after validating its identity."""
    if subsystem not in _SUBSYSTEMS:
        raise ValueError(f"invalid pending subsystem: {subsystem}")
    pending_id = str(record.get("id") or "")
    if len(pending_id) != 8 or any(ch not in "0123456789abcdef" for ch in pending_id):
        raise ValueError("invalid pending record id")
    if record.get("subsystem") != subsystem:
        raise ValueError("pending record subsystem mismatch")
    d = _pending_dir(subsystem)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{pending_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def list_pending(subsystem: str) -> List[Dict[str, Any]]:
    """Return all pending records for ``subsystem``, oldest first."""
    d = _pending_dir(subsystem)
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Skipping unreadable pending record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records


def get_pending(subsystem: str, pending_id: str) -> Optional[Dict[str, Any]]:
    """Return a single pending record by id, or None."""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def discard_pending(subsystem: str, pending_id: str) -> bool:
    """Delete a pending record. Returns True if it existed."""
    path = _pending_dir(subsystem) / f"{pending_id}.json"
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to discard pending %s/%s: %s", subsystem, pending_id, e)
    return False


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
    if not write_approval_enabled(subsystem):
        return GateDecision(allow=True)

    background = is_background()

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
# Skill-specific helpers (refinement candidate + review affordances)
# ---------------------------------------------------------------------------

_REFINEMENT_ACTIONS = {"patch"}


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {"state": "missing", "sha256": ""}
    content = path.read_text(encoding="utf-8")
    return {
        "state": "present",
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _resolve_refinement_target(payload: Dict[str, Any]):
    """Return (skill root, target path, display label, error)."""
    from tools.skill_manager_tool import _find_skill, _resolve_skill_target

    name = str(payload.get("name") or "")
    existing = _find_skill(name)
    if not existing:
        return None, None, "", f"Skill '{name}' is not an existing skill."
    skill_dir = existing["path"]
    rel = str(payload.get("file_path") or "SKILL.md")
    target, error = _resolve_skill_target(skill_dir, rel)
    return skill_dir, target, rel, error


def build_skill_refinement_candidate(
    payload: Dict[str, Any], *, origin: str
) -> Optional[Dict[str, Any]]:
    """Freeze a background-review proposal against one existing skill file.

    New-skill creation and whole-skill deletion deliberately remain ordinary
    pending writes. This first native refinement slice only covers bounded
    improvements to an existing skill.
    """
    action = str(payload.get("action") or "")
    if origin != "background_review" or action not in _REFINEMENT_ACTIONS:
        return None

    skill_dir, target, target_label, error = _resolve_refinement_target(payload)
    if error or skill_dir is None or target is None:
        raise ValueError(error or "Could not resolve refinement target.")

    current = target.read_text(encoding="utf-8") if target.exists() else ""
    if not target.exists():
        raise ValueError(f"Refinement target does not exist: {target_label}")
    from tools.fuzzy_match import fuzzy_find_and_replace

    proposed, _count, _strategy, match_error = fuzzy_find_and_replace(
        current,
        str(payload.get("old_string") or ""),
        payload.get("new_string"),
        bool(payload.get("replace_all", False)),
    )
    if match_error:
        raise ValueError(match_error)

    before_lines = current.splitlines(keepends=True)
    after_lines = [] if proposed is None else proposed.splitlines(keepends=True)
    frozen_diff = "".join(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{target_label}",
        tofile=f"b/{target_label}",
    )) or "(no textual change)"

    proposed_state = (
        {"state": "missing", "sha256": ""}
        if proposed is None
        else {
            "state": "present",
            "sha256": hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        }
    )
    candidate = {
        "schema_version": 1,
        "id": uuid.uuid4().hex,
        "action": action,
        "target": {
            "skill": str(payload.get("name") or ""),
            "file": target_label,
            "root": str(skill_dir.resolve()),
        },
        "evidence": {
            "origin": origin,
            "task_id": str(payload.get("task_id") or ""),
            "session_id": str(payload.get("session_id") or ""),
        },
        "base": _text_state(target),
        "proposed": proposed_state,
        "payload_sha256": _canonical_sha256(payload),
        "diff": frozen_diff,
    }
    candidate["fingerprint"] = _canonical_sha256({
        "action": candidate["action"],
        "target": candidate["target"],
        "base": candidate["base"],
        "proposed": candidate["proposed"],
        "diff": candidate["diff"],
    })
    candidate["integrity_sha256"] = _canonical_sha256(candidate)
    return candidate


def _validate_candidate_integrity(record: Dict[str, Any]):
    candidate = record.get("refinement_candidate")
    if not isinstance(candidate, dict):
        return True, ""
    expected = str(candidate.get("integrity_sha256") or "")
    unsigned = dict(candidate)
    unsigned.pop("integrity_sha256", None)
    if not expected or _canonical_sha256(unsigned) != expected:
        return False, "Refinement candidate integrity check failed."
    if candidate.get("payload_sha256") != _canonical_sha256(record.get("payload", {})):
        return False, "Refinement candidate payload integrity check failed."
    return True, ""


def _refinement_guard_path() -> Path:
    return get_hermes_home() / "pending" / "refinement_guard.json"


def _refinement_guard_lock_path() -> Path:
    return get_hermes_home() / "pending" / ".refinement_guard.lock"


@contextmanager
def _locked_refinement_guard():
    """Serialize guard decisions across threads and Hermes processes."""
    with _refinement_guard_lock:
        depth = int(getattr(_refinement_guard_state, "depth", 0))
        if depth:
            _refinement_guard_state.depth = depth + 1
            try:
                yield
            finally:
                _refinement_guard_state.depth = depth
            return

        lock_path = _refinement_guard_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _refinement_guard_state.depth = 1
            try:
                yield
            finally:
                _refinement_guard_state.depth = 0
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def refinement_candidate_transaction(record: Dict[str, Any]):
    """Hold the lifecycle lock from final validation through pending removal."""
    with _locked_refinement_guard():
        pending_id = str(record.get("id") or "")
        latest = get_pending(SKILLS, pending_id)
        if latest is None:
            raise RuntimeError(
                f"Refinement candidate '{pending_id}' is no longer pending."
            )
        if (
            _canonical_sha256(latest.get("payload"))
            != _canonical_sha256(record.get("payload"))
            or _canonical_sha256(latest.get("refinement_candidate"))
            != _canonical_sha256(record.get("refinement_candidate"))
        ):
            raise RuntimeError(
                f"Refinement candidate '{pending_id}' changed during approval."
            )
        yield latest


def _validate_guard_entry(entry: Any, *, label: str) -> None:
    if not isinstance(entry, dict):
        raise RuntimeError(f"Refinement loop guard entry '{label}' is invalid.")
    for field in ("attempts", "failures"):
        values = entry.get(field, [])
        if not isinstance(values, list):
            raise RuntimeError(
                f"Refinement loop guard field '{label}.{field}' is invalid."
            )
        for value in values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise RuntimeError(
                    f"Refinement loop guard timestamp '{label}.{field}' is invalid."
                )
    outcome = entry.get("last_outcome")
    if outcome is not None and outcome not in {"staged", "rejected", "failed", "applied"}:
        raise RuntimeError(
            f"Refinement loop guard outcome '{label}' is invalid."
        )
    outcome_at = entry.get("last_outcome_at")
    if outcome_at is not None and (
        isinstance(outcome_at, bool)
        or not isinstance(outcome_at, (int, float))
        or not math.isfinite(float(outcome_at))
        or float(outcome_at) < 0
    ):
        raise RuntimeError(
            f"Refinement loop guard outcome timestamp '{label}' is invalid."
        )


def _validate_refinement_guard(data: Any, path: Path) -> Dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RuntimeError(f"Refinement loop guard has an invalid schema: {path}")
    for bucket_name in ("fingerprints", "skills"):
        bucket = data.get(bucket_name)
        if not isinstance(bucket, dict):
            raise RuntimeError(f"Refinement loop guard has an invalid schema: {path}")
        for key, entry in bucket.items():
            if not isinstance(key, str) or not key:
                raise RuntimeError(
                    f"Refinement loop guard key in '{bucket_name}' is invalid."
                )
            _validate_guard_entry(entry, label=f"{bucket_name}.{key}")
    return data


def _load_refinement_guard() -> Dict[str, Any]:
    path = _refinement_guard_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": 1, "fingerprints": {}, "skills": {}}
    except Exception as exc:
        raise RuntimeError(f"Refinement loop guard is unreadable: {path}") from exc

    return _validate_refinement_guard(data, path)


def _save_refinement_guard(data: Dict[str, Any]) -> None:
    _validate_refinement_guard(data, _refinement_guard_path())
    now = time.time()
    cutoff = now - REFINEMENT_ATTEMPT_WINDOW_SECONDS
    for bucket_name in ("skills", "fingerprints"):
        bucket = data.setdefault(bucket_name, {})
        for key, entry in list(bucket.items()):
            entry["attempts"] = _recent_timestamps(entry.get("attempts"), now)
            entry["failures"] = _recent_timestamps(entry.get("failures"), now)
            try:
                last_outcome_at = float(entry.get("last_outcome_at") or 0)
            except (TypeError, ValueError):
                last_outcome_at = 0
            if (
                not entry["attempts"]
                and not entry["failures"]
                and last_outcome_at < cutoff
            ):
                bucket.pop(key, None)

    path = _refinement_guard_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _recent_timestamps(values: Any, now: float) -> List[float]:
    cutoff = now - REFINEMENT_ATTEMPT_WINDOW_SECONDS
    recent = []
    for value in values if isinstance(values, list) else []:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            continue
        if timestamp >= cutoff:
            recent.append(timestamp)
    return recent


def _guard_entry(data: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    fingerprint = str(candidate.get("fingerprint") or "")
    entries = data.setdefault("fingerprints", {})
    entry = entries.setdefault(fingerprint, {})
    entry["skill"] = str(candidate.get("target", {}).get("skill") or "")
    return entry


def _skill_guard_entry(data: Dict[str, Any], skill: str) -> Dict[str, Any]:
    skills = data.setdefault("skills", {})
    return skills.setdefault(skill, {})


def stage_refinement_write(
    payload: Dict[str, Any], *, summary: str, origin: str,
    candidate: Dict[str, Any]
):
    """Atomically enforce anti-loop policy and stage one refinement proposal."""
    ok, message = _validate_candidate_integrity({
        "payload": payload,
        "refinement_candidate": candidate,
    })
    if not ok:
        return None, message

    skill = str(candidate.get("target", {}).get("skill") or "")
    fingerprint = str(candidate.get("fingerprint") or "")
    if not skill or not fingerprint:
        return None, "Refinement candidate is missing its loop-guard identity."

    with _locked_refinement_guard():
        for pending in list_pending(SKILLS):
            active = pending.get("refinement_candidate")
            if not isinstance(active, dict):
                continue
            active_skill = str(active.get("target", {}).get("skill") or "")
            if active_skill == skill:
                return None, (
                    f"Skill '{skill}' already has an active refinement candidate "
                    f"({pending.get('id', '')})."
                )

        now = time.time()
        try:
            data = _load_refinement_guard()
        except RuntimeError as exc:
            return None, str(exc)
        fingerprint_entry = _guard_entry(data, candidate)
        skill_entry = _skill_guard_entry(data, skill)
        attempts = _recent_timestamps(skill_entry.get("attempts"), now)
        failures = _recent_timestamps(skill_entry.get("failures"), now)
        skill_entry["attempts"] = attempts
        skill_entry["failures"] = failures

        fingerprint_attempts = _recent_timestamps(
            fingerprint_entry.get("attempts"), now
        )
        fingerprint_entry["attempts"] = fingerprint_attempts
        if (
            fingerprint_attempts
            and fingerprint_entry.get("last_outcome")
            in {"rejected", "failed", "applied"}
        ):
            return None, (
                f"Duplicate refinement candidate blocked for skill '{skill}'; "
                "the same base and diff were already decided in this guard window."
            )

        if len(attempts) >= REFINEMENT_MAX_ATTEMPTS:
            return None, (
                f"Refinement attempt limit reached for skill '{skill}' "
                f"({REFINEMENT_MAX_ATTEMPTS} per 7 days)."
            )

        last_outcome = str(skill_entry.get("last_outcome") or "")
        try:
            last_outcome_at = float(skill_entry.get("last_outcome_at") or 0)
        except (TypeError, ValueError):
            last_outcome_at = 0
        if (
            last_outcome in {"rejected", "failed"}
            and now - last_outcome_at < REFINEMENT_COOLDOWN_SECONDS
        ):
            return None, (
                f"Refinement cooldown is active for skill '{skill}' after "
                f"{last_outcome}."
            )

        record = stage_write(
            SKILLS,
            payload,
            summary=summary,
            origin=origin,
            refinement_candidate=candidate,
        )
        if get_pending(SKILLS, record["id"]) is None:
            return None, "Refinement candidate could not be persisted safely."

        for entry in (skill_entry, fingerprint_entry):
            own_attempts = _recent_timestamps(entry.get("attempts"), now)
            entry["attempts"] = own_attempts + [now]
            entry["last_outcome"] = "staged"
            entry["last_outcome_at"] = now
        try:
            _save_refinement_guard(data)
        except Exception as exc:
            discard_pending(SKILLS, record["id"])
            return None, f"Refinement loop guard could not be persisted: {exc}"
        return record, ""


def can_attempt_refinement_apply(record: Dict[str, Any]):
    """Block immediate retries and cap failed applies for one candidate."""
    candidate = record.get("refinement_candidate")
    if not isinstance(candidate, dict):
        return True, ""
    if record.get("refinement_apply_state") == "applied_guard_error":
        return False, (
            "Refinement write was already applied but its guard outcome failed; "
            "the pending record is retained and must not be retried."
        )
    ok, message = _validate_candidate_integrity(record)
    if not ok:
        return False, message

    with _locked_refinement_guard():
        now = time.time()
        data = _load_refinement_guard()
        skill = str(candidate.get("target", {}).get("skill") or "")
        entry = _skill_guard_entry(data, skill)
        failures = _recent_timestamps(entry.get("failures"), now)
        if len(failures) >= REFINEMENT_MAX_ATTEMPTS:
            return False, (
                "Refinement apply attempt limit reached; reject this candidate "
                "and create a corrected proposal after the guard window."
            )
        if failures:
            try:
                last_outcome_at = float(entry.get("last_outcome_at") or 0)
            except (TypeError, ValueError):
                last_outcome_at = 0
            if (
                entry.get("last_outcome") == "failed"
                and now - last_outcome_at < REFINEMENT_COOLDOWN_SECONDS
            ):
                return False, "Refinement apply cooldown is active after the last failure."
    return True, ""


def record_refinement_candidate_outcome(
    record: Dict[str, Any], outcome: str
):
    """Persist rejected/failed/applied outcomes for anti-loop enforcement."""
    if outcome not in {"rejected", "failed", "applied"}:
        return False, f"Unsupported refinement outcome: {outcome}"
    candidate = record.get("refinement_candidate")
    if not isinstance(candidate, dict):
        return True, ""
    integrity_ok, message = _validate_candidate_integrity(record)
    if not integrity_ok and outcome != "rejected":
        return False, message

    try:
        with _locked_refinement_guard():
            now = time.time()
            data = _load_refinement_guard()
            payload = record.get("payload", {})
            skill = str(
                payload.get("name")
                or candidate.get("target", {}).get("skill")
                or ""
            )
            entries = [_skill_guard_entry(data, skill)]
            if integrity_ok:
                entries.append(_guard_entry(data, candidate))
            for entry in entries:
                entry["attempts"] = _recent_timestamps(
                    entry.get("attempts"), now
                )
                failures = _recent_timestamps(entry.get("failures"), now)
                if outcome == "failed":
                    failures.append(now)
                entry["failures"] = failures
                entry["last_outcome"] = outcome
                entry["last_outcome_at"] = now
            _save_refinement_guard(data)
        return True, ""
    except Exception as exc:
        return False, f"Refinement loop guard update failed: {exc}"


def validate_refinement_candidate_base(record: Dict[str, Any]):
    """Fail closed when the approved candidate no longer matches its base."""
    ok, message = _validate_candidate_integrity(record)
    if not ok:
        return ok, message
    candidate = record.get("refinement_candidate")
    if not isinstance(candidate, dict):
        return True, ""
    try:
        skill_dir, target, label, error = _resolve_refinement_target(
            record.get("payload", {})
        )
    except Exception as exc:
        return False, f"Refinement candidate target check failed: {exc}"
    if error or skill_dir is None or target is None:
        return False, f"Refinement candidate base changed: {error or label}"
    bound_root = str(candidate.get("target", {}).get("root") or "")
    if str(skill_dir.resolve()) != bound_root:
        return False, "Refinement target root changed; create a new candidate."
    if _text_state(target) != candidate.get("base"):
        return False, "Refinement candidate base changed; create and approve a new candidate."
    return True, ""


def validate_refinement_candidate_result(record: Dict[str, Any]):
    """Verify that the approved skill write produced the frozen result."""
    candidate = record.get("refinement_candidate")
    if not isinstance(candidate, dict):
        return True, ""
    try:
        skill_dir, target, label, error = _resolve_refinement_target(
            record.get("payload", {})
        )
    except Exception as exc:
        return False, f"Refinement result check failed: {exc}"
    if error or skill_dir is None or target is None:
        return False, f"Refinement result target missing: {error or label}"
    bound_root = str(candidate.get("target", {}).get("root") or "")
    if str(skill_dir.resolve()) != bound_root:
        return False, "Refinement target root changed after apply."
    if _text_state(target) != candidate.get("proposed"):
        return False, "Refinement result hash did not match the approved candidate."
    return True, ""


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
    """Build the review diff for a staged skill write.

    Refinement candidates return the integrity-bound frozen diff captured at
    staging. Ordinary pending writes retain the legacy live-diff behaviour.
    """
    candidate = record.get("refinement_candidate")
    if isinstance(candidate, dict):
        ok, message = _validate_candidate_integrity(record)
        if not ok:
            return f"({message})"
        return str(candidate.get("diff") or "(no textual change)")

    payload = record.get("payload", {})
    action = payload.get("action", "")
    name = payload.get("name", "")

    if action == "create":
        return (payload.get("content") or "")

    # Resolve current on-disk content for diffable actions.
    try:
        from tools.skill_manager_tool import _find_skill
    except Exception:
        _find_skill = None  # type: ignore

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
                p = base / rel
                target_label = rel
            else:
                p = base / "SKILL.md"
            try:
                if p.exists():
                    current = p.read_text(encoding="utf-8")
            except Exception:
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
