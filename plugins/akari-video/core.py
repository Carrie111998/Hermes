"""Core logic for the AKARI Video Hermes plugin."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# Path to the pinned akari-video submodule
REPO_ROOT = Path(__file__).resolve().parents[2]  # hermes-agent/
SUBMODULE_PATH = REPO_ROOT / "vendor" / "akari-video"
LAUNCHER_SCRIPT = SUBMODULE_PATH / "packages" / "akari-launcher" / "bin" / "akari.mjs"

TOOLSET = "akari-video"

STATUS_SCHEMA: dict[str, Any] = {
    "name": "akari_video_status",
    "description": "Check the status of the pinned AKARI Video submodule (vendor/akari-video).",
    "parameters": {
        "type": "object",
        "properties": {
            "detail": {
                "type": "boolean",
                "description": "Include detailed submodule info (git status, package.json versions, etc.)",
                "default": False,
            }
        },
        "additionalProperties": False,
    },
}

SKILLS_SCHEMA: dict[str, Any] = {
    "name": "akari_video_skills",
    "description": "List the AKARI Video skills catalog from the submodule (AGENTS.md skills index).",
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

LAUNCH_SCHEMA: dict[str, Any] = {
    "name": "akari_video_launch",
    "description": "Launch the AKARI Video launcher (akari.mjs) with isolated workspace and receipt tracking.",
    "parameters": {
        "type": "object",
        "properties": {
            "project_dir": {
                "type": "string",
                "description": "Directory to run the launcher in (will be created if it doesn't exist). Default: a new .akari-project under HERMES_HOME.",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments to pass to the akari launcher (e.g., ['--help']).",
                "default": [],
            },
        },
        "additionalProperties": False,
    },
}


def check_available() -> bool:
    """Check if the akari-video submodule and launcher are available."""
    return LAUNCHER_SCRIPT.exists()


def _extract_skills_index() -> str:
    """Extract the skills index from AGENTS.md."""
    agents_md = SUBMODULE_PATH / "AGENTS.md"
    if not agents_md.exists():
        return ""

    content = agents_md.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- BEGIN GENERATED skills-index.*?-->([\s\S]*?)<!-- END GENERATED skills-index -->",
        content,
    )
    if match:
        return match.group(1).strip()
    return ""


def handle_status(args: dict[str, Any], **kwargs) -> str:
    """Handle akari_video_status tool call."""
    detail = args.get("detail", False)

    result = {
        "submodule_path": str(SUBMODULE_PATH),
        "submodule_exists": SUBMODULE_PATH.exists(),
        "launcher_exists": LAUNCHER_SCRIPT.exists(),
    }

    if detail and SUBMODULE_PATH.exists():
        # Git submodule status
        try:
            git_status = subprocess.run(
                ["git", "submodule", "status", "vendor/akari-video"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            result["git_submodule_status"] = git_status.stdout.strip()
        except Exception as e:
            result["git_submodule_status_error"] = str(e)

        # Package.json info
        pkg_json = SUBMODULE_PATH / "package.json"
        if pkg_json.exists():
            try:
                result["package_json"] = json.loads(pkg_json.read_text(encoding="utf-8"))
            except Exception as e:
                result["package_json_error"] = str(e)

        # Skills index
        skills_index = _extract_skills_index()
        result["skills_index_available"] = bool(skills_index)
        result["skills_index_preview"] = skills_index[:2000] if skills_index else ""

    return json.dumps(result, ensure_ascii=False, indent=2)


def handle_skills(args: dict[str, Any], **kwargs) -> str:
    """Handle akari_video_skills tool call."""
    skills_index = _extract_skills_index()
    if not skills_index:
        return json.dumps(
            {"error": "Skills index not found in AGENTS.md. The submodule may need regeneration."},
            ensure_ascii=False,
        )

    return json.dumps({"skills_index": skills_index}, ensure_ascii=False)


def handle_launch(args: dict[str, Any], **kwargs) -> str:
    """Handle akari_video_launch tool call."""
    project_dir_str = args.get("project_dir")
    if project_dir_str:
        project_dir = Path(project_dir_str)
    else:
        # Default to HERMES_HOME/.akari-project/<timestamp>
        hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        project_dir = Path(hermes_home) / ".akari-project" / f"project-{int(__import__('time').time())}"

    project_dir.mkdir(parents=True, exist_ok=True)

    launcher_args = args.get("args", [])

    try:
        # Run the akari launcher from the project directory
        result = subprocess.run(
            ["node", str(LAUNCHER_SCRIPT)] + launcher_args,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "HERMES_HOME": os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))},
        )

        return json.dumps(
            {
                "project_dir": str(project_dir),
                "exit_code": result.returncode,
                "stdout": result.stdout[-5000:] if result.stdout else "",
                "stderr": result.stderr[-5000:] if result.stderr else "",
            },
            ensure_ascii=False,
        )
    except subprocess.TimeoutExpired:
        return json.dumps(
            {"error": "Launcher timed out after 300 seconds", "project_dir": str(project_dir)},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": str(e), "project_dir": str(project_dir)}, ensure_ascii=False)


def handle_slash(args: list[str] | None = None, **kwargs) -> str:
    """Handle /akari-video slash command."""
    args = args or []
    subcmd = args[0] if args else "status"

    if subcmd == "status":
        return handle_status({"detail": "detail" in args})
    elif subcmd == "skills":
        return handle_skills({})
    elif subcmd == "launch":
        launch_args = args[1:] if len(args) > 1 else []
        return handle_launch({"args": launch_args})
    else:
        return f"Unknown subcommand: {subcmd}. Use: status, skills, launch"