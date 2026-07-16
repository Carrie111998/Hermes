"""Ingestion orchestration for the jobflow_inbox plugin."""

from __future__ import annotations

import json
import logging
import pathlib

from . import extract

logger = logging.getLogger(__name__)

_URL_FIELDS = ("url", "apply_url", "canonical_ats_url", "ats_url")


def is_duplicate(normalized_url: str, pipeline_path) -> bool:
    try:
        data = json.loads(pathlib.Path(pipeline_path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — dedup is best-effort
        logger.debug("jobflow_inbox: pipeline read failed, skipping dedup: %s", exc)
        return False
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for field in _URL_FIELDS:
            val = job.get(field)
            if isinstance(val, str) and val:
                try:
                    if extract.normalize_url(val) == normalized_url:
                        return True
                except Exception:  # noqa: BLE001
                    continue
    return False
