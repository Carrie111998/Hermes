"""Keep interactive Desktop / gateway hosts from inheriting Kanban worker identity.

A dispatcher worker that relaunches Hermes.app (packaged install, ``hermes
desktop``, detached updater ``open``) otherwise copies ``HERMES_KANBAN_*``
lifecycle ownership and ``HERMES_PROFILE`` into the long-lived Command Center
process. Interactive turns then load the worker protocol and act as the
worker's profile instead of the user's desk profile.

This module sanitizes launch env *copies*. ``apply_desktop_host_env_isolation``
mutates a host mapping on purpose for ``hermes serve`` / ``dashboard``. Do not
point it at a live worker's ``os.environ`` — cron Bot Chat (PO-0015) and
in-process cron isolation depend on the worker process keeping those variables.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Mapping, MutableMapping

# Task / run ownership. BOARD and DB stay: they are board routing pins, the
# same pair PO-0015 Bot Chat and BF-0016 child-chat isolation keep.
KANBAN_LIFECYCLE_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
)

_WORKER_SESSION_KEYS: tuple[str, ...] = (
    "HERMES_SESSION_ID",
    "HERMES_SINGLE_QUERY_SESSION",
)


def _path_for(value: str):
    if "\\" in value and value[1:3] == ":\\":
        return PureWindowsPath(value)
    return PurePosixPath(value)


def inherited_kanban_lifecycle(env: Mapping[str, str]) -> bool:
    """True when *env* carries dispatcher worker lifecycle ownership."""
    if any((env.get(key) or "").strip() for key in KANBAN_LIFECYCLE_ENV_KEYS):
        return True
    return (env.get("HERMES_SESSION_SOURCE") or "").strip().lower() == "kanban"


def default_desk_home(env: Mapping[str, str]) -> str | None:
    """Peel ``<root>/profiles/<name>`` back to the Hermes root."""
    home = (env.get("HERMES_HOME") or "").strip()
    if not home:
        return None
    path = _path_for(home)
    if path.parent.name.lower() == "profiles":
        return str(path.parent.parent)
    return home


def sanitize_desktop_host_env(
    env: Mapping[str, str],
    *,
    explicit_profile: str | None = None,
) -> dict[str, str]:
    """Return a Desktop/gateway host env without inherited worker ownership.

    * Always drops lifecycle keys. Board routing pins are kept.
    * If this env looked like a worker and no explicit desk profile was
      requested, drop ``HERMES_PROFILE`` and peel profile-scoped ``HERMES_HOME``.
    * ``explicit_profile`` is the user's chosen desk profile (``-p`` / Desktop
      ``active-profile.json``) and wins over the worker's profile.
    * The input mapping is never mutated.
    """
    cleaned = dict(env)
    had_lifecycle = inherited_kanban_lifecycle(cleaned)

    for key in KANBAN_LIFECYCLE_ENV_KEYS:
        cleaned.pop(key, None)

    if had_lifecycle:
        if (cleaned.get("HERMES_SESSION_SOURCE") or "").strip().lower() == "kanban":
            cleaned.pop("HERMES_SESSION_SOURCE", None)
        for key in _WORKER_SESSION_KEYS:
            cleaned.pop(key, None)
        peeled = default_desk_home(cleaned)
        if peeled:
            cleaned["HERMES_HOME"] = peeled
        if explicit_profile:
            name = explicit_profile.strip()
            if name and name != "default":
                cleaned["HERMES_PROFILE"] = name
            else:
                cleaned.pop("HERMES_PROFILE", None)
        else:
            cleaned.pop("HERMES_PROFILE", None)

    elif explicit_profile:
        name = explicit_profile.strip()
        if name and name != "default":
            cleaned["HERMES_PROFILE"] = name

    return cleaned


def apply_desktop_host_env_isolation(
    env: MutableMapping[str, str],
    *,
    explicit_profile: str | None = None,
) -> bool:
    """Sanitize *env* in place. Returns True when the mapping changed."""
    cleaned = sanitize_desktop_host_env(env, explicit_profile=explicit_profile)
    if cleaned == dict(env):
        return False
    for key in list(env):
        if key not in cleaned:
            del env[key]
    env.update(cleaned)
    return True


def explicit_profile_from_argv(argv: list[str] | tuple[str, ...] | None) -> str | None:
    """Return ``-p`` / ``--profile`` from *argv*, or None."""
    if not argv:
        return None
    args = list(argv)
    if args and args[0].endswith(("hermes", "hermes.exe", "python", "python3", "python.exe")):
        args = args[1:]
        if args and args[0] in {"-m",} and len(args) > 1:
            args = args[2:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if arg in {"--profile", "-p"} and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1]
        i += 1
    return None
