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


import hashlib
import os


def build_message(job_fields, *, url, normalized_url, cid, message_id, ts_iso) -> dict:
    key = hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()
    return {
        "message_id": message_id,
        "protocol_version": "2.0",
        "idempotency_key": f"user_submitted:{key}",
        "attempt": 1,
        "max_attempts": 3,
        "lease_timeout_seconds": 300,
        "reply_expected": False,
        "intent_only": False,
        "type": "USER_SUBMITTED_JOB",
        "from": "jobflow_inbox",
        "to": "tracker",
        "job_id": key[:16],
        "timestamp": ts_iso,
        "correlation_id": cid,
        "payload": {
            "job": {
                "source": "user-submitted",
                "user_submitted": True,
                "fast_track": True,
                "url": url,
                "apply_url": url,
                "title": job_fields.title,
                "company": job_fields.company,
                "location": job_fields.location,
                "salary": job_fields.salary,
                "description": job_fields.description,
                "enrichment_status": job_fields.enrichment_status,
                "discovered_at": ts_iso,
            }
        },
    }


def write_to_tracker_inbox(msg: dict, inbox_dir) -> str:
    inbox = pathlib.Path(inbox_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    ts = str(msg.get("timestamp", "")).replace(":", "").replace("-", "")[:15] or "ts"
    cid8 = str(msg.get("correlation_id", "cid"))[:8]
    fname = f"{ts}_USER_SUBMITTED_JOB_jobflow_inbox_{cid8}.json"
    tmp = inbox / (fname + ".tmp")
    tmp.write_text(json.dumps(msg, indent=2), encoding="utf-8")
    os.replace(tmp, inbox / fname)
    return fname
