"""Canonical tracker-pipeline business-state reader for the reaper's gate B.

The convergence-reaper (IntentApplier.reap_converged_partials) confirms a capped
partial is truly converged with TWO independent snapshots: native Postgres
(gate A) AND the tracker's own canonical pipeline.json (gate B). This module
supplies gate B: a job_id -> currentBusinessState map read fresh from
profiles/tracker/workspace/pipeline.json.

IMPORTANT: the canonical file's `.stage` field is LEGACY-space (e.g. "review").
The business-state value that lines up with _STAGE_SATISFIED_BY (valued in
business_states like "materials_ready") is `.currentBusinessState`. Reading
`.stage` here would fail-closed on real convergence. Verified against the live
41MB file 2026-07-20.

The canonical file keys `jobs` as a DICT by job_id (== jobs.external_job_key ==
intent job_id) -- a different shape from the PipelineManager legacy projection
(jobs = list), which this reader deliberately ignores (returns {}).

Fail-soft: any error (missing file, bad JSON, unexpected shape) yields an empty
map, so gate B simply can't confirm convergence and the reaper leaves the
partial capped -- never a wrong reap.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def load_canonical_business_states(path: Path) -> dict[str, str]:
    """Return {job_id: currentBusinessState} from a tracker canonical pipeline.json.

    Jobs with no non-empty currentBusinessState are omitted. jobs must be a dict
    (the canonical shape); a list-shaped legacy projection yields {}. Any failure
    yields {} (fail-soft).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            return {}
        out: dict[str, str] = {}
        for job_id, rec in jobs.items():
            if not isinstance(rec, dict):
                continue
            cbs = rec.get("currentBusinessState")
            if isinstance(cbs, str) and cbs:
                out[job_id] = cbs
        return out
    except Exception:
        logger.debug(
            "canonical-reader: read failed for %s (fail-soft)", path, exc_info=True
        )
        return {}


def _default_canonical_path() -> Path:
    root = Path(os.environ.get("HERMES_ROOT", str(Path.home() / ".hermes")))
    return root / "profiles" / "tracker" / "workspace" / "pipeline.json"


def build_default_canonical_reader(
    path: Optional[Path] = None,
) -> Callable[[], dict[str, str]]:
    """A zero-arg callable returning a fresh {job_id: currentBusinessState} map.

    The reaper invokes it AT MOST once per sweep. Bound to the tracker canonical
    pipeline.json under HERMES_ROOT unless an explicit path is given.
    """
    target = path or _default_canonical_path()
    return lambda: load_canonical_business_states(target)
