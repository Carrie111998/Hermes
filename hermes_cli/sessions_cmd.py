"""``hermes sessions`` command â€” extracted from ``hermes_cli/main.py``.

Mechanical move (main.py decomposition): ``cmd_sessions`` was a ``def`` nested
inside ``main()``'s body; its dispatch on ``args.sessions_action`` is lifted
byte-identical. A symtable/AST closure check found exactly two free variables:

* ``_confirm_prompt`` â€” a sibling nested def with zero captures of its own;
  moved here to module level, byte-identical.
* ``sessions_parser`` â€” a ``main()``-local (the argparse subparser, used only
  for ``sessions_parser.print_help()`` in the fallthrough branch). It is
  threaded as a keyword parameter via ``functools.partial`` at the
  ``set_defaults(func=...)`` wiring site in ``main()``.

Helpers that stay in ``hermes_cli.main`` (``get_hermes_home``,
``_relative_time``, ``_session_browse_picker``, ``_size_delta_label``) are
delegated through call-time wrappers below so existing test monkeypatches on
``hermes_cli.main.<name>`` keep reaching this code path, and so imports stay
one-way (main.py imports this module; the reverse happens only lazily at call
time â€” no import cycle).
"""

import os
import sys
from pathlib import Path


def _m():
    """Lazy ``hermes_cli.main`` reference (call-time, keeps patches working)."""
    from hermes_cli import main

    return main


def get_hermes_home():
    return _m().get_hermes_home()


def _relative_time(ts):
    return _m()._relative_time(ts)


def _session_browse_picker(sessions):
    return _m()._session_browse_picker(sessions)


def _size_delta_label(saved_mb):
    return _m()._size_delta_label(saved_mb)


def _confirm_prompt(prompt: str) -> bool:
    """Prompt for y/N confirmation, safe against non-TTY environments."""
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_sessions(args, sessions_parser=None):
    import json as _json

    action = args.sessions_action

    # 'repair' and 'recover' must run BEFORE opening SessionDB(): a
    # malformed schema is exactly the case where SessionDB() can't open.
    # Recovery additionally promises never to open the supplied source
    # directly, so it operates through its own disposable source copy.
    if action == "repair":
        from hermes_state import (
            DEFAULT_DB_PATH,
            _db_opens_cleanly,
            repair_state_db_schema,
        )

        db_path = DEFAULT_DB_PATH
        if not db_path.exists():
            print(f"No session database at {db_path} (nothing to repair).")
            return
        reason = _db_opens_cleanly(db_path)
        if reason is None:
            print(f"âœ“ {db_path} opens cleanly â€” no repair needed.")
            return
        print(f"âœ— {db_path} does not open cleanly: {reason}")
        if getattr(args, "check_only", False):
            return
        print("Repairing (a backup copy is made first)â€¦")
        report = repair_state_db_schema(
            db_path, backup=not getattr(args, "no_backup", False)
        )
        if report.get("repaired"):
            if report.get("backup_path"):
                print(f"  backup: {report['backup_path']}")
            print(f"  strategy: {report.get('strategy')}")
            try:
                from hermes_state import SessionDB

                db = SessionDB()
                try:
                    n = db._conn.execute(
                        "SELECT COUNT(*) FROM sessions"
                    ).fetchone()[0]
                finally:
                    db.close()
                print(f"âœ“ Repaired â€” {n} sessions recovered.")
            except Exception:
                print("âœ“ Repaired.")
        else:
            print(f"âœ— Repair failed: {report.get('error')}")
            if report.get("backup_path"):
                print(f"  A backup is preserved at: {report['backup_path']}")
            print("  Keep state.db and the backup; do not delete them.")
            # Without this pointer the user is at a dead end: in-place
            # repair has failed and nothing tells them the non-destructive
            # offline recovery path exists. Lead with --inspect-only so
            # they confirm the data is readable before writing anything.
            print("")
            print("  Next step â€” offline recovery (never modifies the source):")
            source_hint = report.get("backup_path") or db_path
            print(f"    hermes sessions recover --source {source_hint} \\")
            print("        --inspect-only")
            print("  If that reports the data is recoverable, rebuild it into")
            print("  a NEW database (the active one is left untouched):")
            print(f"    hermes sessions recover --source {source_hint} \\")
            print("        --output recovered-state.db")
        return

    if action == "recover":
        import sqlite3 as _sqlite3

        from hermes_cli.session_recovery import (
            SessionRecoveryError,
            inspect_session_database,
            recover_session_database,
            write_recovery_report,
        )

        source = args.source
        output = getattr(args, "output", None)
        inspect_only = bool(getattr(args, "inspect_only", False))
        allow_partial = bool(getattr(args, "allow_partial", False))
        report_path = getattr(args, "report", None)
        if inspect_only and output is not None:
            print("Error: --output cannot be used with --inspect-only.")
            return 2
        if inspect_only and allow_partial:
            print("Error: --allow-partial cannot be used with --inspect-only.")
            return 2
        if not inspect_only and output is None:
            print("Error: --output is required unless --inspect-only is used.")
            return 2
        if not inspect_only and report_path is None:
            report_path = output.with_name(output.name + ".recovery.json")
        if (
            report_path is not None
            and os.path.lexists(report_path.expanduser())
        ):
            print(f"Error: refusing to overwrite existing report: {report_path}")
            return 2

        try:
            if inspect_only:
                report = inspect_session_database(
                    source,
                    work_dir=getattr(args, "work_dir", None),
                )
            else:
                last_progress = {"table": None}

                def _recovery_progress(info):
                    table = info.get("table")
                    copied = int(info.get("copied_rows") or 0)
                    total = info.get("source_rows")
                    if table != last_progress["table"]:
                        if last_progress["table"] is not None:
                            print()
                        print(f"  {table}: ", end="", flush=True)
                        last_progress["table"] = table
                    suffix = f"/{int(total):,}" if total is not None else ""
                    print(f"\r  {table}: {copied:,}{suffix}", end="", flush=True)

                print("Recovering canonical session data into a new databaseâ€¦")
                report = recover_session_database(
                    source,
                    output,
                    work_dir=getattr(args, "work_dir", None),
                    chunk_size=getattr(args, "chunk_size", 1000),
                    progress_cb=_recovery_progress,
                    allow_partial=allow_partial,
                )
                if last_progress["table"] is not None:
                    print()
        except (SessionRecoveryError, OSError, _sqlite3.DatabaseError) as exc:
            print(f"Error: session recovery failed: {exc}")
            print("The supplied source database was not replaced or deleted.")
            return 1

        if report_path is not None:
            try:
                written_report = write_recovery_report(report_path, report)
            except (FileExistsError, OSError) as exc:
                print(f"Error: could not write recovery report: {exc}")
                return 1
            print(f"Recovery report: {written_report}")
        else:
            print(_json.dumps(report, indent=2, sort_keys=True))

        if inspect_only:
            return 0 if report.get("recoverable") else 1
        if report.get("complete"):
            print(f"âœ“ Recovered database verified at: {output}")
            print("  The active session database was not changed.")
            print("  Review the JSON report before installing this database.")
            return 0
        if allow_partial and report.get("verified"):
            counts = report.get("verification", {}).get("table_counts", {})
            print(f"âœ“ Partial recovery output verified at: {output}")
            print(
                "  Recovered "
                f"{int(counts.get('sessions') or 0):,} sessions and "
                f"{int(counts.get('messages') or 0):,} messages."
            )
            print("  The active session database was not changed.")
            print(
                "  This output is incomplete. Review every skipped range "
                "and orphan count in the JSON report before installing it."
            )
            return 0
        print("âœ— Recovery output did not pass every verification check.")
        print("  Do not install it. Review the JSON report for partial data or errors.")
        return 1

    try:
        from hermes_state import SessionDB

        db = SessionDB()
    except Exception as e:
        print(f"Error: Could not open session database: {e}")
        return

    # Hide third-party tool sessions by default, but honour explicit --source
    _source = getattr(args, "source", None)
    _exclude = None if _source else ["tool"]

    if action == "list":
        from hermes_state import workspace_key as _ws_key

        sessions = db.list_sessions_rich(
            source=args.source, exclude_sources=_exclude, limit=args.limit
        )

        # Workspace filter: match a session by its workspace key (git repo
        # root, else cwd) â€” path substring or exact basename.
        _ws_filter = (getattr(args, "workspace", None) or "").strip()
        if _ws_filter:
            _needle = _ws_filter.lower()

            def _in_workspace(s):
                key = (_ws_key(s) or "").lower()
                return bool(key) and (
                    _needle in key or _needle == os.path.basename(key.rstrip("/\\"))
                )

            sessions = [s for s in sessions if _in_workspace(s)]

        if not sessions:
            print("No sessions found.")
            return

        # Short workspace label: the repo/dir basename, "â€”" when unbound. The
        # Workspace column only appears once at least one session carries one
        # (or when filtering), so all-unbound listings read as before.
        def _ws_label(s):
            key = _ws_key(s)
            return (os.path.basename(key.rstrip("/\\")) or key) if key else "â€”"

        has_ws = bool(_ws_filter) or any(_ws_key(s) for s in sessions)
        has_titles = any(s.get("title") for s in sessions)

        if has_ws:
            if has_titles:
                print(f"{'Title':<28} {'Workspace':<18} {'Last Active':<13} {'ID'}")
                print("â”€" * 110)
            else:
                print(f"{'Preview':<38} {'Workspace':<18} {'Last Active':<13} {'Src':<6} {'ID'}")
                print("â”€" * 100)
            for s in sessions:
                last_active = _relative_time(s.get("last_active"))
                ws = _ws_label(s)[:16]
                if has_titles:
                    title = (s.get("title") or "â€”")[:26]
                    print(f"{title:<28} {ws:<18} {last_active:<13} {s['id']}")
                else:
                    preview = s.get("preview", "")[:36]
                    print(f"{preview:<38} {ws:<18} {last_active:<13} {s['source']:<6} {s['id']}")
            return

        if has_titles:
            print(f"{'Title':<32} {'Preview':<40} {'Last Active':<13} {'ID'}")
            print("â”€" * 110)
        else:
            print(f"{'Preview':<50} {'Last Active':<13} {'Src':<6} {'ID'}")
            print("â”€" * 95)
        for s in sessions:
            last_active = _relative_time(s.get("last_active"))
            preview = (
                s.get("preview", "")[:38]
                if has_titles
                else s.get("preview", "")[:48]
            )
            if has_titles:
                title = (s.get("title") or "â€”")[:30]
                sid = s["id"]
                print(f"{title:<32} {preview:<40} {last_active:<13} {sid}")
            else:
                sid = s["id"]
                print(f"{preview:<50} {last_active:<13} {s['source']:<6} {sid}")

    elif action == "export":
        from hermes_cli.session_filters import (
            build_prune_filters,
            describe_filters,
        )

        _filter_arg_names = (
            "older_than", "newer_than", "before", "after",
            "source", "title", "end_reason", "cwd",
            "min_messages", "max_messages", "model", "provider",
            "user", "chat_id", "chat_type", "branch",
            "min_tokens", "max_tokens", "min_cost", "max_cost",
            "min_tool_calls", "max_tool_calls",
        )
        _any_filters = any(
            getattr(args, a, None) is not None for a in _filter_arg_names
        )
        filters = None
        if _any_filters:
            try:
                filters = build_prune_filters(args)
            except ValueError as e:
                print(f"Error: {e}")
                return
            # Unlike prune/archive, export includes archived sessions.
            filters["archived"] = None

        def _redact(data):
            if not args.redact or data is None:
                return data
            from hermes_cli.session_export_md import redact_session_data

            return redact_session_data(data)

        def _collect_sessions():
            """Resolve --session-id / filters / bare export into a list
            of redacted session dicts, or None after printing an error."""
            if args.session_id:
                resolved = db.resolve_session_id(args.session_id)
                data = _redact(db.export_session(resolved)) if resolved else None
                if not data:
                    print(f"Session '{args.session_id}' not found.")
                    return None
                return [data]
            if filters:
                candidates = db.list_prune_candidates(**filters)
                if args.dry_run:
                  ïm{¶‰žËkºwµçhðÄÝô€ˆ4(€€€€€€€€€€€€€€€€€€€˜‰íÍlÍ½ÕÉ”tèðÄÁôíµ½‘•°èðÈÑô€ˆ4(€€€€€€€€€€€€€€€€€€€˜‰íÍlµ•ÍÍ…•}½Õ¹ÐtèøÑôµÍÌ€íÑ¥Ñ±•ôˆ4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€¥˜±•¸¡…¹‘¥‘…Ñ•Ì¤€ø±•¸¡Í¡½Ý¸¤è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€ƒŠ˜…¹í±•¸¡…¹‘¥‘…Ñ•Ì¤€´±•¸¡Í¡½Ý¸¥ôµ½É”ˆ¤4(€€€€€€€€€€€¥˜…ÉÌ¹‘Éå}ÉÕ¸è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰ÉäÉÕ¸ƒŠP¹½Ñ¡¥¹œì‘•±•Ñ•œ¥˜…Ñ¥½¸€ôô€ÁÉÕ¹”œ•±Í”€…É¡¥Ù•ô¸ˆ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€¥˜¹½Ð…ÉÌ¹å•Ìè4(€€€€€€€€€€€¥˜¹½Ð}½¹™¥Éµ}ÁÉ½µÁÐ 4(€€€€€€€€€€€€€€€˜‰íÙ•É‰ôÑ¡•Í”í±•¸¡…¹‘¥‘…Ñ•Ì¥ôÍ•ÍÍ¥½¸¡Ì¤€¡í}ÍÁ…¹ô¤ümä½9t€ˆ4(€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð ‰…¹•±±•¸ˆ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€¥˜…Ñ¥½¸€ôô€‰ÁÉÕ¹”ˆè4(€€€€€€€€€€€Í•ÍÍ¥½¹Í}‘¥È€ô•Ñ}¡•Éµ•Í}¡½µ” ¤€¼€‰Í•ÍÍ¥½¹Ìˆ4(€€€€€€€€€€€½Õ¹Ð€ô‘ˆ¹ÁÉÕ¹•}Í•ÍÍ¥½¹Ì¡Í•ÍÍ¥½¹Í}‘¥ÈõÍ•ÍÍ¥½¹Í}‘¥È°€¨©™¥±Ñ•ÉÌ¤4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰AÉÕ¹•í½Õ¹ÑôÍ•ÍÍ¥½¸¡Ì¤¸ˆ¤4(€€€€€€€•±Í”è4(€€€€€€€€€€€½Õ¹Ð€ô‘ˆ¹…É¡¥Ù•}Í•ÍÍ¥½¹Ì ¨©™¥±Ñ•ÉÌ¤4(€€€€€€€€€€€ÁÉ¥¹Ð 4(€€€€€€€€€€€€€€€˜‰É¡¥Ù•í½Õ¹ÑôÍ•ÍÍ¥½¸¡Ì¤¸Q¡•äÉ”¡¥‘‘•¸™É½´±¥ÍÑ¥¹Ì€ˆ4(€€€€€€€€€€€€€€€€‰‰ÕÐ™Õ±±äÉ•½Ù•É…‰±”€¡¹½Ñ¡¥¹œÝ…Ì‘•±•Ñ•¤¸ˆ4(€€€€€€€€€€€€¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰É•¹…µ”ˆè4(€€€€€€€É•Í½±Ù•‘}Í•ÍÍ¥½¹}¥€ô‘ˆ¹É•Í½±Ù•}Í•ÍÍ¥½¹}¥¡…ÉÌ¹Í•ÍÍ¥½¹}¥¤4(€€€€€€€¥˜¹½ÐÉ•Í½±Ù•‘}Í•ÍÍ¥½¹}¥è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰M•ÍÍ¥½¸€í…ÉÌ¹Í•ÍÍ¥½¹}¥‘ôœ¹½Ð™½Õ¹¸ˆ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€Ñ¥Ñ±”€ô€ˆ€ˆ¹©½¥¸¡…ÉÌ¹Ñ¥Ñ±”¤4(€€€€€€€ÑÉäè4(€€€€€€€€€€€¥˜‘ˆ¹Í•Ñ}Í•ÍÍ¥½¹}Ñ¥Ñ±”¡É•Í½±Ù•‘}Í•ÍÍ¥½¹}¥°Ñ¥Ñ±”¤è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰M•ÍÍ¥½¸€íÉ•Í½±Ù•‘}Í•ÍÍ¥½¹}¥‘ôœÉ•¹…µ•Ñ¼èíÑ¥Ñ±•ôˆ¤4(€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰M•ÍÍ¥½¸€í…ÉÌ¹Í•ÍÍ¥½¹}¥‘ôœ¹½Ð™½Õ¹¸ˆ¤4(€€€€€€€•á•ÁÐY…±Õ•ÉÉ½È…Ì”è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰ÉÉ½Èèí•ôˆ¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰É•Ñ¥Ñ±”µÍ­¥±±Ìˆè4(€€€€€€€™É½´…•¹Ð¹Í­¥±±}½µµ…¹‘Ì¥µÁ½ÉÐ‘•ÍÉ¥‰•}Í­¥±±}¥¹Ù½…Ñ¥½¸4(€€€€€€€™É½´…•¹Ð¹Ñ¥Ñ±•}•¹•É…Ñ½È¥µÁ½ÉÐ•¹•É…Ñ•}Ñ¥Ñ±”4(4(€€€€€€€±¥µ¥Ð€ôµ…à Ä°¥¹Ð¡•Ñ…ÑÑÈ¡…ÉÌ°€‰±¥µ¥Ðˆ°€ÈÀÀ¤½È€ÈÀÀ¤¤4(€€€€€€€…ÁÁ±å}¡…¹•Ì€ô‰½½°¡•Ñ…ÑÑÈ¡…ÉÌ°€‰…ÁÁ±äˆ°…±Í”¤¤4(4(€€€€€€€‘•˜}¥Í}Ñ¥Ñ±•±¥­”¡…¹‘¥‘…Ñ”èÍÑÈ¤€´ø‰½½°è4(€€€€€€€€€€€€ˆˆ‰I•©•Ð„…¹‘¥‘…Ñ”Ñ¡…Ð¥Í¸Ð„Ñ¥Ñ±”…Ð…±°¸4(4(€€€€€€€€€€€¸…Õá¥±¥…Éäµ½‘•°½…Í¥½¹…±±ä…¹ÍÝ•ÉÌÑ¡”ÁÉ½µÁÐ¥¹ÍÑ•…½˜4(€€€€€€€€€€€Ñ¥Ñ±¥¹œ¥Ð…¹•¡½•ÌÑ¡”…ÍÍ¥ÍÑ…¹ÐÌ½ÕÑÁÕÐ€ œ‘˜€µ €¼œ¤¸Q¡”4(€€€€€€€€€€€±¥Ù”Á…Ñ ¡…Ì¹¼…±Ñ•É¹…Ñ¥Ù”…¹Ñ…­•ÌÝ¡…Ð¥Ð•ÑÌ°‰ÕÐÑ¡¥Ì¥Ì4(€€€€€€€€€€€„IA%HƒŠPÉ•Á±…¥¹œ„Í•ÉÙ¥•…‰±”Ñ¥Ñ±”Ý¥Ñ ½µµ…¹½ÕÑÁÕÐ4(€€€€€€€€€€€Ý½Õ±µ…­”Ñ¡¥¹ÌÝ½ÉÍ”°Í¼­••ÀÑ¡”½±½¹”¸4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€É•ÑÕÉ¸‰½½°¡…¹‘¥‘…Ñ”¤…¹…¹‘¥‘…Ñ•lÁt¹¥Í…±¹Õ´ ¤4(4(€€€€€€€…¹‘¥‘…Ñ•Ì€ô‘ˆ¹±¥ÍÑ}Í­¥±±}Í…™™½±‘•‘}Í•ÍÍ¥½¹Ì¡±¥µ¥Ðõ±¥µ¥Ð¤4(€€€€€€€¥˜¹½Ð…¹‘¥‘…Ñ•Ìè4(€€€€€€€€€€€ÁÉ¥¹Ð ‰9¼Í•ÍÍ¥½¹ÌÝ•É”Ñ¥Ñ±•™É½´„€½Í­¥±°¥¹Ù½…Ñ¥½¸¸ˆ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€ÁÉ¥¹Ð 4(€€€€€€€€€€€˜‰í±•¸¡…¹‘¥‘…Ñ•Ì¥ôÍ•ÍÍ¥½¸¡Ì¤½Á•¹•Ý¥Ñ „€½Í­¥±°ˆ4(€€€€€€€€€€€˜‰ìœœ¥˜…ÁÁ±å}¡…¹•Ì•±Í”€œ€¡‘ÉäÉÕ¸ƒŠPÁ…ÍÌ€´µ…ÁÁ±äÑ¼ÝÉ¥Ñ”¤ôèˆ4(€€€€€€€€¤4(€€€€€€€¡…¹•€ô€À4(€€€€€€€™½ÈÉ½Ü¥¸…¹‘¥‘…Ñ•Ìè4(€€€€€€€€€€€Í•ÍÍ¥½¹}¥€ôÉ½Ýl‰¥‰t4(€€€€€€€€€€€ÑåÁ•€ô‘•ÍÉ¥‰•}Í­¥±±}¥¹Ù½…Ñ¥½¸¡É½Ýl‰½¹Ñ•¹Ð‰t¤½È€ˆˆ4(€€€€€€€€€€€¹•Ý}Ñ¥Ñ±”€ô•¹•É…Ñ•}Ñ¥Ñ±”¡ÑåÁ•¤4(€€€€€€€€€€€¥˜¹½Ð¹•Ý}Ñ¥Ñ±”½È¹•Ý}Ñ¥Ñ±”€ôôÉ½Ýl‰Ñ¥Ñ±”‰tè4(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€¥˜¹½Ð}¥Í}Ñ¥Ñ±•±¥­”¡¹•Ý}Ñ¥Ñ±”¤è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€íÍ•ÍÍ¥½¹}¥‘õq¸€€€­•ÁÐíÉ½ÝlÑ¥Ñ±”t…ÉôƒŠP½Ðí¹•Ý}Ñ¥Ñ±”…Éôˆ¤4(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€íÍ•ÍÍ¥½¹}¥‘õq¸€€€íÉ½ÝlÑ¥Ñ±”t…Éõq¸€€€ƒŠHí¹•Ý}Ñ¥Ñ±”…Éôˆ¤4(€€€€€€€€€€€¡…¹•€¬ô€Ä4(€€€€€€€€€€€¥˜¹½Ð…ÁÁ±å}¡…¹•Ìè4(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€‘ˆ¹Í•Ñ}Í•ÍÍ¥½¹}Ñ¥Ñ±”¡Í•ÍÍ¥½¹}¥°¹•Ý}Ñ¥Ñ±”¤4(€€€€€€€€€€€•á•ÁÐY…±Õ•ÉÉ½Èè4(€€€€€€€€€€€€€€€€ŒU¹¥ÅÕ”µÑ¥Ñ±”½±±¥Í¥½¸¸•‘ÕÁ”Ñ¡”Í…µ”Ý…äÑ¡”±¥Ù”4(€€€€€€€€€€€€€€€€Œ…ÕÑ¼µÑ¥Ñ±•È‘½•Ì€¡‰…Í”€ŒÈ°‰…Í”€ŒÌ°€¸¸¸¤É…Ñ¡•ÈÑ¡…¸4(€€€€€€€€€€€€€€€€Œ±•…Ù¥¹œÑ¡”±•…­•Ñ¥Ñ±”¥¸Á±…”¸4(€€€€€€€€€€€€€€€‘•‘ÕÁ•€ô‘ˆ¹•Ñ}¹•áÑ}Ñ¥Ñ±•}¥¹}±¥¹•…”¡¹•Ý}Ñ¥Ñ±”¤4(€€€€€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€€€€€‘ˆ¹Í•Ñ}Í•ÍÍ¥½¹}Ñ¥Ñ±”¡Í•ÍÍ¥½¹}¥°‘•‘ÕÁ•¤4(€€€€€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€€€€¡É•¹…µ•Ñ¼í‘•‘ÕÁ•…ÉôƒŠPÑ¥Ñ±”Ý…ÌÑ…­•¸¤ˆ¤4(€€€€€€€€€€€€€€€•á•ÁÐY…±Õ•ÉÉ½È…Ì”è4(€€€€€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€€€Í­¥ÁÁ•èí•ôˆ¤4(€€€€€€€€€€€€€€€€€€€¡…¹•€´ô€Ä4(4(€€€€€€€¥˜¹½Ð¡…¹•è4(€€€€€€€€€€€ÁÉ¥¹Ð ˆ€•Ù•ÉäÑ¥Ñ±”…±É•…‘äÉ•™±•ÑÌÑ¡”ÕÍ•ÈÌÉ•ÅÕ•ÍÐ¸ˆ¤4(€€€€€€€•±¥˜…ÁÁ±å}¡…¹•Ìè4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‹ŠrLI”µÑ¥Ñ±•í¡…¹•‘ôÍ•ÍÍ¥½¸¡Ì¤¸ˆ¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰‰É½ÝÍ”ˆè4(€€€€€€€±¥µ¥Ð€ô•Ñ…ÑÑÈ¡…ÉÌ°€‰±¥µ¥Ðˆ°€ÔÀÀ¤½È€ÔÀÀ4(€€€€€€€Í½ÕÉ”€ô•Ñ…ÑÑÈ¡…ÉÌ°€‰Í½ÕÉ”ˆ°9½¹”¤4(€€€€€€€}‰É½ÝÍ•}•á±Õ‘”€ô9½¹”¥˜Í½ÕÉ”•±Í”l‰Ñ½½°‰t4(€€€€€€€Í•ÍÍ¥½¹Ì€ô‘ˆ¹±¥ÍÑ}Í•ÍÍ¥½¹Í}É¥  4(€€€€€€€€€€€Í½ÕÉ”õÍ½ÕÉ”°•á±Õ‘•}Í½ÕÉ•Ìõ}‰É½ÝÍ•}•á±Õ‘”°±¥µ¥Ðõ±¥µ¥Ð4(€€€€€€€€¤4(€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€¥˜¹½ÐÍ•ÍÍ¥½¹Ìè4(€€€€€€€€€€€ÁÉ¥¹Ð ‰9¼Í•ÍÍ¥½¹Ì™½Õ¹¸ˆ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€Í•±•Ñ•‘}¥€ô}Í•ÍÍ¥½¹}‰É½ÝÍ•}Á¥­•È¡Í•ÍÍ¥½¹Ì¤4(€€€€€€€¥˜¹½ÐÍ•±•Ñ•‘}¥è4(€€€€€€€€€€€ÁÉ¥¹Ð ‰…¹•±±•¸ˆ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€€Œ1…Õ¹ ¡•Éµ•Ì€´µÉ•ÍÕµ”€ñ¥ø‰äÉ•Á±…¥¹œÑ¡”ÕÉÉ•¹ÐÁÉ½•ÍÌ4(€€€€€€€ÁÉ¥¹Ð¡˜‰I•ÍÕµ¥¹œÍ•ÍÍ¥½¸èíÍ•±•Ñ•‘}¥‘ôˆ¤4(€€€€€€€™É½´¡•Éµ•Í}±¤¹É•±…Õ¹ ¥µÁ½ÉÐÉ•±…Õ¹ 4(4(€€€€€€€É•±…Õ¹ ¡lˆ´µÉ•ÍÕµ”ˆ°Í•±•Ñ•‘}¥‘t¤4(€€€€€€€É•ÑÕÉ¸€€ŒÝ½¸ÐÉ•… ¡•É”…™Ñ•È•á•ÙÀ4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰½ÁÑ¥µ¥é”ˆè4(€€€€€€€‘‰}Á…Ñ €ô‘ˆ¹‘‰}Á…Ñ 4(€€€€€€€‰•™½É•}µˆ€ô€ 4(€€€€€€€€€€€½Ì¹Á…Ñ ¹•ÑÍ¥é”¡‘‰}Á…Ñ ¤€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤4(€€€€€€€€€€€¥˜‘‰}Á…Ñ ¹•á¥ÍÑÌ ¤4(€€€€€€€€€€€•±Í”€À¸À4(€€€€€€€€¤4(€€€€€€€ÁÉ¥¹Ð ‰=ÁÑ¥µ¥é¥¹œÍ•ÍÍ¥½¸ÍÑ½É”€¡QLµ•É”€¬YUU4§Š˜ˆ¤4(€€€€€€€ÑÉäè4(€€€€€€€€€€€€ŒÙ…ÕÕ´ ¤µ•É•ÌQLÔÍ•µ•¹ÑÌ€¡½ÁÑ¥µ¥é•}™ÑÌ¤Ñ¡•¸YUU5Ì°4(€€€€€€€€€€€€Œ…¹É•ÑÕÉ¹ÌÑ¡”¹Õµ‰•È½˜¥¹‘•á•Ì¥Ðµ•É•¸4(€€€€€€€€€€€¸€ô‘ˆ¹Ù…ÕÕ´ ¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰ÉÉ½Èè½ÁÑ¥µ¥é…Ñ¥½¸™…¥±•èí•ôˆ¤4(€€€€€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€…™Ñ•É}µˆ€ô€ 4(€€€€€€€€€€€½Ì¹Á…Ñ ¹•ÑÍ¥é”¡‘‰}Á…Ñ ¤€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤4(€€€€€€€€€€€¥˜‘‰}Á…Ñ ¹•á¥ÍÑÌ ¤4(€€€€€€€€€€€•±Í”€À¸À4(€€€€€€€€¤4(€€€€€€€€ŒM…µ”]0…Ù•…Ð…Ì½ÁÑ¥µ¥é”µÍÑ½É…”è…™Ñ•È„YUU4Ñ¡”µ…¥¸™¥±”4(€€€€€€€€Œ½¸‘¥Í¬±…ÌÕ¹Ñ¥°Ñ¡”]0¥Ì¡•­Á½¥¹Ñ•‰…¬€¡É•™ÕÍ•Ý¡¥±”„4(€€€€€€€€Œ±¥Ù”…Ñ•Ý…ä¡½±‘Ì„É•…µµ…É¬¤°Í¼ÍÑ…Ð ¤Õ¹‘•ÉÍÑ…Ñ•ÌÑ¡”Ý¥¸…¹4(€€€€€€€€Œ…¸¼¹•…Ñ¥Ù”¸ME1¥Ñ”ÌÁ…”…½Õ¹Ñ¥¹œ¥Ì½ÉÉ•Ð¥µµ•‘¥…Ñ•±ä¸4(€€€€€€€±½¥…±}…™Ñ•È€ô‘ˆ¹±½¥…±}Í¥é•}‰åÑ•Ì ¤4(€€€€€€€¥˜±½¥…±}…™Ñ•È¥Ì¹½Ð9½¹”è4(€€€€€€€€€€€…™Ñ•É}µˆ€ô±½¥…±}…™Ñ•È€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤4(€€€€€€€Í…Ù•€ô‰•™½É•}µˆ€´…™Ñ•É}µˆ4(€€€€€€€ÁÉ¥¹Ð¡˜‰=ÁÑ¥µ¥é•í¹ôQL¥¹‘•à¡•Ì¤¸ˆ¤4(€€€€€€€ÁÉ¥¹Ð 4(€€€€€€€€€€€˜‰…Ñ…‰…Í”Í¥é”èí‰•™½É•}µˆè¸Å™ô5€´øí…™Ñ•É}µˆè¸Å™ô5€ˆ4(€€€€€€€€€€€˜ˆ¡í}Í¥é•}‘•±Ñ…}±…‰•°¡Í…Ù•¥ô¤ˆ4(€€€€€€€€¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰±•…¸µµ…É­•ÉÌˆè4(€€€€€€€¥˜…ÉÌ¹‘Éå}ÉÕ¸è4(€€€€€€€€€€€ÁÉ¥¹Ð ‰ÉäÉÕ¸ƒŠPÍ…¹¹¥¹œ™½ÈÍÑ…±”Ñ½½°µ…±°µ…É­•ÈÉ½ÝÌ€ ŒÜàÄÐà§Š˜ˆ¤4(€€€€€€€•±Í”è4(€€€€€€€€€€€ÁÉ¥¹Ð ‰M…¹¹¥¹œ™½ÈÍÑ…±”Ñ½½°µ…±°µ…É­•ÈÉ½ÝÌ€ ŒÜàÄÐà§Š˜ˆ¤4(€€€€€€€É•Á½ÉÐ€ô‘ˆ¹ÁÕÉ•}ÍÑ…±•}Ñ½½±}…±±}µ…É­•ÉÌ 4(€€€€€€€€€€€‘Éå}ÉÕ¸õ…ÉÌ¹‘Éå}ÉÕ¸°‰…­ÕÀõ¹½Ð…ÉÌ¹¹½}‰…­ÕÀ4(€€€€€€€€¤4(€€€€€€€¥˜É•Á½ÉÑl‰É½ÝÍ}…™™•Ñ•‰t€ôô€Àè4(€€€€€€€€€€€ÁÉ¥¹Ð ‹ŠrL9¼…™™•Ñ•É½ÝÌ™½Õ¹ƒŠP¹½Ñ¡¥¹œÑ¼±•…¸¸ˆ¤4(€€€€€€€•±¥˜…ÉÌ¹‘Éå}ÉÕ¸è4(€€€€€€€€€€€ÁÉ¥¹Ð 4(€€€€€€€€€€€€€€€˜‰]½Õ±±•…ÈíÉ•Á½ÉÑlÉ½ÝÍ}…™™•Ñ•uôÉ½Ü¡Ì¤è€ˆ4(€€€€€€€€€€€€€€€˜‰¥‘ÌíÉ•Á½ÉÑlÉ½Ý}¥‘Ìuôˆ4(€€€€€€€€€€€€¤4(€€€€€€€•±Í”è4(€€€€€€€€€€€¥˜É•Á½ÉÑl‰‰…­ÕÁ}Á…Ñ ‰tè4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€‰…­ÕÀèíÉ•Á½ÉÑl‰…­ÕÁ}Á…Ñ uôˆ¤4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‹ŠrL±•…É•íÉ•Á½ÉÑlÉ½ÝÍ}…™™•Ñ•uôÉ½Ü¡Ì¤¸ˆ¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰½ÁÑ¥µ¥é”µÍÑ½É…”ˆè4(€€€€€€€‘‰}Á…Ñ €ô‘ˆ¹‘‰}Á…Ñ 4(€€€€€€€¥˜¹½Ð‘ˆ¹™ÑÍ}½ÁÑ¥µ¥é•}…Ù…¥±…‰±” ¤è4(€€€€€€€€€€€ÁÉ¥¹Ð ‰M•…É ¥¹‘•à¥Ì…±É•…‘ä½¸Ñ¡”½µÁ…Ð±…å½ÕÐƒŠP¹½Ñ¡¥¹œÑ¼‘¼¸ˆ¤4(€€€€€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€‰•™½É•}‰åÑ•Ì€ô½Ì¹Á…Ñ ¹•ÑÍ¥é”¡‘‰}Á…Ñ ¤¥˜‘‰}Á…Ñ ¹•á¥ÍÑÌ ¤•±Í”€À4(€€€€€€€‰•™½É•}µˆ€ô‰•™½É•}‰åÑ•Ì€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤4(4(€€€€€€€€Œ¥Í¬ÁÉ•™±¥¡ÐèÑ¡”É•‰Õ¥±…‘‘ÌÑ¡”¹•Ü¥¹‘•à‰•™½É”Ñ¡”½±¥Ì4(€€€€€€€€ŒÑ½É¸‘½Ý¸°…¹Ñ¡”™¥¹…°YUU4¹••‘Ì„™Õ±°Í•½¹½Áä½˜Ñ¡”4(€€€€€€€€Œ™¥±”¸I•ÅÕ¥É”¡•…‘É½½´ƒŠ& ÕÉÉ•¹Ð™¥±”Í¥é”Ñ¼™¥¹¥Í ±•…¹±ä¸4(€€€€€€€‘½}Ù…ÕÕ´€ô¹½Ð•Ñ…ÑÑÈ¡…ÉÌ°€‰¹½}Ù…ÕÕ´ˆ°…±Í”¤4(€€€€€€€ÑÉäè4(€€€€€€€€€€€¥µÁ½ÉÐÍ¡ÕÑ¥°…Ì}Í¡ÕÑ¥°4(€€€€€€€€€€€™É••}‰åÑ•Ì€ô}Í¡ÕÑ¥°¹‘¥Í­}ÕÍ…”¡‘‰}Á…Ñ ¹Á…É•¹Ð¤¹™É•”4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è4(€€€€€€€€€€€™É••}‰åÑ•Ì€ô9½¹”4(€€€€€€€¹••‘}‰åÑ•Ì€ô‰•™½É•}‰åÑ•Ì¥˜‘½}Ù…ÕÕ´•±Í”¥¹Ð¡‰•™½É•}‰åÑ•Ì€¨€À¸Ì¤4(€€€€€€€ÁÉ¥¹Ð¡˜‰M•…É µ¥¹‘•à½ÁÑ¥µ¥é…Ñ¥½¸™½Èí‘‰}Á…Ñ¡ôˆ¤4(€€€€€€€ÁÉ¥¹Ð¡˜ˆ€ÕÉÉ•¹Ð‘…Ñ…‰…Í”Í¥é”èí‰•™½É•}µˆè¸Å™ô5ˆ¤4(€€€€€€€¥˜™É••}‰åÑ•Ì¥Ì¹½Ð9½¹”è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€É•”‘¥Í¬èí™É••}‰åÑ•Ì€¼€ ÄÀÈÐ¨ÄÀÈÐ¤è¸Á™ô5€ˆ4(€€€€€€€€€€€€€€€€€˜ˆ¡¹••ùí¹••‘}‰åÑ•Ì€¼€ ÄÀÈÐ¨ÄÀÈÐ¤è¸Á™ô5Ñ¼½µÁ±•Ñ”ˆ4(€€€€€€€€€€€€€€€€€˜‰ìœ¥¹°¸YUU4œ¥˜‘½}Ù…ÕÕ´•±Í”€œô¤ˆ¤4(€€€€€€€€€€€¥˜™É••}‰åÑ•Ì€ð¹••‘}‰åÑ•Ìè4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð ¤4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð ‹Šj€9½Ð•¹½Õ ™É•”‘¥Í¬Ñ¼½µÁ±•Ñ”Í…™•±ä¸É•”ÕÀ€ˆ4(€€€€€€€€€€€€€€€€€€€€€€‰ÍÁ…”°½ÈÉÕ¸Ý¥Ñ €´µ¹¼µÙ…ÕÕ´€¡É•‰Õ¥±‘ÌÑ¡”¥¹‘•à€ˆ4(€€€€€€€€€€€€€€€€€€€€€€‰‰ÕÐ‘½•Í¸ÐÉ•±…¥´ÍÁ…”Õ¹Ñ¥°„±…Ñ•ÈYUU4¤¸ˆ¤4(€€€€€€€€€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€¥˜‰•™½É•}µˆ€ø€ÔÀÀè4(€€€€€€€€€€€ÁÉ¥¹Ð ˆ€Q¡¥Ìµ…äÑ…­”„Ý¡¥±”½¸„±…É”‘…Ñ…‰…Í”¸%ÐÉÕ¹Ì¥¸€ˆ4(€€€€€€€€€€€€€€€€€€‰Ñ¡”™½É•É½Õ¹Ý¥Ñ ÁÉ½É•ÍÌ‰•±½ÜìÍ…™”Ñ¼ÑÉ°µ…¹€ˆ4(€€€€€€€€€€€€€€€€€€‰É”µÉÕ¸€¡¥ÐÉ•ÍÕµ•Ì¤¸ˆ¤4(€€€€€€€¥˜¹½Ð•Ñ…ÑÑÈ¡…ÉÌ°€‰å•Ìˆ°…±Í”¤è4(€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€É•ÍÀ€ô¥¹ÁÕÐ ‰AÉ½••ümä½9t€ˆ¤¹ÍÑÉ¥À ¤¹±½Ý•È ¤4(€€€€€€€€€€€•á•ÁÐ=ÉÉ½Èè4(€€€€€€€€€€€€€€€É•ÍÀ€ô€ˆˆ4(€€€€€€€€€€€¥˜É•ÍÀ¹½Ð¥¸€ ‰äˆ°€‰å•Ìˆ¤è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð ‰…¹•±±•¸ˆ¤4(€€€€€€€€€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€}±…ÍÐ€ôì‰Á¡…Í”ˆè9½¹•ô4(4(€€€€€€€‘•˜}ÁÉ½É•ÍÌ¡¥¹™¼¤è4(€€€€€€€€€€€Á¡…Í”€ô¥¹™¼¹•Ð ‰Á¡…Í”ˆ¤4(€€€€€€€€€€€ÁÐ€ô¥¹™¼¹•Ð ‰Á•É•¹Ðˆ°€À¤4(€€€€€€€€€€€¥˜Á¡…Í”€ôô€‰‰…­™¥±°ˆè4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰qÈ€I•‰Õ¥±‘¥¹œ¥¹‘•àèíÁÐèÍ‘ô”€ˆ4(€€€€€€€€€€€€€€€€€€€€€˜ˆ¡í¥¹™¼¹•Ð ¥¹‘•á•œ°À¤è±ô½í¥¹™¼¹•Ð Ñ½Ñ…°œ°À¤è±ô¤ˆ°4(€€€€€€€€€€€€€€€€€€€€€•¹ôˆˆ°™±ÕÍ õQÉÕ”¤4(€€€€€€€€€€€•±¥˜Á¡…Í”€„ô}±…ÍÑl‰Á¡…Í”‰tè4(€€€€€€€€€€€€€€€±…‰•°€ôì‰Ñ•…É‘½Ý¸ˆè€‰I•±…¥µ¥¹œ½±¥¹‘•àˆ°4(€€€€€€€€€€€€€€€€€€€€€€€€€‰Ù…ÕÕ´ˆè€‰½µÁ…Ñ¥¹œ‘…Ñ…‰…Í”€¡YUU4¤ˆ°4(€€€€€€€€€€€€€€€€€€€€€€€€€‰‘½¹”ˆè€‰½¹”‰ô¹•Ð¡Á¡…Í”°Á¡…Í”¤4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰q¸€í±…‰•±÷Š˜ˆ°™±ÕÍ õQÉÕ”¤4(€€€€€€€€€€€}±…ÍÑl‰Á¡…Í”‰t€ôÁ¡…Í”4(4(€€€€€€€ÁÉ¥¹Ð ‰=ÁÑ¥µ¥é¥¹œÍ•…É µ¥¹‘•àÍÑ½É…—Š˜ˆ¤4(€€€€€€€ÑÉäè4(€€€€€€€€€€€É•ÍÕ±Ð€ô‘ˆ¹½ÁÑ¥µ¥é•}™ÑÍ}ÍÑ½É…” 4(€€€€€€€€€€€€€€€ÁÉ½É•ÍÍ}ˆõ}ÁÉ½É•ÍÌ°Ù…ÕÕ´õ‘½}Ù…ÕÕ´4(€€€€€€€€€€€€¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰q¹ÉÉ½Èè½ÁÑ¥µ¥é…Ñ¥½¸™…¥±•èí•ôˆ¤4(€€€€€€€€€€€ÁÉ¥¹Ð ‰9¼‘…Ñ„Ý…Ì±½ÍÐ¸I”µÉÕ¸Ñ¼É•ÍÕµ”¸ˆ¤4(€€€€€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€¥˜¹½ÐÉ•ÍÕ±Ð¹•Ð ‰½¬ˆ¤è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰q¹½Õ±¹½Ð½ÁÑ¥µ¥é”èíÉ•ÍÕ±Ð¹•Ð É•…Í½¸œ°€Õ¹­¹½Ý¸œ¥ôˆ¤4(€€€€€€€€€€€‘ˆ¹±½Í” ¤4(€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€…™Ñ•É}µˆ€ô€ 4(€€€€€€€€€€€½Ì¹Á…Ñ ¹•ÑÍ¥é”¡‘‰}Á…Ñ ¤€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤¥˜‘‰}Á…Ñ ¹•á¥ÍÑÌ ¤•±Í”€À¸À4(€€€€€€€€¤4(€€€€€€€€ŒAÉ•™•ÈME1¥Ñ”Ì½Ý¸Á…”…½Õ¹Ñ¥¹œ½Ù•ÈÍÑ…Ð ¤¸%¸]0µ½‘”„4(€€€€€€€€ŒYUU4ÌÉ•ÝÉ¥Ñ”Í¥ÑÌ¥¸Ñ¡”€µÝ…°™¥±”Õ¹Ñ¥°„¡•­Á½¥¹Ð™½±‘Ì¥Ð4(€€€€€€€€Œ‰…¬°…¹Ñ¡…Ð¡•­Á½¥¹Ð¥ÌÉ•™ÕÍ•Ý¡¥±”…¹½Ñ¡•È½¹¹•Ñ¥½¸€¡„4(€€€€€€€€Œ±¥Ù”…Ñ•Ý…ä¤¡½±‘Ì„É•…µµ…É¬ƒŠPÍ¼Ñ¡”µ…¥¸™¥±”½¸‘¥Í¬ÍÑ¥±°4(€€€€€€€€ŒÉ•…‘Ì…Ð¥ÑÌÁÉ”µYUU4Í¥é”…¹­••ÁÌÉ½Ý¥¹œ¸ÍÑ…Ð ¥¥¹œ¥Ð¡•É”4(€€€€€€€€ŒÉ•Á½ÉÑ•€‰É•±…¥µ•€´ÌàÈÀ¸Ä5ˆ½¸„Ñ¡…Ð¡……ÑÕ…±±äÍ¡ÉÕ¹¬4(€€€€€€€€Œ€ØÀ”¸Á…•}½Õ¹Ð€¨Á…•}Í¥é”¥Ì½ÉÉ•Ð¥µµ•‘¥…Ñ•±ä¸4(€€€€€€€±½¥…±}…™Ñ•È€ô‘ˆ¹±½¥…±}Í¥é•}‰åÑ•Ì ¤4(€€€€€€€¥˜±½¥…±}…™Ñ•È¥Ì¹½Ð9½¹”è4(€€€€€€€€€€€…™Ñ•É}µˆ€ô±½¥…±}…™Ñ•È€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤4(€€€€€€€Í…Ù•€ô‰•™½É•}µˆ€´…™Ñ•É}µˆ4(€€€€€€€ÁÉ¥¹Ð¡˜‰q»ŠrLM•…É ¥¹‘•à½ÁÑ¥µ¥é•¸ˆ¤4(€€€€€€€ÁÉ¥¹Ð 4(€€€€€€€€€€€˜ˆ€…Ñ…‰…Í”Í¥é”èí‰•™½É•}µˆè¸Å™ô5€´øí…™Ñ•É}µˆè¸Å™ô5€ˆ4(€€€€€€€€€€€˜ˆ¡í}Í¥é•}‘•±Ñ…}±…‰•°¡Í…Ù•¥ô¤ˆ4(€€€€€€€€¤4(€€€€€€€¥˜É•ÍÕ±Ð¹•Ð ‰Ù…ÕÕµ•ˆ¤¥Ì…±Í”è4(€€€€€€€€€€€ÁÉ¥¹Ð ˆ€€¡YUU4Ý…ÌÍ­¥ÁÁ•½È™…¥±•ƒŠPÉÕ¸€ˆ4(€€€€€€€€€€€€€€€€€€‰¡•Éµ•ÌÍ•ÍÍ¥½¹Ì½ÁÑ¥µ¥é•€±…Ñ•ÈÑ¼É•±…¥´™É••ÍÁ…”¸¤ˆ¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰É•Á…¥ÈµÉ½ÕÑ¥¹œˆè4(€€€€€€€É•½É‘Ì€ô‘ˆ¹™¥¹‘}½ÉÁ¡…¹•‘}…Ñ•Ý…å}Í•ÍÍ¥½¹Ì 4(€€€€€€€€€€€µ…á}…Á}Ìõ•Ñ…ÑÑÈ¡…ÉÌ°€‰µ…á}…Á}Í•½¹‘Ìˆ°9½¹”¤4(€€€€€€€€¤4(€€€€€€€…‘½ÁÑ…‰±”€ômÈ™½ÈÈ¥¸É•½É‘Ì¥˜Él‰…‘½ÁÑ…‰±”‰ut4(€€€€€€€™½ÈÉ•½É¥¸É•½É‘Ìè4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰íÉ•½É‘l½ÉÁ¡…¹}¥uô€€¡íÉ•½É‘lÍ½ÕÉ”uô°€ˆ4(€€€€€€€€€€€€€€€€€˜‰íÉ•½É‘lµ•ÍÍ…•}½Õ¹Ðuôµ•ÍÍ…•Ì¤ˆ¤4(€€€€€€€€€€€¥˜É•½É‘l‰…‘½ÁÑ…‰±”‰tè4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€ƒŠH…‘½ÁÐ¥¹Ñ¼íÉ•½É‘lÍ•ÍÍ¥½¹}­•äuô€ˆ4(€€€€€€€€€€€€€€€€€€€€€˜ˆ¡™É½´íÉ•½É‘l‘½¹½É}¥uô°€ˆ4(€€€€€€€€€€€€€€€€€€€€€˜‰•Ù¥‘•¹”èíÉ•½É‘l•Ù¥‘•¹”uô¤ˆ¤4(€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€ƒŠr\¹½ÐÉ•Á…¥É…‰±”ƒŠPíÉ•½É‘lÉ•…Í½¸uôˆ¤4(4(€€€€€€€¥˜¹½ÐÉ•½É‘Ìè4(€€€€€€€€€€€ÁÉ¥¹Ð ‹ŠrL9¼…Ñ•Ý…äÍ•ÍÍ¥½¹Ì…É”µ¥ÍÍ¥¹œÑ¡•¥ÈÉ½ÕÑ¥¹œ¥‘•¹Ñ¥Ñä¸ˆ¤4(€€€€€€€•±¥˜¹½Ð…‘½ÁÑ…‰±”è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰q¹í±•¸¡É•½É‘Ì¥ô½ÉÁ¡…¹•Í•ÍÍ¥½¸¡Ì¤™½Õ¹°¹½¹”€ˆ4(€€€€€€€€€€€€€€€€€€‰Õ¹…µ‰¥Õ½ÕÍ±äÉ•Á…¥É…‰±”¸9½Ñ¡¥¹œÑ¼‘¼¸ˆ¤4(€€€€€€€•±¥˜¹½Ð•Ñ…ÑÑÈ¡…ÉÌ°€‰…ÁÁ±äˆ°…±Í”¤è4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰q¹í±•¸¡…‘½ÁÑ…‰±”¥ô½˜í±•¸¡É•½É‘Ì¥ô½ÉÁ¡…¹•Í•ÍÍ¥½¸¡Ì¤€ˆ4(€€€€€€€€€€€€€€€€€€‰…¸‰”É•Á…¥É•¸I”µÉÕ¸Ý¥Ñ €´µ…ÁÁ±äÑ¼Á•É™½É´Ñ¡•´¸ˆ¤4(€€€€€€€•±Í”è4(€€€€€€€€€€€€ŒÉÕ¹¹¥¹œ…Ñ•Ý…ä¡½±‘ÌÑ¡”½±É½ÕÑ¥¹œµ…ÁÁ¥¹œ¥¸µ•µ½Éä…¹4(€€€€€€€€€€€€ŒÝ½Õ±ÝÉ¥Ñ”¥Ð‰…¬½Ù•ÈÑ¡”É•Á…¥È½¸¥ÑÌ¹•áÐÍ…Ù”¸4(€€€€€€€€€€€ÁÉ¥¹Ð ‰q¹MÑ½ÀÑ¡”…Ñ•Ý…ä‰•™½É”…ÁÁ±å¥¹œƒŠP„ÉÕ¹¹¥¹œ…Ñ•Ý…ä€ˆ4(€€€€€€€€€€€€€€€€€€‰ÍÑ¥±°¡½±‘ÌÑ¡”½±É½ÕÑ¥¹œµ…ÁÁ¥¹œ¥¸µ•µ½Éä¸ˆ¤4(€€€€€€€€€€€¥˜}½¹™¥Éµ}ÁÉ½µÁÐ 4(€€€€€€€€€€€€€€€˜‰‘½ÁÐí±•¸¡…‘½ÁÑ…‰±”¥ô½ÉÁ¡…¹•Í•ÍÍ¥½¸¡Ì¤ümä½9t€ˆ4(€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€É•Á…¥É•€ô€À4(€€€€€€€€€€€€€€€™½ÈÉ•½É¥¸…‘½ÁÑ…‰±”è4(€€€€€€€€€€€€€€€€€€€¥˜‘ˆ¹…‘½ÁÑ}½ÉÁ¡…¹•‘}…Ñ•Ý…å}Í•ÍÍ¥½¸ 4(€€€€€€€€€€€€€€€€€€€€€€€É•½É‘l‰½ÉÁ¡…¹}¥‰t°É•½É‘l‰‘½¹½É}¥‰t4(€€€€€€€€€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€€€€€€€€€É•Á…¥É•€¬ô€Ä4(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‹ŠrLíÉ•½É‘l½ÉÁ¡…¹}¥uô¹½Ü½Ý¹Ì€ˆ4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€˜‰íÉ•½É‘lÍ•ÍÍ¥½¹}­•äuôˆ¤4(€€€€€€€€€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‹Šr\íÉ•½É‘l½ÉÁ¡…¹}¥uôÝ…Ì¹½Ð…‘½ÁÑ•€ˆ4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€ˆ¡Ñ¡”É½Ü¡…¹•Í¥¹”¥ÐÝ…ÌÉ•Á½ÉÑ•¤ˆ¤4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰q¹I•Á…¥É•íÉ•Á…¥É•‘ô½˜í±•¸¡…‘½ÁÑ…‰±”¥ôÍ•ÍÍ¥½¸¡Ì¤¸ˆ¤4(€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð ‰‰½ÉÑ•ƒŠP¹½Ñ¡¥¹œÝ…Ì¡…¹•¸ˆ¤4(4(€€€•±¥˜…Ñ¥½¸€ôô€‰ÍÑ…ÑÌˆè4(€€€€€€€Ñ½Ñ…°€ô‘ˆ¹Í•ÍÍ¥½¹}½Õ¹Ð ¤4(€€€€€€€µÍÌ€ô‘ˆ¹µ•ÍÍ…•}½Õ¹Ð ¤4(€€€€€€€ÁÉ¥¹Ð¡˜‰Q½Ñ…°Í•ÍÍ¥½¹ÌèíÑ½Ñ…±ôˆ¤4(€€€€€€€ÁÉ¥¹Ð¡˜‰Q½Ñ…°µ•ÍÍ…•ÌèíµÍÍôˆ¤4(€€€€€€€™½ÈÍÉŒ¥¸l‰±¤ˆ°€‰Ñ•±•É…´ˆ°€‰‘¥Í½Éˆ°€‰Ý¡…ÑÍ…ÁÀˆ°€‰Í±…¬‰tè4(€€€€€€€€€€€Œ€ô‘ˆ¹Í•ÍÍ¥½¹}½Õ¹Ð¡Í½ÕÉ”õÍÉŒ¤4(€€€€€€€€€€€¥˜Œ€ø€Àè4(€€€€€€€€€€€€€€€ÁÉ¥¹Ð¡˜ˆ€íÍÉôèíôÍ•ÍÍ¥½¹Ìˆ¤4(€€€€€€€‘‰}Á…Ñ €ô‘ˆ¹‘‰}Á…Ñ 4(€€€€€€€¥˜‘‰}Á…Ñ ¹•á¥ÍÑÌ ¤è4(€€€€€€€€€€€Í¥é•}µˆ€ô½Ì¹Á…Ñ ¹•ÑÍ¥é”¡‘‰}Á…Ñ ¤€¼€ ÄÀÈÐ€¨€ÄÀÈÐ¤4(€€€€€€€€€€€ÁÉ¥¹Ð¡˜‰…Ñ…‰…Í”Í¥é”èíÍ¥é•}µˆè¸Å™ô5ˆ¤4(4(€€€•±Í”è4(€€€€€€€Í•ÍÍ¥½¹Í}Á…ÉÍ•È¹ÁÉ¥¹Ñ}¡•±À ¤4(4(€€€‘ˆ¹±½Í” ¤4(