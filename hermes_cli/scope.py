"""CLI subcommand: `hermes scope <subcommand>`.

Thin shell around hermes_scope.py — status/create/link/unlink/dependency/
audit/complete/archive for the durable per-thread scope object described in
docs/design/thread-scope-isolation.md.

This module intentionally has no side effects at import time — main.py wires
the argparse subparsers on demand, mirroring hermes_cli/curator.py.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

import hermes_scope


def _redact(value: str, keep: int = 6) -> str:
    """Shorten a routing id for default (non-audit) display."""
    if not value:
        return value
    if len(value) <= keep:
        return value
    return f"...{value[-keep:]}"


def _resolve_scope_id(args) -> Optional[str]:
    """Explicit --scope-id wins; otherwise resolve from the live session
    context. Never guesses -- returns None (caller reports "scope unknown")
    when neither is available."""
    explicit = getattr(args, "scope_id", None)
    if explicit:
        return explicit
    return hermes_scope.resolve_current_scope_id()


def _liveness_summary(manifest: dict) -> dict:
    """Best-effort live-state check for owned artifacts.

    Per docs/design/thread-scope-isolation.md: status must check live state
    for owned artifacts rather than trusting the manifest's last-known state
    as current truth (a tmux session, delegation, or cron job the manifest
    still lists may have exited/completed/been removed hours ago). Lives
    here rather than in hermes_scope.py -- process_registry/async_delegation/
    cron.jobs are heavier deps than the storage layer needs, and
    process_registry.py already imports hermes_scope for auto-registration,
    so importing them back from there would be circular.

    Returns {category: {"count": N, "active": M or None}}. ``active`` is
    None (not 0) when liveness genuinely can't be determined for that
    category, so a caller can tell "checked, none active" apart from
    "couldn't check" -- fail closed to unknown, never to "still active."
    Only categories with a liveness check and at least one owned id are
    included.
    """
    owned = manifest.get("owned", {})
    summary: dict = {}

    tmux_keys = owned.get("tmux_session_keys") or []
    if tmux_keys:
        try:
            from tools.process_registry import process_registry

            active = sum(1 for k in tmux_keys if process_registry.has_active_for_session(k))
        except Exception:
            active = None
        summary["tmux_session_keys"] = {"count": len(tmux_keys), "active": active}

    delegation_ids = owned.get("delegation_ids") or []
    if delegation_ids:
        try:
            from tools.async_delegation import get_durable_delegation

            active = 0
            for delegation_id in delegation_ids:
                record = get_durable_delegation(delegation_id)
                if record and (
                    record.get("state") in ("running", "finalizing")
                    or record.get("delivery_state") == "pending"
                ):
                    active += 1
        except Exception:
            active = None
        summary["delegation_ids"] = {"count": len(delegation_ids), "active": active}

    cron_job_ids = owned.get("cron_job_ids") or []
    if cron_job_ids:
        try:
            from cron.jobs import get_job

            active = sum(1 for job_id in cron_job_ids if (get_job(job_id) or {}).get("enabled", False))
        except Exception:
            active = None
        summary["cron_job_ids"] = {"count": len(cron_job_ids), "active": active}

    return summary


def _identity_from_args_or_env(args) -> Optional[dict]:
    """Build the identity tuple from explicit flags, falling back to the
    live session context. Explicit flags let `hermes scope create` be used
    from a plain shell (no HERMES_SESSION_* context) for scripting/testing."""
    platform = getattr(args, "platform", None)
    chat_id = getattr(args, "chat_id", None)
    if platform and chat_id:
        try:
            return hermes_scope.normalize_scope_identity(
                profile=getattr(args, "profile", None) or "main",
                platform=platform,
                chat_id=chat_id,
                account_id=getattr(args, "account_id", None),
                guild_scope_id=getattr(args, "guild_scope_id", None),
                thread_id=getattr(args, "thread_id", None),
                topic=getattr(args, "topic", None),
            )
        except hermes_scope.ScopeIdentityError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None
    return hermes_scope.identity_from_session_env()


def _cmd_create(args) -> int:
    identity = _identity_from_args_or_env(args)
    if identity is None:
        print(
            "error: could not resolve scope identity — no live session context "
            "and no --platform/--chat-id given",
            file=sys.stderr,
        )
        return 1
    manifest = hermes_scope.create_scope(
        identity,
        goal=args.goal,
        included_topics=args.included_topics or [],
        excluded_topics=args.excluded_topics or [],
    )
    if identity.get("account_id") or identity.get("guild_scope_id"):
        # No live-session field currently carries account_id/guild_scope_id
        # (see hermes_scope.identity_from_session_env) -- a scope created
        # with either set can only ever be resolved again via an explicit
        # --scope-id. Warn instead of letting it silently become
        # unreachable to the auto-registration hooks and to a bare
        # `hermes scope status`/`link`/... in a later turn.
        print(
            "warning: --account-id/--guild-scope-id are not carried by the "
            "live session context, so this scope can only be reached again "
            "via --scope-id " + _redact(manifest["scope_id"]) + " -- it will "
            "not be found by auto-registration hooks or by a bare "
            "`hermes scope status`/`link`.",
            file=sys.stderr,
        )
    print(f"scope {_redact(manifest['scope_id'])}: {manifest['goal']} ({manifest['lifecycle']})")
    return 0


def _cmd_status(args) -> int:
    scope_id = _resolve_scope_id(args)
    if scope_id is None:
        print("scope unknown — no live session context and no --scope-id given", file=sys.stderr)
        return 1
    manifest = hermes_scope.load_scope(scope_id)
    if manifest is None:
        print(f"scope unknown or unreadable: {_redact(scope_id)}", file=sys.stderr)
        return 1
    print(f"scope:    {_redact(manifest['scope_id'])}")
    print(f"goal:     {manifest['goal']}")
    print(f"lifecycle: {manifest['lifecycle']}")
    print(f"created:  {manifest['created_at']}")
    print(f"updated:  {manifest['updated_at']}")
    owned = manifest.get("owned", {})
    liveness = _liveness_summary(manifest)
    print("owned artifacts:")
    for category, values in owned.items():
        live = liveness.get(category)
        if live is None:
            print(f"  {category}: {len(values)}")
        elif live["active"] is None:
            print(f"  {category}: {live['count']} (liveness unknown)")
        else:
            print(f"  {category}: {live['count']} ({live['active']} still active)")
    deps = manifest.get("external_dependencies", [])
    if deps:
        print("external dependencies (not verified progress):")
        for dep in deps:
            print(f"  - {dep.get('description')} (linked {dep.get('linked_at')})")
    return 0


def _cmd_audit(args) -> int:
    """Unredacted dump — the explicit opt-in to see raw routing IDs."""
    scope_id = _resolve_scope_id(args)
    if scope_id is None:
        print("scope unknown — no live session context and no --scope-id given", file=sys.stderr)
        return 1
    manifest = hermes_scope.load_scope(scope_id)
    if manifest is None:
        print(f"scope unknown or unreadable: {scope_id}", file=sys.stderr)
        return 1
    import json

    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def _cmd_link(args) -> int:
    scope_id = _resolve_scope_id(args)
    if scope_id is None:
        print("scope unknown — no live session context and no --scope-id given", file=sys.stderr)
        return 1
    try:
        hermes_scope.link_artifact(scope_id, args.category, args.value)
    except hermes_scope.ScopeIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"linked {args.category}={args.value} to scope {_redact(scope_id)}")
    return 0


def _cmd_unlink(args) -> int:
    scope_id = _resolve_scope_id(args)
    if scope_id is None:
        print("scope unknown — no live session context and no --scope-id given", file=sys.stderr)
        return 1
    try:
        hermes_scope.unlink_artifact(scope_id, args.category, args.value)
    except hermes_scope.ScopeIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"unlinked {args.category}={args.value} from scope {_redact(scope_id)}")
    return 0


def _cmd_dependency(args) -> int:
    scope_id = _resolve_scope_id(args)
    if scope_id is None:
        print("scope unknown — no live session context and no --scope-id given", file=sys.stderr)
        return 1
    try:
        hermes_scope.add_dependency(scope_id, args.description)
    except hermes_scope.ScopeIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"recorded external dependency on scope {_redact(scope_id)}: {args.description}")
    return 0


def _cmd_complete(args) -> int:
    return _set_lifecycle(args, "completed")


def _cmd_archive(args) -> int:
    return _set_lifecycle(args, "archived")


def _set_lifecycle(args, lifecycle: str) -> int:
    scope_id = _resolve_scope_id(args)
    if scope_id is None:
        print("scope unknown — no live session context and no --scope-id given", file=sys.stderr)
        return 1
    try:
        hermes_scope.set_lifecycle(scope_id, lifecycle)
    except hermes_scope.ScopeIdentityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"scope {_redact(scope_id)} -> {lifecycle}")
    return 0


def _add_scope_id_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope-id", dest="scope_id", default=None,
        help="Target scope id. Omit to use the current conversation's scope.",
    )


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach `scope` subcommands to *parent*.

    main.py calls this with the ArgumentParser returned by
    ``subparsers.add_parser("scope", ...)``.
    """
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    subs = parent.add_subparsers(dest="scope_command")

    p_create = subs.add_parser("create", help="Create the scope for this conversation")
    p_create.add_argument("--goal", required=True, help="Free-text goal for this scope")
    p_create.add_argument("--topic", default=None, help="Disambiguating topic label")
    p_create.add_argument("--included-topics", nargs="*", default=None)
    p_create.add_argument("--excluded-topics", nargs="*", default=None)
    p_create.add_argument("--platform", default=None, help="Override: identity platform (for scripted use)")
    p_create.add_argument("--chat-id", default=None, help="Override: identity chat id")
    p_create.add_argument("--thread-id", default=None, help="Override: identity thread id")
    p_create.add_argument("--account-id", default=None, help="Override: identity account id")
    p_create.add_argument("--guild-scope-id", default=None, help="Override: identity guild/workspace id")
    p_create.add_argument("--profile", default=None, help="Override: profile name")
    p_create.set_defaults(func=_cmd_create)

    p_status = subs.add_parser("status", help="Show this conversation's scope and owned artifacts")
    _add_scope_id_arg(p_status)
    p_status.set_defaults(func=_cmd_status)

    p_audit = subs.add_parser("audit", help="Dump the full scope manifest, unredacted")
    _add_scope_id_arg(p_audit)
    p_audit.set_defaults(func=_cmd_audit)

    p_link = subs.add_parser("link", help="Manually link an artifact this scope owns")
    _add_scope_id_arg(p_link)
    p_link.add_argument(
        "category",
        choices=sorted(hermes_scope._OWNED_CATEGORIES),
        help="Artifact category",
    )
    p_link.add_argument("value", help="Artifact id/name (e.g. branch name, PR url)")
    p_link.set_defaults(func=_cmd_link)

    p_unlink = subs.add_parser("unlink", help="Remove an artifact link from this scope")
    _add_scope_id_arg(p_unlink)
    p_unlink.add_argument("category", choices=sorted(hermes_scope._OWNED_CATEGORIES))
    p_unlink.add_argument("value", help="Artifact id/name to unlink")
    p_unlink.set_defaults(func=_cmd_unlink)

    p_dependency = subs.add_parser(
        "dependency", help="Record an external dependency (tracked separately from owned progress)"
    )
    _add_scope_id_arg(p_dependency)
    p_dependency.add_argument("description", help="What this scope is waiting on")
    p_dependency.set_defaults(func=_cmd_dependency)

    p_complete = subs.add_parser("complete", help="Mark this scope's work complete")
    _add_scope_id_arg(p_complete)
    p_complete.set_defaults(func=_cmd_complete)

    p_archive = subs.add_parser("archive", help="Archive this scope")
    _add_scope_id_arg(p_archive)
    p_archive.set_defaults(func=_cmd_archive)


def cli_main(argv=None) -> int:
    """Standalone entry (also usable by hermes_cli.main fallthrough)."""
    parser = argparse.ArgumentParser(prog="hermes scope")
    register_cli(parser)
    args = parser.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        return 0
    return int(fn(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli_main())
