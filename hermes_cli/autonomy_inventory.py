"""Read-only, redacted inventory of Hermes autonomy surfaces."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from agent.skill_utils import is_excluded_skill_path, parse_frontmatter
from hermes_cli.config import read_raw_config
from hermes_constants import (
    get_env_path,
    get_hermes_home,
    get_skills_dir,
)


_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|auth|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_ENV_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD))"
    r"(\s*=\s*)([^\s,;]+)"
)
_CLI_SECRET = re.compile(
    r"(?i)(--(?:api[-_]?key|key|token|secret|passw(?:or)?d|credentials?|auth"
    r"|authorization)(?:\s+|=))([^\s,;]+)"
)
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/@\s]+@")
_URL_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:api[-_]?key|key|token|secret|password)=)([^&#\s]+)"
)
_TEXT_SECRET_FIELD = re.compile(
    r"""(?ix)
    (
        ["']?(?:api[-_]?key|key|token|secret|password|authorization)["']?
        \s*[:=]\s*
    )
    (
        ["'][^"']*["'] | [^\s,}\]]+
    )
    """
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\s,;\"']+"
)
_WINDOWS_UNC_PATH = re.compile(
    r"(?<!\\)\\\\[^\\\s,;\"']+\\[^\\\s,;\"']+(?:\\[^\s,;\"']*)?"
)
_UNIX_ABSOLUTE_PATH = re.compile(
    r"(?<![:/A-Za-z0-9_])/(?!/)[^/\s,;\"']+/[^\s,;\"']*"
)
_RECOMMENDED_SECTIONS = (
    "objective",
    "preconditions",
    "procedure",
    "error handling",
    "success criteria",
    "tests",
)
_SENSITIVE_OPTIONS = frozenset(
    {
        "--token",
        "--api-key",
        "--apikey",
        "--key",
        "--secret",
        "--password",
        "--passwd",
        "--credential",
        "--credentials",
        "--auth",
        "--authorization",
        "-p",
    }
)


def _redact_string(value: str, known_values: frozenset[str]) -> str:
    result = value
    for secret in sorted(known_values, key=len, reverse=True):
        if secret:
            result = result.replace(secret, "<redacted>")
    result = _URL_USERINFO.sub(r"\1<redacted>@", result)
    result = _URL_SECRET_QUERY.sub(r"\1<redacted>", result)
    result = _BEARER_VALUE.sub("Bearer <redacted>", result)
    result = _ENV_ASSIGNMENT.sub(r"\1\2<redacted>", result)
    result = _CLI_SECRET.sub(r"\1<redacted>", result)
    result = _TEXT_SECRET_FIELD.sub(r"\1<redacted>", result)
    result = _WINDOWS_UNC_PATH.sub("<absolute-path>", result)
    result = _WINDOWS_ABSOLUTE_PATH.sub("<absolute-path>", result)
    result = _UNIX_ABSOLUTE_PATH.sub("<absolute-path>", result)
    return result


def _redact(value: Any, known_values: frozenset[str] = frozenset()) -> Any:
    """Recursively redact sensitive keys and secret-shaped string fragments."""
    if isinstance(value, dict):
        return {
            str(key): "<redacted>"
            if _SENSITIVE_KEY.search(str(key))
            else _redact(item, known_values)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        redacted: list[Any] = []
        redact_next = False
        for item in items:
            if redact_next:
                redacted.append("<redacted>")
                redact_next = False
                continue
            if isinstance(item, str):
                option, separator, _option_value = item.partition("=")
                if option.casefold() in _SENSITIVE_OPTIONS:
                    if separator:
                        redacted.append(f"{option}=<redacted>")
                    else:
                        redacted.append(item)
                        redact_next = True
                    continue
            redacted.append(_redact(item, known_values))
        return tuple(redacted) if isinstance(value, tuple) else redacted
    if isinstance(value, str):
        return _redact_string(value, known_values)
    return value


def _redact_mcp(entry: dict[str, Any]) -> dict[str, Any]:
    """Redact all MCP connection material, regardless of operator key names."""
    result = _redact(entry)
    for key in ("url", "command", "args"):
        if key in result:
            result[key] = "<redacted>"
    for key in ("env", "headers"):
        raw = entry.get(key)
        if isinstance(raw, dict):
            result[key] = {str(name): "<redacted>" for name in raw}
    return result


def _env_key_names(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.partition("=")[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            names.add(name)
    return sorted(names)


def _known_env_values(path: Path) -> frozenset[str]:
    values: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        value = line.partition("=")[2].strip().strip("\"'")
        if len(value) >= 4:
            values.add(value)
    for value in os.environ.values():
        if len(value) >= 4:
            values.add(value)
    return frozenset(values)


def _frontmatter_is_yaml(content: str) -> tuple[bool, str | None]:
    if not content.startswith("---"):
        return False, "missing YAML frontmatter"
    match = re.search(r"\n---\s*(?:\n|$)", content[3:])
    if match is None:
        return False, "unterminated YAML frontmatter"
    try:
        parsed = yaml.safe_load(content[3 : match.start() + 3]) or {}
    except yaml.YAMLError as exc:
        return False, str(exc)
    if not isinstance(parsed, dict):
        return False, "frontmatter must be a mapping"
    return True, None


def inventory_skills(skills_dir: Path | None = None) -> list[dict[str, Any]]:
    root = skills_dir or get_skills_dir()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for skill_path in sorted(root.rglob("SKILL.md")):
        if is_excluded_skill_path(skill_path, root=root):
            continue
        try:
            content = skill_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            rows.append(
                {
                    "path": f"skills/{skill_path.parent.name}/SKILL.md",
                    "valid": False,
                    "issues": [type(exc).__name__],
                }
            )
            continue
        yaml_valid, yaml_issue = _frontmatter_is_yaml(content)
        frontmatter, body = parse_frontmatter(content)
        headings = {
            line.lstrip("#").strip().casefold()
            for line in body.splitlines()
            if line.lstrip().startswith("#")
        }
        issues: list[str] = []
        if yaml_issue:
            issues.append(yaml_issue)
        if not frontmatter.get("name"):
            issues.append("missing frontmatter name")
        if not frontmatter.get("description"):
            issues.append("missing frontmatter description")
        rows.append(
            {
                "name": str(frontmatter.get("name") or skill_path.parent.name),
                "path": f"skills/{skill_path.parent.name}/SKILL.md",
                "yaml_valid": yaml_valid,
                "valid": yaml_valid
                and bool(frontmatter.get("name"))
                and bool(frontmatter.get("description")),
                "recommended_sections_present": [
                    section for section in _RECOMMENDED_SECTIONS if section in headings
                ],
                "issues": issues,
            }
        )
    return rows


def inventory_tools() -> dict[str, Any]:
    modules_before = dict(sys.modules)
    try:
        import tools.registry as registry_module

        return _inventory_tools_from_registry(registry_module)
    finally:
        for name in set(sys.modules) - set(modules_before):
            sys.modules.pop(name, None)
        for name, module in modules_before.items():
            if sys.modules.get(name) is not module:
                sys.modules[name] = module


def _inventory_tools_from_registry(registry_module: Any) -> dict[str, Any]:
    """Inspect loaded state and static declarations without persistent imports."""
    registry = registry_module.registry
    tools_dir = Path(registry_module.__file__).resolve().parent
    declared_modules = sorted(
        path.stem
        for path in tools_dir.glob("*.py")
        if path.name not in {"__init__.py", "registry.py", "mcp_tool.py"}
        and registry_module._module_registers_tools(path)
    )
    declared_toolsets: dict[str, set[str]] = {}
    for module_name in declared_modules:
        module_path = tools_dir / f"{module_name}.py"
        tree = ast.parse(
            module_path.read_text(encoding="utf-8"),
            filename=f"tools/{module_name}.py",
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "registry"
                and function.attr == "register"
            ):
                continue
            keywords = {item.arg: item.value for item in node.keywords if item.arg}
            name_node = keywords.get("name")
            toolset_node = keywords.get("toolset")
            if not (
                isinstance(name_node, ast.Constant)
                and isinstance(name_node.value, str)
                and isinstance(toolset_node, ast.Constant)
                and isinstance(toolset_node.value, str)
            ):
                continue
            declared_toolsets.setdefault(toolset_node.value, set()).add(name_node.value)

    toolset_names = set(declared_toolsets) | set(registry.get_registered_toolset_names())
    toolsets = {
        name: {
            "available": None,
            "availability_checked": False,
            "tools": sorted(
                declared_toolsets.get(name, set())
                | set(registry.get_tool_names_for_toolset(name))
            ),
        }
        for name in sorted(toolset_names)
    }
    declared_tool_count = len(
        {tool for tools in declared_toolsets.values() for tool in tools}
    )
    return {
        "declared_modules": declared_modules,
        "loaded_tool_count": len(registry.get_all_tool_names()),
        "tool_count": max(declared_tool_count, len(registry.get_all_tool_names())),
        "toolsets": toolsets,
        "aliases": registry.get_registered_toolset_aliases(),
    }


def inventory_mcp(config: dict[str, Any]) -> list[dict[str, Any]]:
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return []
    rows: list[dict[str, Any]] = []
    for name, value in sorted(servers.items()):
        entry = value if isinstance(value, dict) else {}
        tool_rules = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
        serialized = json.dumps(entry, ensure_ascii=False, default=str)
        rows.append(
            {
                "name": str(name),
                "enabled": entry.get("enabled", True) is not False,
                "transport": entry.get("transport")
                or ("streamable-http" if entry.get("url") else "stdio"),
                "has_url": bool(entry.get("url")),
                "has_command": bool(entry.get("command")),
                "env_refs": sorted(set(_ENV_REFERENCE.findall(serialized))),
                "env_keys": sorted(str(key) for key in entry.get("env", {}))
                if isinstance(entry.get("env"), dict)
                else [],
                "header_keys": sorted(str(key) for key in entry.get("headers", {}))
                if isinstance(entry.get("headers"), dict)
                else [],
                "tool_allowlist": sorted(tool_rules.get("include", []))
                if isinstance(tool_rules.get("include"), list)
                else [],
                "tool_denylist": sorted(tool_rules.get("exclude", []))
                if isinstance(tool_rules.get("exclude"), list)
                else [],
                "redacted_config": _redact_mcp(entry),
            }
        )
    return rows


def _scheduled_files(home: Path) -> list[str]:
    cron_dir = home / "cron"
    if not cron_dir.is_dir():
        return []
    try:
        return sorted(
            str(path.relative_to(cron_dir))
            for path in cron_dir.rglob("*")
            if path.is_file() and "output" not in path.relative_to(cron_dir).parts
        )
    except OSError:
        return []


def build_inventory() -> dict[str, Any]:
    home = Path(get_hermes_home())
    config = read_raw_config()
    env_path = Path(get_env_path())
    env_keys = _env_key_names(env_path)
    known_values = _known_env_values(env_path)
    agent_config = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    return {
        "hermes_home": "hermes_home",
        "config_path": "config.yaml",
        "skills": inventory_skills(),
        "tools": inventory_tools(),
        "mcp_servers": inventory_mcp(config),
        "secrets": {
            "env_file": ".env",
            "env_keys": env_keys,
            "count": len(env_keys),
            "values_redacted": True,
        },
        "permissions": {
            "approvals": _redact(config.get("approvals", {}), known_values),
            "command_allowlist": _redact(
                config.get("command_allowlist", []), known_values
            ),
            "security": _redact(config.get("security", {}), known_values),
            "agent_disabled_toolsets": agent_config.get("disabled_toolsets", []),
        },
        "memory": _redact(config.get("memory", {}), known_values),
        "cron": {
            "config": _redact(config.get("cron", {}), known_values),
            "files": _scheduled_files(home),
        },
        "interfaces": {
            "top_level_toolsets": config.get("toolsets", []),
            "platform_toolsets": config.get("platform_toolsets", {}),
        },
    }


def cmd_security_inventory(args: argparse.Namespace) -> int:
    diagnostics = io.StringIO()
    try:
        with contextlib.redirect_stdout(diagnostics):
            report = build_inventory()
    except Exception as exc:
        captured = diagnostics.getvalue()
        if captured:
            print(captured, file=sys.stderr, end="")
        print(f"security inventory failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    captured = diagnostics.getvalue()
    if captured:
        print(captured, file=sys.stderr, end="")
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        invalid = sum(not row.get("valid", False) for row in report["skills"])
        print("Hermes autonomy inventory")
        print(f"  Skills: {len(report['skills'])} ({invalid} with issues)")
        print(f"  Tools: {report['tools']['tool_count']}")
        print(f"  MCP servers: {len(report['mcp_servers'])}")
        print(f"  Environment keys: {report['secrets']['count']} (values redacted)")
    return 0


__all__ = [
    "build_inventory",
    "cmd_security_inventory",
    "inventory_mcp",
    "inventory_skills",
    "inventory_tools",
]
