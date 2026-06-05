"""Age- and count-based retention for the Hermes Canvas artifact directory.

Agents/crons drop `<id>.html` (+ optional `<id>.json` manifest) into
`~/.hermes/artifacts/` and nothing ever removes them, so the directory grows
unbounded (Phase 2 deferred item in the canvas design spec). `prune()` deletes
artifacts that fail the configured policies, removing the `.html` and its `.json`
sidecar together.

Policies (both optional; an artifact is deleted if it violates *either*):
  - max_age_days:    delete artifacts older than D days (by manifest created_at,
                     falling back to file mtime).
  - max_per_source:  within each `source`, keep only the newest N artifacts.

Protected from deletion regardless of policy:
  - any artifact whose manifest sets ``"pinned": true`` (e.g. the committed
    "Ops Overview" reference copy).

Fail-soft by design: a single unreadable/undeletable file is recorded in
``PruneResult.errors`` and never aborts the sweep — a cron must not crash the
scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .manifest import Manifest, parse_manifest
from .store import artifacts_dir


@dataclass
class _Entry:
    html: Path
    manifest: Manifest
    created: datetime


@dataclass
class PruneResult:
    deleted: list[str] = field(default_factory=list)
    kept: int = 0
    pinned_kept: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary_line(self) -> str:
        bits = [f"deleted={len(self.deleted)}", f"kept={self.kept}",
                f"pinned={self.pinned_kept}"]
        if self.errors:
            bits.append(f"errors={len(self.errors)}")
        if self.dry_run:
            bits.append("(dry-run)")
        return "artifact-retention: " + " ".join(bits)


def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _mtime_dt(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def prune(
    directory: Optional[Path] = None,
    *,
    max_age_days: Optional[int] = None,
    max_per_source: Optional[int] = None,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> PruneResult:
    """Prune artifacts under ``directory`` (defaults to the canvas artifacts dir).

    Returns a :class:`PruneResult`. With no policy set, nothing is deleted.
    """
    d = directory if directory is not None else artifacts_dir()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    result = PruneResult(dry_run=dry_run)

    if not d.exists():
        return result

    entries: list[_Entry] = []
    for html in sorted(d.glob("*.html")):
        try:
            manifest = parse_manifest(html.with_suffix(".json"), html_path=html)
            created = _parse_dt(manifest.created_at) or _mtime_dt(html)
            entries.append(_Entry(html=html, manifest=manifest, created=created))
        except Exception as exc:  # never let one bad file abort the sweep
            result.errors.append(f"{html.name}: read failed: {exc}")

    # Identify count-stale ids: per source, anything beyond the newest N.
    count_stale: set[str] = set()
    if max_per_source is not None and max_per_source >= 0:
        by_source: dict[str, list[_Entry]] = {}
        for e in entries:
            by_source.setdefault(e.manifest.source, []).append(e)
        for group in by_source.values():
            # newest first; stable tie-break on id for determinism
            group.sort(key=lambda e: (e.created, e.manifest.id), reverse=True)
            for e in group[max_per_source:]:
                count_stale.add(e.manifest.id)

    age_cutoff = (now - timedelta(days=max_age_days)) if max_age_days is not None else None

    for e in entries:
        if e.manifest.pinned:
            result.pinned_kept += 1
            result.kept += 1
            continue
        age_stale = age_cutoff is not None and e.created < age_cutoff
        if age_stale or e.manifest.id in count_stale:
            if _delete(e, dry_run, result):
                result.deleted.append(e.manifest.id)
            else:
                result.kept += 1
        else:
            result.kept += 1

    result.deleted.sort()
    return result


def _delete(entry: _Entry, dry_run: bool, result: PruneResult) -> bool:
    """Remove the artifact's html + sidecar. Returns True if it counts as deleted.

    In dry-run mode nothing is unlinked but the artifact still counts as deleted
    (so callers can preview). Fail-soft: unlink errors are recorded, not raised.
    """
    if dry_run:
        return True
    ok = True
    sidecar = entry.html.with_suffix(".json")
    for path in (entry.html, sidecar):
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            result.errors.append(f"{path.name}: delete failed: {exc}")
            ok = False
    return ok
