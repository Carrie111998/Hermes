"""Projects CRUD + repo-discovery / project-tree helpers (moved verbatim from server.py).

Handler bodies are byte-identical to their pre-split server.py form; they
are rebound onto server.py's globals at install time — see method_ctx.py.

Projects — first-class, per-profile, multi-folder workspaces (server.py
slice R5-S1: C1 projects CRUD + C2 repo discovery / project-tree).
"""

import json
import logging
import os
import sys

from tui_gateway import git_probe

from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method

# `_git_common_repo_root_for_cwd` / `_resolve_cwd_git` mirror server.py's
# bindings of the same git_probe attributes (server.py:2374-2375), so the
# moved helpers resolve identical objects without importing server here.
_git_common_repo_root_for_cwd = git_probe.common_repo_root
_resolve_cwd_git = git_probe.resolve

# server.py-level names the moved code closes over. The install() rebinding
# only covers the outer @method wrappers; inner handler bodies and plain
# helpers resolve their free names through THIS module's namespace. These
# bindings delegate at call time to server.py's live module state, so
# monkeypatches of ``server.<name>`` stay visible (no frozen copy at
# import, the stale-copy trap) without importing server at module level.
def _server_module():
    return sys.modules["tui_gateway.server"]


def _load_cfg():
    return _server_module()._load_cfg()


def _ok(*args, **kwargs):
    return _server_module()._ok(*args, **kwargs)


def _completion_cwd(*args, **kwargs):
    return _server_module()._completion_cwd(*args, **kwargs)


def _git_branch_for_cwd(*args, **kwargs):
    return _server_module()._git_branch_for_cwd(*args, **kwargs)


# Same named logger singleton server.py binds (logging.getLogger is
# name-keyed), so this IS server.py's logger object.
logger = logging.getLogger("tui_gateway.server")

_E_PROJECTS = 5061  # generic failure
_E_NO_PROJECT = 5062  # id resolved to nothing
_E_PROJECT_ARG = 5063  # invalid argument (e.g. bad name/slug)


class _NoProject(Exception):
    """Raised inside a projects handler when ``params['id']`` resolves to None."""


def _projects_payload(conn) -> dict:
    from hermes_cli import projects_db as pdb

    return {
        "projects": [p.to_dict() for p in pdb.list_projects(conn, include_archived=True)],
        "active_id": pdb.get_active_id(conn),
    }


def _projects_method(name: str):
    """Register a projects RPC, injecting (pdb, conn) and unifying error mapping.

    Every project CRUD handler opened the per-profile DB, mapped a missing id to
    5062, bad args to 5063, and everything else to 5061. This collapses that
    boilerplate so each handler is just its one meaningful operation.
    """

    def decorator(fn):
        @method(name)
        def handler(rid, params: dict) -> dict:
            try:
                from hermes_cli import projects_db as pdb

                with pdb.connect_closing() as conn:
                    return fn(rid, params, pdb, conn)
            except _NoProject:
                return _err(rid, _E_NO_PROJECT, "no such project")
            except ValueError as e:
                return _err(rid, _E_PROJECT_ARG, str(e))
            except Exception as e:
                return _err(rid, _E_PROJECTS, str(e))

        return handler

    return decorator


def _require_project(pdb, conn, params: dict):
    """The project named by ``params['id']`` (or raise ``_NoProject``)."""
    proj = pdb.get_project(conn, str(params.get("id") or ""))
    if proj is None:
        raise _NoProject
    return proj


@_projects_method("projects.list")
def _(rid, params, pdb, conn) -> dict:
    return _ok(rid, _projects_payload(conn))


@_projects_method("projects.get")
def _(rid, params, pdb, conn) -> dict:
    return _ok(rid, {"project": _require_project(pdb, conn, params).to_dict()})


@_projects_method("projects.create")
def _(rid, params, pdb, conn) -> dict:
    pid = pdb.create_project(
        conn,
        name=str(params.get("name") or ""),
        slug=params.get("slug"),
        folders=params.get("folders") or [],
        primary_path=params.get("primary_path"),
        description=params.get("description"),
        icon=params.get("icon"),
        color=params.get("color"),
        board_slug=params.get("board_slug"),
    )
    if params.get("use"):
        pdb.set_active(conn, pid)
    proj = pdb.get_project(conn, pid)
    return _ok(rid, {"project": proj.to_dict() if proj else None})


@_projects_method("projects.update")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    pdb.update_project(
        conn,
        proj.id,
        name=params.get("name"),
        description=params.get("description"),
        icon=params.get("icon"),
        color=params.get("color"),
        board_slug=params.get("board_slug"),
    )
    return _ok(rid, {"project": pdb.get_project(conn, proj.id).to_dict()})


@_projects_method("projects.add_folder")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    pdb.add_folder(
        conn,
        proj.id,
        str(params.get("path") or ""),
        label=params.get("label"),
        is_primary=bool(params.get("is_primary")),
    )
    return _ok(rid, {"project": pdb.get_project(conn, proj.id).to_dict()})


@_projects_method("projects.remove_folder")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    pdb.remove_folder(conn, proj.id, str(params.get("path") or ""))
    return _ok(rid, {"project": pdb.get_project(conn, proj.id).to_dict()})


@_projects_method("projects.set_primary")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    pdb.set_primary(conn, proj.id, str(params.get("path") or ""))
    return _ok(rid, {"project": pdb.get_project(conn, proj.id).to_dict()})


@_projects_method("projects.archive")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    (pdb.restore_project if params.get("restore") else pdb.archive_project)(conn, proj.id)
    return _ok(rid, _projects_payload(conn))


@_projects_method("projects.delete")
def _(rid, params, pdb, conn) -> dict:
    proj = _require_project(pdb, conn, params)
    pdb.delete_project(conn, proj.id)
    return _ok(rid, _projects_payload(conn))


@_projects_method("projects.set_active")
def _(rid, params, pdb, conn) -> dict:
    pdb.set_active(conn, _require_project(pdb, conn, params).id if params.get("id") else None)
    return _ok(rid, {"active_id": pdb.get_active_id(conn)})


@_projects_method("projects.for_cwd")
def _(rid, params, pdb, conn) -> dict:
    cwd = _completion_cwd({"cwd": str(params.get("cwd") or "").strip()} if params.get("cwd") else {})
    proj = pdb.project_for_path(conn, cwd)
    return _ok(rid, {"project": proj.to_dict() if proj else None, "cwd": cwd, "branch": _git_branch_for_cwd(cwd)})


def _is_repo_junk(root: str) -> bool:
    """A git root we never auto-surface as a project: the bare home dir or
    anything under HERMES_HOME (~/.hermes by default) — config/sessions/skills,
    not a workspace. User-created projects pointing there are still honored."""
    if not root:
        return True

    from hermes_constants import get_hermes_home

    real = os.path.realpath(root)
    home = os.path.realpath(os.path.expanduser("~"))
    hermes_home = os.path.realpath(str(get_hermes_home()))

    return real == home or real == hermes_home or real.startswith(hermes_home + os.sep)


def _is_session_cwd_junk(cwd: str) -> bool:
    """A non-git cwd that should stay in flat Recents rather than auto-group.

    Unlike discovered git roots, an explicitly selected descendant of
    HERMES_HOME may be an intentional prose/data workspace. The pre-Projects
    desktop surfaced every such cwd, so exclude only the two broad defaults
    that would create catch-all projects.
    """
    if not cwd:
        return True

    from hermes_constants import get_hermes_home

    real = os.path.normcase(os.path.realpath(cwd))
    home = os.path.normcase(os.path.realpath(os.path.expanduser("~")))
    hermes_home = os.path.normcase(os.path.realpath(str(get_hermes_home())))
    return real == home or real == hermes_home


def _repo_discovery_policy(raw: dict | None = None) -> dict:
    """Return the effective, profile-local Desktop repository scan policy."""
    from hermes_cli.config import DEFAULT_CONFIG

    defaults = DEFAULT_CONFIG["desktop"]
    source = raw if isinstance(raw, dict) else (_load_cfg().get("desktop") or {})
    if not isinstance(source, dict):
        source = {}

    enabled = source.get("enabled", source.get("repo_scan_enabled", defaults["repo_scan_enabled"]))
    roots = source.get("roots", source.get("repo_scan_roots", defaults["repo_scan_roots"]))
    excludes = source.get(
        "exclude_paths",
        source.get("repo_scan_exclude_paths", defaults["repo_scan_exclude_paths"]),
    )

    return {
        "enabled": enabled if isinstance(enabled, bool) else defaults["repo_scan_enabled"],
        "roots": [value.strip() for value in roots if isinstance(value, str) and value.strip()]
        if isinstance(roots, list)
        else list(defaults["repo_scan_roots"]),
        "exclude_paths": [
            value.strip()
            for value in excludes
            if isinstance(value, str) and value.strip()
        ]
        if isinstance(excludes, list)
        else list(defaults["repo_scan_exclude_paths"]),
    }


def _repo_discovery_policy_key(policy: dict) -> str:
    def _paths(values: list[str]) -> list[str]:
        normalized = set()
        home = os.path.expanduser("~")
        for value in values:
            expanded = os.path.expanduser(value)
            if not os.path.isabs(expanded):
                expanded = os.path.join(home, expanded)
            normalized.add(os.path.normcase(os.path.abspath(expanded)))
        return sorted(normalized)

    canonical = {
        "enabled": bool(policy["enabled"]),
        "roots": _paths(policy["roots"]),
        "exclude_paths": _paths(policy["exclude_paths"]),
    }
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def _repo_discovery_policy_is_default(policy: dict) -> bool:
    from hermes_cli.config import DEFAULT_CONFIG

    return _repo_discovery_policy_key(policy) == _repo_discovery_policy_key(
        _repo_discovery_policy(DEFAULT_CONFIG["desktop"])
    )


def _discover_repos_payload(
    db, *, conn=None, backfill: bool = True, include_cached: bool = True
) -> list[dict]:
    """Merge filesystem-scanned repos (cached) with session-derived repo roots.

    Repo-first: the disk scan (persisted by `projects.record_repos`) surfaces
    repos even with zero hermes sessions. Session-derived roots cover repos
    outside the scan roots. Both are junk-filtered (hermes home subtree + bare
    home) and carry their session totals for the overview.

    ``conn`` reuses an already-open projects.db connection (the tree path holds
    one); ``backfill`` persists resolved roots back onto session rows — kept off
    the per-turn tree path (grouping uses the live git resolver regardless) and
    done only on the explicit discover/record refresh.
    """
    _is_junk = _is_repo_junk
    repos: dict[str, dict] = {}

    def _agg(root: str) -> dict:
        return repos.setdefault(root, {"root": root, "label": "", "sessions": 0, "last_active": 0.0})

    # Session-derived roots (common repo root, folding worktrees; cached) +
    # backfill the column so persisted git_repo_root matches the tree grouping.
    cwd_rows = list(db.distinct_session_cwds())
    # Warm the per-cwd git probes in parallel so a cold first paint doesn't
    # serialize one subprocess per distinct cwd before this loop reads the cache.
    git_probe.warm_roots(str(r.get("cwd") or "") for r in cwd_rows)
    cwd_to_root: dict[str, str] = {}
    for row in cwd_rows:
        cwd = str(row.get("cwd") or "")
        root = _git_common_repo_root_for_cwd(cwd)
        if not root:
            continue
        cwd_to_root[cwd] = root
        if _is_junk(root):
            continue
        agg = _agg(root)
        agg["sessions"] += int(row.get("sessions") or 0)
        agg["last_active"] = max(agg["last_active"], float(row.get("last_active") or 0))

    if backfill:
        try:
            db.backfill_repo_roots(cwd_to_root)
        except Exception:
            logger.debug("failed to backfill repo roots", exc_info=True)

    if not include_cached:
        out = sorted(repos.values(), key=lambda repo: repo["last_active"], reverse=True)
        for repo in out:
            repo["label"] = (
                repo["label"]
                or os.path.basename(repo["root"].rstrip("/\\"))
                or repo["root"]
            )
        return out

    # Filesystem-scanned roots from the cache (may have zero sessions). Reuse the
    # caller's projects.db connection when given, else open a short-lived one.
    try:
        from hermes_cli import projects_db as pdb

        def _read(c) -> None:
            for entry in pdb.list_discovered_repos(c):
                root = str(entry.get("root") or "")
                if not root or _is_junk(root):
                    continue
                agg = _agg(root)
                if entry.get("label"):
                    agg["label"] = entry["label"]
                # NOTE: `last_seen` is when the disk scan last saw the directory,
                # not when the user last worked in it. Folding it into
                # `last_active` stamped every scanned repo with the scan time —
                # i.e. "just now" — so a git checkout with zero Hermes sessions
                # outranked the repos the user actually works in. Activity stays
                # session-derived; a repo with no sessions has no activity.

        if conn is not None:
            _read(conn)
        else:
            with pdb.connect_closing() as own:
                _read(own)
    except Exception:
        logger.debug("failed to read discovered repo cache", exc_info=True)

    out = sorted(repos.values(), key=lambda r: r["last_active"], reverse=True)
    for r in out:
        r["label"] = r["label"] or os.path.basename(r["root"].rstrip("/\\")) or r["root"]
    return out


# Sources excluded from the project tree: cron runs, and kanban dispatcher
# workers, are not user conversations. Subagent/compression children are
# already dropped by list_sessions_rich(include_children=False); cron has its
# own section, and kanban runs are read on the board.
_PROJECT_TREE_EXCLUDED_SOURCES = ["cron", "kanban"]


def _project_tree_row(r: dict) -> dict:
    """Project a SessionDB row to the minimal shape the sidebar renders.

    Keeps the fields the grouping needs (cwd / git_branch / git_repo_root) plus
    everything ``SidebarSessionRow`` reads, and drops the heavy columns
    (system_prompt, model_config, ...) so the tree payload stays lean.
    """
    return {
        "id": r.get("id"),
        "_lineage_root_id": r.get("_lineage_root_id"),
        # The sidebar nests branch/fork sessions under their parent
        # (flattenSessionsWithBranches keys on this); without it, lane rows can't
        # draw the └─ connector the flat Recents list shows.
        "parent_session_id": r.get("parent_session_id"),
        "title": r.get("title"),
        "preview": r.get("preview"),
        "started_at": r.get("started_at") or 0,
        "ended_at": r.get("ended_at"),
        "last_active": r.get("last_active") or r.get("started_at") or 0,
        "source": r.get("source"),
        "archived": bool(r.get("archived")),
        "message_count": r.get("message_count") or 0,
        "tool_call_count": r.get("tool_call_count") or 0,
        "input_tokens": r.get("input_tokens") or 0,
        "output_tokens": r.get("output_tokens") or 0,
        "model": r.get("model"),
        "is_active": False,
        "cwd": r.get("cwd"),
        "git_branch": r.get("git_branch"),
        "git_repo_root": r.get("git_repo_root"),
    }


def _project_tree_inputs(
    db, session_limit: int, *, include_discovered: bool
) -> tuple[list[dict], list[dict], list[dict], str | None]:
    """Gather (sessions, projects, discovered_repos, active_id) for build_tree.

    ``include_discovered`` is the zero-session-repo overview tier; the entered
    view (drill-in) skips it entirely — it only needs the project it's showing,
    which already has sessions — avoiding the distinct-cwd scan + git probes on
    that per-turn path. One projects.db connection serves both reads.
    """
    rows = db.list_sessions_rich(
        limit=session_limit,
        offset=0,
        order_by_last_active=True,
        min_message_count=1,
        include_children=False,
        exclude_sources=_PROJECT_TREE_EXCLUDED_SOURCES,
        include_archived=False,
    )
    sessions = [_project_tree_row(r) for r in rows]
    # Parallel-warm the git cache so build_tree's resolver reads it instead of
    # cold-probing each cwd in sequence (matters on the drill-in path, which
    # skips the discovery warm-up below).
    git_probe.warm_roots(s["cwd"] for s in sessions if s.get("cwd"))

    from hermes_cli import projects_db as pdb

    policy = _repo_discovery_policy()
    policy_key = _repo_discovery_policy_key(policy)
    with pdb.connect_closing() as conn:
        if include_discovered:
            pdb.reconcile_discovered_repos_policy(
                conn,
                policy_key,
                preserve_unversioned=_repo_discovery_policy_is_default(policy),
            )
        projects = [p.to_dict() for p in pdb.list_projects(conn)]
        active_id = pdb.get_active_id(conn)
        # backfill stays off the hot tree path — grouping uses the live resolver.
        discovered = (
            _discover_repos_payload(
                db,
                conn=conn,
                backfill=False,
                include_cached=policy["enabled"],
            )
            if include_discovered
            else []
        )

    return sessions, projects, discovered, active_id


# Per-build memo for `_dir_exists_cached`. Cleared at the top of every
# `_build_project_tree`, so a dir created or deleted between sidebar refreshes
# is seen on the next one.
_DIR_EXISTS_CACHE: dict[str, bool] = {}


def _dir_exists_cached(path: str) -> bool:
    """``os.path.isdir`` for the project tree, memoized per build.

    ``build_tree`` asks per SESSION, not per distinct path, so a power user with
    hundreds of sessions across a handful of dirs would otherwise fire hundreds
    of redundant stats on every sidebar open. The memo is per build, so a dir
    created or deleted between refreshes is picked up on the next one.
    """
    hit = _DIR_EXISTS_CACHE.get(path)
    if hit is None:
        hit = os.path.isdir(path)
        _DIR_EXISTS_CACHE[path] = hit
    return hit


def _build_project_tree(
    db, *, preview_limit: int, hydrate: bool, session_limit: int, include_discovered: bool
) -> tuple[dict, str | None]:
    """Gather inputs and run the one authoritative builder. Returns (tree, active_id)."""
    from tui_gateway import project_tree

    _DIR_EXISTS_CACHE.clear()
    sessions, projects, discovered, active_id = _project_tree_inputs(
        db, session_limit, include_discovered=include_discovered
    )
    tree = project_tree.build_tree(
        projects,
        sessions,
        discovered,
        _resolve_cwd_git,
        preview_limit=preview_limit,
        hydrate=hydrate,
        is_junk_root=_is_repo_junk,
        is_junk_cwd=_is_session_cwd_junk,
        exists=_dir_exists_cached,
    )
    return tree, active_id


def register(server) -> None:
    """Bind this module's handlers onto ``server``'s globals and registry."""
    _registry.install(server)
