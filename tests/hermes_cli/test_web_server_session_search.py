import asyncio

from hermes_cli import web_server


class _FakeSessionDB:
    """Fake backing the /api/sessions/search endpoint.

    The endpoint surfaces direct session-id matches first, then metadata
    (title/id) infix hits, then FTS message matches, deduping by compression
    lineage root. This fake has no compression chains (get_session returns no
    parent), so each session is its own lineage root.
    """

    closed = False
    opened_read_only = None
    requested_fields = None
    last_meta_kwargs = None

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
                "title": "Exact ID Session",
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
        # Metadata infix path — empty by default so ID + FTS stay primary in
        # the merge fixture. Title-infix + source filter covered separately.
        type(self).last_meta_kwargs = dict(kwargs)
        assert kwargs.get("search_query") == "20260603"
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
        if session_id == "content_session":
            return {
                "id": session_id,
                "parent_session_id": None,
                "title": "Content Title",
            }
        return {
            "id": session_id,
            "parent_session_id": None,
            "title": "Exact ID Session",
        }

    def get_session_rich_row(self, session_id):
        # Minimal rich row so add_lineage_result can stamp id/title without
        # inventing unrelated fields the assertion must ignore.
        base = self.get_session(session_id)
        if session_id == "content_session":
            return {
                **base,
                "source": "desktop",
                "model": "gpt",
                "started_at": 200,
                "last_active": 200,
                "message_count": 1,
                "tool_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "preview": "content hit",
                "archived": False,
            }
        return {
            **base,
            "source": "cli",
            "model": "claude",
            "started_at": 100,
            "last_active": 100,
            "message_count": 1,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "preview": "ID match preview",
            "archived": False,
        }

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


def _result_core(row: dict) -> dict:
    """Strip rich-row enrichment noise; keep search contract fields."""
    keys = (
        "id",
        "session_id",
        "lineage_root",
        "snippet",
        "title",
        "matched_on",
        "role",
        "source",
        "model",
        "session_started",
    )
    return {k: row.get(k) for k in keys}


def test_desktop_session_search_merges_id_matches_before_content_matches(monkeypatch):
    _FakeSessionDB.opened_read_only = None
    _FakeSessionDB.requested_fields = None
    _FakeSessionDB.last_meta_kwargs = None
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)

    response = asyncio.run(web_server.search_sessions(q="20260603", limit=2))

    assert _FakeSessionDB.requested_fields is not None
    assert "context" not in _FakeSessionDB.requested_fields
    assert _FakeSessionDB.last_meta_kwargs is not None
    assert _FakeSessionDB.last_meta_kwargs.get("search_query") == "20260603"
    # ID match surfaces first; the content hit on the SAME session is deduped
    # by lineage root (not double-listed); the unrelated content hit follows.
    cores = [_result_core(r) for r in response["results"]]
    assert cores == [
        {
            "id": "20260603_090200_exact",
            "session_id": "20260603_090200_exact",
            "lineage_root": "20260603_090200_exact",
            "snippet": "ID match preview",
            "title": "Exact ID Session",
            "matched_on": "id",
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
            "title": "Content Title",
            "matched_on": "message",
            "role": "assistant",
            "source": "desktop",
            "model": "gpt",
            "session_started": 200,
        },
    ]
    assert _FakeSessionDB.opened_read_only is True


class _MetaSourceFake(_FakeSessionDB):
    """Metadata path returns a title hit; must honor source filters."""

    def search_sessions_by_id(self, query, limit=20, include_archived=True, source=None, sources=None, exclude_sources=None):
        return []

    def search_messages(self, query, source_filter=None, exclude_sources=None, limit=20, fields=None):
        type(self).requested_fields = fields
        return []

    def list_sessions_rich(self, **kwargs):
        type(self).last_meta_kwargs = dict(kwargs)
        rows = [
            {
                "id": "title_cli",
                "title": "Budget 20260603 review",
                "preview": "cli preview",
                "source": "cli",
                "model": "claude",
                "started_at": 10,
                "last_active": 10,
            },
            {
                "id": "title_desktop",
                "title": "Budget 20260603 desk",
                "preview": "desk preview",
                "source": "desktop",
                "model": "gpt",
                "started_at": 20,
                "last_active": 20,
            },
        ]
        return [
            row
            for row in rows
            if self._source_allowed(
                row,
                source=kwargs.get("source"),
                sources=kwargs.get("sources"),
                exclude_sources=kwargs.get("exclude_sources"),
            )
        ]

    def get_session(self, session_id):
        return {"id": session_id, "parent_session_id": None, "title": session_id}

    def get_session_rich_row(self, session_id):
        return {
            "id": session_id,
            "title": "Budget 20260603 review" if session_id == "title_cli" else "Budget 20260603 desk",
            "source": "cli" if session_id == "title_cli" else "desktop",
            "model": "claude" if session_id == "title_cli" else "gpt",
            "started_at": 10 if session_id == "title_cli" else 20,
            "last_active": 10 if session_id == "title_cli" else 20,
            "message_count": 1,
            "tool_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "preview": "x",
            "archived": False,
        }


def test_session_search_metadata_path_forwards_source_filters(monkeypatch):
    _MetaSourceFake.opened_read_only = None
    _MetaSourceFake.last_meta_kwargs = None
    monkeypatch.setattr("hermes_state.SessionDB", _MetaSourceFake)

    response = asyncio.run(
        web_server.search_sessions(q="20260603", limit=10, source="cli")
    )

    assert _MetaSourceFake.last_meta_kwargs is not None
    assert _MetaSourceFake.last_meta_kwargs.get("source") == "cli"
    cores = [_result_core(r) for r in response["results"]]
    assert len(cores) == 1
    assert cores[0]["id"] == "title_cli"
    assert cores[0]["matched_on"] == "title"
    assert cores[0]["source"] == "cli"
