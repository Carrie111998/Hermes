"""fleet_pulse — fleet activity monitoring plugin for Hermes Agent.

Inspired by the Lofoten fishing fleet: during the Lofotfisket (the seasonal
cod fishery, February through April), every boat in the fleet knows where
the others are, what they're catching, and when they're active. This plugin
brings that same fleet-wide awareness to Hermes.

It hooks into session lifecycle events and tool calls to build a real-time
picture of what every Hermes profile is doing. The /fleet-pulse slash command
gives any agent instant visibility into the entire fleet — who's active,
what they're working on, and how busy they are.

Hooks:
  - on_session_start: records session start with profile, timestamp, source
  - on_session_end: records session end, computes duration, logs to activity
  - post_tool_call: increments per-profile tool counters, tracks last activity

Data is stored in a JSON file under HERMES_HOME/fleet-pulse/activity.json,
shared across all profiles via the global ~/.hermes/ directory.

No external dependencies. No API keys. No network calls. Just local
file-based fleet awareness.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()


def _get_global_hermes_home() -> Path:
    """Get the global ~/.hermes directory (not profile-specific)."""
    # HERMES_HOME for the default profile is ~/.hermes
    # For named profiles it's ~/.hermes/profiles/<name>
    # We want the root ~/.hermes so all profiles share the same data
    hh = os.environ.get("HERMES_HOME", "")
    if hh:
        p = Path(hh).resolve()
        # If we're in a profile subdir, go up to find ~/.hermes
        if "profiles" in p.parts:
            idx = p.parts.index("profiles")
            return Path(*p.parts[:idx])
        return p
    return Path.home() / ".hermes"


def _get_data_dir() -> Path:
    """Data directory under the global HERMES_HOME."""
    return _get_global_hermes_home() / "fleet-pulse"


def _get_activity_file() -> Path:
    return _get_data_dir() / "activity.json"


def _get_log_file() -> Path:
    return _get_data_dir() / "fleet.log"


def _ensure_data_dir() -> None:
    """Create the data directory if it doesn't exist."""
    try:
        _get_data_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _load_activity() -> Dict[str, Any]:
    """Load the activity data file. Returns empty structure if missing."""
    try:
        path = _get_activity_file()
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("fleet-pulse: failed to load activity: %s", e)
    return {
        "profiles": {},
        "total_sessions": 0,
        "total_tool_calls": 0,
        "created": _now_iso(),
        "last_updated": _now_iso(),
    }


def _save_activity(data: Dict[str, Any]) -> None:
    """Save activity data atomically."""
    _ensure_data_dir()
    data["last_updated"] = _now_iso()
    try:
        path = _get_activity_file()
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
    except OSError as e:
        logger.debug("fleet-pulse: failed to save activity: %s", e)


def _log_event(message: str) -> None:
    """Append a line to the fleet log."""
    _ensure_data_dir()
    try:
        log_path = _get_log_file()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def _get_profile_name() -> str:
    """Extract the profile name from HERMES_HOME or context."""
    hh = os.environ.get("HERMES_HOME", "")
    if hh:
        p = Path(hh)
        if "profiles" in p.parts:
            idx = p.parts.index("profiles")
            if idx + 1 < len(p.parts):
                return p.parts[idx + 1]
    return "default"


# ---------------------------------------------------------------------------
# Profile activity tracking
# ---------------------------------------------------------------------------


def _ensure_profile(data: Dict[str, Any], profile: str) -> Dict[str, Any]:
    """Ensure a profile entry exists in the data structure."""
    if profile not in data["profiles"]:
        data["profiles"][profile] = {
            "session_count": 0,
            "tool_call_count": 0,
            "tool_counts": {},
            "first_seen": _now_iso(),
            "last_active": _now_iso(),
            "last_active_epoch": _now_epoch(),
            "current_session_start": None,
            "current_session_source": None,
            "status": "idle",
            "recent_tools": [],
        }
    return data["profiles"][profile]


def _update_last_active(profile_data: Dict[str, Any]) -> None:
    """Update the last active timestamp."""
    profile_data["last_active"] = _now_iso()
    profile_data["last_active_epoch"] = _now_epoch()


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _on_session_start(
    session_id: str = "",
    source: str = "",
    **_: Any,
) -> None:
    """Record session start for the current profile."""
    profile = _get_profile_name()
    with _LOCK:
        data = _load_activity()
        pdata = _ensure_profile(data, profile)
        pdata["session_count"] += 1
        pdata["current_session_start"] = _now_iso()
        pdata["current_session_source"] = source or "cli"
        pdata["status"] = "active"
        _update_last_active(pdata)
        data["total_sessions"] += 1
        _save_activity(data)
    _log_event(f"SESSION_START profile={profile} session={session_id} source={source}")


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Record session end for the current profile."""
    profile = _get_profile_name()
    with _LOCK:
        data = _load_activity()
        if profile in data["profiles"]:
            pdata = data["profiles"][profile]
            start = pdata.get("current_session_start")
            if start:
                # Compute duration
                try:
                    start_dt = datetime.fromisoformat(
                        start.replace("Z", "+00:00")
                    )
                    duration = _now_epoch() - start_dt.timestamp()
                    pdata["last_session_duration"] = round(duration, 1)
                except (ValueError, OSError):
                    pass
            pdata["current_session_start"] = None
            pdata["current_session_source"] = None
            pdata["status"] = "completed" if completed else "interrupted"
            _update_last_active(pdata)
            _save_activity(data)
    status = "completed" if completed else "interrupted"
    _log_event(f"SESSION_END profile={profile} session={session_id} status={status}")


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    **_: Any,
) -> None:
    """Track tool usage per profile."""
    if not tool_name:
        return
    profile = _get_profile_name()
    with _LOCK:
        data = _load_activity()
        pdata = _ensure_profile(data, profile)
        pdata["tool_call_count"] += 1
        pdata["tool_counts"][tool_name] = pdata["tool_counts"].get(tool_name, 0) + 1
        # Keep last 10 tools
        pdata["recent_tools"].append(tool_name)
        if len(pdata["recent_tools"]) > 10:
            pdata["recent_tools"] = pdata["recent_tools"][-10:]
        pdata["status"] = "active"
        _update_last_active(pdata)
        data["total_tool_calls"] += 1
        _save_activity(data)


# ---------------------------------------------------------------------------
# Activity summary (for /fleet-pulse command)
# ---------------------------------------------------------------------------


def _is_profile_active(profile_data: Dict[str, Any], threshold_seconds: int = 300) -> bool:
    """Check if a profile was active within the threshold (default 5 min)."""
    last_epoch = profile_data.get("last_active_epoch", 0)
    if not last_epoch:
        return False
    return (_now_epoch() - last_epoch) < threshold_seconds


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def _format_activity() -> str:
    """Build the fleet activity summary string."""
    data = _load_activity()
    profiles = data.get("profiles", {})

    if not profiles:
        return (
            "Fleet Pulse — No activity recorded yet.\n"
            "Activity is tracked automatically as profiles run sessions.\n"
            f"Data file: {_get_activity_file()}"
        )

    lines = []
    lines.append("Fleet Pulse — SMF Works Fleet Activity Monitor")
    lines.append("=" * 55)
    lines.append("")

    # Sort profiles: active first, then by last_active
    sorted_profiles = sorted(
        profiles.items(),
        key=lambda x: (-int(_is_profile_active(x[1])), -x[1].get("last_active_epoch", 0)),
    )

    active_count = 0
    total_sessions = data.get("total_sessions", 0)
    total_tools = data.get("total_tool_calls", 0)

    for name, pdata in sorted_profiles:
        is_active = _is_profile_active(pdata)
        status_icon = "🟢" if is_active else "⚪"
        if is_active:
            active_count += 1

        status = pdata.get("status", "idle")
        sessions = pdata.get("session_count", 0)
        tools = pdata.get("tool_call_count", 0)
        last = pdata.get("last_active", "never")

        # Top 3 tools
        tool_counts = pdata.get("tool_counts", {})
        top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:3]
        tool_str = ", ".join(f"{t}({c})" for t, c in top_tools) if top_tools else "none"

        lines.append(f"{status_icon} {name}")
        lines.append(f"   Status: {status} | Sessions: {sessions} | Tools: {tools}")
        lines.append(f"   Top tools: {tool_str}")
        lines.append(f"   Last active: {last}")
        lines.append("")

    lines.append("-" * 55)
    lines.append(f"Fleet total: {active_count} active, {len(profiles)} profiles")
    lines.append(f"Total sessions: {total_sessions} | Total tool calls: {total_tools}")
    lines.append(f"Data: {_get_activity_file()}")
    lines.append(f"Log:  {_get_log_file()}")

    return "\n".join(lines)


def _format_profile_detail(profile_name: str) -> str:
    """Build detailed view for a single profile."""
    data = _load_activity()
    profiles = data.get("profiles", {})

    if profile_name not in profiles:
        return f"Profile '{profile_name}' not found in fleet data.\nAvailable: {', '.join(sorted(profiles.keys()))}"

    pdata = profiles[profile_name]
    lines = []
    lines.append(f"Fleet Pulse — Profile: {profile_name}")
    lines.append("=" * 55)
    lines.append("")

    is_active = _is_profile_active(pdata)
    lines.append(f"Status: {'ACTIVE' if is_active else 'INACTIVE'}")
    lines.append(f"Current session: {pdata.get('current_session_start', 'none')}")
    lines.append(f"Session source: {pdata.get('current_session_source', 'n/a')}")
    lines.append(f"Total sessions: {pdata.get('session_count', 0)}")
    lines.append(f"Total tool calls: {pdata.get('tool_call_count', 0)}")
    lines.append(f"First seen: {pdata.get('first_seen', 'unknown')}")
    lines.append(f"Last active: {pdata.get('last_active', 'never')}")

    last_dur = pdata.get("last_session_duration")
    if last_dur:
        lines.append(f"Last session duration: {_format_duration(last_dur)}")

    lines.append("")
    lines.append("Tool breakdown:")
    tool_counts = pdata.get("tool_counts", {})
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {tool}: {count}")

    lines.append("")
    lines.append("Recent tools (last 10):")
    recent = pdata.get("recent_tools", [])
    if recent:
        lines.append(f"  {' → '.join(recent)}")
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def _format_log(lines: int = 20) -> str:
    """Show recent fleet log entries."""
    log_path = _get_log_file()
    if not log_path.exists():
        return "Fleet log is empty (no activity recorded yet)."

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
    except OSError as e:
        return f"Error reading log: {e}"

    if not all_lines:
        return "Fleet log is empty."

    recent = all_lines[-lines:]
    header = f"Fleet Pulse — Last {len(recent)} log entries\n" + "=" * 55 + "\n"
    return header + "".join(recent)


def _reset_data() -> str:
    """Reset all fleet data."""
    try:
        activity = _get_activity_file()
        log = _get_log_file()
        if activity.exists():
            activity.unlink()
        if log.exists():
            log.unlink()
        return "Fleet pulse data reset. All profiles and history cleared."
    except OSError as e:
        return f"Error resetting data: {e}"


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
/fleet-pulse — Fleet Activity Monitor

Subcommands:
  (none)              Show fleet overview (all profiles)
  status              Same as overview
  detail <profile>    Show detailed activity for a specific profile
  log [N]             Show last N log entries (default 20)
  reset               Clear all fleet data (cannot be undone)
  help                Show this help

Activity is tracked automatically via session lifecycle hooks.
No configuration required — just install and enable the plugin.
"""


def _handle_slash(raw_args: str) -> Optional[str]:
    argv = raw_args.strip().split()
    if not argv or argv[0] in {"status", "overview"}:
        return _format_activity()

    sub = argv[0]

    if sub in {"help", "-h", "--help"}:
        return _HELP_TEXT

    if sub == "detail":
        if len(argv) < 2:
            return "Usage: /fleet-pulse detail <profile-name>"
        return _format_profile_detail(argv[1])

    if sub == "log":
        n = 20
        if len(argv) >= 2:
            try:
                n = int(argv[1])
            except ValueError:
                return f"Invalid number: {argv[1]}"
        return _format_log(n)

    if sub == "reset":
        if "--confirm" not in argv[1:]:
            return "This will erase ALL fleet data. Use: /fleet-pulse reset --confirm"
        return _reset_data()

    return f"Unknown subcommand: {sub}\n\n{_HELP_TEXT}"


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register fleet-pulse hooks and slash command."""
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_command(
        "fleet-pulse",
        handler=_handle_slash,
        description="Monitor fleet activity across all Hermes profiles.",
        args_hint="[status|detail <profile>|log [N]|reset]",
    )
    logger.info("fleet-pulse plugin registered")