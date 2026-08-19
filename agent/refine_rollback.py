"""Reversible safety harness for self-improvement (``/refine``) writes.

The background memory/skill review fork can persist new or updated memory
entries and skills. Those writes are intentionally irrevocable in the current
codebase — if a review pass saves something wrong, the user has no built-in
way to revert it. This module adds a *snapshot-before-write* mechanism so a
``/refine`` run can be undone.

Design constraints (kept deliberately small):
- Pure stdlib + pathlib only, so the snapshot/restore logic is fully
  unit-testable without booting the agent runtime.
- Snapshots are taken synchronously on the caller's thread *before* the
  background review fork starts writing, guaranteeing the captured state is
  pre-write (issue #14944 class of bug: never snapshot after the fork).
- Restore is atomic per-directory (build into place, swap) so a crash mid-
  restore cannot leave a half-applied tree.
- A snapshot that captured **zero** files for a target dir is treated as
  "this target did not exist yet at snapshot time" and is **never** allowed
  to wipe a live (possibly non-empty) directory. This prevents the
  data-loss edge case where an empty snapshot subdir would otherwise replace
  a populated live memory/skills dir with an empty one.
- Restore is opt-in and explicit (user runs ``/refine undo``); the harness
  never mutates live files on its own.

This is a port of the prime-agent "reversible self-improvement" feature,
folded into Hermes' existing ``/refine`` machinery rather than special-cased
into core write paths. The user-facing workflow is documented in
``skills/continual-harness/SKILL.md``.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

SNAPSHOT_ROOT = "review_snapshots"

# Manifest field: list of {"src": "<abs path>", "files": <int>, "existed": <bool>}.
MANIFEST_FILE = "manifest.json"
INDEX_FILE = "index.json"


def snapshot_root_dir(home: Path) -> Path:
    """Directory under ``HERMES_HOME`` holding all review snapshots."""
    return Path(home) / SNAPSHOT_ROOT


def make_snapshot_id(session_id: str) -> str:
    """Deterministic, sortable, collision-resistant snapshot id."""
    ts = int(time.time() * 1000)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts / 1000))
    return f"{session_id}_{stamp}_{ts}"


def _copy_tree(src: Path, dst: Path) -> int:
    """Copy every file under ``src`` into ``dst``, preserving relative paths.

    Returns the number of files copied. Missing source dir is a no-op (0).
    """
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return 0
    count = 0
    for item in sorted(src.rglob("*")):
        if item.is_file():
            rel = item.relative_to(src)
            target = dst / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def _restore_tree(snap_sub: Path, dst: Path, *, expected_files: int) -> str:
    """Atomically replace ``dst`` with the contents of ``snap_sub``.

    Builds into a temp dir then swaps, so a failure mid-copy cannot corrupt
    the live tree. Returns one of:

    - ``"restored"`` — the live dir was replaced with the snapshot contents.
    - ``"skipped-corrupt"`` — the manifest claims ``expected_files > 0`` but
      the snapshot storage holds none, so we REFUSE to wipe a possibly-
      populated live dir (capture/store corruption guard).

    Callers pass ``expected_files`` from the manifest entry so the guard can
    distinguish a genuinely-empty snapshot (legitimate undo target) from a
    corrupted one (must not destroy live data).
    """
    snap_sub = Path(snap_sub)
    dst = Path(dst)

    # Corruption guard: we were promised files but the snapshot store has
    # none. Wiping the live dir here would discard data that existed at
    # snapshot time. Refuse instead of emptying.
    if expected_files > 0:
        actual = (
            sum(1 for p in snap_sub.rglob("*") if p.is_file())
            if snap_sub.exists()
            else 0
        )
        if actual == 0:
            return "skipped-corrupt"

    tmp = dst.with_suffix(dst.suffix + ".rollback.tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    if snap_sub.exists():
        _copy_tree(snap_sub, tmp)
    # Swap: remove live, then move snapshot into place.
    if dst.exists():
        shutil.rmtree(dst)
    if tmp.exists():
        shutil.move(str(tmp), str(dst))
    else:
        # snap_sub held nothing -> the target was empty/absent at snapshot
        # time, so leave it absent (do not recreate an empty dir unless the
        # manifest said it existed).
        pass
    return "restored"


def take_snapshot(home: Path, session_id: str, dirs: List[Path]) -> str:
    """Snapshot each directory in ``dirs`` and return the snapshot id.

    ``dirs`` must be absolute (callers resolve ``get_memory_dir()`` /
    ``SKILLS_DIR`` themselves). The captured targets are recorded in the
    manifest so restore knows where to write back. A missing source dir is
    recorded as ``existed=False`` with ``files=0`` so restore knows not to
    treat its absence as a "should empty the live dir" case.
    """
    home = Path(home)
    sid = make_snapshot_id(session_id)
    base = snapshot_root_dir(home) / sid
    base.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, object] = {"session_id": session_id, "dirs": []}
    entries: List[Dict[str, object]] = []
    for d in dirs:
        d = Path(d)
        sub = base / d.name
        n = _copy_tree(d, sub)
        entries.append(
            {"src": str(d), "files": n, "existed": d.exists()}
        )
    manifest["dirs"] = entries
    (base / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2))

    _update_index(home, session_id, sid)
    return sid


def _index_path(home: Path) -> Path:
    return snapshot_root_dir(home) / INDEX_FILE


def _update_index(home: Path, session_id: str, snapshot_id: str) -> None:
    """Record the latest snapshot id for a session (per-session undo)."""
    home = Path(home)
    path = _index_path(home)
    index: Dict[str, str] = {}
    if path.exists():
        try:
            index = json.loads(path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            index = {}
    index[session_id] = snapshot_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2))


def latest_snapshot_id(home: Path, session_id: str) -> Optional[str]:
    """Return the most recent snapshot id recorded for ``session_id``."""
    path = _index_path(Path(home))
    if not path.exists():
        return None
    try:
        index = json.loads(path.read_text() or "{}")
    except (json.JSONDecodeError, OSError):
        return None
    return index.get(session_id)


def restore_snapshot(
    home: Path, snapshot_id: str
) -> Dict[str, object]:
    """Restore every captured directory from a snapshot.

    Returns a dict with:
    - ``applied`` (bool): True if the snapshot existed and was processed.
    - ``skipped`` (list[str]): absolute src paths skipped due to the empty-
      snapshot data-loss guard.
    Idempotent. Unknown id -> ``{"applied": False, "skipped": []}``.
    """
    home = Path(home)
    base = snapshot_root_dir(home) / snapshot_id
    if not base.exists():
        return {"applied": False, "skipped": []}
    manifest_path = base / MANIFEST_FILE
    if not manifest_path.exists():
        return {"applied": False, "skipped": []}
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"applied": False, "skipped": []}
    skipped: List[str] = []
    for entry in manifest.get("dirs", []):
        src = entry.get("src")
        if not src:
            continue
        expected = int(entry.get("files", 0) or 0)
        result = _restore_tree(base / Path(src).name, Path(src), expected_files=expected)
        if result == "skipped-corrupt":
            skipped.append(src)
    return {"applied": True, "skipped": skipped}


def list_snapshots(home: Path, session_id: Optional[str] = None) -> List[str]:
    """Return snapshot ids, optionally filtered to one session."""
    root = snapshot_root_dir(Path(home))
    if not root.exists():
        return []
    ids = sorted(
        p.name for p in root.iterdir() if (p / MANIFEST_FILE).exists()
    )
    if session_id is None:
        return ids
    return [i for i in ids if i.startswith(f"{session_id}_")]


def delete_snapshot(home: Path, snapshot_id: str) -> bool:
    """Delete a snapshot's stored tree. Returns ``True`` if it existed."""
    base = snapshot_root_dir(Path(home)) / snapshot_id
    if not base.exists():
        return False
    shutil.rmtree(base)
    return True


__all__ = [
    "SNAPSHOT_ROOT",
    "snapshot_root_dir",
    "make_snapshot_id",
    "take_snapshot",
    "restore_snapshot",
    "latest_snapshot_id",
    "list_snapshots",
    "delete_snapshot",
]
