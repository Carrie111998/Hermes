"""Shared SKILL.md preprocessing helpers."""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from hermes_cli._subprocess_compat import IS_WINDOWS, windows_hide_flags

logger = logging.getLogger(__name__)

# Matches ${HERMES_SKILL_DIR} / ${HERMES_SESSION_ID} / ${HERMES_HOME} tokens in SKILL.md.
# Tokens that don't resolve (e.g. ${HERMES_SESSION_ID} with no session) are
# left as-is so the user can debug them.
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID|HERMES_HOME)\}")

# Matches inline shell snippets like:  !`date +%Y-%m-%d`
# Non-greedy, single-line only -- no newlines inside the backticks.
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

# Cap inline-shell output so a runaway command can't blow out the context.
_INLINE_SHELL_MAX_OUTPUT = 4000


def load_skills_config() -> dict:
    """Load the ``skills`` section of config.yaml (best-effort)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        skills_cfg = cfg.get("skills")
        if isinstance(skills_cfg, dict):
            return skills_cfg
    except Exception:
        logger.debug("Could not read skills config", exc_info=True)
    return {}


def substitute_template_vars(
    content: str,
    skill_dir: Path | None,
    session_id: str | None,
) -> str:
    """Replace ${HERMES_SKILL_DIR} / ${HERMES_SESSION_ID} / ${HERMES_HOME} in skill content.

    Only substitutes tokens for which a concrete value is available --
    unresolved tokens are left in place so the author can spot them.
    """
    if not content:
        return content

    skill_dir_str = str(skill_dir) if skill_dir else None
    hermes_home = os.environ.get("HERMES_HOME")

    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "HERMES_SKILL_DIR" and skill_dir_str:
            return skill_dir_str
        if token == "HERMES_SESSION_ID" and session_id:
            return str(session_id)
        if token == "HERMES_HOME" and hermes_home:
            return hermes_home
        return match.group(0)

    return _SKILL_TEMPLATE_RE.sub(_replace, content)


def run_inline_shell(command: str, cwd: Path | None, timeout: int) -> str:
    """Execute a single inline-shell snippet and return its stdout (trimmed).

    Failures return a short ``[inline-shell error: ...]`` marker instead of
    raising, so one bad snippet can't wreck the whole skill message.
    """
    _popen_kwargs = {"creationflags": windows_hide_flags()} if IS_WINDOWS else {}
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True, encoding='utf-8', errors='replace',
            timeout=max(1, int(timeout)),
            check=False,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        return f"[inline-shell timeout after {timeout}s: {command}]"
    except FileNotFoundError:
        return "[inline-shell error: bash not found]"
    except RuntimeError as exc:
        # tests/conftest.py installs a live-system guard that blocks real
        # os.kill on out-of-tree PIDs. subprocess.run(timeout=...) may trip
        # that guard while trying to clean up the timed-out shell; treat that
        # as the same timeout outcome instead of surfacing the guard error.
        if "live-system guard: blocked os.kill" in str(exc):
            return f"[inline-shell timeout after {timeout}s: {command}]"
        return f"[inline-shell error: {exc}]"
    except Exception as exc:
        return f"[inline-shell error: {exc}]"

    output = (completed.stdout or "").rstrip("\n")
    if not output and completed.stderr:
        output = completed.stderr.rstrip("\n")
    if len(output) > _INLINE_SHELL_MAX_OUTPUT:
        output = output[:_INLINE_SHELL_MAX_OUTPUT] + "...[truncated]"
    return output


def expand_inline_shell(
    content: str,
    skill_dir: Path | None,
    timeout: int,
) -> str:
    """Replace every !`cmd` snippet in ``content`` with its stdout.

    Runs each snippet with the skill directory as CWD so relative paths in
    the snippet work the way the author expects.
    """
    if "!`" not in content:
        return content

    def _replace(match: re.Match) -> str:
        cmd = match.group(1).strip()
        if not cmd:
            return ""
        return run_inline_shell(cmd, skill_dir, timeout)

    return _INLINE_SHELL_RE.sub(_replace, content)


def preprocess_skill_content(
    content: str,
    skill_dir: Path | None,
    session_id: str | None = None,
    skills_cfg: dict | None = None,
) -> str:
    """Apply configured SKILL.md template and inline-shell preprocessing."""
    if not content:
        return content

    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
    if cfg.get("template_vars", True):
        content = substitute_template_vars(content, skill_dir, session_id)
    if cfg.get("inline_shell", False):
        timeout = int(cfg.get("inline_shell_timeout", 10) or 10)
        content = expand_inline_shell(content, skill_dir, timeout)
    return content


def _is_onload_authorized(skills_cfg: dict | None = None) -> bool:
    """Check whether onload execution is enabled in config.

    Authorized when either:
      * ``skills.inline_shell`` is true (existing trust gate), OR
      * ``skills.onload_enabled`` is true (new separate gate)

    Both default to false.
    """
    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
    return bool(cfg.get("inline_shell", False)) or bool(cfg.get("onload_enabled", False))


def _build_onload_env(extra_env: dict | None = None) -> dict[str, str]:
    """Construct an allowlisted environment for onload child processes.

    Only passes variables the child genuinely needs.  Strips ALL secrets,
    API keys, tokens, and credentials from the parent environment.
    """
    allowlist = {
        "PATH",
        "HERMES_MODEL",
        "HERMES_PROVIDER",
        "HERMES_HOME",
        "HERMES_SKILL_DIR",
    }
    if IS_WINDOWS:
        allowlist.update({"SYSTEMROOT", "TEMP", "TMP"})

    # Start with empty dict, explicitly copy only allowlisted vars
    child_env: dict[str, str] = {}
    for _key in allowlist:
        _val = os.environ.get(_key)
        if _val is not None:
            child_env[_key] = _val

    # Merge any extra_env keys that were explicitly configured to pass through
    if extra_env:
        for _key, _val in extra_env.items():
            child_env[_key] = str(_val)

    return child_env


def _validate_onload_path(
    script_rel_path: str,
    skill_dir: Path,
) -> Path | None:
    """Validate and resolve an onload script path.

    Returns the resolved ``Path`` if the path is safe, or ``None`` (with a
    logged warning) if the path attempts to escape the skill directory.

    Safety checks:
      1. Absolute paths are rejected immediately.
      2. The resolved path MUST be inside the resolved skill directory.
    """
    rel = Path(script_rel_path)

    # Reject absolute paths
    if rel.is_absolute():
        logger.warning(
            "Absolute onload script path rejected: %s (skill_dir=%s)",
            script_rel_path, skill_dir,
        )
        return None

    resolved_skill = skill_dir.resolve()
    candidate = (resolved_skill / rel).resolve()

    # Verify containment inside the skill directory
    try:
        candidate.relative_to(resolved_skill)
    except ValueError:
        logger.warning(
            "onload script path escapes skill root: %s -> %s (resolved skill_dir=%s)",
            script_rel_path, candidate, resolved_skill,
        )
        return None

    return candidate


def run_onload_script(
    script_rel_path: str,
    skill_dir: Path,
    extra_env: dict | None = None,
    timeout: int = 30,
    skills_cfg: dict | None = None,
) -> str:
    """Run a skill's onload script and return its stdout (stripped).

    The script is expected to print ``INJECT`` (case-insensitive, trimmed)
    to signal that the skill body should be pre-loaded into the system
    prompt.  Any other output (or an empty string) means "skip".

    .. security::

       * Execution requires ``skills.inline_shell`` or
         ``skills.onload_enabled`` in config (both default false).
       * The child process receives only an allowlisted environment —
         no secrets, API keys, or credentials from the parent.
       * Path traversal (absolute paths, ``../``, symlink escapes) is
         rejected before any subprocess is spawned.

    Supports ``.sh`` (bash) and ``.py`` (python) scripts.  Falls back to
    running the command directly via bash for unrecognised extensions.

    Reuses the same subprocess machinery as ``run_inline_shell`` so any
    platform quirks (Windows hide-console flags) are already handled.
    """
    # ── Security gate: trust check ────────────────────────────────────
    if not _is_onload_authorized(skills_cfg):
        logger.debug(
            "onload execution blocked for %s (trust gate not enabled)",
            script_rel_path,
        )
        return ""

    # ── Security gate: path containment ───────────────────────────────
    script_path = _validate_onload_path(script_rel_path, skill_dir)
    if script_path is None:
        return ""
    if not script_path.exists():
        logger.warning(
            "onload script %s not found for skill at %s",
            script_rel_path, skill_dir,
        )
        return ""

    # Normalise to forward slashes so bash doesn't interpret backslashes
    # as escape characters on Windows.
    script_path_str = str(script_path).replace("\\", "/")

    ext = script_path.suffix.lower()
    if ext == ".py":
        # Run Python scripts via python directly to avoid bash PATH
        # resolution issues on Windows (git-bash vs WSL relay).
        _python = sys.executable or "python"
        args = [_python, script_path_str]
    elif ext == ".sh":
        args = ["bash", script_path_str]
    else:
        args = ["bash", script_path_str]  # default fallback

    # ── Security gate: allowlisted environment ────────────────────────
    env = _build_onload_env(extra_env)

    _popen_kwargs = {"creationflags": windows_hide_flags()} if IS_WINDOWS else {}
    try:
        completed = subprocess.run(
            args,
            cwd=str(skill_dir).replace("\\", "/"),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(timeout)),
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
            **_popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        logger.warning("onload script %s timed out after %ss", script_path, timeout)
        return "[TIMEOUT]"
    except Exception as exc:
        logger.warning("onload script %s failed: %s", script_path, exc)
        return ""

    output = (completed.stdout or "").strip()
    return output
