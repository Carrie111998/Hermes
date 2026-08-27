"""Compatibility helpers for Agent Plugins v1 portable directory packages.

This module validates the versioned portable format locally and translates its
supported components into records consumed by Hermes' existing skill and MCP
runtimes. It deliberately performs no schema fetching and imports no plugin
Python code.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from agent.skill_utils import yaml_load


PLUGIN_SCHEMA_V1 = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_V1 = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

_PLUGIN_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
_AUTHOR_FIELDS = {"name", "email", "url"}
_STDIO_FIELDS = {"type", "command", "args", "env", "cwd"}
_REMOTE_FIELDS = {"type", "url", "headers"}
_PLUGIN_NAME_RE = re.compile(
    r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
)
_SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9]+(?:-[a-z0-9]+)*$")
_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class AgentPluginError(ValueError):
    """Fatal portable manifest validation failure."""


@dataclass(frozen=True)
class AgentPluginDiagnostic:
    scope: str
    message: str


@dataclass(frozen=True)
class AgentPluginSkill:
    name: str
    description: str
    root: Path
    skill_md: Path
    frontmatter: Mapping[str, Any]


@dataclass(frozen=True)
class AgentPluginPackage:
    name: str
    version: str
    description: str
    root: Path
    data_root: Path
    content_digest: str
    manifest: Mapping[str, Any]
    skills: Tuple[AgentPluginSkill, ...]
    mcp_servers: Mapping[str, Dict[str, Any]]
    diagnostics: Tuple[AgentPluginDiagnostic, ...]


_DIGEST_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_DIGEST_EXCLUDED_FILES = frozenset({".DS_Store"})


def canonical_package_digest(plugin_root: Path) -> str:
    """Hash the exact portable package contents using a stable file framing.

    VCS metadata and generated interpreter/test caches are intentionally not
    authority-bearing.  Everything else is: replacing code, manifests,
    documentation, or bundled assets changes the grant digest.  Symlinks and
    special files fail closed because their meaning is installation-dependent.
    """

    root = Path(plugin_root).resolve(strict=True)
    if not root.is_dir():
        raise AgentPluginError("plugin root must be a directory")
    entries: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            path = current_path / name
            if name in _DIGEST_EXCLUDED_DIRS:
                continue
            if path.is_symlink():
                entries.append((path.relative_to(root).as_posix(), path))
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            path = current_path / name
            if (
                name in _DIGEST_EXCLUDED_FILES
                or name.endswith((".pyc", ".pyo"))
            ):
                continue
            if not path.is_symlink() and not path.is_file():
                raise AgentPluginError(
                    "portable package digest allows regular files only"
                )
            relative = path.relative_to(root).as_posix()
            entries.append((relative, path))

    digest = hashlib.sha256(b"hermes-agent-plugin-package-v1\0")
    for relative, path in sorted(entries):
        encoded_name = relative.encode("utf-8")
        try:
            content = (
                b"symlink-v1\0" + os.readlink(path).encode("utf-8")
                if path.is_symlink()
                else b"file-v1\0" + path.read_bytes()
            )
        except (OSError, UnicodeError) as exc:
            raise AgentPluginError(
                f"portable package file cannot be read: {relative}"
            ) from exc
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _package_contains_symlink(root: Path) -> bool:
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in (*dirs, *files):
            if (current_path / name).is_symlink():
                return True
    return False


def package_has_executable_cache(plugin_root: Path) -> bool:
    """Return true when Python could execute ungranted cached bytecode."""

    root = Path(plugin_root).resolve(strict=True)
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in dirs:
            if name in {".git", ".hg", ".svn"}:
                continue
            if name == "__pycache__":
                return True
            kept.append(name)
        dirs[:] = kept
        if any(name.endswith((".pyc", ".pyo")) for name in files):
            return True
        # A symlink named like executable cache is authority-unsafe too.
        if any(
            (current_path / name).is_symlink()
            and (name == "__pycache__" or name.endswith((".pyc", ".pyo")))
            for name in (*dirs, *files)
        ):
            return True
    return False


def _harden_capability_launch(
    server: Dict[str, Any], plugin_root: Path
) -> str | None:
    """Pin a privileged stdio launch to reviewed package or host bytes.

    Returns a diagnostic message on failure. Direct Python launches are
    rewritten away from mutable ``PATH`` resolution to the interpreter already
    in Hermes' TCB. Other runtimes must be an executable regular file reached
    through the package's translated ``./...`` command form and therefore be
    included in the package digest.
    """

    command = server.get("command")
    if not isinstance(command, str) or not command:
        return "sessionCapability requires a pinned stdio executable"
    command_path = Path(command)
    python_name = re.fullmatch(
        r"python(?:[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?",
        command.lower(),
    )
    if not command_path.is_absolute() and python_name is not None:
        args = server.get("args")
        env = server.get("env")
        if not (
            isinstance(args, list)
            and len(args) >= 4
            and args[:3] == ["-I", "-S", "-B"]
            and isinstance(env, Mapping)
            and env.get("PYTHONDONTWRITEBYTECODE") == "1"
        ):
            return (
                "sessionCapability Python servers require -I -S -B, an in-package "
                "script, and PYTHONDONTWRITEBYTECODE=1"
            )
        script = Path(args[3])
        if (
            not script.is_absolute()
            or not _inside(script, plugin_root)
            or script.is_symlink()
            or not script.is_file()
        ):
            return "sessionCapability Python script must be a regular in-package file"
        try:
            host_python = Path(sys.executable).resolve(strict=True)
        except OSError:
            return "sessionCapability host Python executable is unavailable"
        if not host_python.is_file() or not os.access(host_python, os.X_OK):
            return "sessionCapability host Python executable is unavailable"
        server["command"] = str(host_python)
        return None

    if not command_path.is_absolute():
        return "sessionCapability rejects bare ambient executables"
    if (
        not _inside(command_path, plugin_root)
        or command_path.is_symlink()
        or not command_path.is_file()
        or not os.access(command_path, os.X_OK)
    ):
        return "sessionCapability executable must be a regular in-package file"
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _read_json_object(path: Path, *, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentPluginError(f"{label} is not valid readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentPluginError(f"{label} must contain a JSON object")
    return value


def _validate_manifest(root: Path) -> tuple[dict, list[AgentPluginDiagnostic]]:
    manifest_path = root / "plugin.json"
    if not _inside(manifest_path, root) or not manifest_path.is_file():
        raise AgentPluginError("plugin.json must be a regular file within the plugin root")
    manifest = _read_json_object(manifest_path, label="plugin.json")
    diagnostics: list[AgentPluginDiagnostic] = []

    for field in sorted(set(manifest) - _PLUGIN_FIELDS):
        diagnostics.append(
            AgentPluginDiagnostic("manifest", f"ignored unknown top-level field: {field}")
        )
        manifest.pop(field)

    if manifest.get("$schema") != PLUGIN_SCHEMA_V1:
        raise AgentPluginError(
            "plugin.json declares an unsupported or missing Agent Plugins schema"
        )
    name = manifest.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or _PLUGIN_NAME_RE.fullmatch(name) is None
    ):
        raise AgentPluginError("plugin.json name does not satisfy v1 constraints")

    for field in ("version", "description", "homepage", "repository", "license"):
        if field in manifest and not isinstance(manifest[field], str):
            raise AgentPluginError(f"plugin.json {field} must be a string")

    if "keywords" in manifest:
        keywords = manifest["keywords"]
        if not isinstance(keywords, list) or any(
            not isinstance(value, str) for value in keywords
        ):
            raise AgentPluginError("plugin.json keywords must be an array of strings")

    if "author" in manifest:
        author = manifest["author"]
        if not isinstance(author, dict):
            raise AgentPluginError("plugin.json author must be an object")
        unknown = set(author) - _AUTHOR_FIELDS
        if unknown or any(not isinstance(value, str) for value in author.values()):
            raise AgentPluginError(
                "plugin.json author may contain only string name, email, and url fields"
            )

    if "extensions" in manifest:
        extensions = manifest["extensions"]
        if not isinstance(extensions, dict):
            diagnostics.append(
                AgentPluginDiagnostic(
                    "manifest", "ignored non-object extensions field"
                )
            )
            manifest.pop("extensions")
        elif any(not isinstance(value, dict) for value in extensions.values()):
            raise AgentPluginError("plugin.json extension namespace values must be objects")

    return manifest, diagnostics


def _valid_skill_frontmatter(
    frontmatter: Mapping[str, Any], directory_name: str
) -> str | None:
    name = frontmatter.get("name")
    if (
        not isinstance(name, str)
        or name != directory_name
        or not 1 <= len(name) <= 64
        or _SKILL_NAME_RE.fullmatch(name) is None
    ):
        return "name must match the directory and satisfy Agent Skills constraints"
    description = frontmatter.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        return "description must be a non-empty string of at most 1024 characters"
    if "license" in frontmatter and not isinstance(frontmatter["license"], str):
        return "license must be a string"
    if "compatibility" in frontmatter:
        compatibility = frontmatter["compatibility"]
        if not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500:
            return "compatibility must be a string of 1 to 500 characters"
    if "metadata" in frontmatter:
        metadata = frontmatter["metadata"]
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            return "metadata must map string keys to string values"
    if "allowed-tools" in frontmatter and not isinstance(
        frontmatter["allowed-tools"], str
    ):
        return "allowed-tools must be a string"
    return None


def _discover_skills(
    root: Path, diagnostics: list[AgentPluginDiagnostic]
) -> tuple[AgentPluginSkill, ...]:
    skills_root = root / "skills"
    if not skills_root.exists() and not skills_root.is_symlink():
        return ()
    if not _inside(skills_root, root) or not skills_root.is_dir():
        diagnostics.append(
            AgentPluginDiagnostic("skills", "skills must be an in-root directory")
        )
        return ()

    skills: list[AgentPluginSkill] = []
    try:
        children = sorted(skills_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        diagnostics.append(AgentPluginDiagnostic("skills", f"cannot list skills: {exc}"))
        return ()

    for child in children:
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.exists():
            continue
        scope = f"skill:{child.name}"
        if not _inside(skill_md, root) or not skill_md.is_file():
            diagnostics.append(
                AgentPluginDiagnostic(scope, "SKILL.md must be a regular in-root file")
            )
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            content = content.lstrip("\ufeff")
            if not content.startswith("---"):
                raise ValueError("missing YAML frontmatter")
            end_match = re.search(r"\n---\s*\n", content[3:])
            if end_match is None:
                raise ValueError("unterminated YAML frontmatter")
            try:
                parsed = yaml_load(content[3 : end_match.start() + 3])
            except Exception as exc:
                raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("YAML frontmatter must be an object")
            frontmatter = parsed
        except (OSError, UnicodeError, ValueError) as exc:
            diagnostics.append(AgentPluginDiagnostic(scope, f"invalid SKILL.md: {exc}"))
            continue
        error = _valid_skill_frontmatter(frontmatter, child.name)
        if error:
            diagnostics.append(AgentPluginDiagnostic(scope, error))
            continue
        skills.append(
            AgentPluginSkill(
                name=child.name,
                description=frontmatter["description"],
                root=child.resolve(strict=True),
                skill_md=skill_md.resolve(strict=True),
                frontmatter=dict(frontmatter),
            )
        )
    return tuple(skills)


def _expand(value: str, plugin_root: Path, data_root: Path) -> str:
    replacements = {
        "PLUGIN_ROOT": str(plugin_root),
        "PLUGIN_DATA": str(data_root),
    }
    return _PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], value)


def _resolve_scoped_path(
    value: str,
    plugin_root: Path,
    data_root: Path,
    *,
    expand_placeholders: bool = True,
) -> Path:
    expanded = _expand(value, plugin_root, data_root) if expand_placeholders else value
    if value.startswith("./"):
        base = plugin_root
        candidate = base / expanded[2:]
    elif value == "${PLUGIN_ROOT}" or value.startswith("${PLUGIN_ROOT}/"):
        base = plugin_root
        candidate = Path(expanded)
    elif value == "${PLUGIN_DATA}" or value.startswith("${PLUGIN_DATA}/"):
        base = data_root
        candidate = Path(expanded)
    else:
        raise ValueError("path must start with ./, ${PLUGIN_ROOT}, or ${PLUGIN_DATA}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("path escapes its resolved root") from exc
    return resolved


def _validate_headers(headers: object) -> bool:
    if headers is None:
        return True
    if not isinstance(headers, dict):
        return False
    seen: set[str] = set()
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or _HEADER_NAME_RE.fullmatch(name) is None
            or not isinstance(value, str)
            or "\r" in value
            or "\n" in value
            or name.lower() in seen
        ):
            return False
        seen.add(name.lower())
    return True


def _validate_remote_url(url: object) -> str:
    """Validate a portable remote MCP URL per the v1 spec and return it.

    Rules (Agent Plugins v1 §7.2.1): absolute http(s) URL, no user
    information, no fragment; non-loopback endpoints must use HTTPS. HTTP is
    allowed only when the host is exactly ``localhost`` or an IP literal in a
    loopback range. No placeholder or environment expansion is performed.
    """

    from urllib.parse import urlsplit

    if not isinstance(url, str) or not url:
        raise ValueError("url must be a non-empty string")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError(f"url is not parseable: {exc}") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("url scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not contain user information")
    if parsed.fragment:
        raise ValueError("url must not contain a fragment")
    host = parsed.hostname
    if not host:
        raise ValueError("url must have a host")
    if scheme == "http":
        loopback = False
        if host == "localhost":
            loopback = True
        else:
            import ipaddress

            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValueError("non-loopback url must use https")
    return url


def _translate_remote(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Translate a portable ``streamable-http`` entry into native MCP config.

    The returned record targets Hermes' existing URL-based MCP runtime.
    ``strict_redirect_headers`` instructs the runtime to drop the configured
    headers on any cross-origin redirect, which the v1 spec requires for
    portable packages (configured headers must not be forwarded to a
    different origin without explicit user authorization).
    """

    if set(config) - _REMOTE_FIELDS:
        raise ValueError("unknown remote field")
    url = _validate_remote_url(config.get("url"))
    if not _validate_headers(config.get("headers")):
        raise ValueError("invalid headers")
    translated: Dict[str, Any] = {
        "url": url,
        "strict_redirect_headers": True,
    }
    headers = config.get("headers")
    if headers:
        translated["headers"] = dict(headers)
    return translated


def _translate_stdio(
    config: Mapping[str, Any], plugin_root: Path, data_root: Path
) -> Dict[str, Any]:
    if set(config) - _STDIO_FIELDS:
        raise ValueError("unknown stdio field")
    command = config.get("command")
    if not isinstance(command, str) or not command or "\x00" in command:
        raise ValueError("command must be a non-empty executable token")
    if command.startswith("./"):
        command_value = str(
            _resolve_scoped_path(
                command,
                plugin_root,
                data_root,
                expand_placeholders=False,
            )
        )
    elif any(character.isspace() for character in command):
        raise ValueError("command must contain one executable token")
    elif "/" in command or "\\" in command or command in {".", ".."}:
        raise ValueError("command must be a bare executable or begin with ./")
    else:
        command_value = command

    args = config.get("args", [])
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        raise ValueError("args must be an array of strings")
    env = config.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise ValueError("env must map string keys to string values")
    env_keys = {key.upper() if os.name == "nt" else key for key in env}
    if "PLUGIN_ROOT" in env_keys or "PLUGIN_DATA" in env_keys:
        raise ValueError("PLUGIN_ROOT and PLUGIN_DATA are reserved")

    cwd = config.get("cwd")
    if cwd is None:
        cwd_value = plugin_root
    elif not isinstance(cwd, str):
        raise ValueError("cwd must be a string")
    else:
        cwd_value = _resolve_scoped_path(cwd, plugin_root, data_root)

    translated_env = {
        key: _expand(value, plugin_root, data_root) for key, value in env.items()
    }
    translated_env["PLUGIN_ROOT"] = str(plugin_root)
    translated_env["PLUGIN_DATA"] = str(data_root)
    return {
        "command": command_value,
        "args": [_expand(value, plugin_root, data_root) for value in args],
        "env": translated_env,
        "cwd": str(cwd_value),
    }


def _discover_mcp(
    root: Path,
    data_root: Path,
    diagnostics: list[AgentPluginDiagnostic],
    *,
    create_data: bool = True,
) -> Dict[str, Dict[str, Any]]:
    mcp_path = root / "mcp.json"
    if not mcp_path.exists() and not mcp_path.is_symlink():
        return {}
    if not _inside(mcp_path, root) or not mcp_path.is_file():
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcp.json must be a regular in-root file")
        )
        return {}
    try:
        config = _read_json_object(mcp_path, label="mcp.json")
    except AgentPluginError as exc:
        diagnostics.append(AgentPluginDiagnostic("mcp", str(exc)))
        return {}
    if set(config) != {"$schema", "mcpServers"}:
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcp.json has an invalid top-level shape")
        )
        return {}
    if config.get("$schema") != MCP_SCHEMA_V1:
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcp.json declares an unsupported schema")
        )
        return {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcpServers must be an object")
        )
        return {}

    translated: Dict[str, Dict[str, Any]] = {}
    for name, server in servers.items():
        scope = f"mcp:{name}"
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            diagnostics.append(AgentPluginDiagnostic(scope, "invalid server entry"))
            continue
        server_type = server.get("type")
        if server_type == "stdio":
            try:
                translated_server = _translate_stdio(server, root, data_root)
                if create_data:
                    data_root.mkdir(parents=True, exist_ok=True)
                    cwd_path = Path(translated_server["cwd"])
                    try:
                        cwd_path.relative_to(data_root)
                    except ValueError:
                        pass
                    else:
                        # The MCP client starts stdio servers with this cwd.
                        # Create only data-root descendants; plugin-root paths
                        # remain package-owned and are never made writable as a
                        # side effect of discovery.
                        cwd_path.mkdir(parents=True, exist_ok=True)
                translated[name] = translated_server
            except (OSError, ValueError) as exc:
                diagnostics.append(AgentPluginDiagnostic(scope, str(exc)))
        elif server_type == "streamable-http":
            try:
                translated[name] = _translate_remote(server)
            except ValueError as exc:
                diagnostics.append(AgentPluginDiagnostic(scope, str(exc)))
        elif server_type == "sse":
            if (
                set(server) - _REMOTE_FIELDS
                or not isinstance(server.get("url"), str)
                or not server.get("url")
                or not _validate_headers(server.get("headers"))
            ):
                diagnostics.append(AgentPluginDiagnostic(scope, "invalid remote entry"))
            else:
                diagnostics.append(
                    AgentPluginDiagnostic(
                        scope,
                        f"portable {server_type} transport is not supported",
                    )
                )
        else:
            diagnostics.append(AgentPluginDiagnostic(scope, "unknown MCP server type"))
    return translated


def load_agent_plugin(plugin_root: Path, data_root: Path) -> AgentPluginPackage:
    """Validate and translate one installed Agent Plugins v1 package.

    Fatal manifest errors raise :class:`AgentPluginError`. Component and entry
    failures are returned as diagnostics and isolated to their owning scope.
    """

    root = Path(plugin_root).resolve(strict=True)
    if not root.is_dir():
        raise AgentPluginError("plugin root must be a directory")
    content_digest = canonical_package_digest(root)
    manifest, diagnostics = _validate_manifest(root)
    resolved_data = Path(data_root).resolve(strict=False)
    skills = _discover_skills(root, diagnostics)
    mcp_servers = _discover_mcp(root, resolved_data, diagnostics)
    # Hermes-specific request for a host-minted, call-bound capability.  The
    # package cannot grant this privilege to itself: PluginManager separately
    # requires a profile grant bound to this exact content digest.
    hermes_extension = (manifest.get("extensions") or {}).get("com.hermes")
    capability_names: set[str] = set()
    if isinstance(hermes_extension, dict):
        raw_capability = hermes_extension.get("sessionCapability")
        if isinstance(raw_capability, list) and all(
            isinstance(value, str) and value for value in raw_capability
        ):
            capability_names = set(raw_capability)
        elif raw_capability is not None:
            diagnostics.append(
                AgentPluginDiagnostic(
                    "manifest",
                    "com.hermes.sessionCapability must be an array of MCP server names",
                )
            )
        if "forwardSessionContext" in hermes_extension:
            diagnostics.append(
                AgentPluginDiagnostic(
                    "manifest",
                    "com.hermes.forwardSessionContext is unsupported; request sessionCapability instead",
                )
            )
    for server_name in capability_names:
        server = mcp_servers.get(server_name)
        if server is None:
            diagnostics.append(
                AgentPluginDiagnostic(
                    f"mcp:{server_name}",
                    "sessionCapability names no valid packaged MCP server",
                )
            )
            continue
        if _package_contains_symlink(root):
            diagnostics.append(
                AgentPluginDiagnostic(
                    f"mcp:{server_name}",
                    "sessionCapability is unavailable for packages containing symlinks",
                )
            )
            continue
        if package_has_executable_cache(root):
            diagnostics.append(
                AgentPluginDiagnostic(
                    f"mcp:{server_name}",
                    "sessionCapability is unavailable while Python bytecode caches exist",
                )
            )
            continue
        launch_error = _harden_capability_launch(server, root)
        if launch_error is not None:
            diagnostics.append(
                AgentPluginDiagnostic(
                    f"mcp:{server_name}",
                    launch_error,
                )
            )
            continue
        server["request_session_capability"] = True
    return AgentPluginPackage(
        name=manifest["name"],
        version=manifest.get("version", ""),
        description=manifest.get("description", ""),
        root=root,
        data_root=resolved_data,
        content_digest=content_digest,
        manifest=dict(manifest),
        skills=skills,
        mcp_servers=mcp_servers,
        diagnostics=tuple(diagnostics),
    )


def read_agent_plugin_manifest(plugin_root: Path) -> tuple[dict, tuple[AgentPluginDiagnostic, ...]]:
    """Validate only root ``plugin.json`` without discovering components."""

    root = Path(plugin_root).resolve(strict=True)
    if not root.is_dir():
        raise AgentPluginError("plugin root must be a directory")
    manifest, diagnostics = _validate_manifest(root)
    return manifest, tuple(diagnostics)


def has_enabled_agent_plugin_mcp(raw_config: Mapping[str, Any]) -> bool:
    """Compatibility wrapper for the shared PluginManager MCP probe.

    Directory scanning belongs to :mod:`hermes_cli.plugins` so startup gating
    and full plugin discovery cannot drift apart. Keep this import-compatible
    entry point for callers that used the original helper.
    """

    from hermes_cli.plugins import has_enabled_agent_plugin_mcp as _probe

    return _probe(raw_config)
