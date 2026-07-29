#!/usr/bin/env python3
"""Collect read-only evidence for a Hermes Engineering Review.

The collector intentionally avoids reading secret-bearing files. It records
repository shape, subsystem presence, counts, git metadata, safe command probes,
and lightweight validation signals so reviewers can ground their report in
repeatable facts.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

COLLECTOR_VERSION = "1.1.1"
SCHEMA_VERSION = "1.0"

IGNORE_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

SECRET_LIKE_NAMES = {
    ".env",
    "auth.json",
    "credentials.json",
    "token.json",
    "tokens.json",
    "secrets.json",
}

SUBSYSTEM_PATHS = {
    "agent_loop": ["run_agent.py", "agent"],
    "tools": ["tools", "tools/registry.py", "toolsets.py", "model_tools.py"],
    "cli": ["hermes_cli", "cli.py"],
    "gateway": ["gateway"],
    "scheduler": ["cron"],
    "mcp": ["hermes_cli/subcommands/mcp.py", "tools/mcp_tool.py"],
    "skills": ["skills", "tools/skill_manager_tool.py", "agent/skill_utils.py"],
    "memory": ["agent/memory", "tools/session_search_tool.py", "hermes_state.py"],
    "prompt_system": ["agent/prompt_builder.py"],
    "profiles": ["hermes_cli/subcommands/profile.py"],
    "docs": ["website/docs", "website/scripts/generate-skill-docs.py"],
    "docker": ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"],
    "tests": ["tests"],
}

PROJECT_MARKERS = {
    "pyproject": "pyproject.toml",
    "pytest_config": "pytest.ini",
    "run_tests_script": "scripts/run_tests.sh",
    "package_json": "package.json",
    "website_package_json": "website/package.json",
    "dockerfile": "Dockerfile",
    "github_actions": ".github/workflows",
}


def run_command(repo: Path, args: list[str], timeout: int = 20) -> dict[str, Any]:
    """Run a safe read-only command and return bounded output."""
    try:
        completed = subprocess.run(
            args,
            cwd=str(repo),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "command": args,
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    except FileNotFoundError:
        return {"command": args, "exit_code": None, "error": "command not found"}
    except subprocess.TimeoutExpired:
        return {"command": args, "exit_code": None, "error": f"timeout after {timeout}s"}


def is_ignored(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)


def safe_walk(repo: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirs, names in os.walk(repo):
        root_path = Path(root)
        dirs[:] = [
            d for d in dirs
            if d not in IGNORE_DIRS and not (root_path / d).is_symlink()
        ]
        if is_ignored(root_path.relative_to(repo)):
            continue
        for name in names:
            if (root_path / name).is_symlink():
                continue
            rel = (root_path / name).relative_to(repo)
            if is_ignored(rel):
                continue
            files.append(rel)
    return sorted(files, key=lambda p: p.as_posix())


def count_by_suffix(files: list[Path]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in files:
        suffix = rel.suffix.lower() or "[no extension]"
        counts[suffix] = counts.get(suffix, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def count_matching(files: list[Path], predicate: Callable[[Path], bool]) -> int:
    return sum(1 for rel in files if predicate(rel))


def path_info(repo: Path, rel: str) -> dict[str, Any]:
    path = repo / rel
    if path.is_symlink():
        return {"path": rel, "exists": False, "skipped": "symlink"}
    exists = path.exists()
    info: dict[str, Any] = {"path": rel, "exists": exists}
    if exists:
        info["type"] = "directory" if path.is_dir() else "file"
        if path.is_file() and path.name not in SECRET_LIKE_NAMES:
            try:
                info["size_bytes"] = path.stat().st_size
            except OSError:
                pass
    return info


def subsystem_inventory(repo: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        name: [path_info(repo, rel) for rel in rel_paths]
        for name, rel_paths in SUBSYSTEM_PATHS.items()
    }


def project_markers(repo: Path) -> dict[str, dict[str, Any]]:
    return {name: path_info(repo, rel) for name, rel in PROJECT_MARKERS.items()}


def discover_safe_config_files(files: list[Path]) -> list[str]:
    config_suffixes = {".yaml", ".yml", ".toml", ".ini", ".json"}
    safe: list[str] = []
    for rel in files:
        if rel.name in SECRET_LIKE_NAMES:
            continue
        if rel.suffix.lower() in config_suffixes and any(
            part in {"config", "configs", ".github", "website"} or "config" in rel.name.lower()
            for part in rel.parts
        ):
            safe.append(rel.as_posix())
    return safe[:200]


def discover_test_targets(files: list[Path]) -> dict[str, Any]:
    tests = [rel.as_posix() for rel in files if rel.as_posix().startswith("tests/") and rel.suffix == ".py"]
    grouped: dict[str, int] = {}
    for rel in tests:
        parts = rel.split("/")
        bucket = parts[1] if len(parts) > 2 else "[root]"
        grouped[bucket] = grouped.get(bucket, 0) + 1
    return {
        "total_python_tests": len(tests),
        "by_tests_subdir": dict(sorted(grouped.items(), key=lambda item: (-item[1], item[0]))[:50]),
        "sample_files": tests[:25],
    }


def skill_frontmatter_health(repo: Path, skill_files: list[Path]) -> dict[str, Any]:
    invalid: list[dict[str, str]] = []
    names: list[str] = []
    name_pattern = re.compile(r"^name:\s*['\"]?([^'\"\n]+)['\"]?\s*$", re.MULTILINE)
    description_pattern = re.compile(r"^description:\s*(.+)$", re.MULTILINE)

    for rel in skill_files:
        path = repo / rel
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            invalid.append({"path": rel.as_posix(), "reason": "not utf-8"})
            continue
        except OSError as exc:
            invalid.append({"path": rel.as_posix(), "reason": f"read failed: {exc}"})
            continue

        if not text.startswith("---"):
            invalid.append({"path": rel.as_posix(), "reason": "missing opening frontmatter delimiter"})
            continue
        match = re.search(r"\n---\s*\n", text[3:])
        if not match:
            invalid.append({"path": rel.as_posix(), "reason": "missing closing frontmatter delimiter"})
            continue
        frontmatter = text[3 : match.start() + 3]
        name_match = name_pattern.search(frontmatter)
        if not name_match:
            invalid.append({"path": rel.as_posix(), "reason": "missing name field"})
            continue
        if not description_pattern.search(frontmatter):
            invalid.append({"path": rel.as_posix(), "reason": "missing description field"})
            continue
        names.append(name_match.group(1).strip())

    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    return {
        "skill_files_checked": len(skill_files),
        "invalid_frontmatter_count": len(invalid),
        "invalid_frontmatter": invalid[:50],
        "duplicate_skill_names": duplicate_names,
    }


def suggested_review_commands(repo: Path) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    if (repo / "pyproject.toml").exists() or (repo / "tests").exists():
        suggestions.append({
            "purpose": "Python test suite or targeted subset",
            "command": "python3 -m pytest tests/ -q -o 'addopts='",
        })
    if (repo / "scripts/run_tests.sh").exists():
        suggestions.append({"purpose": "Hermes test wrapper", "command": "scripts/run_tests.sh"})
    if (repo / "website/package.json").exists():
        suggestions.append({"purpose": "Documentation build", "command": "cd website && npm run build"})
    if (repo / "website/scripts/generate-skill-docs.py").exists():
        suggestions.append({
            "purpose": "Generated skill docs drift check",
            "command": "python3 website/scripts/generate-skill-docs.py && git diff --exit-code -- website/docs website/sidebars.ts",
        })
    return suggestions


def collect(repo: Path) -> dict[str, Any]:
    repo = repo.resolve()
    files = safe_walk(repo)
    git_status = run_command(repo, ["git", "status", "--short", "--branch"])
    git_root = run_command(repo, ["git", "rev-parse", "--show-toplevel", "HEAD"])
    branch = run_command(repo, ["git", "branch", "--show-current"])

    skill_files = [rel for rel in files if rel.name == "SKILL.md"]
    markdown_files = [rel for rel in files if rel.suffix.lower() == ".md"]
    python_files = [rel for rel in files if rel.suffix.lower() == ".py"]
    test_files = [rel for rel in files if rel.as_posix().startswith("tests/") and rel.suffix.lower() == ".py"]
    docs_files = [rel for rel in files if rel.as_posix().startswith("website/docs/")]

    return {
        "schema_version": SCHEMA_VERSION,
        "collector": {
            "name": "engineering-review-evidence-collector",
            "version": COLLECTOR_VERSION,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "repository": {
            "path": str(repo),
            "git_status": git_status,
            "git_root_and_head": git_root,
            "branch": branch,
            "dirty": bool(git_status.get("stdout", "").strip().splitlines()[1:])
            if git_status.get("stdout")
            else None,
        },
        "inventory": {
            "total_files": len(files),
            "by_suffix": count_by_suffix(files),
            "python_files": len(python_files),
            "test_files": len(test_files),
            "markdown_files": len(markdown_files),
            "docs_files": len(docs_files),
            "skill_files": len(skill_files),
            "gateway_platform_adapters": count_matching(
                files,
                lambda rel: rel.as_posix().startswith("gateway/platforms/") and rel.suffix == ".py",
            ),
            "cron_files": count_matching(files, lambda rel: rel.as_posix().startswith("cron/")),
            "mcp_mentions_files": count_matching(files, lambda rel: "mcp" in rel.as_posix().lower()),
        },
        "subsystems": subsystem_inventory(repo),
        "project_markers": project_markers(repo),
        "test_targets": discover_test_targets(files),
        "skill_frontmatter_health": skill_frontmatter_health(repo, skill_files),
        "safe_config_files": discover_safe_config_files(files),
        "suggested_review_commands": suggested_review_commands(repo),
        "top_level": sorted({rel.parts[0] for rel in files if rel.parts}),
        "notes": [
            "Collector is read-only except for optional --output write.",
            "Secret-like files are not read; only path existence may be inferred from inventory.",
            "Counts exclude common build/cache/virtualenv directories.",
            "Suggested commands are not executed by the collector; reviewers decide which are safe and relevant.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Hermes Engineering Review evidence as JSON")
    parser.add_argument("--repo", default=".", help="Hermes repository root or subdirectory")
    parser.add_argument("--output", help="Optional JSON output path")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser()
    if not repo.exists():
        print(json.dumps({"error": f"repo path does not exist: {repo}"}, indent=2), file=sys.stderr)
        return 2

    data = collect(repo)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)

    if args.output:
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = repo.resolve() / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(str(output))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
