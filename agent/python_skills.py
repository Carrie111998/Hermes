"""Python-backed skills — code-based skills callable as typed functions.

Python-backed skills live alongside the existing markdown (SKILL.md) skills.
A Python-backed skill is a directory under ``~/.hermes/skills/`` that contains
a ``_skill.py`` file instead of (or alongside) a ``SKILL.md``.

Directory structure::

    skills/
    └── my-python-skill/
        ├── _skill.py          # Required: defines the skill's callable interface
        ├── _skill.md          # Optional: markdown instructions (shown in index)
        ├── references/        # Optional: supporting files
        └── scripts/           # Optional: helper scripts

_skill.py must define a ``SKILL_INFO`` dict and at least one async function
prefixed with ``skill_`` or decorated with ``@python_skill``.

Example::

    # skills/my-web-search/_skill.py
    from agent.python_skills import python_skill, PythonSkillInterface

    @python_skill
    async def web_search(query: str, max_results: int = 5) -> dict:
        \"\"\"Search the web and return results.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.

        Returns:
            Dict with 'results' (list) and 'query' (str).
        \"\"\"
        # Implementation using web_search tool or API
        return {"results": [], "query": query}

    SKILL_INFO = {
        "name": "my-web-search",
        "description": "Search the web programmatically",
        "version": "1.0.0",
    }

Invocation
----------
Python-backed skills are exposed to the agent in two ways:

1. **System prompt index**: Listed in ``<available_skills>`` alongside
   markdown skills, with their callable function signatures.

2. **Slash command dispatch**: Invoked via ``/<skill-name>`` just like
   markdown skills. The agent receives the skill's instructions + the
   list of callable functions with their signatures.

The agent can then call these functions using the ``execute_code`` tool
or directly via the Hermes tool interface (when wired up).

Architecture
------------
- Discovery: Scans ``~/.hermes/skills/*/`` for ``_skill.py`` files.
- Loading: Imports the module once, extracts ``SKILL_INFO`` and callable
  functions. Cached in-process.
- Invocation: Functions are registered as Hermes tools (when possible)
  or made available via the ``execute_code`` tool with a typed wrapper.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from hermes_constants import get_skills_dir

logger = logging.getLogger(__name__)

# ── Data structures ───────────────────────────────────────────────────────


@dataclass
class PythonSkillFunction:
    """A single callable function from a Python-backed skill.

    Attributes:
        name: The function name (without ``skill_`` prefix when displayed).
        func: The actual async/sync callable.
        description: Docstring description.
        signature: Human-readable function signature string.
        is_async: Whether the function is async.
        parameters: List of parameter dicts for the function signature.
    """
    name: str
    func: Callable
    description: str
    signature: str
    is_async: bool
    parameters: List[Dict[str, Any]]


@dataclass
class PythonSkillInfo:
    """Metadata and callable interface for a Python-backed skill.

    Attributes:
        name: Skill name (directory name).
        description: Short description.
        version: Optional version string.
        path: Absolute path to the skill directory.
        functions: List of callable functions.
        instructions: Markdown instructions from _skill.md or SKILL.md.
        tags: Optional tags for categorization.
        conditions: Optional conditions for when the skill should be shown.
        config: Optional config vars from frontmatter.
    """
    name: str
    description: str
    version: Optional[str] = None
    path: Optional[Path] = None
    functions: List[PythonSkillFunction] = field(default_factory=list)
    instructions: str = ""
    tags: List[str] = field(default_factory=list)
    conditions: Dict[str, Any] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)


# ── Decorator ──────────────────────────────────────────────────────────────

_PYTHON_SKILL_MARKER = "__python_skill__"


def python_skill(func: Callable) -> Callable:
    """Decorate a function to mark it as a Python-backed skill function.

    Usage::

        @python_skill
        async def my_function(arg: str) -> dict:
            \"\"\"Description of the function.\"\"\"
            return {}

    Functions without this decorator are still discovered if they start with
    ``skill_`` prefix.

    Args:
        func: The function to mark.

    Returns:
        The original function with a marker attribute.
    """
    setattr(func, _PYTHON_SKILL_MARKER, True)
    return func


# ── Discovery ──────────────────────────────────────────────────────────────

# Cache of loaded Python skills keyed by directory name.
_python_skills_cache: Dict[str, PythonSkillInfo] = {}
_python_skills_mtime: float = 0


def _scan_python_skill_dirs(skills_dir: Path) -> List[Path]:
    """Find all directories containing a ``_skill.py`` file.

    Skips directories in ``EXCLUDED_SKILL_DIRS``.
    """
    from agent.skill_utils import EXCLUDED_SKILL_DIRS

    candidates: List[Path] = []
    if not skills_dir.exists():
        return candidates

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name in EXCLUDED_SKILL_DIRS or entry.name.startswith("."):
            continue
        if (entry / "_skill.py").exists():
            candidates.append(entry)
    return candidates


def _check_python_skill_dir_mtime(skills_dir: Path) -> float:
    """Get the max mtime of all Python skill directories."""
    dirs = _scan_python_skill_dirs(skills_dir)
    mtimes = []
    for d in dirs:
        try:
            mtimes.append(d.stat().st_mtime)
            skill_py = d / "_skill.py"
            if skill_py.exists():
                mtimes.append(skill_py.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0


# ── Loading ────────────────────────────────────────────────────────────────


def _extract_function_info(func: Callable) -> PythonSkillFunction:
    """Extract metadata from a skill function.

    Args:
        func: The function to inspect.

    Returns:
        PythonSkillInfo with extracted metadata.
    """
    is_async = inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)
    sig = inspect.signature(func)

    # Build human-readable signature
    params = []
    sig_parts = []
    for name, param in sig.parameters.items():
        param_info: Dict[str, Any] = {"name": name}
        if param.annotation != inspect.Parameter.empty:
            param_info["type"] = _type_to_str(param.annotation)
        if param.default != inspect.Parameter.empty:
            param_info["default"] = repr(param.default)
        params.append(param_info)

        if param.default == inspect.Parameter.empty:
            sig_parts.append(name)
        else:
            sig_parts.append(f"{name}={param.default}")

    signature_str = f"({', '.join(sig_parts)})"

    # Get docstring as description
    description = (inspect.getdoc(func) or "").strip()

    # Normalize function name: strip skill_ prefix for display
    display_name = func.__name__
    if display_name.startswith("skill_"):
        display_name = display_name[6:]

    return PythonSkillFunction(
        name=display_name,
        func=func,
        description=description,
        signature=f"{func.__name__}{signature_str}",
        is_async=is_async,
        parameters=params,
    )


def _type_to_str(tp: Any) -> str:
    """Convert a type annotation to a readable string."""
    if tp is type(None):
        return "None"
    if hasattr(tp, "__name__"):
        return tp.__name__
    if hasattr(tp, "__origin__"):
        origin = tp.__origin__
        if hasattr(origin, "__name__"):
            args = ", ".join(_type_to_str(a) for a in getattr(tp, "__args__", ()))
            return f"{origin.__name__}[{args}]"
        return str(tp)
    return str(tp)


def _load_python_skill_from_dir(skill_dir: Path) -> Optional[PythonSkillInfo]:
    """Load a Python-backed skill from a directory.

    Args:
        skill_dir: Path to the skill directory.

    Returns:
        PythonSkillInfo or None if loading failed.
    """
    skill_name = skill_dir.name
    skill_py = skill_dir / "_skill.py"

    if not skill_py.exists():
        return None

    try:
        # Load module from file
        spec = importlib.util.spec_from_file_location(
            f"hermes_python_skill_{skill_name}", skill_py
        )
        if spec is None or spec.loader is None:
            logger.warning("Could not create module spec for %s", skill_py)
            return None

        module = importlib.util.module_from_spec(spec)

        # Prevent this module from being found in sys.modules for future imports
        # (it's a one-shot load)
        sys.modules[f"hermes_python_skill_{skill_name}"] = module

        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            logger.warning(
                "Failed to load Python skill %s: %s", skill_name, exc, exc_info=True
            )
            return None

        # Extract SKILL_INFO
        skill_info_dict = getattr(module, "SKILL_INFO", {})
        if not isinstance(skill_info_dict, dict):
            logger.warning(
                "Python skill %s: SKILL_INFO is not a dict", skill_name
            )
            return None

        name = str(skill_info_dict.get("name", skill_name))
        description = str(skill_info_dict.get("description", ""))

        if not description:
            description = f"Python-backed skill: {name}"

        version = skill_info_dict.get("version")
        tags = skill_info_dict.get("tags", [])
        conditions = skill_info_dict.get("conditions", {})
        config = skill_info_dict.get("config", {})

        if not isinstance(tags, list):
            tags = [tags]
        if not isinstance(conditions, dict):
            conditions = {}
        if not isinstance(config, dict):
            config = {}

        # Discover callable functions
        functions: List[PythonSkillFunction] = []
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if not callable(obj):
                continue
            if not inspect.isfunction(obj) and not inspect.iscoroutinefunction(obj):
                continue

            # Check if marked with decorator or has skill_ prefix
            is_skill_func = (
                getattr(obj, _PYTHON_SKILL_MARKER, False)
                or attr_name.startswith("skill_")
            )
            if not is_skill_func:
                continue

            try:
                func_info = _extract_function_info(obj)
                functions.append(func_info)
            except Exception as exc:
                logger.warning(
                    "Error extracting function info for %s.%s: %s",
                    skill_name, attr_name, exc,
                )

        # Load instructions from _skill.md or SKILL.md
        instructions = ""
        for md_name in ("_skill.md", "SKILL.md"):
            md_path = skill_dir / md_name
            if md_path.exists():
                try:
                    instructions = md_path.read_text(encoding="utf-8").strip()
                    break
                except Exception as exc:
                    logger.warning(
                        "Could not read %s: %s", md_path, exc
                    )

        return PythonSkillInfo(
            name=name,
            description=description,
            version=str(version) if version else None,
            path=skill_dir,
            functions=functions,
            instructions=instructions,
            tags=tags,
            conditions=conditions,
            config=config,
        )

    except Exception as exc:
        logger.warning(
            "Failed to load Python skill %s: %s", skill_name, exc, exc_info=True
        )
        return None


# ── Public API ─────────────────────────────────────────────────────────────


def scan_python_skills() -> Dict[str, PythonSkillInfo]:
    """Scan for all Python-backed skills and load them.

    Returns:
        Dict mapping skill name to PythonSkillInfo.
    """
    skills_dir = get_skills_dir()
    try:
        from agent.skill_utils import get_external_skills_dirs
        external_dirs = get_external_skills_dirs()
    except Exception:
        external_dirs = []
    all_dirs = [skills_dir] + external_dirs

    result: Dict[str, PythonSkillInfo] = {}
    for dir_path in all_dirs:
        for skill_dir in _scan_python_skill_dirs(dir_path):
            info = _load_python_skill_from_dir(skill_dir)
            if info:
                # Local skills take precedence over external
                if dir_path == skills_dir or info.name not in result:
                    result[info.name] = info
    return result


def get_python_skills() -> Dict[str, PythonSkillInfo]:
    """Return cached Python skills, rescanning if needed.

    Returns:
        Dict mapping skill name to PythonSkillInfo.
    """
    global _python_skills_cache, _python_skills_mtime

    skills_dir = get_skills_dir()
    current_mtime = _check_python_skill_dir_mtime(skills_dir)

    # Rescan if any Python skill directory has changed
    if not _python_skills_cache or current_mtime > _python_skills_mtime:
        _python_skills_cache = scan_python_skills()
        _python_skills_mtime = current_mtime

    return _python_skills_cache


def get_python_skill(name: str) -> Optional[PythonSkillInfo]:
    """Get a Python-backed skill by name.

    Args:
        name: The skill name (case-insensitive, slug-normalized).

    Returns:
        PythonSkillInfo or None.
    """
    skills = get_python_skills()
    for info in skills.values():
        if info.name.lower().replace("-", "").replace("_", "") == name.lower().replace("-", "").replace("_", ""):
            return info
    return None


def reload_python_skills() -> Dict[str, Any]:
    """Re-scan Python skills and return a diff.

    Returns:
        Dict with 'added', 'removed', 'unchanged', 'total'.
    """
    before = {info.name for info in _python_skills_cache.values()}
    new = scan_python_skills()
    after = {info.name for info in new.values()}

    added_names = sorted(after - before)
    removed_names = sorted(before - after)
    unchanged_names = sorted(after & before)

    return {
        "added": [{"name": n} for n in added_names],
        "removed": [{"name": n} for n in removed_names],
        "unchanged": unchanged_names,
        "total": len(after),
    }


def build_python_skill_message(skill_info: PythonSkillInfo) -> str:
    """Build the agent-facing message for a Python-backed skill.

    This message is injected into the conversation when the skill is invoked,
    containing the skill's instructions and the callable function signatures.

    Args:
        skill_info: The loaded Python skill info.

    Returns:
        Formatted message string.
    """
    lines = [
        f"[IMPORTANT: The user has invoked the \"{skill_info.name}\" Python skill. "
        f"Follow its instructions and use the callable functions below.]",
        "",
    ]

    # Instructions
    if skill_info.instructions:
        lines.append("## Instructions")
        lines.append(skill_info.instructions)
        lines.append("")

    # Callable functions
    if skill_info.functions:
        lines.append("## Callable Functions")
        lines.append(
            "The following functions are available for invocation. "
            "Use the execute_code tool to call them, or call them directly "
            "if the Hermes tool interface supports Python skills."
        )
        lines.append("")
        for func in skill_info.functions:
            lines.append(f"### `{func.signature}`")
            if func.description:
                lines.append(func.description)
            if func.parameters:
                lines.append("")
                lines.append("**Parameters:**")
                for p in func.parameters:
                    default = f" (default: {p.get('default', 'N/A')})" if 'default' in p else ""
                    type_str = p.get('type', 'any')
                    lines.append(f"- `{p['name']}` ({type_str}){default}")
            lines.append("")

    # Skill directory
    if skill_info.path:
        lines.append(f"[Skill directory: {skill_info.path}]")

    return "\n".join(lines)


def build_python_skills_index() -> str:
    """Build the skills index section for Python-backed skills.

    Returns a string suitable for inclusion in the system prompt's
    ``<available_skills>`` block.

    Returns:
        Formatted index string, or empty string if no Python skills.
    """
    skills = get_python_skills()
    if not skills:
        return ""

    lines = ["  **Python-backed skills:**"]
    for name in sorted(skills.keys()):
        info = skills[name]
        desc = info.description or f"Python skill: {name}"
        lines.append(f"    - {name}: {desc}")
    return "\n".join(lines)


def get_python_skill_slash_commands() -> Dict[str, Dict[str, Any]]:
    """Return Python-backed skills as slash commands.

    Returns:
        Dict mapping ``/command-name`` to command info dict, compatible
        with the format used by ``skill_commands.scan_skill_commands()``.
    """
    skills = get_python_skills()
    commands: Dict[str, Dict[str, Any]] = {}

    for name, info in skills.items():
        cmd_name = name.lower().replace(" ", "-").replace("_", "-")
        commands[f"/{cmd_name}"] = {
            "name": info.name,
            "description": info.description or f"Invoke the {info.name} Python skill",
            "skill_dir": str(info.path) if info.path else "",
            "python_skill": True,
            "python_skill_info": info,
        }

    return commands
