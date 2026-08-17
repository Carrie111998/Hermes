"""Agent definition system debug logger.

Writes to /tmp/hermes_agent_debug.log for diagnosing
agent loading, delegate_task agent resolution, and skill_path filtering.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_LOG_PATH = Path("/tmp/hermes_agent_debug.log")
_logger = logging.getLogger("agent_debug")


def _write(msg: str) -> None:
    """Append a timestamped line to the debug log."""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_PATH, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def log_registry_load(source: str, count: int, names: list[str]) -> None:
    """Log when agents are loaded into registry."""
    _write(f"REGISTRY_LOAD source={source} count={count} names={names}")


def log_registry_lookup(agent_name: str, found: bool, registry_size: int) -> None:
    """Log agent lookup in registry."""
    _write(f"REGISTRY_LOOKUP agent={agent_name} found={found} registry_size={registry_size}")


def log_agent_config(agent_name: str, config: dict) -> None:
    """Log agent config being applied."""
    _write(f"AGENT_CONFIG agent={agent_name} config={json.dumps(config, default=str)}")


def log_skill_path(agent_name: str, skill_path: str, skills: list[str]) -> None:
    """Log skill_path and skills filter."""
    _write(f"SKILL_PATH agent={agent_name} path={skill_path} skills={skills}")


def log_env_inject(var_name: str, value: str) -> None:
    """Log environment variable injection."""
    _write(f"ENV_INJECT {var_name}={value}")


def log_env_restore(var_name: str, value: str | None) -> None:
    """Log environment variable restoration."""
    _write(f"ENV_RESTORE {var_name}={value}")


def log_delegate_start(agent: str, goal: str) -> None:
    """Log delegate_task start."""
    _write(f"DELEGATE_START agent={agent} goal={goal[:80]}")


def log_delegate_result(agent: str, status: str, error: str = "") -> None:
    """Log delegate_task result."""
    _write(f"DELEGATE_RESULT agent={agent} status={status} error={error[:200]}")


def log_skill_view(skill_name: str, search_dir: str, found: bool) -> None:
    """Log skill_view attempt."""
    _write(f"SKILL_VIEW name={skill_name} dir={search_dir} found={found}")


def log_error(context: str, error: str) -> None:
    """Log an error."""
    _write(f"ERROR context={context} error={error[:300]}")


def clear_log() -> None:
    """Clear the debug log."""
    try:
        _LOG_PATH.write_text("")
    except Exception:
        pass


def get_log() -> str:
    """Read the debug log."""
    try:
        return _LOG_PATH.read_text()
    except Exception:
        return "(no log)"
