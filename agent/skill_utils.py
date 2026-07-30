"""Lightweight skill metadata utilities shared by prompt_builder and skills_tool.

This module intentionally avoids importing the tool registry, CLI config, or any
heavy dependency chain.  It is safe to import at module level without triggering
tool registration or provider resolution.
"""

import logging
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from hermes_constants import get_config_path, get_skills_dir, is_termux

logger = logging.getLogger(__name__)

# ── Platform mapping ──────────────────────────────────────────────────────

PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}

EXCLUDED_SKILL_DIRS = frozenset(
    (
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
)

# Supporting files live inside a skill package and are loaded explicitly via
# skill_view(skill, file_path=...). They are not standalone skills and must not
# be scanned for active SKILL.md/DESCRIPTION.md entries, even if a Curator or
# archive workflow preserves a complete old skill package under references/.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))

# ── Org-shared skills (sync contract) ───────────────────────────
# Org mirrors live under ~/.hermes/skills/_org/<org_id>/. Resolution is
# TOKEN-GATED via a marker file the sync client writes after verifying the
# token (skills_sync_client.pull_org_skills): only the marked org's mirror is
# scanned. No marker ⇒ no org skills load. The marker is plain data (org_id
# string) so this module stays import-light; the VERIFICATION lives in the
# sync client, which is the only writer. Offline grace: the marker persists,
# so already-pulled org skills keep working without connectivity; a VERIFIED
# org change (or personal-org token) rewrites/removes it.

ORG_MIRROR_DIR_NAME = "_org"
ORG_ACTIVE_MARKER = ".active_org"
ORG_PROVENANCE_FILE = ".org-provenance.json"
# Records the fingerprint of each skill exactly as upstream sent it, so a
# later local edit is detectable and an org pull can refuse to clobber it.
ORG_BASELINE_FILE = ".org-baseline.json"


def read_active_org_id(skills_dir: Path) -> Optional[str]:
    """The org id whose mirror may resolve, or None (no org skills load)."""
    try:
        marker = skills_dir / ORG_MIRROR_DIR_NAME / ORG_ACTIVE_MARKER
        if not marker.exists():
            return None
        val = marker.read_text(encoding="utf-8").strip()
        return val or None
    except OSError:
        return None


def is_org_mirror_path(path, skills_dir: Path) -> bool:
    """True when *path* is inside the org mirror (``_org/``)."""
    try:
        rel = Path(path).resolve().relative_to(Path(skills_dir).resolve())
    except (OSError, ValueError):
        return False
    return bool(rel.parts) and rel.parts[0] == ORG_MIRROR_DIR_NAME


def org_id_of_path(path, skills_dir: Path) -> Optional[str]:
    """The ``<org_id>`` segment for a path under ``_org/<org_id>/...``."""
    try:
        rel = Path(path).resolve().relative_to(Path(skills_dir).resolve())
    except (OSError, ValueError):
        return None
    if len(rel.parts) >= 2 and rel.parts[0] == ORG_MIRROR_DIR_NAME:
        return rel.parts[1]
    return None


def is_excluded_skill_path(path, *, root: Optional[Path] = None) -> bool:
    """True if *path* should be skipped by active skill scanners.

    Use this on every ``SKILL.md`` path produced by direct ``rglob`` scans to
    prune dependency, virtualenv, VCS, cache, and progressive-disclosure
    support-package paths. Centralising the check here keeps every
    skill-scanning site in sync with the shared exclusion set.

    Accepts a Path or string.
    """
    try:
        parts = path.parts  # Path
    except AttributeError:
        from pathlib import PurePath
        parts = PurePath(str(path)).parts
    return any(part in EXCLUDED_SKILL_DIRS for part in parts) or is_skill_support_path(
        path, root=root
    )


def is_skill_support_path(path, *, root: Optional[Path] = None) -> bool:
    """True if *path* is under a support dir of an actual skill root.

    ``references/``, ``templates/``, ``assets/``, and ``scripts/`` are
    progressive-disclosure support areas when they sit directly inside a skill
    directory containing ``SKILL.md``. They are not active discovery roots for
    standalone skills. A preserved package such as
    ``some-skill/references/old-skill-package/SKILL.md`` is documentation data
    unless the caller explicitly loads it via ``file_path``.

    Legitimate categories or skill names such as ``skills/scripts/foo`` remain
    discoverable because their ``scripts`` component is not directly under a
    directory that contains ``SKILL.md``.
    """
    path_obj = path if isinstance(path, Path) else Path(str(path))
    parts = path_obj.parts
    # Last component may be a file or candidate skill directory name. Only
    # components before the leaf can be containing support directories.
    for idx, part in enumerate(parts[:-1]):
        if part not in SKILL_SUPPORT_DIRS or idx == 0:
            continue
        skill_root = Path(*parts[:idx])
        if root is not None and not path_obj.is_absolute():
            skill_root = root / skill_root
        if (skill_root / "SKILL.md").exists():
            return True
    return False


# ── Lazy YAML loader ─────────────────────────────────────────────────────

_yaml_load_fn = None


def yaml_load(content: str):
    """Parse YAML with lazy import and CSafeLoader preference."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import yaml

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

        def _load(value: str):
            return yaml.load(value, Loader=loader)

        _yaml_load_fn = _load
    return _yaml_load_fn(content)


# ── Frontmatter parsing ──────────────────────────────────────────────────


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Uses yaml with CSafeLoader for full YAML support (nested metadata, lists)
    with a fallback to simple key:value splitting for robustness.

    A single leading UTF-8 BOM (U+FEFF) is stripped before parsing. Windows
    GUI editors (Notepad, PowerShell ``>``) prepend one when saving a SKILL.md
    as UTF-8, and ``read_text(encoding="utf-8")`` preserves it (only
    ``utf-8-sig`` strips it). Left in place, the BOM defeats the ``---`` fence
    check below and the whole frontmatter is silently discarded — name,
    description, ``platforms`` gating, env-var setup, and conditional
    activation all vanish. See CONTRIBUTING.md "File encoding".

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: Dict[str, Any] = {}

    # Strip only a leading BOM; a BOM mid-content is data, not a marker.
    if content.startswith("\ufeff"):
        content = content[1:]
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # Fallback: simple key:value parsing for malformed YAML
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


def parse_strict_fenced_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse canonical fenced YAML frontmatter without the legacy fallback.

    Active skill discovery treats a leading, exact ``---`` fence as a
    declaration boundary.  Such a fence must close and contain a YAML mapping;
    otherwise a malformed skill could silently claim a directory-derived name
    and shadow a legitimate package in another root.  Markdown with no exact
    opening fence remains a supported legacy skill and returns empty metadata.

    ``parse_frontmatter`` intentionally keeps a permissive key/value fallback
    for compatibility in non-discovery consumers.  Do not use it to decide
    whether a filesystem entry is an active skill.
    """
    if content.startswith("\ufeff"):
        content = content[1:]

    opening = re.match(r"---[ \t]*\r?\n", content)
    if opening is None:
        return {}, content

    remainder = content[opening.end():]
    closing = re.search(r"\r?\n---[ \t]*(?:\r?\n|$)", remainder)
    if closing is None:
        raise ValueError("frontmatter fence is not closed")

    parsed = yaml_load(remainder[:closing.start()])
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter is not a mapping")
    return parsed, remainder[closing.end():]


def read_strict_skill_index_file(skill_file: Path) -> Tuple[str, Dict[str, Any], str]:
    """Open one canonical SKILL.md without following an entry-file symlink.

    Discovery may traverse configured category symlinks, but the canonical
    entry file itself must be a regular file in the selected package.  Opening
    it with ``O_NOFOLLOW`` prevents an attacker from redirecting only
    ``SKILL.md`` outside that package after the directory has been accepted.
    """
    if os.name == "nt":
        from tools.nt_secure_fs_optional import open_directory, read_regular_file

        # Configured roots and category links are an existing compatibility
        # feature. Resolve that package alias once, then bind the resulting
        # physical directory with the NT no-reparse open; the canonical file
        # itself is still opened handle-relative and can never be a symlink.
        package_path = skill_file.parent.resolve(strict=True)
        with open_directory(package_path, writable=False) as package:
            payload, _metadata = read_regular_file(package, skill_file.name)
        content = payload.decode("utf-8")
        frontmatter, body = parse_strict_fenced_frontmatter(content)
        return content, frontmatter, body

    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("secure SKILL.md opens are unsupported on this platform")

    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(skill_file, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("SKILL.md is not a regular file")
        chunks: List[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RuntimeError("SKILL.md changed during discovery")
    finally:
        os.close(fd)

    content = b"".join(chunks).decode("utf-8")
    frontmatter, body = parse_strict_fenced_frontmatter(content)
    return content, frontmatter, body


# ── Platform matching ─────────────────────────────────────────────────────


def skill_matches_platform_list(platforms: Any) -> bool:
    """Return True when *platforms* is compatible with the current OS."""
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    running_in_termux = is_termux()
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
        # Termux runs a Linux userland on Android. Accept linux-tagged
        # skills regardless of whether sys.platform is "linux" (pre-3.13
        # Termux) or "android" (Python 3.13+ Termux, and any other
        # Android runtime).
        if running_in_termux and mapped == "linux":
            return True
        # Explicit termux/android tags match a Termux session too.
        if running_in_termux and mapped in ("termux", "android"):
            return True
    return False


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is compatible with the current OS.

    Skills declare platform requirements via a top-level ``platforms`` list
    in their YAML frontmatter::

        platforms: [macos]          # macOS only
        platforms: [macos, linux]   # macOS and Linux

    If the field is absent or empty the skill is compatible with **all**
    platforms (backward-compatible default).

    Termux note: on Termux/Android, ``sys.platform`` is ``"linux"`` on
    older Pythons but became ``"android"`` on Python 3.13+. Termux is a
    Linux userland riding on the Android kernel, so skills tagged
    ``linux`` are treated as compatible in Termux regardless of which
    ``sys.platform`` value Python reports. Individual Linux commands
    inside a skill may still misbehave (no systemd, BusyBox utils, no
    apt/dnf, etc.) but that is on the skill, not on platform gating.
    """
    return skill_matches_platform_list(frontmatter.get("platforms"))


# ── Environment matching ──────────────────────────────────────────────────

# Recognized environment tags and how each is detected. An environment tag is
# a *relevance* gate, not a hard-compatibility gate (that is what ``platforms:``
# is for). A skill tagged for an environment it isn't relevant to is hidden from
# the skills index / offer surfaces so it does not add noise for users who will
# never need it — but it can ALWAYS still be loaded explicitly (``skill_view``,
# ``--skills``), because an explicit request is explicit consent.
#
# Container/supervisor detection is stable for the process lifetime and is
# cached via ``_ENV_DETECT_CACHE``. Kanban relevance is intentionally dynamic:
# a long-lived gateway can move between ordinary sessions, dispatcher workers,
# and profiles with the kanban toolset without restarting.
_KNOWN_ENVIRONMENTS = frozenset({"kanban", "docker", "s6"})

_ENV_DETECT_CACHE: Dict[str, bool] = {}


def _detect_environment(env: str) -> bool:
    """Return True when the named runtime environment is currently active.

    Stable process environments are cached. Kanban is evaluated on every call
    because its env/profile signals can change in a long-lived process.
    Unknown env names return True (fail-open: never hide a skill because of a
    tag we don't understand).
    """
    if env != "kanban" and env in _ENV_DETECT_CACHE:
        return _ENV_DETECT_CACHE[env]

    result = True
    if env == "kanban":
        # Kanban is "active" either as a dispatcher-spawned worker (the
        # dispatcher sets ``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_BOARD`` in the
        # worker env) or as an orchestrator profile that has opted into the
        # kanban toolset. Mirror the same signals the kanban tools themselves
        # gate on (``tools/kanban_tools.py``) so the offer filter agrees with
        # tool availability.
        if os.getenv("HERMES_KANBAN_TASK") or os.getenv("HERMES_KANBAN_BOARD"):
            result = True
        else:
            try:
                from tools.kanban_tools import _profile_has_kanban_toolset

                result = bool(_profile_has_kanban_toolset())
            except Exception:
                result = False
    elif env == "docker":
        try:
            from hermes_constants import is_container

            result = is_container()
        except Exception:
            result = False
    elif env == "s6":
        # The Hermes Docker image runs s6-overlay as PID 1 (/init). s6 plants
        # its runtime scaffolding under /run/s6 and ships its admin tree under
        # /package/admin/s6-overlay. Either marker means we're inside an
        # s6-supervised container.
        result = os.path.isdir("/run/s6") or os.path.isdir(
            "/package/admin/s6-overlay"
        )

    if env != "kanban":
        _ENV_DETECT_CACHE[env] = result
    return result


def get_skill_environment_fingerprint() -> Tuple[Tuple[str, bool], ...]:
    """Return the current offer-time environment state for cache keys.

    Discovery, slash-command, and system-prompt caches all filter skills via
    :func:`skill_matches_environment`. Sharing one fingerprint keeps those
    surfaces coherent when a process enters or leaves dynamic Kanban mode.
    """
    return tuple(
        (env, _detect_environment(env)) for env in sorted(_KNOWN_ENVIRONMENTS)
    )


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is relevant to the current runtime environment.

    Skills may declare an ``environments`` list in their YAML frontmatter::

        environments: [kanban]        # only relevant when kanban is active
        environments: [s6]            # only relevant inside the s6 Docker image
        environments: [docker]        # only relevant inside any container

    If the field is absent or empty the skill is relevant in **all**
    environments (backward-compatible default).

    This is an OFFER-time filter: it controls whether a skill shows up in the
    skills index / autocomplete / slash-command list. It is intentionally NOT
    enforced by ``skill_view`` or ``--skills`` preloading — an explicit load is
    explicit consent, and load-bearing force-loads (e.g. a dispatcher pinning
    a task to a specialist skill via ``--skills``) must always succeed
    regardless of how the offer surfaces filter the skill.

    A skill matches when ANY of its declared environments is currently active
    (OR semantics, mirroring ``platforms``). Unknown env tags fail open.
    """
    environments = frontmatter.get("environments")
    if not environments:
        return True
    if not isinstance(environments, list):
        environments = [environments]
    for env in environments:
        normalized = str(env).lower().strip()
        if not normalized:
            continue
        if normalized not in _KNOWN_ENVIRONMENTS:
            # Tag we don't understand — don't hide the skill over it.
            return True
        if _detect_environment(normalized):
            return True
    return False


# ── Disabled skills ───────────────────────────────────────────────────────


_RAW_CONFIG_CACHE: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


class SkillsConfigError(RuntimeError):
    """The configured skills scope could not be determined safely."""


def _raw_config_cache_clear() -> None:
    """Test hook — drop the shared raw config cache."""
    _RAW_CONFIG_CACHE.clear()


def _stat_skills_config(
    config_path: Path,
) -> Tuple[Optional[Any], Optional[str]]:
    """Stat config while distinguishing absence from a dangling path entry."""
    try:
        return config_path.stat(), None
    except FileNotFoundError as exc:
        # stat() follows symlinks, so a dangling config symlink raises the same
        # exception as a genuinely absent path. lstat() preserves that
        # distinction for strict mutation/collision callers.
        try:
            config_path.lstat()
        except FileNotFoundError:
            return None, None
        except OSError as lstat_exc:
            return (
                None,
                f"Could not inspect skills config {config_path}: {lstat_exc}",
            )
        return (
            None,
            f"Could not resolve skills config {config_path}: {exc}",
        )
    except OSError as exc:
        return None, f"Could not stat skills config {config_path}: {exc}"


def _load_raw_config_with_error(
    *, use_cache: bool = True
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Read config.yaml with a shared mtime+size keyed cache.

    This module intentionally avoids importing ``hermes_cli.config`` on the
    skill prompt/build path. A tiny local cache gives the same repeated-read
    win without pulling the heavier CLI config stack into startup.

    The second return value distinguishes a genuinely absent/empty config from
    a config that exists but could not be read or parsed.  Ordinary discovery
    remains best-effort through :func:`_load_raw_config`; mutation collision
    scans use this status to fail closed instead of silently forgetting
    configured external roots.
    """
    config_path = get_config_path()
    config_stat, stat_error = _stat_skills_config(config_path)
    if stat_error is not None:
        return {}, stat_error
    if config_stat is None:
        return {}, None
    cache_key = (
        str(config_path),
        config_stat.st_mtime_ns,
        config_stat.st_size,
    )

    if use_cache:
        cached = _RAW_CONFIG_CACHE.get(cache_key)
        if cached is not None:
            return cached, None

    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Could not read skill config %s: %s", config_path, e)
        return {}, f"Could not read or parse skills config {config_path}: {e}"
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        return {}, f"Skills config {config_path} must contain a YAML mapping"

    if use_cache:
        _RAW_CONFIG_CACHE.clear()
        _RAW_CONFIG_CACHE[cache_key] = parsed
    return parsed, None


def _load_raw_config() -> Dict[str, Any]:
    """Best-effort compatibility view of ``config.yaml``."""
    parsed, _error = _load_raw_config_with_error()
    return parsed


def get_disabled_skill_names(platform: str | None = None) -> Set[str]:
    """Read disabled skill names from config.yaml.

    Args:
        platform: Explicit platform name (e.g. ``"telegram"``).  When
            *None*, resolves from ``HERMES_PLATFORM`` or
            ``HERMES_SESSION_PLATFORM`` env vars.  Returns the global
            disabled list, unioned with the platform-specific list when a
            platform is resolved (a globally-disabled skill stays disabled
            on every platform).

    Reads the config file directly (no CLI config imports) to stay
    lightweight.
    """
    parsed = _load_raw_config()
    if not parsed:
        return set()

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()

    from gateway.session_context import get_session_env
    resolved_platform = (
        platform
        or os.getenv("HERMES_PLATFORM")
        or get_session_env("HERMES_SESSION_PLATFORM")
    )
    global_disabled = _normalize_string_set(skills_cfg.get("disabled"))
    if resolved_platform:
        platform_disabled = (skills_cfg.get("platform_disabled") or {}).get(
            resolved_platform
        )
        if platform_disabled is not None:
            return global_disabled | _normalize_string_set(platform_disabled)
    return global_disabled


def _normalize_string_set(values) -> Set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


# ── External skills directories ──────────────────────────────────────────

# (config_path_str, mtime_ns) -> resolved external dirs list.  Keyed by
# mtime_ns so a config.yaml edit mid-run is picked up automatically;
# otherwise every call would re-read + re-YAML-parse the 15KB config,
# which becomes the dominant cost of ``hermes`` startup when ~120 skills
# each trigger a category lookup during banner construction (10+ seconds
# of pure waste).
_EXTERNAL_DIRS_CACHE: Dict[Tuple[str, int], List[Path]] = {}


def _external_dirs_cache_clear() -> None:
    """Test hook — drop the in-process cache."""
    _EXTERNAL_DIRS_CACHE.clear()
    _raw_config_cache_clear()


def _configured_skill_root(path: Path) -> Path:
    """Return an absolute, comparison-safe spelling without requiring I/O.

    ``Path.resolve()`` normally gives the best deduplication behavior, but it
    can fail for an inaccessible path or a symlink loop.  A configured root is
    still part of the discovery scope in those cases, so fall back to a purely
    lexical absolute path instead of dropping it.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def _resolve_external_skills_dirs(
    *, require_valid_config: bool = False
) -> List[Path]:
    """Read ``skills.external_dirs`` and return every configured root.

    Each entry is expanded (``~`` and ``${VAR}``) and resolved to an absolute
    path.  Roots are intentionally retained when they are missing,
    inaccessible, or not directories: configuration defines discovery scope,
    while callers separately decide which roots are currently scannable.
    Duplicates and paths that resolve to the local ``~/.hermes/skills/`` are
    silently skipped.

    Cached in-process, keyed on ``config.yaml`` mtime — the function is
    called once per skill during banner / tool-registry scans, and YAML
    parsing a non-trivial config dominates ``hermes`` cold-start time
    when the cache is absent.

    ``require_valid_config=True`` is reserved for mutation/collision scans.
    It bypasses successful parse caches and raises :class:`SkillsConfigError`
    when an existing config cannot be read, parsed, or interpreted.  This
    prevents an unreadable config from masquerading as ``external_dirs: []``
    while preserving the historical best-effort result for ordinary callers.
    """
    config_path = get_config_path()

    # Cache key: (absolute path, mtime_ns).  stat() is ~2us vs ~85ms for
    # the full YAML parse, so the fast path is nearly free.
    config_stat, stat_error = _stat_skills_config(config_path)
    if stat_error is None and config_stat is not None:
        cache_key: Optional[Tuple[str, int]] = (
            str(config_path),
            config_stat.st_mtime_ns,
        )
    elif stat_error is None:
        return []
    else:
        if require_valid_config:
            raise SkillsConfigError(stat_error)
        return []

    if not require_valid_config:
        cached = _EXTERNAL_DIRS_CACHE.get(cache_key)
        if cached is not None:
            # Return a copy so callers can't mutate the cached list.
            return list(cached)

    parsed, config_error = _load_raw_config_with_error(
        use_cache=not require_valid_config
    )
    if config_error is not None:
        if require_valid_config:
            raise SkillsConfigError(config_error)
        return []

    skills_cfg = parsed.get("skills")
    if skills_cfg is None:
        return []
    if not isinstance(skills_cfg, dict):
        if require_valid_config:
            raise SkillsConfigError(
                f"Skills config {config_path} field 'skills' must be a mapping"
            )
        return []

    raw_dirs = skills_cfg.get("external_dirs")
    if raw_dirs is None or (
        isinstance(raw_dirs, str) and not raw_dirs.strip()
    ) or (
        isinstance(raw_dirs, list) and not raw_dirs
    ):
        result: List[Path] = []
        if cache_key is not None:
            _EXTERNAL_DIRS_CACHE[cache_key] = list(result)
        return result
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    if not isinstance(raw_dirs, list):
        if require_valid_config:
            raise SkillsConfigError(
                f"Skills config {config_path} field "
                "'skills.external_dirs' must be a string or list"
            )
        return []

    from hermes_constants import get_hermes_home

    hermes_home = get_hermes_home()
    local_skills = _configured_skill_root(get_skills_dir())
    seen: Set[Path] = set()
    result = []

    for entry in raw_dirs:
        if require_valid_config and not isinstance(entry, str):
            raise SkillsConfigError(
                f"Skills config {config_path} field "
                "'skills.external_dirs' entries must be strings"
            )
        entry = str(entry).strip()
        if not entry:
            if require_valid_config:
                raise SkillsConfigError(
                    f"Skills config {config_path} field "
                    "'skills.external_dirs' entries must be non-empty strings"
                )
            continue
        # Expand ~ and environment variables
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded)
        # Resolve relative paths against HERMES_HOME, not cwd
        if not p.is_absolute():
            p = hermes_home / p
        p = _configured_skill_root(p)
        if p == local_skills:
            continue
        if p in seen:
            continue
        seen.add(p)
        result.append(p)

    if cache_key is not None:
        _EXTERNAL_DIRS_CACHE[cache_key] = list(result)
    return result


def get_external_skills_dirs() -> List[Path]:
    """Return configured external skill roots using compatibility semantics.

    Read-only/discovery callers historically receive an empty list when
    ``config.yaml`` is unavailable or malformed. Keep this public no-argument
    contract stable; security-sensitive callers use
    ``get_all_skills_dirs(require_valid_config=True)``.
    """
    return _resolve_external_skills_dirs()


def get_scannable_external_skills_dirs(
    *, on_error: Optional[Callable[[OSError], None]] = None
) -> List[Path]:
    """Return configured external roots that currently stat as directories.

    This is the explicit best-effort view for non-catalog maintenance callers.
    Catalog and cache builders should use :func:`get_external_skills_dirs`
    directly so an unavailable configured root remains in their scope identity
    and can make the scan fail closed.
    """
    result: List[Path] = []
    for root in get_external_skills_dirs():
        try:
            root_stat = root.stat()
        except OSError as exc:
            if on_error is not None:
                on_error(exc)
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            error = NotADirectoryError(f"External skills root is not a directory: {root}")
            if on_error is not None:
                on_error(error)
            continue
        result.append(root)
    return result


def get_all_skills_dirs(
    *, require_valid_config: bool = False
) -> List[Path]:
    """Return the complete configured skill-root scope, local first.

    The local dir is always first (and always included even if it doesn't exist
    yet — callers handle that). Configured external roots follow in config
    order even when they cannot currently be scanned. Mutation/collision scans
    pass ``require_valid_config=True`` so a broken config cannot shrink that
    security boundary to the local root.
    """
    dirs = [get_skills_dir()]
    if require_valid_config:
        dirs.extend(_resolve_external_skills_dirs(require_valid_config=True))
    else:
        dirs.extend(get_external_skills_dirs())
    return dirs


def normalize_skill_lookup_name(identifier: str) -> str:
    """Normalize a skill identifier to a ``skill_view()``-safe relative path.

    Slash commands and cron jobs may store absolute paths to skills that live
    under ``~/.hermes/skills/`` (including via symlinks) or configured
    ``skills.external_dirs``. ``skill_view()`` rejects absolute names for
    security, so callers must translate trusted absolute paths to their
    relative form first.
    """
    raw_identifier = (identifier or "").strip()
    if not raw_identifier:
        return raw_identifier

    identifier_path = Path(raw_identifier).expanduser()
    if not identifier_path.is_absolute():
        return raw_identifier.lstrip("/")

    # Look the primary skills root up at CALL time. ``_skills_dir()`` resolves
    # the active profile/HERMES_HOME, while retaining the legacy patched
    # ``SKILLS_DIR`` behavior used by callers and tests. This must agree with
    # the exact root ``skill_view()`` enforces, otherwise a slash command from
    # one profile can be normalised relative to another profile's root.
    try:
        from tools import skills_tool as _skills_tool
        primary_root = Path(_skills_tool._skills_dir())
    except Exception:
        primary_root = get_skills_dir()

    trusted_roots = [primary_root]
    try:
        trusted_roots.extend(get_external_skills_dirs())
    except Exception:
        pass

    # Prefer the lexical path under a trusted skill root before resolving
    # symlinks. Slash-command discovery can legitimately find a skill via
    # ~/.hermes/skills/<name> where <name> is a symlink to a checked-out
    # skill elsewhere. Resolving first turns that trusted visible path into
    # an arbitrary absolute path that skill_view() refuses to load.
    for root in trusted_roots:
        try:
            return str(identifier_path.relative_to(root))
        except ValueError:
            continue

    try:
        return str(identifier_path.resolve().relative_to(primary_root.resolve()))
    except Exception:
        logger.debug(
            "Skill identifier %r is an absolute path outside trusted skills "
            "roots — passing through unchanged (skill_view will reject it)",
            raw_identifier,
        )
        return raw_identifier


def _resolve_for_skill_ownership(path) -> Path:
    path_obj = path if isinstance(path, Path) else Path(str(path))
    try:
        return path_obj.expanduser().resolve()
    except (OSError, RuntimeError):
        return path_obj.expanduser().absolute()


def is_external_skill_path(path) -> bool:
    """Return True when ``path`` lives under a configured external skills dir.

    ``skills.external_dirs`` are externally owned: Hermes can discover and view
    their skills, and foreground user-directed tool calls may still edit them,
    but autonomous lifecycle maintenance must treat them as read-only. This
    helper centralizes the ownership boundary so curator/reporting/tool paths do
    not each need to re-interpret the config.
    """
    candidate = _resolve_for_skill_ownership(path)
    for root in get_external_skills_dirs():
        resolved_root = _resolve_for_skill_ownership(root)
        try:
            candidate.relative_to(resolved_root)
            return True
        except ValueError:
            continue
    return False


# ── Condition extraction ──────────────────────────────────────────────────


def extract_skill_conditions(frontmatter: Dict[str, Any]) -> Dict[str, List]:
    """Extract conditional activation fields from parsed frontmatter."""
    metadata = frontmatter.get("metadata")
    # Handle cases where metadata is not a dict (e.g., a string from malformed YAML)
    if not isinstance(metadata, dict):
        metadata = {}
    hermes = metadata.get("hermes") or {}
    if not isinstance(hermes, dict):
        hermes = {}
    return {
        "fallback_for_toolsets": hermes.get("fallback_for_toolsets", []),
        "requires_toolsets": hermes.get("requires_toolsets", []),
        "fallback_for_tools": hermes.get("fallback_for_tools", []),
        "requires_tools": hermes.get("requires_tools", []),
    }


# ── Skill config extraction ───────────────────────────────────────────────


def extract_skill_config_vars(frontmatter: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract config variable declarations from parsed frontmatter.

    Skills declare config.yaml settings they need via::

        metadata:
          hermes:
            config:
              - key: wiki.path
                description: Path to the LLM Wiki knowledge base directory
                default: "~/wiki"
                prompt: Wiki directory path

    Returns a list of dicts with keys: ``key``, ``description``, ``default``,
    ``prompt``.  Invalid or incomplete entries are silently skipped.
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return []
    hermes = metadata.get("hermes")
    if not isinstance(hermes, dict):
        return []
    raw = hermes.get("config")
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key or key in seen:
            continue
        # Must have at least key and description
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        entry: Dict[str, Any] = {
            "key": key,
            "description": desc,
        }
        default = item.get("default")
        if default is not None:
            entry["default"] = default
        prompt_text = item.get("prompt")
        if isinstance(prompt_text, str) and prompt_text.strip():
            entry["prompt"] = prompt_text.strip()
        else:
            entry["prompt"] = desc
        seen.add(key)
        result.append(entry)
    return result


def discover_all_skill_config_vars() -> List[Dict[str, Any]]:
    """Scan all enabled skills and collect their config variable declarations.

    Walks every skills directory, parses each SKILL.md frontmatter, and returns
    a deduplicated list of config var dicts.  Each dict also includes a
    ``skill`` key with the skill name for attribution.

    Disabled, platform-incompatible, and environment-incompatible skills are
    excluded. Any unreadable or malformed candidate makes the entire result
    unsafe to use, so discovery fails closed instead of returning a partial
    configuration prompt.
    """
    all_vars: List[Dict[str, Any]] = []
    seen_keys: set = set()

    disabled = get_disabled_skill_names()
    scan_errors: List[OSError] = []
    skills_dirs: List[Path] = []
    local_skills_dir = get_skills_dir()
    try:
        local_stat = local_skills_dir.stat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        scan_errors.append(exc)
    else:
        if stat.S_ISDIR(local_stat.st_mode):
            skills_dirs.append(local_skills_dir)
        else:
            scan_errors.append(
                NotADirectoryError(
                    f"Local skills root is not a directory: {local_skills_dir}"
                )
            )

    try:
        configured_external_dirs = _resolve_external_skills_dirs(
            require_valid_config=True
        )
    except SkillsConfigError as exc:
        scan_errors.append(exc)
        configured_external_dirs = []
    for root in configured_external_dirs:
        try:
            root_stat = root.stat()
        except OSError as exc:
            scan_errors.append(exc)
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            scan_errors.append(
                NotADirectoryError(
                    f"External skills root is not a directory: {root}"
                )
            )
            continue
        skills_dirs.append(root)
    if scan_errors:
        logger.warning(
            "Skill config discovery is incomplete; refusing a partial result: %s",
            scan_errors[0],
        )
        return []

    for skills_dir in skills_dirs:
        for skill_file in iter_skill_index_files(
            skills_dir, "SKILL.md", on_error=scan_errors.append
        ):
            try:
                _, frontmatter, _ = read_strict_skill_index_file(skill_file)
            except Exception as exc:
                scan_errors.append(exc)
                continue

            skill_name = frontmatter.get("name") or skill_file.parent.name
            if str(skill_name) in disabled:
                continue
            if not skill_matches_platform(frontmatter):
                continue
            if not skill_matches_environment(frontmatter):
                continue

            config_vars = extract_skill_config_vars(frontmatter)
            for var in config_vars:
                if var["key"] not in seen_keys:
                    entry = dict(var)
                    entry["skill"] = str(skill_name)
                    all_vars.append(entry)
                    seen_keys.add(var["key"])

    if scan_errors:
        logger.warning(
            "Skill config discovery is incomplete; refusing a partial result: %s",
            scan_errors[0],
        )
        return []

    return all_vars


# Storage prefix: all skill config vars are stored under skills.config.*
# in config.yaml.  Skill authors declare logical keys (e.g. "wiki.path");
# the system adds this prefix for storage and strips it for display.
SKILL_CONFIG_PREFIX = "skills.config"


def _resolve_dotpath(config: Dict[str, Any], dotted_key: str):
    """Walk a nested dict following a dotted key.  Returns None if any part is missing."""
    parts = dotted_key.split(".")
    current = config
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def resolve_skill_config_values(
    config_vars: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve current values for skill config vars from config.yaml.

    Skill config is stored under ``skills.config.<key>`` in config.yaml.
    Returns a dict mapping **logical** keys (as declared by skills) to their
    current values (or the declared default if the key isn't set).
    Path values are expanded via ``os.path.expanduser``.
    """
    config = _load_raw_config()

    resolved: Dict[str, Any] = {}
    for var in config_vars:
        logical_key = var["key"]
        storage_key = f"{SKILL_CONFIG_PREFIX}.{logical_key}"
        value = _resolve_dotpath(config, storage_key)

        if value is None or (isinstance(value, str) and not value.strip()):
            value = var.get("default", "")

        # Expand ~ in path-like values
        if isinstance(value, str) and ("~" in value or "${" in value):
            value = os.path.expanduser(os.path.expandvars(value))

        resolved[logical_key] = value

    return resolved


# ── Description extraction ────────────────────────────────────────────────

SKILL_PROMPT_DESC_LIMIT = 60


def _normalize_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Normalize a skill's description field for comparison/truncation."""
    raw_desc = frontmatter.get("description", "")
    return str(raw_desc).strip().strip("'\"") if raw_desc else ""


def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Extract a system-prompt-length description from parsed frontmatter."""
    desc = _normalize_skill_description(frontmatter)
    if not desc:
        return ""
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return desc


def is_skill_description_truncated_for_prompt(frontmatter: Dict[str, Any]) -> bool:
    """True when the description will be truncated in the system prompt skill index."""
    desc = _normalize_skill_description(frontmatter)
    return len(desc) > SKILL_PROMPT_DESC_LIMIT


# ── File iteration ────────────────────────────────────────────────────────


def iter_skill_index_files(
    skills_dir: Path,
    filename: str,
    *,
    on_error: Optional[Callable[[OSError], None]] = None,
):
    """Walk skills_dir yielding sorted paths matching *filename*.

    Excludes Hermes metadata, VCS, virtualenv/dependency, cache, and skill
    support directories. Support directories (references/templates/assets/
    scripts) can contain arbitrary markdown and even archived package
    ``SKILL.md`` files, but they are progressive-disclosure data loaded through
    ``skill_view(..., file_path=...)`` rather than active skill roots.

    M2 org mirrors (``_org/``): TOKEN-GATED resolution. Only the active org's
    subdir (per the sync-client-written ``.active_org`` marker) is walked;
    every other ``_org/<id>/`` (stale mirror from a previous org, or no
    marker at all) is pruned — leave an org and its skills stop resolving,
    without any manual cleanup.

    Directory traversal errors are skipped for best-effort callers and reported
    through ``on_error`` when supplied. Catalog builders that cache the result
    must provide this callback so a partial walk is never committed as a
    complete snapshot.
    """
    skills_dir_str = str(skills_dir)
    active_org = read_active_org_id(skills_dir)
    org_root = os.path.join(skills_dir_str, ORG_MIRROR_DIR_NAME)
    matches: list[str] = []

    def report_error(error: OSError) -> None:
        if on_error is not None:
            on_error(error)

    # ``followlinks=True`` is required for supported symlinked skill roots, but
    # os.walk does not detect directory cycles. Track physical directories so a
    # ``loop -> ..`` link cannot make startup/indexing recurse forever.
    visited_dirs: set[tuple[int, int]] = set()
    for root, dirs, files in os.walk(
        skills_dir_str,
        followlinks=True,
        onerror=report_error,
    ):
        try:
            stat = os.stat(root, follow_symlinks=True)
            identity = (stat.st_dev, stat.st_ino)
        except OSError as exc:
            report_error(exc)
            dirs[:] = []
            continue
        if identity in visited_dirs:
            dirs[:] = []
            continue
        visited_dirs.add(identity)

        has_skill_md = "SKILL.md" in files
        if root == skills_dir_str and ORG_MIRROR_DIR_NAME in dirs and active_org is None:
            dirs.remove(ORG_MIRROR_DIR_NAME)
        elif root == org_root:
            # Inside _org/: descend ONLY into the active org's mirror.
            dirs[:] = [d for d in dirs if d == active_org]
        dirs[:] = [
            d
            for d in dirs
            if d not in EXCLUDED_SKILL_DIRS
            and not (has_skill_md and d in SKILL_SUPPORT_DIRS)
        ]
        if filename in files:
            matches.append(os.path.join(root, filename))
    for path in sorted(matches):
        yield Path(path)


# ── Namespace helpers for plugin-provided skills ───────────────────────────

_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_qualified_name(name: str) -> Tuple[Optional[str], str]:
    """Split ``'namespace:skill-name'`` into ``(namespace, bare_name)``.

    Returns ``(None, name)`` when there is no ``':'``.
    """
    if ":" not in name:
        return None, name
    return tuple(name.split(":", 1))  # type: ignore[return-value]


def is_valid_namespace(candidate: Optional[str]) -> bool:
    """Check whether *candidate* is a valid namespace (``[a-zA-Z0-9_-]+``)."""
    if not candidate:
        return False
    return bool(_NAMESPACE_RE.match(candidate))
