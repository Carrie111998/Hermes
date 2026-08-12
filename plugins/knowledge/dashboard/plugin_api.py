"""
Knowledge — interactive knowledge graph over a markdown wiki.

Scans ``$HERMES_HOME/knowledge/**/*.md`` (created on first use) and builds a
graph of the pages' ``[[wikilink]]`` relationships so the desktop UI can
render a force-directed knowledge graph, browse pages, search, and follow
links/backlinks.

Frontmatter fields (YAML) are surfaced as node metadata:

* ``title`` — display title (falls back to the file name)
* ``type`` — node type (guide / project / reference / concept / note / …)
  used for node colours
* ``tags``, ``summary``, ``domain``, ``status``, ``created``, ``updated``

Adapted from the knowledge browser of the community `JPeetz/Hermes-Studio`
web UI. Route prefix (mounted by the dashboard): ``/api/plugins/knowledge``.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

try:
    import yaml
except Exception:  # pragma: no cover - pyyaml is a core dependency
    yaml = None

log = __import__("logging").getLogger(__name__)

router = APIRouter()

# ─── Locations ────────────────────────────────────────────────────────────────

def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _knowledge_root() -> Path:
    root = Path(os.environ.get("HERMES_KNOWLEDGE_DIR") or (_hermes_home() / "knowledge"))
    root.mkdir(parents=True, exist_ok=True)
    return root


SKIP_DIRS = {".git", "node_modules", ".obsidian", ".trash"}


def _seed_readme_if_empty(root: Path) -> None:
    """Drop a small README so a fresh install never shows an empty graph and
    the wiki-link format is discoverable."""
    readme = root / "README.md"
    if readme.exists():
        return
    try:
        readme.write_text(
            "# Knowledge\n"
            "\n"
            "Welcome to your Hermes knowledge wiki. Every `.md` file in this\n"
            "folder (and subfolders) becomes a node in the **Knowledge Graph**.\n"
            "\n"
            "## Linking pages\n"
            "\n"
            "Use wiki-links to connect pages — the graph is built from them:\n"
            "\n"
            "```\n"
            "See [[concepts/agents]] for details.\n"
            "```\n"
            "\n"
            "## Frontmatter\n"
            "\n"
            "Optional YAML frontmatter gives nodes metadata:\n"
            "\n"
            "```\n"
            "---\n"
            "title: Agents\n"
            "type: concept\n"
            "tags: [agent, orchestration]\n"
            "summary: How Hermes agents work.\n"
            "---\n"
            "```\n"
            "\n"
            "Type is used for node colours (guide / project / reference /\n"
            "concept / note / …).\n",
            "utf-8",
        )
    except Exception:
        pass


# ─── Parsing ─────────────────────────────────────────────────────────────────

def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    match = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$", raw)
    if not match:
        return {}, raw
    data: dict[str, Any] = {}
    if yaml is not None:
        try:
            parsed = yaml.safe_load(match.group(1))
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            data = {}
    return data, match.group(2) or ""


def _clean_wikilink_target(input_: str) -> str:
    return (input_.split("|")[0].split("#")[0] or "").strip()


def _extract_wikilinks(content: str) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\[\[([^\]]+)\]\]", content):
        target = _clean_wikilink_target(match.group(1) or "")
        if target and target not in seen:
            seen.add(target)
            links.append(target)
    return links


def _normalize_tag_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _normalize_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _page_meta(relative_path: str, stat) -> dict[str, Any]:
    raw = _read_text_safe(_knowledge_root() / relative_path)
    data, content = _parse_frontmatter(raw)
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime))
    name = Path(relative_path).name
    title = _normalize_str(data.get("title")) or Path(relative_path).stem
    updated = _normalize_str(data.get("updated")) or modified
    return {
        "path": relative_path,
        "name": name,
        "title": title,
        "type": _normalize_str(data.get("type")),
        "domain": _normalize_str(data.get("domain")),
        "status": _normalize_str(data.get("status")),
        "tags": _normalize_tag_list(data.get("tags")),
        "summary": _normalize_str(data.get("summary")),
        "created": _normalize_str(data.get("created")),
        "updated": updated,
        "size": stat.st_size,
        "modified": modified,
        "wikilinks": _extract_wikilinks(content),
    }


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_markdown(root: Path) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    if not root.exists():
        return pages
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except Exception:
                continue
            if not stat.st_mode or not full.is_file():
                continue
            rel = full.relative_to(root).as_posix()
            pages.append(_page_meta(rel, stat))
    pages.sort(key=lambda p: (p.get("updated") or p["modified"], p["path"]), reverse=True)
    return pages


def _wikilink_resolver(pages: list[dict[str, Any]]) -> dict[str, str]:
    by_path: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for page in pages:
        by_path[page["path"][:-3].lower() if page["path"].lower().endswith(".md") else page["path"].lower()] = page["path"]
        by_name[Path(page["path"]).stem.lower()] = page["path"]
    return {**by_path, **by_name}


def _resolve_link(resolver: dict[str, str], link_text: str) -> Optional[str]:
    cleaned = _clean_wikilink_target(link_text)
    if not cleaned:
        return None
    normalized = cleaned.replace("\\", "/").strip()
    if normalized.lower().endswith(".md"):
        normalized = normalized[:-3]
    return resolver.get(normalized.lower())


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/graph")
def graph() -> dict[str, Any]:
    root = _knowledge_root()
    _seed_readme_if_empty(root)
    pages = _walk_markdown(root)
    resolver = _wikilink_resolver(pages)

    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for page in pages:
        for link in page["wikilinks"]:
            target = _resolve_link(resolver, link)
            if not target:
                continue
            key = (page["path"], target)
            if key not in seen:
                seen.add(key)
                edges.append({"source": page["path"], "target": target})

    return {
        "ok": True,
        "root": str(root),
        "nodes": [
            {"id": p["path"], "title": p["title"], "type": p["type"], "tags": p["tags"]}
            for p in pages
        ],
        "edges": edges,
    }


@router.get("/list")
def list_pages() -> dict[str, Any]:
    _seed_readme_if_empty(_knowledge_root())
    return {"ok": True, "pages": _walk_markdown(_knowledge_root())}


@router.get("/read")
def read_page(path: str = Query(..., description="Relative path under the knowledge root")) -> dict[str, Any]:
    root = _knowledge_root()
    normalized = path.replace("\\", "/").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Path is required")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    if not normalized.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="Only Markdown files are allowed")

    full = (root / normalized).resolve()
    if not str(full).startswith(str(root.resolve())):
        raise HTTPException(status_code=400, detail="Resolved path is outside knowledge root")
    if not full.is_file():
        raise HTTPException(status_code=404, detail="Knowledge page not found")

    stat = full.stat()
    meta = _page_meta(normalized, stat)
    raw = _read_text_safe(full)
    _, content = _parse_frontmatter(raw)

    pages = _walk_markdown(root)
    resolver = _wikilink_resolver(pages)
    backlinks = [
        p["path"]
        for p in pages
        if p["path"] != normalized
        and any(_resolve_link(resolver, link) == normalized for link in p["wikilinks"])
    ]

    return {"ok": True, "meta": meta, "content": content, "backlinks": backlinks}


@router.get("/search")
def search_pages(q: str = Query(..., min_length=1, max_length=200)) -> dict[str, Any]:
    needle = q.strip().lower()
    if not needle:
        return {"ok": True, "matches": []}
    pages = _walk_markdown(_knowledge_root())
    matches: list[dict[str, Any]] = []
    for page in pages:
        raw = _read_text_safe(_knowledge_root() / page["path"])
        for idx, line in enumerate(raw.splitlines(), start=1):
            if needle in line.lower():
                matches.append({"path": page["path"], "title": page["title"], "line": idx, "text": line})
                if len(matches) >= 200:
                    return {"ok": True, "matches": matches}
    return {"ok": True, "matches": matches}
