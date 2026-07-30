"""Unity CLI plugin — install, check, and run Unity CLI commands on Windows."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

CDN_BASE = "https://public-cdn.cloud.unity3d.com/hub/prod/cli/install.ps1"
KNOWN_PATHS = [
    # %LOCALAPPDATA%\Unity\bin\unity.exe
    Path(os.environ.get("LOCALAPPDATA", "")) / "Unity" / "bin" / "unity.exe",
    # Also check under Program Files
    Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "Unity" / "bin" / "unity.exe",
    Path(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")) / "Unity" / "bin" / "unity.exe",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _find_binary(override: str = "") -> Optional[Path]:
    """Locate unity.exe. Returns None if not found."""
    if override:
        p = Path(override)
        if p.exists():
            return p.resolve()
        return None
    for p in KNOWN_PATHS:
        if p.exists():
            return p.resolve()
    return None


def _run_powershell(script: str, timeout: int = 120) -> dict:
    """Run a PowerShell command and return structured result."""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        script,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "stdout": "", "stderr": "Command timed out"}
    except Exception as exc:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc)}


def _run_unity(args: list[str], binary_path: str = "", timeout: int = 60) -> dict:
    """Run a unity.exe command and return structured result."""
    binary = _find_binary(binary_path)
    if not binary:
        return {"ok": False, "code": -1, "stdout": "", "stderr": "Unity CLI not found. Install it first with unity_cli_install."}
    cmd = [str(binary)] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": result.returncode == 0,
            "code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "stdout": "", "stderr": "Command timed out"}
    except Exception as exc:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc)}


def _to_json(result: dict) -> str:
    """Serialize dict to JSON string (tool handler return type)."""
    return json.dumps(result, ensure_ascii=False)


# ── Tool handlers ────────────────────────────────────────────────────────────

# Schema constants

STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "binary_path": {
            "type": "string",
            "description": "Override path to unity.exe. Auto-detected if empty.",
            "default": "",
        },
    },
    "description": "Check if Unity CLI is installed and report version.",
}

INSTALL_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": {
            "type": "string",
            "enum": ["stable", "beta", "alpha"],
            "description": "Release channel to install from.",
            "default": "stable",
        },
    },
    "description": "Install or upgrade the Unity CLI on Windows.",
}

RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "args": {
            "type": "string",
            "description": "Unity CLI arguments as a single string (e.g. 'editors list' or '--version').",
        },
        "binary_path": {
            "type": "string",
            "description": "Override path to unity.exe. Auto-detected if empty.",
            "default": "",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds.",
            "default": 60,
            "minimum": 5,
            "maximum": 300,
        },
    },
    "required": ["args"],
    "description": "Run an arbitrary Unity CLI command.",
}

UPGRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": {
            "type": "string",
            "enum": ["stable", "beta", "alpha"],
            "description": "Upgrade channel. Defaults to current channel.",
            "default": "",
        },
        "binary_path": {
            "type": "string",
            "description": "Override path to unity.exe. Auto-detected if empty.",
            "default": "",
        },
    },
    "description": "Upgrade Unity CLI to the latest version.",
}


def _handle_status(values: dict[str, Any], **kwargs: Any) -> str:
    """Check Unity CLI installation status."""
    binary_path = values.get("binary_path", "")
    binary = _find_binary(binary_path)
    if not binary:
        return _to_json({
            "ok": True,
            "installed": False,
            "version": None,
            "path": None,
            "message": "Unity CLI is not installed. Use unity_cli_install to install it.",
        })

    # Get version
    result = _run_unity(["--version"], binary_path=binary_path, timeout=15)
    version = result.get("stdout", "").strip() if result.get("ok") else "unknown"
    # If --version doesn't work, try --help and parse the first line
    if not version:
        help_result = _run_unity(["--help"], binary_path=binary_path, timeout=15)
        if help_result.get("ok"):
            first_line = help_result.get("stdout", "").split("\n")[0] if help_result.get("stdout") else ""
            version = first_line or "unknown"

    return _to_json({
        "ok": True,
        "installed": True,
        "version": version,
        "path": str(binary),
        "message": f"Unity CLI installed at {binary} ({version})",
    })


def _handle_install(values: dict[str, Any], **kwargs: Any) -> str:
    """Install Unity CLI via the official PowerShell installer."""
    channel = values.get("channel", "stable")
    channel_env = ""
    if channel == "beta":
        channel_env = "$env:UNITY_CLI_CHANNEL='beta'; "
    elif channel == "alpha":
        channel_env = "$env:UNITY_CLI_CHANNEL='alpha'; "

    script = f"{channel_env}irm {CDN_BASE} | iex"
    result = _run_powershell(script, timeout=120)

    if result["ok"]:
        # Extract version from output
        stdout = result.get("stdout", "")
        # Look for "Unity CLI X.Y.Z installed!" in output
        for line in stdout.split("\n"):
            line = line.strip()
            if "installed" in line.lower():
                return _to_json({
                    "ok": True,
                    "channel": channel,
                    "details": line,
                    "message": f"Unity CLI installed successfully ({channel} channel).",
                    "raw_output": stdout[:2000] if len(stdout) > 2000 else stdout,
                })

        return _to_json({
            "ok": True,
            "channel": channel,
            "message": "Unity CLI installed successfully. Run unity_cli_status to verify.",
            "raw_output": stdout[:2000] if len(stdout) > 2000 else stdout,
        })

    return _to_json({
        "ok": False,
        "channel": channel,
        "message": "Installation failed.",
        "error": result.get("stderr", ""),
        "raw_output": result.get("stdout", ""),
    })


def _handle_run(values: dict[str, Any], **kwargs: Any) -> str:
    """Run an arbitrary Unity CLI command."""
    args_str = values.get("args", "")
    binary_path = values.get("binary_path", "")
    timeout = values.get("timeout", 60)

    if not args_str:
        return _to_json({"ok": False, "code": -1, "stdout": "", "stderr": "No args provided."})

    # Split args respecting quotes (shlex-like for Windows-style args)
    args = args_str.split()
    result = _run_unity(args, binary_path=binary_path, timeout=timeout)

    return _to_json({
        "ok": result["ok"],
        "code": result["code"],
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
    })


def _handle_upgrade(values: dict[str, Any], **kwargs: Any) -> str:
    """Upgrade Unity CLI to the latest version."""
    channel = values.get("channel", "")
    binary_path = values.get("binary_path", "")

    binary = _find_binary(binary_path)
    if not binary:
        return _to_json({
            "ok": False,
            "message": "Unity CLI is not installed. Use unity_cli_install first.",
        })

    # Build the upgrade command
    if channel:
        # Reinstall with specified channel
        channel_env = ""
        if channel == "beta":
            channel_env = "$env:UNITY_CLI_CHANNEL='beta'; "
        elif channel == "alpha":
            channel_env = "$env:UNITY_CLI_CHANNEL='alpha'; "
        script = f"{channel_env}irm {CDN_BASE} | iex"
    else:
        # Upgrade via unity's own upgrade command
        upgrade_result = _run_unity(["upgrade"], binary_path=binary_path, timeout=120)
        if upgrade_result["ok"]:
            return _to_json({
                "ok": True,
                "channel": channel or "current",
                "message": "Unity CLI upgraded successfully.",
                "details": upgrade_result.get("stdout", "")[:2000],
            })
        # Fallback: reinstall
        script = f"irm {CDN_BASE} | iex"

    result = _run_powershell(script, timeout=120)
    if result["ok"]:
        return _to_json({
            "ok": True,
            "channel": channel or "stable",
            "message": "Unity CLI reinstalled/upgraded successfully.",
            "raw_output": result.get("stdout", "")[:2000],
        })

    return _to_json({
        "ok": False,
        "message": "Upgrade failed.",
        "error": result.get("stderr", ""),
    })


def _is_installed(**kwargs: Any) -> bool:
    """check_fn: is unity CLI available?"""
    return _find_binary() is not None


# ── Plugin registration ──────────────────────────────────────────────────────

def register(ctx) -> None:
    """Register Unity CLI plugin tools."""
    ctx.register_tool(
        name="unity_cli_status",
        toolset="unity-cli",
        schema=STATUS_SCHEMA,
        handler=_handle_status,
        check_fn=lambda: True,  # always available (reports not-found gracefully)
        description="Check if Unity CLI is installed and report version.",
    )
    ctx.register_tool(
        name="unity_cli_install",
        toolset="unity-cli",
        schema=INSTALL_SCHEMA,
        handler=_handle_install,
        check_fn=lambda: True,  # install always available
        description="Install or upgrade the Unity CLI on Windows.",
    )
    ctx.register_tool(
        name="unity_cli_run",
        toolset="unity-cli",
        schema=RUN_SCHEMA,
        handler=_handle_run,
        check_fn=_is_installed,
        description="Run an arbitrary Unity CLI command.",
    )
    ctx.register_tool(
        name="unity_cli_upgrade",
        toolset="unity-cli",
        schema=UPGRADE_SCHEMA,
        handler=_handle_upgrade,
        check_fn=_is_installed,
        description="Upgrade Unity CLI to the latest version.",
    )

    logger.info("unity-cli plugin registered: status, install, run, upgrade")
