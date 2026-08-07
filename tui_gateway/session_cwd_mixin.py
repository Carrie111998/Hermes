"""Session cwd resolution helpers (moved verbatim from tui_gateway/server.py).

The functions in this module are byte-identical to their pre-split
server.py bodies.  server.py imports this module at the end of its own
import and rebinds every function onto its namespace with
``types.FunctionType`` (same seam as the methods_* handler split — see
method_ctx.py), so all module-global references (``_sessions``,
``_load_cfg``, ``_session_db``, ``_session_source``, the ``_git_*``
aliases, ``logger``, ...) keep resolving against server.py's namespace
exactly as before the move.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _normalize_completion_path(path_part: str) -> str:
    expanded = os.path.expanduser(path_part)
    if os.name != "nt":
        normalized = expanded.replace("\\", "/")
        if (
            len(normalized) >= 3
            and normalized[1] == ":"
            and normalized[2] == "/"
            and normalized[0].isalpha()
        ):
            return f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    return expanded


def _completion_cwd(params: dict | None = None) -> str:
    params = params or {}
    raw = (
        params.get("cwd")
        or _sessions.get(params.get("session_id") or "", {}).get("cwd")
        # A session bound to another profile resolves its workspace from THAT
        # profile's config before falling back to the launch profile's env var.
        or _profile_configured_cwd(_profile_home(params.get("profile")))
        # The launch profile's dashboard /chat attaches to the dashboard's
        # in-memory gateway, which does NOT inherit the PTY child's bridged
        # TERMINAL_CWD. Read the launch profile's config.yaml directly so a
        # configured terminal.cwd wins over a stale process env / launch dir.
        or _launch_configured_cwd()
        or os.environ.get("TERMINAL_CWD")
        or os.getcwd()
    )
    try:
        resolved = os.path.abspath(os.path.expanduser(str(raw)))
        if os.path.isdir(resolved):
            return resolved
    except Exception:
        pass
    return os.getcwd()


def _terminal_task_cwd(session: dict | None) -> str:
    """Return the cwd that terminal_tool should use for this TUI session.

    ``_completion_cwd`` validates paths on the host so file completion does not
    point at nonsense.  Non-local terminal backends are different: their cwd is
    inside the target environment, so an SSH path like /home/user/workspace may
    not exist on the local macOS host but is still the correct execution cwd.

    When ``TERMINAL_ENV`` is unset (dashboard/TUI process) the config's
    ``terminal.backend`` is consulted as a fallback so the non-local cwd
    resolution path is taken even when the dashboard entrypoint did not call
    ``apply_terminal_config_to_env`` on its own ``os.environ``.
    """
    backend = (os.environ.get("TERMINAL_ENV") or "").strip().lower()
    if not backend or backend == "local":
        # Fall back to config when TERMINAL_ENV is unset (dashboard/TUI process
        # never calls apply_terminal_config_to_env on os.environ).
        try:
            terminal_cfg = _load_cfg().get("terminal", {})
            if isinstance(terminal_cfg, dict):
                cfg_backend = str(terminal_cfg.get("backend") or "").strip().lower()
                if cfg_backend and cfg_backend != "local":
                    backend = cfg_backend
        except Exception:
            pass

    if backend and backend != "local":
        raw = os.environ.get("TERMINAL_CWD", "").strip()
        if not raw:
            try:
                terminal_cfg = _load_cfg().get("terminal", {})
                if isinstance(terminal_cfg, dict):
                    raw = str(terminal_cfg.get("cwd") or "").strip()
            except Exception:
                raw = ""
        if raw and raw not in {".", "auto", "cwd"}:
            return raw

    return _session_cwd(session)


def _session_cwd(session: dict | None) -> str:
    if session and session.get("cwd"):
        return str(session["cwd"])
    return _completion_cwd()


def _persisted_session_cwd(session: dict) -> str | None:
    """The cwd to stamp on the session's DB row, or None to leave it unset.

    See :func:`_ensure_session_db_row` for why the launch directory counts as a
    workspace for terminal sessions but not for the desktop.
    """
    if session.get("explicit_cwd"):
        return _session_cwd(session)
    if _session_source(session) in _LAUNCH_CWD_NOT_A_WORKSPACE:
        return None
    # Only the session's OWN directory. `_session_cwd` falls back to the
    # gateway-wide completion cwd, which belongs to no session in particular —
    # stamping that would invent a workspace for a session that never had one.
    return str(session.get("cwd") or "") or None


def _heal_dead_cwd(cwd: str) -> str:
    """Resolve a session cwd that points at a now-deleted directory.

    A session anchored to a linked worktree (``<repo>/.worktrees/<name>``) keeps
    that path after the worktree is removed (branch merged, `git worktree
    remove`, etc). The literal dir is gone, so a probe of it returns nothing and
    the composer shows no branch — while the sidebar still folds the path up to
    the repo's main lane. Heal the mismatch: walk up to the first existing
    ancestor, then resolve its common git root, so a dead-worktree cwd collapses
    to the live repo root (and its real current branch).

    Only meaningful for local backends; a remote/SSH cwd may legitimately not
    exist on the host, so callers must skip healing there.
    """
    raw = (cwd or "").strip()
    if not raw or os.path.isdir(raw):
        return raw

    probe = raw
    # Climb to the first ancestor that still exists on disk.
    for _ in range(64):
        parent = os.path.dirname(probe)
        if not parent or parent == probe:
            break
        probe = parent
        if os.path.isdir(probe):
            break

    if not os.path.isdir(probe):
        return raw

    try:
        root = _git_common_repo_root_for_cwd(probe) or _git_repo_root_for_cwd(probe)
    except Exception:
        root = ""

    return root or probe


def _is_local_terminal_backend() -> bool:
    backend = (os.environ.get("TERMINAL_ENV") or "").strip().lower()
    return not backend or backend == "local"


def _display_session_cwd(session: dict | None) -> str:
    """Session cwd for display/probe surfaces, healed past deleted worktrees.

    Persists the healed value back to the session row (best-effort, local only)
    so the next load is already coherent and the sidebar lane stops showing a
    session pinned to a vanished path.
    """
    cwd = _session_cwd(session)
    if not _is_local_terminal_backend():
        return cwd

    healed = _heal_dead_cwd(cwd)
    if healed and healed != cwd and session is not None:
        session["cwd"] = healed
        try:
            with _session_db(session) as db:
                if db is not None:
                    db.update_session_cwd(session.get("session_key", ""), healed)
        except Exception:
            logger.debug("failed to persist healed session cwd", exc_info=True)
        _persist_session_git_meta(session, healed)

    return healed


def _reconcile_session_cwd_from_terminal(session: dict | None) -> bool:
    """Re-anchor a session that SETTLED in another git checkout. Returns moved.

    An agent told to work in a fresh worktree does exactly that — `git worktree
    add`, `cd` into it, and every later command runs there — but the session
    stayed pinned to wherever it started, so the desktop kept labelling the chat
    with the primary checkout's branch while all the work landed elsewhere.

    A plain `cd` is deliberately NOT a workspace move (see
    ``_apply_project_workspace``): browsing to /tmp to read a log must not
    re-home the chat. What we adopt here is narrower — the session's recorded
    cwd is in a DIFFERENT working tree of the SAME repository (the shape
    ``git worktree add`` produces). Everything else — a non-git workspace
    stepping into a repo, or a git workspace visiting an unrelated repo — is
    a browsing visit, and a user's explicitly chosen workspace is never
    overridden at all.

    Local backends only: a remote/SSH cwd names a path on the host, which this
    gateway can neither stat nor probe with git.
    """
    if not session or not _is_local_terminal_backend():
        return False

    # A workspace the user (or GUI) explicitly chose is never overridden by
    # where the agent's terminal happened to settle — only another explicit
    # action (`_set_session_cwd`, a project switch) moves it. A cwd this very
    # function adopted is marked `cwd_from_settle` so a session can keep
    # following the agent through successive worktrees.
    if session.get("explicit_cwd") and not session.get("cwd_from_settle"):
        return False

    try:
        from tools.terminal_tool import get_session_cwd

        recorded = get_session_cwd(session.get("session_key") or "")
    except Exception:
        return False

    if not recorded:
        return False

    resolved = os.path.abspath(os.path.expanduser(str(recorded)))
    current = os.path.abspath(os.path.expanduser(_session_cwd(session)))
    if resolved == current or not os.path.isdir(resolved):
        return False

    # The worktree ROOT, not the common repo root: folding worktrees together
    # here is exactly what hides the move we're looking for.
    landed = _git_repo_root_for_cwd(resolved)
    current_root = _git_repo_root_for_cwd(current)
    # A relocation is a move between two DIFFERENT git working trees. When the
    # session's own workspace is not in a git repo, the agent stepping into one
    # to read a file or run a command is a browsing visit, not a re-home:
    # adopting it would hijack a non-git workspace onto whatever repo a tool
    # call touched first (e.g. a home-directory session pinned to the checkout
    # it read a file from).
    if not landed or not current_root or landed == current_root:
        return False

    # And only between checkouts of the SAME repository — the shape a real
    # `git worktree add` produces (linked worktrees share the common .git
    # dir). Settling in an UNRELATED repo (`cd ~/other-project && git log`)
    # is likewise a visit: adopting it would re-home the chat onto whatever
    # foreign repo the terminal last touched.
    landed_common = _git_common_repo_root_for_cwd(resolved)
    current_common = _git_common_repo_root_for_cwd(current)
    if not landed_common or landed_common != current_common:
        return False

    session["cwd"] = resolved
    # The session works here now, so this is its workspace — a desktop chat
    # whose cwd was an unpersisted launch artifact earns a real row. The
    # settle marker keeps this adoption overridable by the NEXT settle while
    # still yielding to a user's explicit choice (see the guard above).
    session["explicit_cwd"] = True
    session["cwd_from_settle"] = True
    _register_session_cwd(session)

    with _session_db(session) as db:
        if db is not None:
            try:
                db.update_session_cwd(session.get("session_key", ""), resolved)
            except Exception:
                logger.debug("failed to persist settled session cwd", exc_info=True)

    _persist_session_git_meta(session, resolved)
    return True


def _emit_settled_session_info(sid: str, session: dict, agent) -> None:
    """Emit end-of-turn ``session.info``, reconciling a settled cwd first.

    The turn is over, so the agent has stopped moving: this is the one moment
    where its recorded cwd is a stable answer to "where does this session
    work". Reconciling before building the payload means the same event that
    already tells the desktop the turn ended also carries the new cwd/branch —
    the client follows it with no new event type and no extra round trip.
    """
    try:
        _reconcile_session_cwd_from_terminal(session)
    except Exception:
        logger.debug("failed to reconcile settled session cwd", exc_info=True)
    _emit("session.info", sid, _session_info(agent, session))


def _register_session_cwd(session: dict | None) -> None:
    if not session:
        return
    try:
        from tools.terminal_tool import register_task_env_overrides

        register_task_env_overrides(
            session["session_key"], {"cwd": _terminal_task_cwd(session)}
        )
    except Exception:
        pass
