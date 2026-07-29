"""Cron model/provider pinning audit.

Identifies which cron jobs have pinned models (safe from provider drift),
which inherit the global config (will drift if config changes), and which
are script-only (no LLM call, unaffected).
"""

from __future__ import annotations

import json
from typing import Optional


def audit_cron_models(json_output: bool = False) -> str:
    """Audit all cron jobs for model/provider pinning status.

    Identifies which jobs have pinned models (safe from provider drift),
    which inherit the global config (will drift if config changes), and
    which are script-only (no LLM call, unaffected).
    """
    from cron.jobs import load_jobs, _resolve_default_model_snapshot
    from hermes_cli.config import load_config

    jobs = load_jobs()
    config = load_config()

    # Resolve global defaults for context
    global_model = _resolve_default_model_snapshot()
    # Resolve global provider
    model_cfg = config.get("model") or {}
    if isinstance(model_cfg, dict):
        global_provider = model_cfg.get("provider", "")
    else:
        global_provider = ""

    results = []
    for job in jobs:
        model = job.get("model")
        provider = job.get("provider")
        no_agent = job.get("no_agent", False)
        job_id = job.get("id", "")
        name = job.get("name", "") or ""

        if no_agent:
            status = "script-only"
            status_icon = "\u2713"  # checkmark
        elif model is not None and provider is not None:
            status = "pinned"
            status_icon = "\u2713"
        elif model is not None or provider is not None:
            status = "partial"
            status_icon = "\u26a0"  # warning
        else:
            status = "inherited"
            status_icon = "\u26a0"

        results.append({
            "id": job_id,
            "name": name,
            "model": model or "(inherited)",
            "provider": provider or "(inherited)",
            "status": status,
            "status_icon": status_icon,
            "no_agent": no_agent,
        })

    if json_output:
        return json.dumps({
            "global_model": global_model or "(none)",
            "global_provider": global_provider or "(none)",
            "jobs": results,
        }, indent=2)

    # Format as table
    lines = ["Cron Model Audit", "=" * 60, ""]
    lines.append(f"Global model: {global_model or '(none)'}")
    lines.append(f"Global provider: {global_provider or '(none)'}")
    lines.append("")

    # Table header
    lines.append(f"{'ID':<14} {'Name':<24} {'Model':<18} {'Provider':<18} {'Status'}")
    lines.append("\u2500" * 80)

    for r in results:
        lines.append(
            f"{r['id'][:12]:<14} {r['name'][:22]:<24} {r['model'][:16]:<18} "
            f"{r['provider'][:16]:<18} {r['status_icon']} {r['status']}"
        )

    # Summary
    pinned = sum(1 for r in results if r["status"] == "pinned")
    inherited = sum(1 for r in results if r["status"] == "inherited")
    script_only = sum(1 for r in results if r["status"] == "script-only")
    partial = sum(1 for r in results if r["status"] == "partial")

    lines.append("")
    lines.append(
        f"Summary: {pinned} pinned, {inherited} inherited, {partial} partial, {script_only} script-only"
    )

    if inherited > 0 or partial > 0:
        lines.append("")
        lines.append(
            "\u26a0  Inherited/partial jobs will silently fail if global model/provider changes."
        )
        lines.append(
            "   Pin them with: hermes cron update <id> --model <model> --provider <provider>"
        )

    return "\n".join(lines)