"""Session-db / profile-scoping / cwd helpers.

God-file slice R1-S2 (epic #78647, target #78630): the db/profile/cwd cluster
(12 members) was moved verbatim out of ``tui_gateway/server.py``. Module-global
state ``_db`` / ``_db_error`` intentionally STAYS on ``tui_gateway.server``
(tests patch it directly, e.g. test_undo_command.py:93); this module reads and
writes it through the server module object at call time so runtime
reassignment and monkeypatching keep working (see R1-CONSENSUS.md).
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path

from hermes_constants import reset_hermes_home_override, set_hermes_home_override

logger = logging.getLogger("tui_gateway.server")


def _get_db():
    from tui_gateway import server  # lazy: call-time attribute access only

    if server._db is None:
        from hermes_state import SessionDB

        try:
            server._db = SessionDB()
            server._db_error = None
        except Exception as exc:
            server._db_error = str(exc)
            logger.warning(
                "TUI session store unavailable — continuing without state.db features: %s",
                exc,
            )
            return None
    return server._db


def _db_for_profile(profile: str | None = None):
    """Return SessionDB for ``params.profile`` when it differs from launch.

    App-global remote mode passes ``profile`` on session.* RPCs so history/list/
    create operate on that profile's ``state.db``. Launch/own profile → shared
    ``_get_db()`` handle (left open). Non-launch profile → a dedicated handle
    the caller should ``close()`` (see :func:`_profile_db` contextmanager).

    Returns (db, owns_handle). ``db`` is None when unavailable.
    """
    from tui_gateway import server  # lazy: call-time attribute access only

    profile_home = server._profile_home(profile)
    if profile_home is None:
        return server._get_db(), False
    try:
        from hermes_state import SessionDB

        return SessionDB(db_path=Path(profile_home) / "state.db"), True
    except Exception as exc:
        logger.warning(
            "TUI profile session store unavailable for %s: %s",
            profile,
            exc,
        )
        return None, False


@contextlib.contextmanager
def _profile_db(params: dict | None = None):
    """Yield the SessionDB for ``params['profile']`` (app-global remote mode).

    Closes dedicated profile handles; leaves the launch-profile shared handle open.
    Yields None when the db is unavailable.
    """
    profile = None
    if isinstance(params, dict):
        profile = (params.get("profile") or "").strip() or None
    from tui_gateway import server  # lazy: call-time attribute access only

    db, owns = server._db_for_profile(profile)
    try:
        yield db
    finally:
        if owns and db is not None:
            with contextlib.suppress(Exception):
                db.close()


def _response_profile_name(profile: str | None = None) -> str:
    """Profile name to report on session.* payloads.

    Prefer the RPC's requested profile when it is a real non-launch profile;
    otherwise the process launch profile.
    """
    from tui_gateway import server  # lazy: call-time attribute access only

    name = (profile or "").strip()
    if name and server._profile_home(name) is not None:
        return name
    return server._current_profile_name()


def _db_unavailable_error(rid, *, code: int):
    from tui_gateway import server  # lazy: call-time attribute access only

    detail = server._db_error or "state.db unavailable"
    return server._err(rid, code, f"state.db unavailable: {detail}")


# ── per-session profile scoping (global remote mode) ───────────────────────────
# One dashboard normally serves its launch profile. But the desktop's app-global
# remote mode points every profile at this single backend, so resume/prompt must
# be able to act on ANOTHER local profile's state.db + home. The desktop passes
# ``profile`` on those calls; we open that profile's db and bind its HERMES_HOME
# (a ContextVar override) for the duration of the call so config/skills/model and
# message persistence all resolve to the right profile. Omitted/own profile → the
# launch profile (unchanged for single-profile and per-profile-remote setups).
def _profile_home(profile: str | None) -> Path | None:
    """Resolve a named profile's home on THIS host, or None for the launch profile."""
    name = (profile or "").strip()
    if not name:
        return None
    from tui_gateway import server  # lazy: call-time attribute access only

    try:
        from hermes_cli import profiles as profiles_mod

        home = Path(profiles_mod.get_profile_dir(name))
    except Exception:
        return None
    # Already the launch profile? No override needed.
    if home.resolve() == Path(server._hermes_home).resolve():
        return None
    return home if (home / "state.db").exists() or home.exists() else None


def _profile_scoped(handler):
    """Bind ``params['profile']``'s HERMES_HOME around a pet RPC handler.

    Pets are per-profile: ``display.pet.*`` lives in the profile's config.yaml and
    sprites install under its ``pets/`` dir (both resolve via ``get_hermes_home``).
    The desktop sends ``profile`` on pet calls so config + pets dir resolve to the
    focused profile even in app-global remote mode, where one backend serves every
    profile. No-op for the launch profile (own-profile backends already resolve it).
    """

    def wrapper(rid, params):
        from tui_gateway import server  # lazy: call-time attribute access only

        home = server._profile_home(params.get("profile") if isinstance(params, dict) else None)
        if home is None:
            return handler(rid, params)
        token = set_hermes_home_override(home)
        try:
            return handler(rid, params)
        finally:
            reset_hermes_home_override(token)

    return wrapper


# Placeholder ``terminal.cwd`` values that don't name a real directory — the
# gateway resolves these to the home dir at runtime, so they must NOT be treated
# as an explicit workspace (mirrors gateway/run.py's config bridge).
_CWD_PLACEHOLDERS = {".", "auto", "cwd"}


def _configured_cwd_from_cfg(cfg: dict | None) -> str | None:
    """Return an absolute, existing ``terminal.cwd`` from a config mapping.

    Returns None for placeholders (``.``/``auto``/``cwd``), missing values, or
    paths that don't resolve to a real directory.
    """
    if not isinstance(cfg, dict):
        return None
    terminal_cfg = cfg.get("terminal")
    if not isinstance(terminal_cfg, dict):
        return None
    raw = str(terminal_cfg.get("cwd") or "").strip()
    if not raw or raw in _CWD_PLACEHOLDERS:
        return None
    resolved = os.path.abspath(os.path.expanduser(raw))
    return resolved if os.path.isdir(resolved) else None


def _profile_configured_cwd(profile_home: Path | None) -> str | None:
    """Resolve a non-launch profile's ``terminal.cwd`` from its own config.yaml.

    The desktop's app-global remote mode serves every profile from one backend,
    so the process-global ``TERMINAL_CWD`` belongs to the *launch* profile. A new
    session bound to another profile must take its workspace from THAT profile's
    config, not the stale env var (issue #40334). Returns an absolute, existing
    directory, or None for placeholders / missing / invalid paths.
    """
    if profile_home is None:
        return None
    from tui_gateway import server  # lazy: call-time attribute access only

    try:
        from hermes_cli.config import _expand_env_vars, read_user_config_raw

        p = Path(profile_home) / "config.yaml"
        if not p.exists():
            return None
        # Behavioral read of a NON-launch profile's config: load_config()
        # would resolve the ACTIVE profile's path, so read this profile's
        # file directly, then apply the same read-side pipeline as
        # _load_cfg (managed overlay + ${VAR} expansion). Fail-open.
        data = server._apply_managed(read_user_config_raw(p))
        expanded = _expand_env_vars(data)
        if isinstance(expanded, dict):
            data = expanded
        return server._configured_cwd_from_cfg(data)
    except Exception:
        return None


def _launch_configured_cwd() -> str | None:
    """Resolve the launch profile's ``terminal.cwd`` from config.yaml.

    Dashboard ``/chat`` for the launch profile attaches to the dashboard
    process's in-memory TUI gateway. The Node PTY child receives a bridged
    ``TERMINAL_CWD`` env var, but this in-memory process does not — so reading
    the process env alone leaves a fresh chat starting in ``os.getcwd()``
    (wherever ``hermes dashboard`` was launched) instead of the configured
    ``terminal.cwd``. Read config directly so changing ``terminal.cwd`` affects
    new in-memory TUI sessions too.
    """
    from tui_gateway import server  # lazy: call-time attribute access only

    try:
        return server._configured_cwd_from_cfg(server._load_cfg())
    except Exception:
        return None


def _default_session_cwd() -> str:
    """Fallback cwd for a session with no explicit / stored / profile cwd.

    Mirrors the launch-config-aware tail of :func:`_completion_cwd` so freshly
    created AND resumed sessions land in the configured ``terminal.cwd`` rather
    than ``os.getcwd()`` when the in-memory gateway's process env has no bridged
    ``TERMINAL_CWD``.
    """
    from tui_gateway import server  # lazy: call-time attribute access only

    return server._launch_configured_cwd() or os.getenv("TERMINAL_CWD") or os.getcwd()
