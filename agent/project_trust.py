"""Machine-written trust sidecar for project-local skills.

Trust for repo-local skills (``<root>/.hermes/skills``, ``<root>/.agents/skills``)
lives in a machine-managed JSON sidecar at ``~/.hermes/project-trust.json`` —
NOT in ``config.yaml``. That separation is the security boundary: a
repo-committed file (including ``config.yaml`` copied into a checkout) must
never be able to grant itself trust, which would turn every ``git clone`` into
an arbitrary-instruction-injection vector. Only Hermes itself writes this file,
via ``hermes skills trust`` / ``hermes skills untrust`` and the one-time
legacy-config migration.

This mirrors ``agent/shell_hooks.py``'s ``shell-hooks-allowlist.json`` pattern:
versioned schema, atomic ``mkstemp`` + ``os.replace`` writes, and a sibling
``.lock`` file (``fcntl.flock``) serialising cross-process read-modify-write.

Schema (``version`` bumps only on a breaking change)::

    {
      "version": 2,
      "projects": {
        "/abs/resolved/project/root": {
          "status": "trusted",              # "trusted" | "denied"
          "approved_at": "2026-08-18T...Z",
          "fingerprints": {                  # relative skill dir -> sha256
            "repo-skill": "ab12…",
            "team/deploy": "cd34…"
          }
        }
      }
    }

Fingerprints
------------
At trust time we record one deterministic manifest digest for every discovered
project skill package.  The manifest includes every regular file below the
skill directory as ``(relative path, sha256(contents))`` and refuses packages
containing symlinks.  Keys include the source root (for example
``.hermes/skills/repo-skill``), so an identically named ``.agents`` package is
a distinct approval.  At agent-build time the trust gate re-reads each package
once and compares:

* a skill whose name is **new** since approval — excluded (not yet approved);
* a skill whose hash **changed** since approval — excluded (injection-swap
  defense: the content the user approved is not what is on disk now);
* a skill **removed** since approval — silently pruned from the sidecar on the
  next ``hermes skills trust`` run.

Re-running ``hermes skills trust`` re-fingerprints everything — that IS the
re-approval. A project with ``status == "denied"`` produces no notice ever
(sticky deny / silence).

Cache safety
------------
The resolved approved-package snapshot is computed once per project/profile at
agent build.  Its cached ``SKILL.md`` bytes are reused by index/parse surfaces,
so verification and instruction parsing operate on the same read.  Supporting
scripts are necessarily opened later when executed, leaving a residual window
in which an already-approved script can be replaced after the build snapshot.
A future hardening follow-up can eliminate that window by copying approved
packages into a private immutable session directory; that larger architecture
is intentionally outside this change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import fcntl  # POSIX only; Windows falls back to best-effort without flock.
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)

SIDECAR_FILENAME = "project-trust.json"
SCHEMA_VERSION = 2

STATUS_TRUSTED = "trusted"
STATUS_DENIED = "denied"

# Intra-process fallback lock for platforms without ``fcntl``.
import threading

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def normalize_skill_content(text: str) -> str:
    """Normalise SKILL.md content for hashing: collapse line endings to ``\\n``.

    Line-ending churn (a Windows checkout, an editor rewrite) must NOT read as a
    content change — only a real edit to the instruction body should invalidate
    trust. CRLF and lone CR both fold to LF.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def fingerprint_skill_md(path: Path) -> Optional[str]:
    """sha256 of a single SKILL.md's normalised content, or None on read error."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    normalized = normalize_skill_content(raw)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def skill_identity(skills_dir: Path, skill_dir: Path) -> str:
    """Return a root-keyed identity such as ``.hermes/skills/foo``."""
    try:
        relative = Path(skill_dir).relative_to(Path(skills_dir)).as_posix()
    except ValueError:
        relative = Path(skill_dir).name
    source = Path(skills_dir).parent.name
    return f"{source}/skills/{relative}"


def fingerprint_skill_package(skill_dir: Path) -> Tuple[Optional[str], Optional[bytes]]:
    """Return ``(manifest_digest, SKILL.md bytes)`` from one package walk.

    Every filesystem entry must be a real directory or regular file.  Symlinks
    and unsupported entry types invalidate the package instead of being
    followed.  File bytes are read once; the returned ``SKILL.md`` bytes are
    therefore exactly those folded into the manifest.
    """
    skill_dir = Path(skill_dir)
    manifest: List[Tuple[str, str]] = []
    skill_md_bytes: Optional[bytes] = None
    try:
        if skill_dir.is_symlink() or not skill_dir.is_dir():
            return None, None
        entries = sorted(
            skill_dir.rglob("*"), key=lambda p: p.relative_to(skill_dir).as_posix()
        )
        for path in entries:
            if path.is_symlink():
                return None, None
            mode = path.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                return None, None
            raw = path.read_bytes()
            rel = path.relative_to(skill_dir).as_posix()
            hashed_raw = raw
            if rel == "SKILL.md":
                text = raw.decode("utf-8-sig", errors="replace")
                hashed_raw = normalize_skill_content(text).encode("utf-8")
            manifest.append((rel, hashlib.sha256(hashed_raw).hexdigest()))
            if rel == "SKILL.md":
                skill_md_bytes = raw
    except OSError:
        return None, None
    if skill_md_bytes is None:
        return None, None
    encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest(), skill_md_bytes


def fingerprint_project_skills(skills_dirs: List[Path]) -> Dict[str, str]:
    """Map ``{root-keyed skill package -> manifest sha256}`` for a project.

    Imports :func:`iter_skill_index_files` lazily to avoid a module import cycle
    (``skill_utils`` imports this module for the gate).
    """
    from agent.skill_utils import iter_skill_index_files

    fingerprints: Dict[str, str] = {}
    for d in skills_dirs:
        d = Path(d)
        try:
            for skill_md in iter_skill_index_files(d, "SKILL.md"):
                identity = skill_identity(d, Path(skill_md).parent)
                digest, _ = fingerprint_skill_package(Path(skill_md).parent)
                if digest is not None:
                    fingerprints[identity] = digest
        except OSError:
            continue
    return fingerprints


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------


def sidecar_path() -> Path:
    """Path to the machine-written project-trust sidecar under HERMES_HOME."""
    return get_hermes_home() / SIDECAR_FILENAME


class SidecarData(dict):
    """Mapping-compatible sidecar payload carrying its load state."""

    def __init__(self, *args, load_state: str = "valid", **kwargs):
        super().__init__(*args, **kwargs)
        self.load_state = load_state


def _empty_sidecar(*, load_state: str = "absent") -> SidecarData:
    return SidecarData(
        {"version": SCHEMA_VERSION, "projects": {}}, load_state=load_state
    )


def load_sidecar() -> SidecarData:
    """Return the parsed sidecar with ``absent``/``valid``/``corrupt`` state.

    A malformed sidecar fails *closed* (empty → nothing trusted): we never let a
    parse error silently promote an untrusted project. ``projects`` is always a
    dict on return.
    """
    try:
        raw = json.loads(sidecar_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_sidecar(load_state="absent")
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"Warning: project trust sidecar is corrupt; project skills are blocked: {exc}",
            file=sys.stderr,
        )
        return _empty_sidecar(load_state="corrupt")
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        print(
            "Warning: project trust sidecar is corrupt or has an unsupported version; project skills are blocked.",
            file=sys.stderr,
        )
        return _empty_sidecar(load_state="corrupt")
    projects = raw.get("projects")
    if not isinstance(projects, dict):
        print(
            "Warning: project trust sidecar has an invalid projects map; project skills are blocked.",
            file=sys.stderr,
        )
        return _empty_sidecar(load_state="corrupt")
    return SidecarData(raw, load_state="valid")


def save_sidecar(data: Dict[str, Any]) -> None:
    """Atomically persist the sidecar (mkstemp + ``atomic_replace``).

    Cross-process read-modify-write races are serialised by
    :func:`_locked_update` (``fcntl.flock``). Failures propagate to the caller;
    success means the temporary file and containing directory were fsynced.
    """
    p = sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f"{p.name}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        atomic_replace(tmp_path, p)
        try:
            parent_fd = os.open(str(p.parent), os.O_RDONLY)
        except OSError:
            parent_fd = None
        if parent_fd is not None:
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def _locked_update() -> Iterator[Dict[str, Any]]:
    """Serialise read-modify-write on the sidecar across processes.

    Holds an exclusive ``flock`` on a sibling ``.lock`` file for the duration
    of the update. Falls back to an in-process lock where ``fcntl`` is missing.
    """
    p = sidecar_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")

    if fcntl is None:  # pragma: no cover — non-POSIX fallback
        # Windows limitation: this protects threads in this process only;
        # concurrent Hermes processes can still race without a platform lock.
        with _write_lock:
            data = load_sidecar()
            yield data
            save_sidecar(data)
        return

    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            data = load_sidecar()
            yield data
            save_sidecar(data)
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _key(root: Path) -> str:
    """Canonical sidecar key for a project root (resolved absolute string)."""
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(Path(root))


# ---------------------------------------------------------------------------
# Read API (agent build time)
# ---------------------------------------------------------------------------


def get_project_entry(root: Path) -> Optional[Dict[str, Any]]:
    """The sidecar entry dict for *root*, or None when there is none."""
    entry = load_sidecar().get("projects", {}).get(_key(root))
    return entry if isinstance(entry, dict) else None


def is_trusted(root: Path) -> bool:
    """True when *root* has a ``status == "trusted"`` entry in the sidecar."""
    entry = get_project_entry(root)
    return bool(entry) and entry.get("status") == STATUS_TRUSTED


def is_denied(root: Path) -> bool:
    """True when *root* has a sticky ``status == "denied"`` entry."""
    entry = get_project_entry(root)
    return bool(entry) and entry.get("status") == STATUS_DENIED


def approved_fingerprints(root: Path) -> Dict[str, str]:
    """The ``{skill -> sha256}`` map recorded at last trust, or empty."""
    entry = get_project_entry(root)
    if not entry:
        return {}
    fps = entry.get("fingerprints")
    return dict(fps) if isinstance(fps, dict) else {}


def changed_or_new_skills(
    root: Path,
    current: Dict[str, str],
) -> List[str]:
    """Skill names present on disk (``current``) that are new OR hash-changed.

    Compares the freshly-computed ``{skill -> sha256}`` against the approved
    fingerprints. A skill is flagged when it was not approved at all, or when
    its approved hash differs from the current one. Removed skills (approved but
    no longer on disk) are NOT flagged here — they are pruned at the next trust
    run, never a notice trigger.
    """
    approved = approved_fingerprints(root)
    flagged: List[str] = []
    for name, digest in current.items():
        if approved.get(name) != digest:
            flagged.append(name)
    return sorted(flagged)


# ---------------------------------------------------------------------------
# Write API (CLI + migration)
# ---------------------------------------------------------------------------


def trust_project(root: Path, fingerprints: Dict[str, str]) -> Dict[str, Any]:
    """Record *root* as trusted with a fresh fingerprint snapshot.

    Re-running this re-fingerprints everything (the re-approval), and prunes any
    skills that no longer exist on disk simply by overwriting ``fingerprints``
    with ``fingerprints`` (which was computed from current disk state). Returns
    the stored entry.
    """
    key = _key(root)
    entry = {
        "status": STATUS_TRUSTED,
        "approved_at": _utc_now_iso(),
        "fingerprints": dict(fingerprints),
    }
    with _locked_update() as data:
        data.setdefault("projects", {})[key] = entry
    _clear_resolved_snapshot_cache()
    return entry


def deny_project(root: Path) -> Dict[str, Any]:
    """Record *root* as sticky-denied (no notice ever). Returns the entry."""
    key = _key(root)
    entry = {
        "status": STATUS_DENIED,
        "approved_at": _utc_now_iso(),
        "fingerprints": {},
    }
    with _locked_update() as data:
        data.setdefault("projects", {})[key] = entry
    _clear_resolved_snapshot_cache()
    return entry


def forget_project(root: Path) -> bool:
    """Remove *root*'s entry entirely (back to notice-eligible).

    Returns True when an entry was actually removed.
    """
    key = _key(root)
    removed = False
    with _locked_update() as data:
        projects = data.setdefault("projects", {})
        if key in projects:
            del projects[key]
            removed = True
    _clear_resolved_snapshot_cache()
    return removed


def _clear_resolved_snapshot_cache() -> None:
    """Invalidate build resolution after an explicit local trust mutation."""
    try:
        from agent.skill_utils import _resolve_project_skill_snapshot_cached

        _resolve_project_skill_snapshot_cached.cache_clear()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Legacy config migration
# ---------------------------------------------------------------------------


def legacy_config_trusts(root: Path) -> bool:
    """True when *root* is listed in the legacy ``skills.trusted_project_dirs``.

    Back-compat only. Reads config via the same light path the skills index
    uses (no ``hermes_cli.config`` import on the build path).
    """
    from agent.skill_utils import _project_trusted_dirs_from_config

    try:
        return Path(root).resolve() in _project_trusted_dirs_from_config()
    except OSError:
        return False


def migrate_legacy_if_needed(root: Path, skills_dirs: List[Path]) -> bool:
    """Auto-migrate a legacy config-trusted *root* into the sidecar.

    A bare ``skills.trusted_project_dirs`` entry has no fingerprints, so honoring
    it as-is would be trust WITHOUT the hash gate — exactly the unsafe state the
    sidecar exists to prevent. Instead, the first time the agent (or CLI) sees a
    legacy entry for a project that has no sidecar record yet, it fingerprints
    the current on-disk skills and writes a normal trusted sidecar entry. From
    then on the project behaves like any sidecar-trusted project (hash gated).

    Returns True when a migration was performed this call. Idempotent: once a
    sidecar entry (trusted OR denied) exists, the legacy list is ignored.
    """
    if not legacy_config_trusts(root):
        return False
    fingerprints = fingerprint_project_skills(skills_dirs)
    key = _key(root)
    entry = {
        "status": STATUS_TRUSTED,
        "approved_at": _utc_now_iso(),
        "fingerprints": dict(fingerprints),
    }
    try:
        with _locked_update() as data:
            if getattr(data, "load_state", "valid") == "corrupt":
                raise _MigrationSkipped
            if isinstance(data.get("projects", {}).get(key), dict):
                raise _MigrationSkipped
            # Config may have changed while package hashing was in progress.
            if not legacy_config_trusts(root):
                raise _MigrationSkipped
            data.setdefault("projects", {})[key] = entry
    except _MigrationSkipped:
        return False
    _clear_resolved_snapshot_cache()
    _remove_legacy_config_entry(root)
    logger.info(
        "Migrated legacy skills.trusted_project_dirs entry for %s into the "
        "project-trust sidecar (%d skill fingerprint(s) recorded).",
        root,
        len(fingerprints),
    )
    return True


class _MigrationSkipped(Exception):
    """Abort a locked migration without writing the loaded sidecar."""


def _remove_legacy_config_entry(root: Path) -> bool:
    """Remove *root* from legacy config after sidecar persistence succeeds."""
    from hermes_cli.config import load_config, save_config

    config = load_config()
    skills_cfg = config.get("skills")
    if not isinstance(skills_cfg, dict):
        return False
    trusted = skills_cfg.get("trusted_project_dirs") or []
    if not isinstance(trusted, list):
        trusted = [trusted]
    root_key = _key(root)
    kept = []
    for entry in trusted:
        try:
            if _key(Path(str(entry)).expanduser()) == root_key:
                continue
        except OSError:
            pass
        kept.append(entry)
    if len(kept) == len(trusted):
        return False
    if kept:
        skills_cfg["trusted_project_dirs"] = kept
    else:
        skills_cfg.pop("trusted_project_dirs", None)
    save_config(config)
    try:
        from agent.skill_utils import _raw_config_cache_clear

        _raw_config_cache_clear()
    except ImportError:
        pass
    return True
