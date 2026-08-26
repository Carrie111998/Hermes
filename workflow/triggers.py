"""Register the world's start conditions against existing Hermes surfaces.

A trigger node is not a new HTTP stack. Cron expressions become cron jobs;
webhook specs become rows in ``webhook_subscriptions.json``. Both already
exist. This module is the sync so authoring a trigger on the canvas is
enough.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from utils import atomic_write_text

from workflow.store import load_documents, put_secret, secret_for, workflows_dir

logger = logging.getLogger(__name__)

ROUTE_PREFIX = "wf:"
_OWNED = "hermes_workflow"


def _scenario(doc: dict) -> dict:
    raw = doc.get("scenario")
    return raw if isinstance(raw, dict) else {"steps": [], "edges": []}


def _steps(doc: dict) -> list[dict]:
    steps = _scenario(doc).get("steps") or []
    return [s for s in steps if isinstance(s, dict)]


def _config(step: dict) -> dict:
    raw = step.get("config")
    return raw if isinstance(raw, dict) else {}


def trigger_of(doc: dict) -> dict | None:
    """The first trigger step — a workflow has one start condition."""
    for step in _steps(doc):
        kind = step.get("kind") or (step.get("def") or {}).get("kind")
        if kind != "trigger":
            continue
        on = _config(step).get("on") or {}
        return {"type": str(on.get("type") or "manual"), "spec": str(on.get("spec") or "").strip()}
    return None


def route_name(workflow_id: str) -> str:
    return f"{ROUTE_PREFIX}{workflow_id}"


def webhook_secret(workflow_id: str) -> str:
    existing = secret_for(workflow_id)
    if existing:
        return existing
    secret = secrets.token_hex(16)
    put_secret(workflow_id, secret)
    return secret


def _subscriptions_path() -> Path:
    return get_hermes_home() / "webhook_subscriptions.json"


def _read_subscriptions() -> dict[str, Any]:
    path = _subscriptions_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def sync_webhook_routes(docs: list[dict] | None = None) -> dict[str, str]:
    """Write one dynamic route per webhook-triggered workflow. Leave user routes alone."""
    docs = docs if docs is not None else load_documents()["docs"]
    wanted: dict[str, dict] = {}
    secrets_out: dict[str, str] = {}
    for doc in docs:
        trigger = trigger_of(doc)
        if trigger is None or trigger["type"] != "webhook":
            continue
        wid = doc["id"]
        name = route_name(wid)
        secret = webhook_secret(wid)
        wanted[name] = {
            "secret": secret,
            "workflow": wid,
            "prompt": "",
            _OWNED: True,
        }
        secrets_out[wid] = secret
    existing = _read_subscriptions()
    kept = {k: v for k, v in existing.items() if not (isinstance(v, dict) and v.get(_OWNED))}
    merged = {**kept, **wanted}
    atomic_write_text(_subscriptions_path(), json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
    return secrets_out


def _owned_jobs() -> list[dict]:
    try:
        from cron.jobs import list_jobs
    except Exception:
        return []
    out = []
    for job in list_jobs(include_disabled=True):
        origin = job.get("origin") or {}
        if isinstance(origin, dict) and origin.get("kind") == "workflow":
            out.append(job)
    return out


def _script_path(workflow_id: str) -> Path:
    scripts = get_hermes_home() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    return scripts / f"workflow_{workflow_id}.py"


def _write_tick_script(workflow_id: str) -> str:
    path = _script_path(workflow_id)
    path.write_text(
        "from workflow.runner import start_from_trigger\n"
        f"print(start_from_trigger({workflow_id!r}, source='cron'))\n",
        encoding="utf-8",
    )
    return path.name


def sync_cron_jobs(docs: list[dict] | None = None) -> list[str]:
    """Create or refresh a no-agent cron job for each cron trigger."""
    docs = docs if docs is not None else load_documents()["docs"]
    try:
        from cron.jobs import create_job, remove_job, update_job
    except Exception as exc:
        logger.debug("cron unavailable, skipping workflow cron sync: %s", exc)
        return []

    wanted: dict[str, str] = {}
    for doc in docs:
        trigger = trigger_of(doc)
        if trigger is None or trigger["type"] != "cron" or not trigger["spec"]:
            continue
        wanted[doc["id"]] = trigger["spec"]

    existing = { (job.get("origin") or {}).get("workflow_id"): job for job in _owned_jobs() }
    kept_ids = []
    for workflow_id, schedule in wanted.items():
        script = _write_tick_script(workflow_id)
        job = existing.get(workflow_id)
        if job is None:
            created = create_job(
                prompt="",
                schedule=schedule,
                name=f"workflow:{workflow_id}",
                script=script,
                no_agent=True,
                deliver="local",
                origin={"kind": "workflow", "workflow_id": workflow_id},
            )
            kept_ids.append(created["id"])
            continue
        updates: dict[str, Any] = {"script": script, "no_agent": True}
        if job.get("schedule_display") != schedule:
            updates["schedule"] = schedule
        update_job(job["id"], updates)
        kept_ids.append(job["id"])

    for workflow_id, job in existing.items():
        if workflow_id not in wanted:
            remove_job(job["id"])

    return kept_ids


def sync_triggers(docs: list[dict] | None = None) -> dict[str, Any]:
    docs = docs if docs is not None else load_documents()["docs"]
    return {
        "webhooks": sync_webhook_routes(docs),
        "cron": sync_cron_jobs(docs),
        "home": str(workflows_dir()),
    }
