"""Per-turn file-mutation verifier — content-transition ledger and observation.

Tracks dispatched ``write_file`` / ``patch`` outcomes against stable local
filesystem snapshots.  A safely observed later content change may clear a
stale unresolved entry without proving payload equality (CONTENT-TRANSITION
contract).  When observation is unavailable the ledger fails closed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from agent.tool_result_classification import (
    FILE_MUTATING_TOOL_NAMES,
    file_mutation_result_landed,
)
from agent.tool_dispatch_helpers import (
    _extract_error_preview,
    _extract_file_mutation_targets,
    _extract_landed_file_mutation_paths,
)

logger = logging.getLogger(__name__)

# Turn-wide bounds (fail closed via overflow summary when exceeded).
MAX_LEDGER_ENTRIES = 64
MAX_SNAPSHOT_ATTEMPTS = 128
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 8 * 1024 * 1024
OBSERVATION_TIMEOUT_S = 2.0

_GENERIC_FOOTER_FALLBACK = (
    "⚠️ File-mutation verifier: one or more file edits may not have landed "
    "this turn. Run `git status` or `read_file` to confirm."
)

_FOOTER_INJECTION_RE = re.compile(
    r"(?im)^\s*(MEDIA:|FILE:|ATTACHMENT:|\!\[).*$"
)


class DispatchTriState(str, Enum):
    NOT_DISPATCHED = "not_dispatched"
    DISPATCHED = "dispatched"
    DISPATCHED_NO_RESULT = "dispatched_no_result"


@dataclass(frozen=True)
class MutationIdentity:
    backend_kind: str
    authority: str
    task_scope: str
    path_dialect: str
    path: str

    def display_path(self) -> str:
        return self.path

    def key(self) -> Tuple[str, str, str, str, str]:
        return (
            self.backend_kind,
            self.authority,
            self.task_scope,
            self.path_dialect,
            self.path,
        )


@dataclass
class ContentFingerprint:
    size: int
    digest: bytes
    mtime_ns: int
    inode: int


@dataclass
class LedgerEntry:
    identity: MutationIdentity
    tool: str
    error_preview: str
    baseline: Optional[ContentFingerprint]
    unresolved: bool = True
    overflow: bool = False


@dataclass
class TurnObservationBudget:
    generation: int = 0
    snapshot_attempts: int = 0
    snapshot_bytes: int = 0
    ledger_count: int = 0
    overflow: bool = False


class TurnFileMutationVerifier:
    """Turn-scoped mutation ledger with local content-transition observation."""

    def __init__(
        self,
        *,
        resolve_backend: Optional[Callable[[str], Tuple[str, str, str]]] = None,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolve_backend = resolve_backend or _default_resolve_backend
        self._now_fn = now_fn
        self._budget = TurnObservationBudget()
        self._ledger: Dict[Tuple[str, str, str, str, str], LedgerEntry] = {}
        self._lock = threading.Lock()
        self._active_workers: Set[int] = set()
        self._pre_dispatch_baselines: Dict[Tuple[str, str], Optional[ContentFingerprint]] = {}

    def reset_turn(self, generation: int = 0) -> None:
        with self._lock:
            self._budget = TurnObservationBudget(generation=generation)
            self._ledger.clear()
            self._active_workers.clear()
            self._pre_dispatch_baselines.clear()

    def prepare_mutation_dispatch(
        self,
        *,
        tool_name: str,
        effective_args: Dict[str, Any],
        effective_task_id: str,
        turn_generation: Optional[int] = None,
    ) -> None:
        """Capture filesystem baselines immediately before registry dispatch."""
        if turn_generation is not None and turn_generation != self._budget.generation:
            return
        if tool_name not in FILE_MUTATING_TOOL_NAMES:
            return
        scope = effective_task_id or "default"
        for raw_path in _extract_file_mutation_targets(tool_name, effective_args):
            fp = self._capture_baseline(
                raw_path,
                scope,
                turn_generation=turn_generation,
            )
            self._pre_dispatch_baselines[(scope, raw_path)] = fp

    def clear_turn(self) -> None:
        self.reset_turn(0)

    @property
    def generation(self) -> int:
        return self._budget.generation

    def record_tool_outcome(
        self,
        *,
        tool_name: str,
        effective_args: Dict[str, Any],
        effective_task_id: str,
        raw_result: Any,
        dispatch: DispatchTriState,
        model_is_error: bool,
        blocked: bool = False,
        turn_generation: Optional[int] = None,
    ) -> None:
        if turn_generation is not None and turn_generation != self._budget.generation:
            return
        if tool_name not in FILE_MUTATING_TOOL_NAMES:
            return
        if blocked or dispatch is DispatchTriState.NOT_DISPATCHED:
            return
        targets = _extract_file_mutation_targets(tool_name, effective_args)
        if not targets:
            return

        if dispatch is DispatchTriState.DISPATCHED_NO_RESULT:
            preview = "tool dispatch finished without a result"
            for raw_path in targets:
                self._upsert_unresolved(
                    tool_name=tool_name,
                    raw_path=raw_path,
                    task_id=effective_task_id,
                    error_preview=preview,
                    baseline=None,
                )
            return

        raw_landed = file_mutation_result_landed(tool_name, raw_result)
        if raw_landed:
            landed_paths = _extract_landed_file_mutation_paths(
                tool_name, effective_args, raw_result,
            )
            scope = effective_task_id or "default"
            for raw_path in _extract_file_mutation_targets(tool_name, effective_args):
                self._pre_dispatch_baselines.pop((scope, raw_path), None)
            for raw_path in landed_paths:
                ident = self._identity_for_path(raw_path, effective_task_id)
                if ident is not None:
                    self._ledger.pop(ident.key(), None)
            return

        if model_is_error or not raw_landed:
            preview = _extract_error_preview(raw_result)
            scope = effective_task_id or "default"
            for raw_path in targets:
                baseline = self._pre_dispatch_baselines.pop((scope, raw_path), None)
                if baseline is None:
                    baseline = self._capture_baseline(
                        raw_path,
                        scope,
                        turn_generation=turn_generation,
                    )
                self._upsert_unresolved(
                    tool_name=tool_name,
                    raw_path=raw_path,
                    task_id=effective_task_id,
                    error_preview=preview,
                    baseline=baseline,
                )

    def observe_after_tool(
        self,
        *,
        tool_name: str,
        effective_task_id: str,
        blocked: bool = False,
    ) -> None:
        if blocked:
            return
        self.reconcile_content_transitions(task_id=effective_task_id)

    def reconcile_content_transitions(self, *, task_id: Optional[str] = None) -> None:
        if not self._ledger:
            return
        groups: Dict[str, List[Tuple[Tuple[str, str, str, str, str], LedgerEntry]]] = {}
        for key, entry in list(self._ledger.items()):
            if not entry.unresolved or entry.baseline is None:
                continue
            ident = entry.identity
            scope = ident.task_scope
            if task_id is not None and scope != (task_id or "default"):
                continue
            canon = _canonical_observation_key(ident.path, scope) or ident.path
            groups.setdefault(canon, []).append((key, entry))

        to_remove: List[Tuple[str, str, str, str, str]] = []
        for canon, items in groups.items():
            baseline = items[0][1].baseline
            assert baseline is not None
            sample_path = items[0][1].identity.path
            sample_scope = items[0][1].identity.task_scope
            current = self._capture_baseline(sample_path, sample_scope)
            if current is None:
                continue
            if current.digest != baseline.digest:
                to_remove.extend(k for k, _ in items)
        for key in to_remove:
            self._ledger.pop(key, None)

    def finalize_failed_dict(self) -> Dict[str, Dict[str, Any]]:
        self.reconcile_content_transitions()

        failed: Dict[str, Dict[str, Any]] = {}
        for entry in self._ledger.values():
            if not entry.unresolved:
                continue
            path = entry.identity.display_path()
            if path in failed:
                continue
            failed[path] = {
                "tool": entry.tool,
                "error_preview": _sanitize_footer_text(entry.error_preview),
            }
        if self._budget.overflow:
            failed["__overflow__"] = {
                "tool": "patch",
                "error_preview": "Additional unresolved file mutations were omitted (turn budget).",
            }
        return failed

    def _upsert_unresolved(
        self,
        *,
        tool_name: str,
        raw_path: str,
        task_id: str,
        error_preview: str,
        baseline: Optional[ContentFingerprint],
    ) -> None:
        ident = self._identity_for_path(raw_path, task_id)
        if ident is None:
            return
        key = ident.key()
        with self._lock:
            if len(self._ledger) >= MAX_LEDGER_ENTRIES and key not in self._ledger:
                self._budget.overflow = True
                return
            existing = self._ledger.get(key)
            if existing is not None and existing.unresolved:
                return
            self._ledger[key] = LedgerEntry(
                identity=ident,
                tool=tool_name,
                error_preview=error_preview,
                baseline=baseline,
                unresolved=True,
            )
            self._budget.ledger_count = len(self._ledger)

    def _identity_for_path(
        self, raw_path: str, task_id: str,
    ) -> Optional[MutationIdentity]:
        backend_kind, authority, dialect = self._resolve_backend(task_id or "default")
        display = str(raw_path)
        if backend_kind != "local":
            return MutationIdentity(
                backend_kind=backend_kind,
                authority=authority,
                task_scope=task_id or "default",
                path_dialect=dialect,
                path=display,
            )
        return MutationIdentity(
            backend_kind=backend_kind,
            authority=authority,
            task_scope=task_id or "default",
            path_dialect=dialect,
            path=display,
        )

    def _capture_baseline(
        self,
        raw_path: str,
        task_id: str,
        *,
        turn_generation: Optional[int] = None,
    ) -> Optional[ContentFingerprint]:
        if turn_generation is not None and turn_generation != self._budget.generation:
            return None
        ident = self._identity_for_path(raw_path, task_id)
        if ident is None or ident.backend_kind != "local":
            return None
        if not _path_allowed_for_observation(ident.path):
            return None
        with self._lock:
            if self._budget.snapshot_attempts >= MAX_SNAPSHOT_ATTEMPTS:
                self._budget.overflow = True
                return None
            self._budget.snapshot_attempts += 1
        resolved = _resolve_local_path(raw_path, task_id)
        if resolved is None:
            return None
        fp, read_bytes = _stable_local_fingerprint(
            resolved,
            deadline=self._now_fn() + OBSERVATION_TIMEOUT_S,
            active_workers=self._active_workers,
        )
        with self._lock:
            self._budget.snapshot_bytes += read_bytes
            if self._budget.snapshot_bytes > MAX_SNAPSHOT_BYTES:
                self._budget.overflow = True
        return fp


def _canonical_observation_key(raw_path: str, task_id: str) -> Optional[str]:
    resolved = _resolve_local_path(raw_path, task_id)
    if resolved is None:
        return None
    try:
        from agent.tool_dispatch_helpers import _canonical_path

        return str(_canonical_path(str(resolved)))
    except Exception:
        return str(resolved.resolve())


def _default_resolve_backend(task_id: str) -> Tuple[str, str, str]:
    try:
        from tools.terminal_tool import get_active_env

        env = get_active_env(task_id)
    except Exception:
        env = None
    if env is None:
        return "local", "host", _path_dialect()
    cls = type(env).__name__
    if cls == "LocalEnvironment":
        return "local", "host", _path_dialect()
    if "SSH" in cls:
        return "ssh", cls, "posix"
    if "Docker" in cls or "Singularity" in cls or "Modal" in cls or "Daytona" in cls:
        return "container", cls, "posix"
    return "unknown", cls, "unknown"


def _path_dialect() -> str:
    return "windows" if os.name == "nt" else "posix"


def _resolve_local_path(raw_path: str, task_id: str) -> Optional[Path]:
    try:
        from tools.file_tools import _resolve_path_for_task

        resolved = _resolve_path_for_task(raw_path, task_id or "default")
    except Exception:
        try:
            resolved = Path(raw_path).expanduser()
        except Exception:
            return None
    if not isinstance(resolved, Path):
        return None
    return resolved


def _path_allowed_for_observation(path: str) -> bool:
    if not path or not isinstance(path, str):
        return False
    p = path.strip()
    if not p:
        return False
    lower = p.lower()
    if lower.startswith("\\\\.\\") or lower.startswith("\\\\?\\"):
        return False
    if p.startswith("\\\\") and not p.startswith("\\\\?\\"):
        return False
    if re.match(r"^[a-zA-Z]:\\", p):
        rest = p[3:]
        if rest.startswith("\\") and _is_dos_device_component(rest.lstrip("\\").split("\\")[0]):
            return False
    parts = re.split(r"[\\/]", p)
    for part in parts:
        if _is_dos_device_component(part):
            return False
    if ".." in parts:
        return False
    try:
        from agent.file_safety import get_read_block_error, is_write_denied

        if get_read_block_error(p) or is_write_denied(p):
            return False
    except Exception:
        pass
    return True


def _is_dos_device_component(name: str) -> bool:
    if not name:
        return False
    base = name.split(".")[0].upper()
    return base in {
        "CON", "PRN", "AUX", "NUL",
        *{f"COM{i}" for i in range(1, 10)},
        *{f"LPT{i}" for i in range(1, 10)},
    }


def _stable_local_fingerprint(
    path: Path,
    *,
    deadline: float,
    active_workers: Set[int],
) -> Tuple[Optional[ContentFingerprint], int]:
    if time.monotonic() > deadline:
        return None, 0
    try:
        st = path.lstat()
    except OSError:
        return None, 0
    if stat.S_ISLNK(st.st_mode):
        return None, 0
    if not stat.S_ISREG(st.st_mode):
        return None, 0
    if st.st_size > MAX_SINGLE_FILE_BYTES:
        return None, 0
    try:
        st_open = os.stat(path, follow_symlinks=True)
    except OSError:
        return None, 0
    if not stat.S_ISREG(st_open.st_mode):
        return None, 0
    if st_open.st_size > MAX_SINGLE_FILE_BYTES:
        return None, 0
    read_bytes = 0
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                if time.monotonic() > deadline:
                    return None, read_bytes
                chunk = fh.read(65536)
                if not chunk:
                    break
                read_bytes += len(chunk)
                h.update(chunk)
        st_after = os.stat(path, follow_symlinks=True)
    except OSError:
        return None, read_bytes
    if (
        st_after.st_size != st_open.st_size
        or st_after.st_mtime_ns != st_open.st_mtime_ns
        or st_after.st_ino != st_open.st_ino
    ):
        return None, read_bytes
    return (
        ContentFingerprint(
            size=st_after.st_size,
            digest=h.digest(),
            mtime_ns=st_after.st_mtime_ns,
            inode=st_after.st_ino,
        ),
        read_bytes,
    )


def _sanitize_footer_text(text: str) -> str:
    if not text:
        return text
    cleaned = _FOOTER_INJECTION_RE.sub("[filtered]", text)
    cleaned = cleaned.replace("\x00", "")
    return cleaned


def format_failure_footer(
    failed: Dict[str, Dict[str, Any]],
    *,
    format_paths: Callable[[str], str],
) -> str:
    if not failed:
        return ""
    overflow = failed.pop("__overflow__", None)
    try:
        lines = [
            "⚠️ File-mutation verifier: "
            f"{len(failed)} file(s) were NOT modified this turn despite any "
            "wording above that may suggest otherwise. Run `git status` or "
            "`read_file` to confirm."
        ]
        shown = 0
        for path, info in failed.items():
            if shown >= 10:
                break
            preview = _sanitize_footer_text((info.get("error_preview") or "").strip())
            tool = info.get("tool") or "patch"
            if preview:
                lines.append(f"  • `{path}` — [{tool}] {preview}")
            else:
                lines.append(f"  • `{path}` — [{tool}] failed")
            shown += 1
        remaining = len(failed) - shown
        if remaining > 0:
            lines.append(f"  • … and {remaining} more")
        if overflow:
            lines.append("  • … additional failures omitted (turn observation budget)")
        return format_paths("\n".join(lines))
    except Exception:
        logger.debug("file-mutation footer formatting failed", exc_info=True)
        return _GENERIC_FOOTER_FALLBACK


def get_verifier(agent: Any) -> Optional[TurnFileMutationVerifier]:
    return getattr(agent, "_file_mutation_verifier", None)


def ensure_verifier(agent: Any) -> TurnFileMutationVerifier:
    v = getattr(agent, "_file_mutation_verifier", None)
    if v is None:
        v = TurnFileMutationVerifier()
        agent._file_mutation_verifier = v
    return v


def sync_legacy_failed_state(agent: Any) -> None:
    v = get_verifier(agent)
    state = getattr(agent, "_turn_failed_file_mutations", None)
    if v is None or state is None:
        return
    state.clear()
    state.update(v.finalize_failed_dict())
