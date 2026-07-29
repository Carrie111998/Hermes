"""Cron model/provider pinning and drift-safety audit."""

from __future__ import annotations

import json
import os
from typing import Any


def _text(value: Any) -> str:
    """Return a normalized optional text value."""
    return value.strip() if isinstance(value, str) else ""


def _configured_defaults(config: dict[str, Any]) -> tuple[str, str, bool, bool]:
    """Resolve cron's effective fleet defaults and whether each is explicit."""
    cron_cfg = config.get("cron") or {}
    if not isinstance(cron_cfg, dict):
        cron_cfg = {}
    model_cfg = config.get("model") or {}

    cron_model = _text(cron_cfg.get("model"))
    cron_provider = _text(cron_cfg.get("model_provider"))
    env_model = _text(os.environ.get("HERMES_MODEL"))

    model_default = ""
    model_provider = ""
    if isinstance(model_cfg, str):
        model_default = model_cfg.strip()
    elif isinstance(model_cfg, dict):
        model_default = _text(model_cfg.get("default") or model_cfg.get("model"))
        model_provider = _text(model_cfg.get("provider"))

    return (
        cron_model or model_default or env_model,
        cron_provider or model_provider,
        bool(cron_model),
        bool(cron_provider),
    )


def audit_cron_models(json_output: bool = False) -> str:
    """Audit cron pinning and whether the drift guard would skip a job now.

    Pinning state and drift impact are deliberately separate. An inherited job
    is not automatically broken: the scheduler only skips an unpinned axis
    when the guard is enabled, a creation-time snapshot exists, no cron-fleet
    default covers that axis, and current resolution differs from the snapshot.
    """
    from cron.jobs import (
        _compute_provider_model_snapshots,
        _resolve_default_model_snapshot,
        load_jobs,
    )
    from hermes_cli.config import cron_model_drift_guard_enabled, load_config

    jobs = load_jobs()
    config = load_config()
    if not isinstance(config, dict):
        config = {}

    effective_model, effective_provider, fleet_model, fleet_provider = (
        _configured_defaults(config)
    )
    # Use the same model resolver as job creation/scheduler as a backstop for
    # managed overlays and legacy config forms.
    effective_model = effective_model or (_resolve_default_model_snapshot() or "")
    if not effective_provider:
        # Best-effort provider resolution. Failure is represented as unknown,
        # never as a false drift finding.
        effective_provider, _ = _compute_provider_model_snapshots(
            provider=None,
            model=effective_model or None,
            base_url=None,
            no_agent=False,
        )
        effective_provider = effective_provider or ""

    guard_enabled = cron_model_drift_guard_enabled(config)
    results: list[dict[str, Any]] = []

    for job in jobs:
        model = _text(job.get("model"))
        provider = _text(job.get("provider"))
        no_agent = bool(job.get("no_agent", False))
        enabled = bool(job.get("enabled", True))
        state = _text(job.get("state")) or "active"
        active = enabled and state != "paused"

        if no_agent:
            status = "script-only"
        elif model and provider:
            status = "pinned"
        elif model or provider:
            status = "partial"
        else:
            status = "inherited"

        model_snapshot = _text(job.get("model_snapshot"))
        provider_snapshot = _text(job.get("provider_snapshot"))
        current_model = model or effective_model
        current_provider = provider or effective_provider

        guarded_axes: list[str] = []
        drifted_axes: list[str] = []
        unprotected_axes: list[str] = []

        if not no_agent:
            axes = (
                ("model", model, model_snapshot, current_model, fleet_model),
                (
                    "provider",
                    provider,
                    provider_snapshot,
                    current_provider,
                    fleet_provider,
                ),
            )
            for axis, pinned, snapshot, current, fleet_covered in axes:
                if pinned or fleet_covered:
                    continue
                if guard_enabled and snapshot:
                    guarded_axes.append(axis)
                    if current and current.lower() != snapshot.lower():
                        drifted_axes.append(axis)
                else:
                    unprotected_axes.append(axis)

        results.append(
            {
                "id": _text(job.get("id")),
                "name": _text(job.get("name")),
                "model": model or "(inherited)",
                "provider": provider or "(inherited)",
                "effective_model": current_model or "(unknown)",
                "effective_provider": current_provider or "(unknown)",
                "model_snapshot": model_snapshot or None,
                "provider_snapshot": provider_snapshot or None,
                "status": status,
                "no_agent": no_agent,
                "enabled": enabled,
                "state": state,
                "active": active,
                "guarded_axes": guarded_axes,
                "drifted_axes": drifted_axes,
                "unprotected_axes": unprotected_axes,
                "at_risk": active and bool(drifted_axes),
            }
        )

    payload = {
        "effective_default_model": effective_model or "(none)",
        "effective_default_provider": effective_provider or "(none)",
        "cron_fleet_model_configured": fleet_model,
        "cron_fleet_provider_configured": fleet_provider,
        "drift_guard_enabled": guard_enabled,
        "jobs": results,
    }
    if json_output:
        return json.dumps(payload, indent=2)

    lines = ["Cron Model Audit", "=" * 72, ""]
    lines.append(
        f"Effective default: {payload['effective_default_provider']} / "
        f"{payload['effective_default_model']}"
    )
    lines.append(f"Drift guard: {'enabled' if guard_enabled else 'disabled'}")
    lines.append("")
    lines.append(
        f"{'ID':<14} {'Name':<22} {'Model':<17} {'Provider':<17} {'Pinning':<12} Risk"
    )
    lines.append("─" * 96)

    for result in results:
        if not result["active"] and result["drifted_axes"]:
            risk = "paused; drift on resume: " + ",".join(result["drifted_axes"])
        elif not result["active"]:
            risk = "paused"
        elif result["at_risk"]:
            risk = "SKIP: " + ",".join(result["drifted_axes"])
        elif result["unprotected_axes"]:
            risk = "unguarded: " + ",".join(result["unprotected_axes"])
        elif result["guarded_axes"]:
            risk = "guarded"
        else:
            risk = "none"
        lines.append(
            f"{result['id'][:12]:<14} {result['name'][:20]:<22} "
            f"{result['model'][:15]:<17} {result['provider'][:15]:<17} "
            f"{result['status']:<12} {risk}"
        )

    counts = {
        status: sum(1 for result in results if result["status"] == status)
        for status in ("pinned", "inherited", "partial", "script-only")
    }
    at_risk = [result for result in results if result["at_risk"]]
    unprotected = [result for result in results if result["unprotected_axes"]]
    lines.extend(
        [
            "",
            "Summary: "
            f"{counts['pinned']} pinned, {counts['inherited']} inherited, "
            f"{counts['partial']} partial, {counts['script-only']} script-only; "
            f"{len(at_risk)} would be skipped now",
        ]
    )
    if at_risk:
        lines.extend(
            [
                "",
                "⚠  Drift guard would skip the jobs marked SKIP on their next run.",
                "   Pin intended values with: hermes cron edit <id> "
                "--model <model> --provider <provider>",
            ]
        )
    if unprotected:
        lines.extend(
            [
                "",
                "Note: jobs marked unguarded have an unpinned axis without active "
                "snapshot protection; they follow current configuration.",
            ]
        )

    return "\n".join(lines)
