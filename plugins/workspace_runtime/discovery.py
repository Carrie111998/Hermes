"""Workspace discovery — automatic detection of a Workspace OS workspace root.

This module is the heart of the workspace_runtime plugin. It exposes a single
public function `discover(cwd: Path | str = None)` that returns a
`DiscoveryVerdict` describing one of five states:

1. ``inside_workspace`` — exactly one root with all 4 canonical signals.
2. ``multi_workspace`` — multiple ancestor roots have >= 2 signals each.
3. ``partial_workspace`` — exactly one root with 2 or 3 of 4 signals.
4. ``not_a_workspace`` — no ancestor root has 2+ signals.
5. ``discovery_error`` — an unexpected exception during discovery.

The Verdict is also rendered as a stable text block suitable for inclusion
in a chat message body (caller's choice of channel). The text encoding is
prefix-stable across the same (cwd, mtime-signals) tuple, so a model can
pattern-match the marker.

Canonical-signal definitions live with the discoverer because the
four-file bootstrap load IS the Workspace-OS-recommended way to identify
a workspace. See iterations/iter-02-discovery-algorithm.md in the project-state
for the full algorithm.

Test-helper virtual filesystem via ``MonkeyPatchFS`` is NOT used — instead
tests construct temporary fixtures using ``tempfile.TemporaryDirectory`` to
make assertions readable and reproducible.
"""

from __future__ import annotations

import dataclasses
import enum
import html
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger("workspace_runtime.discovery")


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# Canonical signals per GOVERNANCE/CONTEXT-ROUTING.md Bootstrap Recipe and
# GOVERNANCE/BOOTSTRAP.md:11-21. Names mirror the verdict-block keys so the
# block is human-readable.
SIGNALS: FrozenSet[str] = frozenset({
    "identity",
    "architecture",
    "bootstrap_md",
    "workspace_index",
})

# Relative paths checked per signal. Symlinks ARE allowed — Workspace OS
# uses IDENTITY.md and ARCHITECTURE.md as symlinks (per
# /home/taras/projects/IDENTITY.md -> career-operating-system/EngineeringIdentity.md
# at the canonical workspace). So we accept any non-dir file or any
# symlink pointing at a non-dir file.
SIGNAL_PATHS: Dict[str, str] = {
    "identity": "IDENTITY.md",
    "architecture": "ARCHITECTURE.md",
    "bootstrap_md": "GOVERNANCE/BOOTSTRAP.md",
    "workspace_index": "CONTEXT/workspace-index.json",
}

# Companion file read for the 5th canonical bootstrap-validation question
# (per GOVERNANCE/BOOTSTRAP.md:167 — "How is authority routed? — from
# GOVERNANCE/AUTHORITY-MODEL.md"). Not part of the 4-file bootstrap but
# required for a fully-validated workspace.
AUTHORITY_MODEL_REL = "GOVERNANCE/AUTHORITY-MODEL.md"

# Numerical threshold for "this root looks like a workspace".
# >= 2 of the 4 canonical signals is the discovery threshold.
DISCOVERY_THRESHOLD = 2

# Telemetry target. Hermes may sandbox HERMES_HOME; we honour the env var.
TELEMETRY_DIRNAME = "workspace_runtime"
TELEMETRY_FILENAME = "last_discovery.json"

# Canonical four-file load, in the mandatory order from GOVERNANCE/BOOTSTRAP.md.
BOOTSTRAP_LOAD_PATHS: Tuple[str, ...] = tuple(SIGNAL_PATHS.values())


@dataclasses.dataclass(frozen=True)
class BootstrapContext:
    """Session-start Workspace context assembled from approved authorities."""

    files: Tuple[Tuple[str, str], ...]
    workspace_index: Dict[str, object]
    current_project: Optional[Path] = None
    current_mission: Optional[Path] = None
    mission_files: Tuple[Tuple[str, str], ...] = ()


class BootstrapLoadError(RuntimeError):
    """A canonical bootstrap file could not be loaded or parsed."""


# -----------------------------------------------------------------------------
# Verdict enum + dataclass
# -----------------------------------------------------------------------------


class VerdictState(str, enum.Enum):
    INSIDE = "inside_workspace"
    MULTI = "multi_workspace"
    PARTIAL = "partial_workspace"
    NOT_FOUND = "not_a_workspace"
    ERROR = "discovery_error"


@dataclasses.dataclass(frozen=True)
class DiscoveryVerdict:
    """Result of running `discover(cwd)`."""

    state: VerdictState
    cwd: Path
    root: Optional[Path] = None
    candidates: Tuple[Path, ...] = ()
    present: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    bootstrap_validation: Optional[str] = None
    # Integer 0..5 — number of canonical bootstrap-validation questions
    # answerable from cache. None if not computed (e.g. multi_workspace).
    questions_answerable: Optional[int] = None
    unanswerable_questions: Tuple[int, ...] = ()
    error_message: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> Dict[str, object]:
        d: Dict[str, object] = {
            "state": self.state.value,
            "cwd": str(self.cwd),
            "duration_ms": self.duration_ms,
        }
        if self.root is not None:
            d["root"] = str(self.root)
        if self.candidates:
            d["candidates"] = [str(c) for c in self.candidates]
        if self.present:
            d["present"] = list(self.present)
        if self.missing:
            d["missing"] = list(self.missing)
        if self.bootstrap_validation is not None:
            d["bootstrap_validation"] = self.bootstrap_validation
        if self.questions_answerable is not None:
            d["questions_answerable"] = self.questions_answerable
        if self.unanswerable_questions:
            d["unanswerable_questions"] = list(self.unanswerable_questions)
        if self.error_message is not None:
            d["error_message"] = self.error_message
        return d


# -----------------------------------------------------------------------------
# Signal helpers
# -----------------------------------------------------------------------------


def _signal_present(root: Path, signal: str) -> bool:
    """Return True iff the canonical path for `signal` is a regular file
    or a symlink resolving to a regular file, and the resolved path exists
    and is non-empty.

    Symlink resolution is used to detect dangling links — a broken symlink
    does NOT count. Empty files do NOT count.
    """
    rel = SIGNAL_PATHS[signal]
    p = root / rel
    if not p.exists():
        return False
    if not p.is_file():
        return False
    try:
        if p.stat().st_size == 0:
            return False
    except OSError:
        return False
    return True


def _present_signals(root: Path) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Return (present, missing) — disjoint sorted tuples.

    `present` and `missing` are computed once per call. Order is the
    canonical signal order from SIGNAL_PATHS to keep verdict blocks stable
    across runs.
    """
    present: List[str] = []
    missing: List[str] = []
    for sig in SIGNAL_PATHS:
        if _signal_present(root, sig):
            present.append(sig)
        else:
            missing.append(sig)
    return tuple(present), tuple(missing)


def _signal_count(root: Path) -> int:
    present, _ = _present_signals(root)
    return len(present)


# -----------------------------------------------------------------------------
# Bootstrap validation (5 canonical questions)
# -----------------------------------------------------------------------------


# Question → (file relative-path checked, label)
# Question 5 lives at AUTHORITY_MODEL_REL which is not in the 4-file
# bootstrap but IS required for a fully-validated workspace.
BOOTSTRAP_QUESTIONS: Dict[int, str] = {
    1: "identity",                 # canonical engineering identity
    2: "architecture",             # 6 subsystems
    3: "workspace_index",          # current state
    4: "bootstrap_md",             # what to ignore
    5: AUTHORITY_MODEL_REL,        # authority routing — companion file
}


def _validate_bootstrap(root: Path) -> Tuple[int, Tuple[int, ...]]:
    """Compute the bootstrap-validation score.

    Returns (answerable_count, unanswerable_question_numbers).

    Q1-Q4 map to canonical signals 1..4 (same files).
    Q5 reads AUTHORITY_MODEL_REL (companion). If absent, q5 unanswerable.

    Reading is STATIC (file-presence + non-empty). NLP-level validation of
    "does the file actually answer the question" belongs to the agent, not
    to the discoverer.
    """
    present, _ = _present_signals(root)
    answerable = 0
    unanswerable: List[int] = []
    for qnum, sigkey in BOOTSTRAP_QUESTIONS.items():
        # Map q5 (path-string) back to a presence-check on root/<path>.
        if qnum == 5:
            p = root / sigkey
            ok = p.exists() and p.is_file() and p.stat().st_size > 0
        else:
            ok = sigkey in present
        if ok:
            answerable += 1
        else:
            unanswerable.append(qnum)
    return answerable, tuple(sorted(unanswerable))


# -----------------------------------------------------------------------------
# Candidate ranking
# -----------------------------------------------------------------------------


def _rank_candidates(candidates: List[Path]) -> List[Path]:
    """Stable, deterministic ranking. Same input → same output, always.

    Sort key, computed per iter-03:
      1. depth-descending (more components win)
      2. signal-count-descending (more canonical signals win)
      3. governance-bonus-descending (presence of GOVERNANCE/AMENDMENTS.md
         is a strong root signal — prefer that)
      4. absolute path ascending (final tie-break, alphabetical)
    """
    def key(c: Path) -> Tuple[int, int, int, str]:
        # Positive depth means deepest LAST — but we want deepest FIRST,
        # so negate depth.
        depth = -len(c.parts)
        signals = -_signal_count(c)
        gov_bonus = -(
            1 if (c / "GOVERNANCE" / "AMENDMENTS.md").exists() else 0
        )
        # Lexical ascending (final tie-break).
        return (depth, signals, gov_bonus, c.as_posix())

    return sorted(candidates, key=key)


# -----------------------------------------------------------------------------
# Walk-up algorithm
# -----------------------------------------------------------------------------


def _walk_up(start: Path) -> List[Path]:
    """Walk from `start` (resolved) up to the filesystem root.

    Returns any ancestor (inclusive of `start` itself) at which
    `signal_count(...) >= DISCOVERY_THRESHOLD`. Returns in walk order:
    deepest first, shallowest last.

    Stops when the parent equals the child (i.e. reached fs root, including
    on Windows drive roots).
    """
    matches: List[Path] = []
    p = start.resolve() if start.exists() else start.absolute()
    seen: set = set()
    while True:
        if str(p) in seen:
            break  # cycle safety (should not occur on real fs)
        seen.add(str(p))
        try:
            if _signal_count(p) >= DISCOVERY_THRESHOLD:
                matches.append(p)
            if p.parent == p:
                break
            p = p.parent
        except (OSError, PermissionError) as exc:
            logger.debug("walk_up: stopping at %s due to %s", p, exc)
            break
    return matches


def _resolve_cwd(cwd: Optional[Path]) -> Path:
    """Resolve cwd per cwd-resolution-strategy in iter-01.

    Prefer $PWD over os.getcwd() when both are available and consistent.
    """
    if cwd is not None and str(cwd):
        return Path(cwd).resolve()
    pwd_env = os.environ.get("PWD")
    try:
        cwd_native = Path.cwd()
    except OSError:
        cwd_native = None
    if pwd_env and cwd_native is not None:
        try:
            if Path(pwd_env).resolve() == cwd_native.resolve():
                return cwd_native
        except OSError:
            pass
    if cwd_native is not None:
        return cwd_native.resolve()
    if pwd_env:
        return Path(pwd_env).resolve()
    return Path.cwd().resolve()


def _fallback_cwd(cwd: Optional[Path]) -> Path:
    if cwd is not None and str(cwd):
        try:
            return Path(cwd).absolute()
        except (OSError, RuntimeError, ValueError):
            return Path("/")
    pwd = os.environ.get("PWD", "").strip()
    if pwd:
        try:
            return Path(pwd).absolute()
        except (OSError, RuntimeError, ValueError):
            pass
    return Path("/")


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------


def discover(cwd: Optional[Path] = None) -> DiscoveryVerdict:
    """Discover the Workspace OS workspace root containing `cwd`.

    Returns a frozen `DiscoveryVerdict`.
    """
    started = time.perf_counter()
    try:
        cwd_resolved = _resolve_cwd(cwd)
        if not cwd_resolved.exists():
            return DiscoveryVerdict(
                state=VerdictState.NOT_FOUND,
                cwd=cwd_resolved,
                bootstrap_validation="not_applicable",
                questions_answerable=0,
                duration_ms=_ms_since(started),
            )

        candidates = _walk_up(cwd_resolved)

        # Walk-up may include cwd_resolved itself; deduplicate by absolute path.
        candidates = _dedup(candidates)

        if not candidates:
            return DiscoveryVerdict(
                state=VerdictState.NOT_FOUND,
                cwd=cwd_resolved,
                bootstrap_validation="not_applicable",
                questions_answerable=0,
                duration_ms=_ms_since(started),
            )

        if len(candidates) == 1:
            root = candidates[0]
            present, missing = _present_signals(root)
            ans, unans = _validate_bootstrap(root)
            if ans >= 4:
                # 4-of-5 or 5-of-5 → full inside_workspace.
                # Note: even if Q5 (authority model) is unanswerable, having
                # all 4 bootstrap signals + the optional GOVERNANCE/MISSION
                # plane is enough to apply Workspace OS as the operating
                # system; the missing authority model surfaces in
                # questions_answerable, not in state.
                return DiscoveryVerdict(
                    state=VerdictState.INSIDE,
                    cwd=cwd_resolved,
                    root=root,
                    present=present,
                    missing=missing,
                    bootstrap_validation=(
                        "passed" if ans == 5 else "almost_passed"
                    ),
                    questions_answerable=ans,
                    unanswerable_questions=unans,
                    duration_ms=_ms_since(started),
                )
            return DiscoveryVerdict(
                state=VerdictState.PARTIAL,
                cwd=cwd_resolved,
                root=root,
                present=present,
                missing=missing,
                bootstrap_validation="partial",
                questions_answerable=ans,
                unanswerable_questions=unans,
                duration_ms=_ms_since(started),
            )

        # 2+ candidates → multi.
        ranked = _rank_candidates(candidates)
        return DiscoveryVerdict(
            state=VerdictState.MULTI,
            cwd=cwd_resolved,
            candidates=tuple(ranked),
            bootstrap_validation="not_applicable",
            questions_answerable=0,
            duration_ms=_ms_since(started),
        )

    except Exception as exc:  # noqa: BLE001 — discoverer MUST NOT raise
        logger.exception("discover() crashed unexpectedly")
        return DiscoveryVerdict(
            state=VerdictState.ERROR,
            cwd=_fallback_cwd(cwd),
            error_message=f"{type(exc).__name__}: {exc}",
            duration_ms=_ms_since(started),
        )


# -----------------------------------------------------------------------------
# Canonical bootstrap context
# -----------------------------------------------------------------------------


def load_bootstrap_context(root: Path, cwd: Path) -> BootstrapContext:
    """Load exactly the four approved bootstrap authorities in order.

    The workspace index is parsed so the runtime can recover the current
    project path and an unambiguous active mission. Mission files are loaded
    only when the cwd is inside that mission directory; matching mission scope
    against free-form user intent remains the agent's canonical responsibility.
    """
    loaded: List[Tuple[str, str]] = []
    for rel in BOOTSTRAP_LOAD_PATHS:
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BootstrapLoadError(f"cannot load {path}: {exc}") from exc
        if not text.strip():
            raise BootstrapLoadError(f"canonical bootstrap file is empty: {path}")
        loaded.append((rel, text))

    try:
        index = json.loads(dict(loaded)["CONTEXT/workspace-index.json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise BootstrapLoadError(f"invalid workspace index: {exc}") from exc
    if not isinstance(index, dict):
        raise BootstrapLoadError("workspace index must contain a JSON object")

    current_project = _current_project(root, cwd)
    current_mission = _current_mission(root, cwd, index)
    mission_files = _load_mission_files(current_mission) if current_mission else ()
    return BootstrapContext(
        files=tuple(loaded),
        workspace_index=index,
        current_project=current_project,
        current_mission=current_mission,
        mission_files=mission_files,
    )


def _current_project(root: Path, cwd: Path) -> Path:
    state_root = root / ".project-state"
    if _is_relative_to(cwd, state_root):
        return root
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return root
    return root if not rel.parts else root / rel.parts[0]


def _current_mission(
    root: Path, cwd: Path, workspace_index: Dict[str, object]
) -> Optional[Path]:
    state_root = root / ".project-state"
    if _is_relative_to(cwd, state_root):
        try:
            rel = cwd.relative_to(state_root)
        except ValueError:
            rel = Path()
        if rel.parts:
            candidate = state_root / rel.parts[0]
            if candidate.is_dir():
                return candidate

    active = workspace_index.get("active_sprints", [])
    if not isinstance(active, list):
        return None
    candidates: List[Path] = []
    for raw in active:
        if not isinstance(raw, str):
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate.is_dir() and _is_relative_to(candidate, state_root):
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def _load_mission_files(mission: Path) -> Tuple[Tuple[str, str], ...]:
    loaded: List[Tuple[str, str]] = []
    for name in ("source-task.md", "progress.md"):
        path = mission / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if text.strip():
            loaded.append((name, text))
    return tuple(loaded)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def render_bootstrap_context(context: BootstrapContext) -> str:
    """Render a bounded canonical bootstrap cache for the hook channel.

    Full source contents remain loaded in ``BootstrapContext``. The rendered
    form preserves the authority-bearing startup sections while staying below
    Hermes' 10k hook-output spill threshold; the model receives direct paths
    for any intent-driven follow-up read.
    """
    attrs = ["<workspace-runtime-context"]
    if context.current_project is not None:
        attrs.append(f'  current_project="{html.escape(context.current_project.as_posix())}"')
    if context.current_mission is not None:
        attrs.append(f'  current_mission="{html.escape(context.current_mission.as_posix())}"')
    attrs.append(">")
    lines = attrs
    budgets = {
        "IDENTITY.md": 1_800,
        "ARCHITECTURE.md": 2_500,
        "GOVERNANCE/BOOTSTRAP.md": 2_500,
    }
    for rel, text in context.files:
        if rel == "CONTEXT/workspace-index.json":
            rendered = _render_index_summary(context)
        else:
            rendered = _clip_context(text, budgets[rel])
        lines.extend((f'<bootstrap-file path="{html.escape(rel)}">', rendered, "</bootstrap-file>"))
    for name, text in context.mission_files:
        lines.extend((f'<mission-file path="{html.escape(name)}">', _clip_context(text, 500), "</mission-file>"))
    lines.append("</workspace-runtime-context>")
    return "\n".join(lines)


def _clip_context(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    boundary = clipped.rfind("\n")
    if boundary > max_chars // 2:
        clipped = clipped[:boundary]
    return f"{clipped}\n[bounded bootstrap excerpt; read canonical path on intent]"


def _render_index_summary(context: BootstrapContext) -> str:
    active = context.workspace_index.get("active_sprints", [])
    summary: Dict[str, object] = {
        "workspace_root": context.workspace_index.get("workspace_root"),
        "active_sprints": {
            "count": len(active) if isinstance(active, list) else 0,
            "current": (
                context.current_mission.as_posix()
                if context.current_mission is not None
                else None
            ),
        },
        "top_level_keys": sorted(context.workspace_index.keys()),
    }
    return json.dumps(summary, indent=2, sort_keys=True)


# -----------------------------------------------------------------------------
# Verdict → text encoder (system-prompt / user-message injection body)
# -----------------------------------------------------------------------------


def render_verdict_block(verdict: DiscoveryVerdict) -> str:
    """Render the verdict as a stable XML-like text block.

    The block name `<workspace-runtime-verdict>` is stable. Tags have a
    stable order. Empty values are omitted. The block is byte-stable for
    the same (state, root, present, missing, candidates) tuple.

    Use this to embed in either the system prompt (volatile tier) or the
    user message (caller-chosen).
    """
    lines: List[str] = []
    lines.append("<workspace-runtime-verdict")
    lines.append(f'  state="{verdict.state.value}"')
    lines.append(f'  cwd="{verdict.cwd.as_posix()}"')
    lines.append(f'  duration_ms="{verdict.duration_ms}"')

    if verdict.root is not None:
        lines.append(f'  root="{verdict.root.as_posix()}"')
    if verdict.candidates:
        joined = " ".join(c.as_posix() for c in verdict.candidates)
        lines.append(f'  candidates="{joined}"')
    if verdict.present:
        joined = " ".join(verdict.present)
        lines.append(f'  present="{joined}"')
    if verdict.missing:
        joined = " ".join(verdict.missing)
        lines.append(f'  missing="{joined}"')
    if verdict.questions_answerable is not None:
        lines.append(f'  questions_answerable="{verdict.questions_answerable}/5"')
    if verdict.unanswerable_questions:
        joined = " ".join(str(q) for q in verdict.unanswerable_questions)
        lines.append(f'  unanswerable_questions="{joined}"')
    if verdict.bootstrap_validation is not None:
        lines.append(f'  bootstrap_validation="{verdict.bootstrap_validation}"')
    if verdict.error_message:
        lines.append(f'  error_message="{verdict.error_message}"')

    # Body per state — kept short and operator-readable.
    body = _verdict_body(verdict)
    lines.append(">")
    for line in body.split("\n"):
        lines.append(f"  {line}")
    lines.append("</workspace-runtime-verdict>")
    return "\n".join(lines)


def _verdict_body(v: DiscoveryVerdict) -> str:
    """Human-readable body per state."""
    if v.state == VerdictState.INSIDE:
        return (
            "Workspace OS is applied as the operating system of this Workspace.\n"
            "Cold-start procedure: see workspace-os/docs/BOOTSTRAP-PROCEDURE.md.\n"
            "Canonical procedure owner: {root}/GOVERNANCE/BOOTSTRAP.md."
        ).format(root=v.root.as_posix() if v.root else "")

    if v.state == VerdictState.MULTI:
        rs = "\n".join(f"  - {c.as_posix()} (signals={_signal_count(c)})"
                       for c in v.candidates)
        return (
            "Multiple ancestor roots qualify as Workspace candidates.\n"
            "Most-likely first:\n"
            f"{rs}\n"
            "Disambiguate by starting a fresh session from the intended root."
        )

    if v.state == VerdictState.PARTIAL:
        return (
            "Workspace root is partial — not all canonical signals present.\n"
            "Bootstrap-validation questions answerable: {}/5.\n"
            "Missing signals: {}.\n"
            "Action: populate missing canonical files OR treat this Workspace as outside for state-changing actions."
        ).format(
            v.questions_answerable or 0,
            ", ".join(v.missing) if v.missing else "(none)",
        )

    if v.state == VerdictState.NOT_FOUND:
        return (
            "Workspace discovery did NOT locate a Workspace.\n"
            "Cwd is bare — Workspace OS is NOT applied.\n"
            "Read-only tools continue normally; state-changing actions require explicit operator confirmation."
        )

    if v.state == VerdictState.ERROR:
        return (
            "Workspace discovery errored — Workspace status is unknown.\n"
            f"Error: {v.error_message or '(no message)'}\n"
            "Treat the runtime as bare until the operator clears the underlying fault."
        )

    return "Unknown verdict state."


# -----------------------------------------------------------------------------
# Telemetry
# -----------------------------------------------------------------------------


def write_telemetry(verdict: DiscoveryVerdict, *, session_id: Optional[str],
                     path: Optional[Path] = None) -> Optional[Path]:
    """Write a one-line JSON telemetry record. Returns the path or None.

    The file is debug-only; contains no secrets. It is OVERWRITTEN on
    every invocation so it always reflects the latest session start.
    """
    target = path
    if target is None:
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser(
            "~/.hermes"
        )
        target = Path(hermes_home) / TELEMETRY_DIRNAME / TELEMETRY_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("telemetry mkdir failed at %s: %s", target.parent, exc)
        return None
    payload: Dict[str, object] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
    }
    payload.update(verdict.to_dict())
    try:
        with target.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        return target
    except OSError as exc:
        logger.debug("telemetry write failed at %s: %s", target, exc)
        return None


# -----------------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------------


def _dedup(paths: List[Path]) -> List[Path]:
    seen: set = set()
    out: List[Path] = []
    for p in paths:
        key = p.as_posix()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
