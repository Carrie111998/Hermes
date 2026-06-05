"""Read-only access to the artifact directory (~/.hermes/artifacts/).

Agents drop `<id>.html` (+ optional `<id>.json` manifest); this module lists and
serves them. Artifact ids are sanitized to a safe charset so a request can never
escape the artifacts dir.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from hermes_constants import get_default_hermes_root

from .manifest import Manifest, parse_manifest

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def artifacts_dir() -> Path:
    return get_default_hermes_root() / "artifacts"


def _is_safe_id(artifact_id: str) -> bool:
    return bool(artifact_id) and artifact_id not in {".", ".."} and bool(_SAFE_ID.match(artifact_id))


def list_artifacts() -> list[Manifest]:
    d = artifacts_dir()
    if not d.exists():
        return []
    out: list[Manifest] = []
    for html in d.glob("*.html"):
        out.append(parse_manifest(html.with_suffix(".json"), html_path=html))
    # Newest-first, then a stable sort floats pinned artifacts to the top while
    # preserving newest-first order within each group (Python sort is stable).
    out.sort(key=lambda m: m.created_at or "", reverse=True)
    out.sort(key=lambda m: not m.pinned)
    return out


def get_artifact_html(artifact_id: str) -> Optional[str]:
    if not _is_safe_id(artifact_id):
        return None
    p = artifacts_dir() / f"{artifact_id}.html"
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None
