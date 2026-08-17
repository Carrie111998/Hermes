"""Fetch full job data by job_id (Phase C iter4).

The Critic's `reflexion_replay_node` needs the full job description to re-invoke
the matcher with a modified prompt. The bus events store only summaries; we
have to reconstitute Scout-shape job dicts from one of three persistent stores.

Resolution priority (newest data first):
  1. ~/.hermes/mailbox/matcher-shadow/outbox/*SCORE_RESULT*.json
     payload.job_data is the full Scout shape (set by matcher_shadow_run.py).
  2. ~/.hermes/mailbox/matcher/outbox/*SCORE_RESULT*.json
     Same shape, written by the production Matcher cron.
  3. ~/.hermes/mailbox/tracker/processed/*SCOUT_DISCOVERY*.json
     payload.jobs[] holds the original Scout-discovered records.
  4. Langfuse hermes-jobs-v1 dataset (if the id is `linkedin-<num>`).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

HERMES = Path.home() / ".hermes"
MATCHER_SHADOW_OUTBOX = HERMES / "mailbox" / "matcher-shadow" / "outbox"
MATCHER_OUTBOX = HERMES / "mailbox" / "matcher" / "outbox"
TRACKER_PROCESSED = HERMES / "mailbox" / "tracker" / "processed"


def _scan_score_outboxes(job_id: str) -> Optional[dict]:
    """Look for SCORE_RESULT messages whose payload.job_id matches and pull
    payload.job_data.
    """
    for outbox in (MATCHER_SHADOW_OUTBOX, MATCHER_OUTBOX):
        if not outbox.exists():
            continue
        # Newest first so we get freshest job data
        for path in sorted(
            outbox.glob("*SCORE_RESULT*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            try:
                msg = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            payload = msg.get("payload") or {}
            if str(payload.get("job_id") or "") != str(job_id):
                # Some files have truncated UUIDs in display but full in payload;
                # also accept partial matches as a fallback.
                if not (job_id and str(payload.get("job_id") or "").startswith(str(job_id).rstrip("-"))):
                    continue
            jd = payload.get("job_data")
            if isinstance(jd, dict) and (jd.get("description_raw") or jd.get("description")):
                return jd
    return None


def _scan_scout_discoveries(job_id: str) -> Optional[dict]:
    """Walk SCOUT_DISCOVERY messages. The jobs[] array may have many; match by id."""
    if not TRACKER_PROCESSED.exists():
        return None
    for path in sorted(
        TRACKER_PROCESSED.glob("*SCOUT_DISCOVERY*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        try:
            msg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for job in (msg.get("payload") or {}).get("jobs") or []:
            jid = str(job.get("id") or "")
            if jid == str(job_id) or (job_id and jid.startswith(str(job_id).rstrip("-"))):
                if job.get("description_raw") or job.get("description"):
                    return job
    return None


def _from_langfuse_dataset(job_id: str) -> Optional[dict]:
    """Pull from the hermes-jobs-v1 dataset for `linkedin-<num>` ids."""
    if not job_id.startswith("linkedin-"):
        return None
    try:
        from langfuse import Langfuse  # type: ignore

        c = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "http://localhost:3050"),
        )
        ds = c.get_dataset("hermes-jobs-v1")
        for it in ds.items:
            if it.id == job_id:
                inp = it.input or {}
                if not isinstance(inp, dict):
                    return None
                meta = it.metadata or {}
                return {
                    "id": job_id,
                    "title": inp.get("title", ""),
                    "company": inp.get("company", ""),
                    "location": inp.get("location", ""),
                    "description": inp.get("description", ""),
                    "description_raw": inp.get("description", ""),
                    "url": (meta or {}).get("source_url", ""),
                    "source_board": "linkedin",
                    "salary_range": None,
                    "seniority_level": "",
                }
    except Exception:
        return None
    return None


@lru_cache(maxsize=64)
def get_job_data(job_id: str) -> Optional[dict]:
    """Best-effort lookup of a Scout-shape job dict by id. Cached per process.

    Returns dict with at least: title, company, location, description (or
    description_raw), or None when nothing found.
    """
    if not job_id:
        return None
    # Strip trailing dash artifacts produced by truncation in retro markdown
    job_id_clean = job_id.rstrip("-")
    for resolver in (_scan_score_outboxes, _scan_scout_discoveries, _from_langfuse_dataset):
        try:
            jd = resolver(job_id_clean)
            if jd:
                return jd
        except Exception:
            continue
    return None
