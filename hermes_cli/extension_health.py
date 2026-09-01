"""Read-only health summary for optional third-party extensions.

The registry is declarative. This module never installs extensions, starts
processes, or performs network probes; it reports only local/configured state.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class ExtensionHealthRow:
    """One renderer-independent extension health result."""

    label: str
    status: str
    detail: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _entry_enabled(registry: Mapping[str, Any], name: str) -> bool:
    entry = _mapping(registry.get(name))
    promotion = _mapping(entry.get("promotion"))
    return str(promotion.get("state") or "disabled").lower() != "disabled"


def _local_path(value: Any, hermes_home: Path) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else hermes_home / path


def _command_found(command: Any, which: Callable[[str], str | None]) -> bool:
    name = str(command or "").strip()
    return bool(name and which(name))


def collect_extension_health(
    config: Mapping[str, Any],
    *,
    hermes_home: Path,
    which: Callable[[str], str | None] = shutil.which,
) -> list[ExtensionHealthRow]:
    """Collect the global extension health view without external side effects."""

    extensions = _mapping(config.get("extensions"))
    registry = _mapping(extensions.get("registry"))
    health = _mapping(extensions.get("health"))
    rows: list[ExtensionHealthRow] = []

    omh = _mapping(health.get("omh"))
    omh_command = omh.get("command") or "omh"
    if _command_found(omh_command, which):
        rows.append(ExtensionHealthRow("OMH", "ok", f"command={omh_command}"))
    else:
        rows.append(ExtensionHealthRow("OMH", "warn", f"command {omh_command!r} not detected"))

    retrieval = _mapping(health.get("skill_retrieval"))
    retrieval_index = _local_path(retrieval.get("index_path"), hermes_home)
    retrieval_ready = bool(retrieval_index and retrieval_index.is_file())
    retrieval_enabled = _entry_enabled(registry, "skill-retrieval")
    top_k = retrieval.get("top_k", 8)
    retrieval_state = "index=ready" if retrieval_ready else "index=missing"
    if not retrieval_enabled:
        retrieval_state = f"unconfigured; {retrieval_state}"
    rows.append(
        ExtensionHealthRow(
            "Skill Retrieval",
            "ok" if retrieval_enabled and retrieval_ready else "warn",
            f"{retrieval_state}; top_k={top_k}",
        )
    )

    rtk = _mapping(health.get("rtk"))
    rtk_command = rtk.get("command") or "rtk"
    rtk_found = _command_found(rtk_command, which)
    rtk_enabled = _entry_enabled(registry, "rtk")
    rtk_entry = _mapping(registry.get("rtk"))
    rtk_version = str(rtk_entry.get("version") or "unreported")
    raw_bypass = bool(rtk.get("raw_bypass", False))
    rtk_detail = (
        f"{'detected' if rtk_found else 'not detected'}; version={rtk_version}; "
        f"raw bypass={'available' if raw_bypass else 'missing'}"
    )
    if not rtk_enabled:
        rtk_detail = f"unconfigured; {rtk_detail}"
    rows.append(
        ExtensionHealthRow(
            "RTK",
            "ok" if rtk_enabled and rtk_found and raw_bypass else "warn",
            rtk_detail,
        )
    )

    planning = _mapping(health.get("planning_files"))
    current_plan_value = str(planning.get("current_plan") or "").strip()
    current_plan = _local_path(current_plan_value, hermes_home)
    planning_ready = bool(current_plan and current_plan.is_file())
    planning_enabled = _entry_enabled(registry, "planning-with-files")
    if current_plan is None:
        plan_detail = "current plan unconfigured"
    elif planning_ready:
        plan_detail = f"current plan={current_plan_value}"
    else:
        plan_detail = f"current plan missing: {current_plan_value}"
    if not planning_enabled:
        plan_detail = f"unconfigured; {plan_detail}"
    rows.append(
        ExtensionHealthRow(
            "Planning Files",
            "ok" if planning_enabled and planning_ready else "warn",
            plan_detail,
        )
    )

    projection = _mapping(health.get("planning_plane"))
    projection_status = str(projection.get("status") or "unconfigured")
    projection_hash = str(projection.get("projection_hash") or "").strip()
    projection_ready = projection_status.lower() in {"ok", "ready", "synced"} and bool(projection_hash)
    rows.append(
        ExtensionHealthRow(
            "Planning → Plane",
            "ok" if projection_ready else "warn",
            f"status={projection_status}; hash={projection_hash or 'missing'}",
        )
    )

    delegate = _mapping(health.get("delegate"))
    adapters = _sequence(delegate.get("adapters"))
    rows.append(
        ExtensionHealthRow(
            "Delegate adapters",
            "ok" if adapters else "warn",
            ", ".join(adapters) if adapters else "none detected",
        )
    )

    reach = _mapping(health.get("agent_reach"))
    reach_command = reach.get("command") or "agent-reach"
    reach_found = _command_found(reach_command, which)
    doctor_status = str(reach.get("doctor_status") or "unconfigured")
    sources = _mapping(reach.get("sources"))
    source_detail = ", ".join(f"{name}={state}" for name, state in sorted(sources.items()))
    source_ready = bool(sources) and all(
        str(state).lower() in {"ok", "ready", "healthy"} for state in sources.values()
    )
    reach_ready = reach_found and doctor_status.lower() in {"ok", "ready", "healthy"} and source_ready
    rows.append(
        ExtensionHealthRow(
            "Agent Reach",
            "ok" if reach_ready else "warn",
            f"doctor={doctor_status}; sources={source_detail or 'unconfigured'}; "
            f"command={'detected' if reach_found else 'not detected'}",
        )
    )

    mantis = _mapping(health.get("mantis"))
    mantis_command = mantis.get("command") or "mantis"
    mantis_found = _command_found(mantis_command, which)
    isolation_ready = bool(mantis.get("isolation_ready", False))
    rows.append(
        ExtensionHealthRow(
            "Mantis",
            "ok" if mantis_found and isolation_ready else "warn",
            f"command={'detected' if mantis_found else 'not detected'}; "
            f"isolation={'ready' if isolation_ready else 'not ready'}",
        )
    )

    capabilities = _mapping(health.get("profile_capabilities"))
    expected = set(_sequence(capabilities.get("expected")))
    actual = set(_sequence(capabilities.get("actual")))
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if not expected:
        drift_status = "info"
        drift_detail = "expected capability set unconfigured"
    elif missing or extra:
        drift_status = "warn"
        drift_detail = f"missing={missing or 'none'}; extra={extra or 'none'}"
    else:
        drift_status = "ok"
        drift_detail = "no drift"
    rows.append(ExtensionHealthRow("Profile capability drift", drift_status, drift_detail))

    raw_evidence = _mapping(health.get("reviewer_raw_evidence"))
    raw_capability = str(raw_evidence.get("capability") or "terminal.raw")
    raw_available = bool(raw_evidence.get("available", False))
    rows.append(
        ExtensionHealthRow(
            "Reviewer raw evidence",
            "ok" if raw_available else "warn",
            f"{raw_capability}={'available' if raw_available else 'missing'}",
        )
    )

    cron_dependency = bool(health.get("production_cron_requires_extensions", False))
    scoped_to_cron = any(
        "production-cron" in {scope.lower().replace("_", "-") for scope in _sequence(_mapping(entry).get("scope"))}
        for entry in registry.values()
    )
    cron_unaffected = not cron_dependency and not scoped_to_cron
    rows.append(
        ExtensionHealthRow(
            "Production cron",
            "ok" if cron_unaffected else "warn",
            "unaffected; no optional extension dependency"
            if cron_unaffected
            else "extension dependency configured; review production isolation",
        )
    )

    return rows
