"""Shared session-listing helpers for CLI and gateway slash surfaces."""

from __future__ import annotations

from typing import Any


def parse_session_listing_args(raw_args: str) -> tuple[bool, bool, str, str | None]:
    """Parse `/sessions`-style args into listing flags, a resume target, and a search query.

    Returns ``(include_all_sources, include_unnamed, target, search_query)``.
    ``list``/``ls`` and ``browse`` are display aliases; ``all``/``--all`` widens
    source scope; ``full``/``--full`` keeps unnamed sessions in the listing.
    ``search``/``find`` makes the remaining words a search query —
    ``search_query`` is ``None`` when search wasn't requested and ``""`` when it
    was requested without a query. Flags are only honored before the first
    positional word, so titles containing e.g. "all" aren't misparsed. Anything
    else is treated as a target so `/sessions <id-or-title>` can delegate to
    `/resume`.
    """
    import shlex

    parts = shlex.split(raw_args or "")
    include_all = False
    include_unnamed = False
    target_parts: list[str] = []
    for i, part in enumerate(parts):
        lower = part.strip().lower()
        if not target_parts:
            if lower in {"list", "ls", "browse"}:
                continue
            if lower in {"all", "--all"}:
                include_all = True
                continue
            if lower in {"full", "--full"}:
                include_unnamed = True
                continue
            if lower in {"search", "find"}:
                query = " ".join(parts[i + 1:]).strip()
                return include_all, include_unnamed, "", query
        target_parts.append(part)
    return include_all, include_unnamed, " ".join(target_parts).strip(), None


def query_session_listing(
    session_db: Any,
    *,
    source: str | None,
    current_session_id: str | None = None,
    include_all_sources: bool = False,
    include_unnamed: bool = False,
    search_query: str | None = None,
    limit: int = 10,
    offset: int = 0,
    exclude_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return session rows for interactive listing surfaces.

    This is the shared selection policy behind CLI/gateway session browsing:
    source-scoped by default, optionally global, hide unnamed sessions unless
    the caller asks for a full listing, and never include the current session.
    With ``search_query``, rows are filtered by title/id match (SQL-level, see
    ``SessionDB.list_sessions_rich``) and ordered by most-recent activity;
    unnamed sessions stay visible since an id match may be the only handle.

    ``offset`` skips that many *qualifying* rows (after source/title filtering
    but before the ``limit`` cap) — useful for paginated listing in the CLI.
    """
    query_source = None if include_all_sources else source
    fetch_limit = max((limit + offset) * 4, limit + offset)
    search = (search_query or "").strip()
    rows = session_db.list_sessions_rich(
        source=query_source,
        exclude_sources=exclude_sources,
        limit=fetch_limit,
        search_query=search or None,
        order_by_last_active=bool(search),
    )
    result: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if current_session_id and row.get("id") == current_session_id:
            continue
        if not include_unnamed and not row.get("title") and not search:
            continue
        if skipped < offset:
            skipped += 1
            continue
        result.append(row)
        if len(result) >= limit:
            break
    return result


def format_gateway_session_listing(
    rows: list[dict[str, Any]],
    *,
    include_source: bool = False,
    title: str = "Sessions",
) -> str:
    """Render a compact Markdown-ish session list for gateway messengers."""
    if not rows:
        return (
            "No sessions found.\n"
            "Use `/title My Session` to name this chat, or `/sessions full` "
            "to include unnamed sessions."
        )

    lines = [f"📋 **{title}**", ""]
    for idx, row in enumerate(rows, start=1):
        session_id = str(row.get("id") or "")
        title_text = str(row.get("title") or "—")
        preview = str(row.get("preview") or "")[:40]
        source = str(row.get("source") or "")
        source_part = f" `{source}`" if include_source and source else ""
        preview_part = f" — _{preview}_" if preview else ""
        lines.append(f"{idx}. **{title_text}**{source_part} — `{session_id}`{preview_part}")
    lines.append("")
    lines.append("Resume: `/resume <session id>` or `/resume <number>` from `/resume`.")
    lines.append("More: `/sessions all`, `/sessions full`, `/sessions search <query>`.")
    return "\n".join(lines)


def session_rank_lookup(
    session_db: Any, *, limit: int = 2000
) -> dict[str, int]:
    """Map session id -> position in the canonical `hermes sessions list`.

    Uses the same query as ``hermes sessions list`` (all sources except
    ``tool``, unnamed included, ordered by original start time with
    compression chains projected to their live tip) so the ``#`` column in
    search results shows the same numbers the user sees in the listing.
    The window defaults to 2000 rows so every tip in the store is covered.
    """
    rows = session_db.list_sessions_rich(
        source=None, exclude_sources=["tool"], limit=limit
    )
    return {r["id"]: i + 1 for i, r in enumerate(rows)}


def session_rank(
    session_db: Any, sid: str, rank_of: dict[str, int] | None = None
) -> int | None:
    """Position of ``sid`` in the canonical sessions list, chain-aware.

    ``session_rank_lookup`` keys the map by *projected tip* ids, but search
    results are frequently roots or mid-chain sessions (FTS5 matches old
    messages). So walk forward along continuation children (latest started_at
    first) until an id present in the map is found — every chain's tip is in
    the map — and return its rank. Returns None only if no mapped tip is
    reachable within 20 hops.
    """
    if rank_of is None:
        rank_of = session_rank_lookup(session_db)
    current = sid
    for _ in range(20):
        rank = rank_of.get(current)
        if rank is not None:
            return rank
        row = session_db._conn.execute(
            "SELECT id FROM sessions WHERE parent_session_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (current,),
        ).fetchone()
        if not row:
            return None
        current = row[0]
    return None


def _compression_root(session_db: Any, sid: str, max_hops: int = 50) -> str:
    """Deepest compression ancestor of ``sid`` (itself when not in a chain).

    Follows ``parent_session_id`` upward only across compression edges —
    a child counts only if the parent ended with ``end_reason ==
    "compression"`` and the child is not a branch (branches are their own
    conversation). Does not rely on ``get_compression_lineage``, whose
    forward walk assumes a linear chain and can fragment on divergent
    children.
    """
    current = sid
    for _ in range(max_hops):
        meta = session_db.get_session(current) or {}
        if session_db._is_branch_child_row(meta):
            return current
        parent_id = meta.get("parent_session_id")
        if not parent_id:
            return current
        parent = session_db.get_session(parent_id)
        if not parent or parent.get("end_reason") != "compression":
            return current
        current = parent_id
    return current


def root_started_at(session_db: Any, sid: str) -> float | None:
    """Original creation time of ``sid``'s compression chain (the root).

    The Created column should show when the conversation first began, not
    when its latest compression child was spawned. Plain listings already
    carry the root's ``started_at`` on projected rows; search results are
    keyed by the matched generation, so they resolve the root explicitly.
    """
    root_id = _compression_root(session_db, sid)
    meta = session_db.get_session(root_id) or {}
    return meta.get("started_at")


def dedup_compression_chains(
    session_db: Any, sids: list[str]
) -> set[str]:
    """Keep only the latest descendant per compression chain.

    FTS5 matches messages across compression generations, so several
    children of one conversation (e.g. ``...#7`` and ``...#13`` with the
    intermediate generations missing from the result set) can all appear.
    Group every result by its deepest compression ancestor and keep the
    newest descendant; branches are never collapsed into their source
    chain.
    """
    best: dict[str, tuple[float, str]] = {}
    for sid in sids:
        root = _compression_root(session_db, sid)
        meta = session_db.get_session(sid) or {}
        ts = meta.get("started_at") or 0
        if root not in best or ts > best[root][0]:
            best[root] = (ts, sid)
    return {sid for _, sid in best.values()}


CLI_SESSIONS_LIST_FOOTER = """\
  Tip: hermes sessions list [PAGE] [-l N] [--source SRC] [--workspace NEEDLE]
  Full parameter reference and examples: hermes sessions list --help
"""

INTERACTIVE_SESSIONS_FOOTER = """\
  Use /resume <number> (the # column above), /resume <session id>, or /resume <session title> to continue.
  More: /sessions search <query> · /sessions list [PAGE] · /sessions -l N
"""


def render_sessions_table(
    sessions: list[dict[str, Any]],
    *,
    out=print,
    db: Any = None,
    preview_lookup: dict[str, str] | None = None,
) -> None:
    """Render the canonical sessions table shared by /sessions, /sessions
    search, `hermes sessions list` and `hermes sessions search`.

    Columns: #, Title, Model, Tok (in/out), Created, Last, Preview, ID.
    Title width sizes to the longest title in the result set (min 16, max 50).

    Preview resolution order:
      1. ``preview_lookup[sid]`` — FTS5 search precomputes root-ancestor
         previews and passes them here.
      2. If ``db`` is given and the row has a ``parent_session_id``, walk up
         the compression chain to the root ancestor and use its first user
         message (so compressed children show the original conversation
         opener, not the compaction banner).
      3. The row's own ``preview`` field.
    """
    import time as _time
    from datetime import datetime as _dt

    def _fmt_tok(n: Any) -> str:
        if n is None:
            return "—"
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n//1000}k"
        return str(n)

    def _fmt_age(ts: Any) -> str:
        if not ts:
            return "?"
        age = int(_time.time() - float(ts))
        if age < 60:
            return f"{age}s"
        if age < 3600:
            return f"{age // 60}m"
        if age < 86400:
            return f"{age // 3600}h"
        return f"{age // 86400}d"

    max_title = 0
    max_num = 0
    for s in sessions:
        t = s.get("title") or ""
        if len(t) > max_title:
            max_title = len(t)
        num = s.get("rank") or 0
        if num > max_num:
            max_num = num
    title_w = max(16, min(max_title, 50))
    num_w = max(2, len(str(max_num)))

    # Resolve previews once per session id.
    previews: dict[str, str] = dict(preview_lookup or {})
    for s in sessions:
        sid = s.get("id")
        if not sid or sid in previews:
            continue
        root_id = sid
        if db is not None and s.get("parent_session_id"):
            current = sid
            hops = 0
            while current and hops < 20:
                m = db.get_session(current) or {}
                parent = m.get("parent_session_id")
                if not parent:
                    break
                current = parent
                hops += 1
            root_id = current
        if root_id == sid and s.get("preview"):
            previews[sid] = s["preview"]
        elif db is not None:
            try:
                cur = db._conn.execute(
                    "SELECT substr(content, 1, 60) FROM messages "
                    "WHERE session_id = ? AND role = 'user' "
                    "ORDER BY id ASC LIMIT 1",
                    (root_id,),
                )
                row = cur.fetchone()
                previews[sid] = row[0] if row else ""
            except Exception:
                previews[sid] = s.get("preview") or ""

    out(f"  {'#':>{num_w}}  {'Title':<{title_w}} {'Model':<10} {'Tok(In/Out)':>12}  {'Created':<10} {'Last':<8} {'Preview':<40} {'ID'}")
    out(f"  {'─'*num_w}  {'─'*title_w} {'─'*10} {'─'*12}  {'─'*10} {'─'*8} {'─'*40} {'─'*24}")
    for idx, s in enumerate(sessions, 1):
        sid = s.get("id") or "—"
        num = s.get("rank") or idx
        title = (s.get("title") or "—")[:title_w]
        model_raw = s.get("model") or "—"
        model = model_raw.split("/")[-1] if "/" in model_raw else model_raw
        if len(model) > 10:
            model = model[:9] + "…"
        tok_str = f"{_fmt_tok(s.get('input_tokens'))}/{_fmt_tok(s.get('output_tokens'))}"
        started = s.get("started_at")
        created = _dt.fromtimestamp(started).strftime("%Y-%m-%d") if started else "?"
        when = _fmt_age(s.get("last_active"))
        pv = (previews.get(sid) or "")[:38]
        if len(previews.get(sid) or "") >= 38:
            pv = pv[:37] + "…"
        out(f"  {num:>{num_w}}  {title:<{title_w}} {model:<10} {tok_str:>12}  {created:<10} {when:<8} {pv:<40} {sid}")
