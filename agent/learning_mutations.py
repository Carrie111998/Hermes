"""User-initiated edit/delete for journey nodes (learned skills + memories).

The journey graph (``agent.learning_graph``) gives every node a stable id:

- **skills** → the skill name (e.g. ``"debugging-hermes-desktop"``)
- **memories** → ``memory:<source>:<index>`` where ``source`` is ``memory``
  (``MEMORY.md``) or ``profile`` (``USER.md``) and ``index`` is the node's
  position in the combined card list (``MEMORY.md`` cards first, then
  ``USER.md``).

This module maps a node id back to its on-disk home and performs the mutation,
shared by the CLI (``hermes journey delete|edit``), the TUI ``/journey`` overlay
(gateway RPCs), and the desktop GUI (REST). Deleting a skill *archives* it
(recoverable via ``hermes curator restore``); deleting a memory rewrites its
file. Pure stdlib + existing skill/memory helpers.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_MEMORY_FILES = {"memory": "MEMORY.md", "profile": "USER.md"}
_WIKI_EXCLUSION_LOCK_TIMEOUT_SECONDS = 5.0
_WIKI_EXCLUSION_THREAD_LOCK = threading.Lock()


def parse_node_kind(node_id: str) -> str:
    if node_id.startswith("memory:"):
        return "memory"
    return "wiki" if node_id.startswith("wiki:") else "skill"


def _wiki_path(node_id: str) -> tuple[Path, str]:
    if not node_id.startswith("wiki:"):
        raise ValueError(f"bad wiki node id: {node_id!r}")
    relative = node_id[5:]
    if not relative:
        raise ValueError(f"bad wiki node id: {node_id!r}")
    from agent.learning_graph import _wiki_root

    root = _wiki_root().resolve()
    path = (root / relative).resolve()
    try:
        normalized = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"bad wiki node id: {node_id!r}") from exc
    if normalized != relative or path.suffix.lower() != ".md":
        raise ValueError(f"bad wiki node id: {node_id!r}")
    return path, normalized


def _memories_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "memories"


def _parse_memory_id(node_id: str) -> tuple[str, int]:
    """``memory:<source>:<index>`` → (source, global_index)."""
    parts = node_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "memory" or parts[1] not in _MEMORY_FILES:
        raise ValueError(f"bad memory node id: {node_id!r}")
    try:
        return parts[1], int(parts[2])
    except ValueError as exc:
        raise ValueError(f"bad memory node id: {node_id!r}") from exc


def _memory_local_index(source: str, global_index: int) -> int:
    """Global card index → position within the source's own file.

    ``_memory_cards`` emits all ``MEMORY.md`` cards before ``USER.md`` cards, so
    a profile card's local index is its global index minus the memory count.
    """
    from agent.learning_graph import _memory_cards

    cards = _memory_cards()
    if not 0 <= global_index < len(cards):
        raise IndexError(f"memory index {global_index} out of range")
    if cards[global_index].get("source") != source:
        raise ValueError("memory node id is stale — refresh the graph")
    if source == "memory":
        return global_index
    return global_index - sum(1 for c in cards if c.get("source") == "memory")


def _locate_memory(source: str, gidx: int) -> tuple[Path, list[str], int]:
    """Resolve a memory card to its file, all §-delimited entries, and local index.

    Entries come from ``MemoryStore._read_file`` — the same parser the memory
    tool uses — so journey indices stay aligned with what the graph renders.
    """
    from tools.memory_tool import MemoryStore

    path = _memories_dir() / _MEMORY_FILES[source]
    if not path.exists():
        raise ValueError(f"{path.name} not found")
    chunks = MemoryStore._read_file(path)
    local = _memory_local_index(source, gidx)
    if not 0 <= local < len(chunks):
        raise ValueError("memory node id is stale — refresh the graph")
    return path, chunks, local


# ── Inspect (edit prefill) ──────────────────────────────────────────────────


def node_detail(node_id: str) -> dict[str, Any]:
    """Current content for an edit prefill. ``content`` is the full SKILL.md
    (skills) or the raw memory chunk (memories)."""
    try:
        return _node_detail(node_id)
    except (ValueError, IndexError) as exc:
        return {"ok": False, "message": str(exc)}


def _node_detail(node_id: str) -> dict[str, Any]:
    kind = parse_node_kind(node_id)
    if kind == "memory":
        source, gidx = _parse_memory_id(node_id)
        _, chunks, local = _locate_memory(source, gidx)
        body = chunks[local].strip()

        return {"ok": True, "kind": "memory", "id": node_id, "label": body.splitlines()[0][:80], "content": body}

    if kind == "wiki":
        path, _ = _wiki_path(node_id)
        if not path.is_file():
            return {"ok": False, "message": f"wiki page '{node_id[5:]}' not found"}
        content = path.read_text(encoding="utf-8")
        from agent.learning_graph import _frontmatter

        label = str(_frontmatter(content[:4000]).get("title") or path.stem)
        return {"ok": True, "kind": "wiki", "id": node_id, "label": label, "content": content}

    from tools.skill_manager_tool import _find_skill

    found = _find_skill(node_id)
    if not found:
        return {"ok": False, "message": f"skill '{node_id}' not found"}
    skill_md = Path(found["path"]) / "SKILL.md"
    if not skill_md.exists():
        return {"ok": False, "message": f"SKILL.md missing for '{node_id}'"}

    return {
        "ok": True,
        "kind": "skill",
        "id": node_id,
        "label": node_id,
        "content": skill_md.read_text(encoding="utf-8"),
    }


# ── Delete ──────────────────────────────────────────────────────────────────


def delete_node(node_id: str) -> dict[str, Any]:
    try:
        kind = parse_node_kind(node_id)
        if kind == "memory":
            return _delete_memory(node_id)
        if kind == "wiki":
            return _exclude_wiki(node_id)
        return _delete_skill(node_id)
    except (ValueError, IndexError) as exc:
        return {"ok": False, "message": str(exc)}


def _delete_skill(name: str) -> dict[str, Any]:
    from tools import skill_usage

    if skill_usage.get_record(name).get("pinned"):
        return {"ok": False, "message": f"'{name}' is pinned — unpin it first (hermes curator unpin {name})"}

    ok, message = skill_usage.archive_skill(name)
    if ok:
        _clear_skill_cache()

    return {"ok": ok, "message": f"archived '{name}' — restore with: hermes curator restore {name}" if ok else message}


def _delete_memory(node_id: str) -> dict[str, Any]:
    source, gidx = _parse_memory_id(node_id)
    path, chunks, local = _locate_memory(source, gidx)

    del chunks[local]
    _write_memory(path, chunks)

    return {"ok": True, "message": f"deleted memory from {path.name}"}


@contextlib.contextmanager
def _wiki_exclusion_lock(index: Path):
    """Serialize the wiki exclusion index across threads and processes."""
    lock_path = index.with_name(index.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _WIKI_EXCLUSION_THREAD_LOCK:
        handle = lock_path.open("a+b")
        acquired = False
        try:
            if lock_path.stat().st_size == 0:
                handle.write(b" ")
                handle.flush()
            deadline = time.monotonic() + _WIKI_EXCLUSION_LOCK_TIMEOUT_SECONDS
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
            else:
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except (BlockingIOError, OSError):
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
            if not acquired:
                raise TimeoutError("wiki exclusion index is busy — try again")
            yield
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _exclude_wiki(node_id: str) -> dict[str, Any]:
    import json

    from agent.learning_graph import _wiki_exclusion_roots, _wiki_root
    from hermes_constants import get_hermes_home

    path, relative = _wiki_path(node_id)
    if not path.is_file():
        return {"ok": False, "message": f"wiki page '{relative}' not found"}
    index = get_hermes_home() / "journey" / "wiki-excluded.json"
    try:
        with _wiki_exclusion_lock(index):
            roots = _wiki_exclusion_roots()
            root = str(_wiki_root().resolve())
            roots.setdefault(root, set()).add(relative)
            payload = {"roots": {key: sorted(paths) for key, paths in sorted(roots.items())}}
            fd, temp_name = tempfile.mkstemp(prefix=f".{index.name}.", suffix=".tmp", dir=index.parent)
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, indent=2) + "\n")
                os.replace(temp, index)
            finally:
                temp.unlink(missing_ok=True)
    except (OSError, TimeoutError) as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": f"removed wiki page '{relative}' from journey (file kept)"}


# ── Edit ────────────────────────────────────────────────────────────────────


def edit_node(node_id: str, content: str) -> dict[str, Any]:
    try:
        kind = parse_node_kind(node_id)
        if kind == "memory":
            return _edit_memory(node_id, content)
        if kind == "wiki":
            return _edit_wiki(node_id, content)
        return _edit_skill(node_id, content)
    except (OSError, ValueError, IndexError) as exc:
        return {"ok": False, "message": str(exc)}


def _edit_skill(name: str, content: str) -> dict[str, Any]:
    from tools.skill_manager_tool import _edit_skill as _do_edit

    result = _do_edit(name, content)
    if result.get("success"):
        _clear_skill_cache()

        return {"ok": True, "message": f"updated '{name}'"}

    return {"ok": False, "message": result.get("error", "edit failed")}


def _edit_memory(node_id: str, content: str) -> dict[str, Any]:
    source, gidx = _parse_memory_id(node_id)
    body = content.strip()
    if not body:
        return {"ok": False, "message": "empty memory — use delete to remove it"}
    path, chunks, local = _locate_memory(source, gidx)

    chunks[local] = body
    _write_memory(path, chunks)

    return {"ok": True, "message": f"updated memory in {path.name}"}


def _edit_wiki(node_id: str, content: str) -> dict[str, Any]:
    path, relative = _wiki_path(node_id)
    if not path.is_file():
        return {"ok": False, "message": f"wiki page '{relative}' not found"}
    if not content.strip():
        return {"ok": False, "message": "empty wiki page — edit the source file instead"}
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return {"ok": True, "message": f"updated wiki page '{relative}'"}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_memory(path: Path, chunks: list[str]) -> None:
    """Atomic temp-file + rename via the memory tool, so a concurrent reader
    never sees a half-written file (and the §-join stays single-sourced)."""
    from tools.memory_tool import MemoryStore

    MemoryStore._write_file(path, [c.strip() for c in chunks if c.strip()])


def _clear_skill_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
