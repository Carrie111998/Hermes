"""Session title sanitization, uniqueness, and lineage-aware renaming for SessionDB.

Mixin contract: this is a plain mixin class consumed by
``hermes_state.SessionDB``. It defines no ``__init__`` and no state of its
own; methods access the host's attributes (``self._conn``, ``self._lock``,
``self._execute_write`` and other SessionDB methods) established by
``SessionDB.__init__``. It must never import hermes_state at module scope
(cycle) — the one host-class reference below resolves lazily at call time.
Shared module-level constants live in hermes_state_common.
"""

import re
from typing import Optional

from agent.message_sanitization import _sanitize_surrogates
from hermes_state_common import _COMPRESSION_CHILD_SQL


class _HostSessionDB:
    """Lazy host-class accessor for the moved ``sanitize_title`` staticmethod.

    ``sanitize_title`` (moved byte-verbatim) references
    ``SessionDB.MAX_TITLE_LENGTH`` as a module global. The host class is
    defined in ``hermes_state.py``, which imports this module — a cycle — so
    the name cannot be bound at import time. LOAD_GLOBAL does not consult
    module ``__getattr__``, so this module-level placeholder is the minimal
    seam: attribute reads (only ``MAX_TITLE_LENGTH``) resolve to the real
    class at call time, long after imports complete.
    """

    def __getattr__(self, name: str):
        import hermes_state  # deferred: host fully imported by call time

        return getattr(hermes_state.SessionDB, name)


SessionDB = _HostSessionDB()


class SessionTitleMixin:
    """See module docstring — mixin for SessionDB (Title cluster)."""

    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Lone surrogates cannot be bound by sqlite3 (UnicodeEncodeError at
        # UTF-8 encode time) — scrub them like every other write path here.
        title = _sanitize_surrogates(title)

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def _is_compression_ancestor(
        self, conn, *, ancestor_id: str, descendant_id: str
    ) -> bool:
        """Return True if *ancestor_id* is a compression predecessor of
        *descendant_id* (walking parent links up the continuation chain).

        The continuation edge is the canonical one shared with
        :func:`_ephemeral_child_sql` / :meth:`set_session_archived`
        (``_COMPRESSION_CHILD_SQL``): a parent → child edge counts only when the
        parent ended with ``end_reason = 'compression'`` and the child started
        at or after the parent's ``ended_at``, which distinguishes continuations
        from delegate subagents / branch children that also carry a
        ``parent_session_id``. Expressed as a single recursive CTE rather than a
        per-hop Python walk so the edge definition lives in exactly one place.
        """
        if not ancestor_id or not descendant_id or ancestor_id == descendant_id:
            return False
        # Walk parent links up from the descendant, following only compression
        # continuation edges, and check whether ancestor_id is reached.
        edge = _COMPRESSION_CHILD_SQL.format(a="child")
        row = conn.execute(
            f"""
            WITH RECURSIVE ancestors(id) AS (
                SELECT ?
                UNION
                SELECT parent.id
                FROM ancestors a
                JOIN sessions child ON child.id = a.id
                JOIN sessions parent ON parent.id = child.parent_session_id
                WHERE {edge}
            )
            SELECT 1 FROM ancestors WHERE id = ? AND id != ? LIMIT 1
            """,
            (descendant_id, ancestor_id, descendant_id),
        ).fetchone()
        return row is not None

    def _set_session_title(
        self,
        session_id: str,
        title: str,
        *,
        only_if_empty: bool,
    ) -> bool:
        title = self.sanitize_title(title)

        def _do(conn):
            if only_if_empty:
                current = conn.execute(
                    "SELECT title FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if current is None or current["title"] is not None:
                    return 0

            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    conflict_id = conflict["id"]
                    # A compression continuation is the live, projected-forward
                    # head of its conversation; its compressed predecessors are
                    # ended and hidden from the session list (list_sessions_rich
                    # projects roots → tip). When the title that "conflicts" is
                    # held by such a hidden ancestor, the user has no way to free
                    # it — renaming the visible tip back to the base name would
                    # dead-end with "already in use by <session they can't see>".
                    # Treat this as a transfer: move the title off the ancestor
                    # onto the continuation. Uniqueness is preserved (still only
                    # one session carries the exact title) and the parent-link
                    # lineage is untouched.
                    if self._is_compression_ancestor(
                        conn, ancestor_id=conflict_id, descendant_id=session_id
                    ):
                        conn.execute(
                            "UPDATE sessions SET title = NULL WHERE id = ?",
                            (conflict_id,),
                        )
                    else:
                        raise ValueError(
                            f"Title '{title}' is already in use by session {conflict_id}"
                        )
            predicate = " AND title IS NULL" if only_if_empty else ""
            cursor = conn.execute(
                f"UPDATE sessions SET title = ? WHERE id = ?{predicate}",
                (title, session_id),
            )
            return cursor.rowcount

        rowcount = self._execute_write(_do)
        return rowcount > 0

    def set_session_title(self, session_id: str, title: str) -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).
        """
        return self._set_session_title(session_id, title, only_if_empty=False)

    def set_auto_title_if_empty(self, session_id: str, title: str) -> bool:
        """Set an auto-generated title only when the current title is NULL.

        The predicate and write run in one transaction so a concurrent manual
        rename cannot be overwritten. Validation and uniqueness behavior match
        :meth:`set_session_title`.
        """
        return self._set_session_title(session_id, title, only_if_empty=True)

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None
