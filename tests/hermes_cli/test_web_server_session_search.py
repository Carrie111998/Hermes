import asyncio

from hermes_cli import web_server


class _FakeSessionDB:
    """Fake backing the /api/sessions/search endpoint.

    The endpoint surfaces direct session-id matches first, then FTS message
    matches, deduping both by compression lineage root. This fake has no
    compression chains (get_session returns no parent), so each session is its
    own lineage root.
    """

    closed = False
    opened_read_only = None
    requested_fields = None

    def __init__(self, *args, **kwargs):
        type(self).opened_read_only = kwargs.get("read_only")

    @staticmethod
    def _source_allowed(row, source=None, sources=None, exclude_sources=None):
        row_source = row.get("source")
        if source and row_source != source:
            return False
        if sources and row_source not in sources:
            return False
        if exclude_sources and row_source in exclude_sources:
            return False
        return True

    def search_sessions_by_id(
        self,
        query,
        limit=20,
        include_archived=True,
        source=None,
        sources=None,
        exclude_sources=None,
    ):
        assert query == "20260603"
        assert include_archived is True
        rows = [
            {
                "id": "20260603_090200_exact",
                "preview": "ID match preview",
                "source": "cli",
                "model": "claude",
                "started_at": 100,
            }
        ]
        return [
            row
            for row in rows
            if self._source_allowed(
                row, source=source, sources=sources, exclude_sources=exclude_sources
            )
        ][:limit]

    def list_sessions_rich(self, **kwargs):
        # Whole-store metadata-discovery lane: this fixture has no stored
        # title/display_name rows beyond the id match, so the endpoint's
        # result order stays ID-first then content.
        assert kwargs.get("search_query") == "20260603"
        assert kwargs.get("order_by_last_active") is True
        return []

    def search_messages(
        self,
        query,
        source_filter=None,
        exclude_sources=None,
        limit=20,
        fields=None,
    ):
        assert query == "20260603*"
        type(self).requested_fields = fields
        rows = [
            {
                "session_id": "20260603_090200_exact",
                "snippet": "duplicate content hit should not replace ID hit",
                "role": "user",
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]
        return [
            row
            for row in rows
            if self._source_allowed(
                row, sources=source_filter, exclude_sources=exclude_sources
            )
        ][:limit]

    def get_session(self, session_id):
        # No compression chains in this fixture — every session is its own root.
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


def test_desktop_session_search_merges_id_matches_before_content_matches(monkeypatch):
    _FakeSessionDB.opened_read_only = None
    _FakeSessionDB.requested_fields = None
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)

    response = asyncio.run(web_server.search_sessions(q="20260603", limit=2))

    assert _FakeSessionDB.requested_fields is not None
    assert "context" not in _FakeSessionDB.requested_fields
    # ID match surfaces first; the content hit on the SAME session is deduped
    # by lineage root (not double-listed); the unrelated content hit follows.
    assert response == {
        "results": [
            {
                "id": "20260603_090200_exact",
                "session_id": "20260603_090200_exact",
                "lineage_root": "20260603_090200_exact",
                "snippet": "ID match preview",
                "role": None,
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "id": "content_session",
                "session_id": "content_session",
                "lineage_root": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "started_at": 200,
            },
        ]
    }
    assert _FakeSessionDB.opened_read_only is True


def test_desktop_session_search_surfaces_stored_title_only_session():
    """Behavior regression (#128): a session whose STORED TITLE matches the
    query but whose message body does not is returned by
    ``GET /api/sessions/search``, and the stored title survives in the result.

    On the pre-#128 endpoint this is RED: ID discovery only matches ids and
    message-content FTS only matches the body, so a stored-title-only session
    was invisible from the Desktop search box.
    """
    from hermes_cli import web_server
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    db_path = get_hermes_home() / "state.db"
    seed = SessionDB(db_path=db_path)
    try:
        sid = seed.create_session("sess_title_only", source="cli")
        seed.set_session_title(sid, "Arby's Faribault, MN")
        seed.append_message(
            sid,
            role="user",
            content="weekly sync notes; nothing here about food or cities",
        )
    finally:
        seed.close()

    response = asyncio.run(web_server.search_sessions(q="Faribault", limit=5))

    hits = [r for r in response["results"] if r["id"] == sid]
    assert hits, "stored-title-only session must surface via the metadata lane"
    assert hits[0]["title"] == "Arby's Faribault, MN"
