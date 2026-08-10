"""
Tests for mcp_serve â€” Hermes MCP server.

Three layers of tests:
1. Unit tests â€” helpers, content extraction, attachment parsing
2. EventBridge tests â€” queue mechanics, cursors, waiters, concurrency
3. End-to-end tests â€” call actual MCP tools through FastMCP's tool manager
   with real session data in SQLite and sessions.json
"""

import asyncio
import inspect
import json
import os
import sqlite3
import time
import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Redirect HERMES_HOME to a temp directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        import hermes_constants
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    except (ImportError, AttributeError):
        pass
    return tmp_path


@pytest.fixture
def sessions_dir(tmp_path):
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


@pytest.fixture
def sample_sessions():
    return {
        "agent:main:telegram:dm:123456": {
            "session_key": "agent:main:telegram:dm:123456",
            "session_id": "20260329_120000_abc123",
            "platform": "telegram",
            "chat_type": "dm",
            "display_name": "Alice",
            "created_at": "2026-03-29T12:00:00",
            "updated_at": "2026-03-29T14:30:00",
            "input_tokens": 50000,
            "output_tokens": 2000,
            "total_tokens": 52000,
            "origin": {
                "platform": "telegram",
                "chat_id": "123456",
                "chat_name": "Alice",
                "chat_type": "dm",
                "user_id": "123456",
                "user_name": "Alice",
                "thread_id": None,
                "chat_topic": None,
            },
        },
        "agent:main:discord:group:789:456": {
            "session_key": "agent:main:discord:group:789:456",
            "session_id": "20260329_100000_def456",
            "platform": "discord",
            "chat_type": "group",
            "display_name": "Bob",
            "created_at": "2026-03-29T10:00:00",
            "updated_at": "2026-03-29T13:00:00",
            "input_tokens": 30000,
            "output_tokens": 1000,
            "total_tokens": 31000,
            "origin": {
                "platform": "discord",
                "chat_id": "789",
                "chat_name": "#general",
                "chat_type": "group",
                "user_id": "456",
                "user_name": "Bob",
                "thread_id": None,
                "chat_topic": None,
            },
        },
        "agent:main:slack:group:C1234:U5678": {
            "session_key": "agent:main:slack:group:C1234:U5678",
            "session_id": "20260328_090000_ghi789",
            "platform": "slack",
            "chat_type": "group",
            "display_name": "Carol",
            "created_at": "2026-03-28T09:00:00",
            "updated_at": "2026-03-28T11:00:00",
            "input_tokens": 10000,
            "output_tokens": 500,
            "total_tokens": 10500,
            "origin": {
                "platform": "slack",
                "chat_id": "C1234",
                "chat_name": "#engineering",
                "chat_type": "group",
                "user_id": "U5678",
                "user_name": "Carol",
                "thread_id": None,
                "chat_topic": None,
            },
        },
    }


@pytest.fixture
def populated_sessions_dir(sessions_dir, sample_sessions):
    (sessions_dir / "sessions.json").write_text(json.dumps(sample_sessions))
    return sessions_dir


def _create_test_db(db_path, session_id, messages):
    """Create a minimal SQLite DB mimicking hermes_state schema."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT DEFAULT 'cli',
            started_at TEXT,
            message_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp TEXT,
            token_count INTEGER DEFAULT 0,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_details TEXT,
            codex_reasoning_items TEXT
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, source, started_at, message_count) VALUES (?, 'gateway', ?, ?)",
        (session_id, "2026-03-29T12:00:00", len(messages)),
    )
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, (list, dict)):
            content = json.dumps(content)
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp, tool_calls) VALUES (?, ?, ?, ?, ?)",
            (session_id, msg["role"], content,
             msg.get("timestamp", "2026-03-29T12:00:00"),
             json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else None),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def mock_session_db(tmp_path, populated_sessions_dir):
    """Create a real SQLite DB with test messages and wire it up."""
    db_path = tmp_path / "state.db"
    messages = [
        {"role": "user", "content": "Hello Alice!", "timestamp": "2026-03-29T12:00:01"},
        {"role": "assistant", "content": "Hi! How can I help?", "timestamp": "2026-03-29T12:00:05"},
        {"role": "user", "content": "Check the image MEDIA: /tmp/screenshot.png please",
         "timestamp": "2026-03-29T12:01:00"},
        {"role": "assistant", "content": "I see the screenshot. It shows a terminal.",
         "timestamp": "2026-03-29T12:01:10"},
        {"role": "tool", "content": '{"result": "ok"}', "timestamp": "2026-03-29T12:01:15"},
        {"role": "user", "content": "Thanks!", "timestamp": "2026-03-29T12:02:00"},
    ]
    _create_test_db(db_path, "20260329_120000_abc123", messages)

    # Create a mock SessionDB that reads from our test DB
    class TestSessionDB:
        def __init__(self):
            self._db_path = db_path

        def get_messages(self, session_id):
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
            conn.close()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("tool_calls"):
                    d["tool_calls"] = json.loads(d["tool_calls"])
                result.append(d)
            return result

    return TestSessionDB()


class _FakeTool:
    def __init__(self, fn):
        self.name = fn.__name__
        self.description = inspect.getdoc(fn) or ""
        self.fn = fn


class _FakeToolManager:
    def __init__(self):
        self._tools = {}

    def add_tool(self, fn):
        self._tools[fn.__name__] = _FakeTool(fn)

    async def call_tool(self, name, args=None):
        return self._tools[name].fn(**(args or {}))

    def list_tools(self):
        return list(self._tools.values())


class _FakeFastMCP:
    def __init__(self, *args, **kwargs):
        self._tool_manager = _FakeToolManager()

    def tool(self):
        def decorator(fn):
            self._tool_manager.add_tool(fn)
            return fn

        return decorator


@pytest.fixture
def fake_mcp_server(populated_sessions_dir, mock_session_db, monkeypatch):
    import mcp_serve

    monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: populated_sessions_dir)
    monkeypatch.setattr(mcp_serve, "_get_session_db", lambda: mock_session_db)
    monkeypatch.setattr(mcp_serve, "_load_channel_directory", lambda: {})
    monkeypatch.setattr(mcp_serve, "_MCP_SERVER_AVAILABLE", True)
    monkeypatch.setattr(mcp_serve, "FastMCP", _FakeFastMCP)

    bridge = mcp_serve.EventBridge()
    server = mcp_serve.create_mcp_server(event_bridge=bridge)
    return server, bridge


# ---------------------------------------------------------------------------
# 1. UNIT TESTS â€” helpers, extraction, attachments
# ---------------------------------------------------------------------------

class TestImports:
    def test_import_module(self):
        import mcp_serve
        assert hasattr(mcp_serve, "create_mcp_server")
        assert hasattr(mcp_serve, "run_mcp_server")
        assert hasattr(mcp_serve, "EventBridge")

    def test_mcp_available_flag(self):
        import mcp_serve
        assert isinstance(mcp_serve._MCP_SERVER_AVAILABLE, bool)


class TestHelpers:
    def test_load_session_messages_closes_database_on_error(self, monkeypatch):
        import mcp_serve

        db = MagicMock()
        db.get_messages.side_effect = RuntimeError("read failed")
        monkeypatch.setattr(mcp_serve, "_get_session_db", lambda: db)

        messages, error = mcp_serve._load_session_messages("s1")

        assert messages is None
        assert "read failed" in error
        db.close.assert_called_once()

    def test_get_sessions_dir(self, tmp_path):
        from mcp_serve import _get_sessions_dir
        result = _get_sessions_dir()
        assert result == tmp_path / "sessions"

    def test_coerce_int_handles_invalid_and_out_of_range_values(self):
        from mcp_serve import _coerce_int

        assert _coerce_int(None, default=50, minimum=1, maximum=200) == 50
        assert _coerce_int("20", default=50, minimum=1, maximum=200) == 20
        assert _coerce_int("bad", default=50, minimum=1, maximum=200) == 50
        assert _coerce_int(999, default=50, minimum=1, maximum=200) == 200
        assert _coerce_int(-5, default=50, minimum=1, maximum=200) == 1

    def test_load_sessions_index_empty(self, sessions_dir, monkeypatch):
        import mcp_serve
        monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: sessions_dir)
        assert mcp_serve._load_sessions_index() == {}

    def test_load_sessions_index_with_data(self, populated_sessions_dir, monkeypatch):
        import mcp_serve
        monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: populated_sessions_dir)
        result = mcp_serve._load_sessions_index()
        assert len(result) == 3

    def test_load_sessions_index_corrupt(self, sessions_dir, monkeypatch):
        (sessions_dir / "sessions.json").write_text("not json!")
        import mcp_serve
        monkeypatch.setattr(mcp_serve, "_get_sessions_dir", lambda: sessions_dir)
        assert mcp_serve._load_sessions_index() == {}


class TestContentExtraction:
    def test_text(self):
        from mcp_serve import _extract_message_content
        assert _extract_message_content({"content": "Hello"}) == "Hello"

    def test_multipart(self):
        from mcp_serve import _extract_message_content
        msg = {"content": [
            {"type": "text", "text": "A"},
            {"type": "image", "url": "http://x.com/i.png"},
            {"type": "text", "text": "B"},
        ]}
        assert _extract_message_content(msg) == "A\nB"

    def test_empty(self):
        from mcp_serve import _extract_message_content
        assert _extract_message_content({"content": ""}) == ""
        assert _extract_message_content({}) == ""
        assert _extract_message_content({"content": None}) == ""


class TestAttachmentExtraction:
    def test_image_url_block(self):
        from mcp_serve import _extract_attachments
        msg = {"content": [
            {"type": "image_url", "image_url": {"url": "http://x.com/pic.jpg"}},
        ]}
        att = _extract_attachments(msg)
        assert len(att) == 1
        assert att[0] == {"type": "image", "url": "http://x.com/pic.jpg"}

    def test_media_tag_in_text(self):
        from mcp_serve import _extract_attachments
        msg = {"content": "Here MEDIA: /tmp/out.png done"}
        att = _extract_attachments(msg)
        assert len(att) == 1
        assert att[0] == {"type": "media", "path": "/tmp/out.png"}

    def test_multiple_media_tags(self):
        from mcp_serve import _extract_attachments
        msg = {"content": "MEDIA: /a.png and MEDIA: /b.mp3"}
        assert len(_extract_attachments(msg)) == 2

    def test_no_attachments(self):
        from mcp_serve import _extract_attachments
        assert _extract_attachments({"content": "plain text"}) == []

    def test_image_content_block(self):
        from mcp_serve import _extract_attachments
        msg = {"content": [{"type": "image", "url": "http://x.com/p.png"}]}
        att = _extract_attachments(msg)
        assert att[0]["type"] == "image"


# ---------------------------------------------------------------------------
# 2. EVENT BRIDGE TESTS â€” queue, cursors, waiters, concurrency
# ---------------------------------------------------------------------------

class TestEventBridge:
    def test_create(self):
        from mcp_serve import EventBridge
        b = EventBridge()
        assert b._cursor == 0
        assert b._queue == []

    def test_enqueue_and_poll(self):
        from mcp_serve import EventBridge, QueueEvent
        b = EventBridge()
        b._enqueue(QueueEvent(cursor=0, type="message", session_key="k1",
                              data={"content": "hi"}))
        r = b.poll_events(after_cursor=0)
        assert len(r["events"]) == 1
        assert r["events"][0]["type"] == "message"
        assert r["next_cursor"] == 1

    def test_cursor_filter(self):
        from mcp_serve import EventBridge, QueueEvent
        b = EventBridge()
        for i in range(5):
            b._enqueue(QueueEvent(cursor=0, type="message", session_key=f"s{i}"))
        r = b.poll_events(after_cursor=3)
        assert len(r["events"]) == 2
        assert r["events"][0]["session_key"] == "s3"

    def test_session_filter(self):
        from mcp_serve import EventBridge, QueueEvent
        b = EventBridge()
        b._enqueue(QueueEvent(cursor=0, type="message", session_key="a"))
        b._enqueue(QueueEvent(cursor=0, type="message", session_key="b"))
        b._enqueue(QueueEvent(cursor=0, type="message", session_key="a"))
        r = b.poll_events(after_cursoóÏ8¶‰ËkºwµçM•Ìè4(€€€‘•˜Ñ•ÍÑ}•µÁÑå}Í•ÍÍ¥½¹Í}©Í½¸¡Í•±˜°Í•ÍÍ¥½¹Í}‘¥È°µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€¡Í•ÍÍ¥½¹Í}‘¥È€¼€‰Í•ÍÍ¥½¹Ì¹©Í½¸ˆ¤¹İÉ¥Ñ•}Ñ•áĞ ‰íôˆ¤4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹Í}‘¥Èˆ°±…µ‰‘„èÍ•ÍÍ¥½¹Í}‘¥È¤4(€€€€€€€…ÍÍ•ÉĞµÁ}Í•ÉÙ”¹}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤€ôôíô4(4(€€€‘•˜Ñ•ÍÑ}Í•ÍÍ¥½¹Í}İ¥Ñ¡½ÕÑ}½É¥¥¸¡Í•±˜°Í•ÍÍ¥½¹Í}‘¥È°µ½¹­•åÁ…Ñ ¤è4(€€€€€€€‘…Ñ„€ôì‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´èÄÄÄˆèì4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆè€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´èÄÄÄˆ°4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆè€ˆÈÀÈØÀÌÈå|ÄÈÀÀÀÁ}áåèˆ°4(€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ğˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÈèÀÀèÀÀˆ°4(€€€€€€€õô4(€€€€€€€€¡Í•ÍÍ¥½¹Í}‘¥È€¼€‰Í•ÍÍ¥½¹Ì¹©Í½¸ˆ¤¹İÉ¥Ñ•}Ñ•áĞ¡©Í½¸¹‘ÕµÁÌ¡‘…Ñ„¤¤4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹Í}‘¥Èˆ°±…µ‰‘„èÍ•ÍÍ¥½¹Í}‘¥È¤4(€€€€€€€•¹ÑÉ¥•Ì€ôµÁ}Í•ÉÙ”¹}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤4(€€€€€€€…ÍÍ•ÉĞ•¹ÑÉ¥•Íl‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´èÄÄÄ‰ul‰Á±…Ñ™½É´‰t€ôô€‰Ñ•±•É…´ˆ4(4(€€€‘•˜Ñ•ÍÑ}‰É¥‘•}ÍÑ…ÉÑ}ÍÑ½À¡Í•±˜¤è4(€€€€€€€™É½´µÁ}Í•ÉÙ”¥µÁ½ÉĞÙ•¹Ñ	É¥‘”4(€€€€€€€ˆ€ôÙ•¹Ñ	É¥‘” ¤4(€€€€€€€…ÍÍ•ÉĞ¹½Ğˆ¹}ÉÕ¹¹¥¹œ4(€€€€€€€ˆ¹}ÉÕ¹¹¥¹œ€ôQÉÕ”4(€€€€€€€ˆ¹ÍÑ½À ¤4(€€€€€€€…ÍÍ•ÉĞ¹½Ğˆ¹}ÉÕ¹¹¥¹œ4(4(€€€‘•˜Ñ•ÍÑ}ÑÉÕ¹…Ñ¥½¸¡Í•±˜¤è4(€€€€€€€…ÍÍ•ÉĞ±•¸  ‰àˆ€¨€ÔÀÀÀ¥lèÈÀÀÁt¤€ôô€ÈÀÀÀ4(4(4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(Œ€Ü¸Y9P	I%A=101==@ÉƒŠPÉ•…°ME1¥Ñ”°µÑ¥µ”½ÁÑ¥µ¥é…Ñ¥½¸4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4)±…ÍÌQ•ÍÑÙ•¹Ñ	É¥‘•A½±±Éè4(€€€€ˆˆ‰¹µÑ¼µ•¹Ñ•ÍÑÌ™½ÈÑ¡”Ù•¹Ñ	É¥‘”Á½±±¥¹œ±½½Àİ¥Ñ É•…°™¥±•Ì¸ˆˆˆ4(4(€€€‘•˜Ñ•ÍÑ}Á½±±}‘•Ñ•ÑÍ}¹•İ}µ•ÍÍ…•Ì¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰]É¥Ñ”Ñ¼ME1¥Ñ”€¬Í•ÍÍ¥½¹Ì¹©Í½¸°Ù•É¥™äÙ•¹Ñ	É¥‘”Á¥­Ì¥ĞÕÀ¸ˆˆˆ4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í•ÍÍ¥½¹Ìˆ4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È¹µ­‘¥È ¤4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹Í}‘¥Èˆ°±…µ‰‘„èÍ•ÍÍ¥½¹Í}‘¥È¤4(4(€€€€€€€Í•ÍÍ¥½¹}¥€ô€ˆÈÀÈØÀÌÈå|ÄÔÀÀÀÁ}Á½±±}Ñ•ÍĞˆ4(€€€€€€€‘‰}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰ÍÑ…Ñ”¹‘ˆˆ4(4(€€€€€€€€Œ]É¥Ñ”Í•ÍÍ¥½¹Ì¹©Í½¸4(€€€€€€€Í•ÍÍ¥½¹Í}‘…Ñ„€ôì4(€€€€€€€€€€€€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´éÁ½±±}Ñ•ÍĞˆèì4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆè€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´éÁ½±±}Ñ•ÍĞˆ°4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥°4(€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€€€€€‰¡…Ñ}ÑåÁ”ˆè€‰‘´ˆ°4(€€€€€€€€€€€€€€€€‰‘¥ÍÁ±…å}¹…µ”ˆè€‰A½±±Q•ÍĞˆ°4(€€€€€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ğˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÔˆ°4(€€€€€€€€€€€€€€€€‰½É¥¥¸ˆèì‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°€‰¡…Ñ}¥ˆè€‰Á½±±}Ñ•ÍĞ‰ô°4(€€€€€€€€€€€ô4(€€€€€€€ô4(€€€€€€€€¡Í•ÍÍ¥½¹Í}‘¥È€¼€‰Í•ÍÍ¥½¹Ì¹©Í½¸ˆ¤¹İÉ¥Ñ•}Ñ•áĞ¡©Í½¸¹‘ÕµÁÌ¡Í•ÍÍ¥½¹Í}‘…Ñ„¤¤4(4(€€€€€€€€Œ]É¥Ñ”µ•ÍÍ…•ÌÑ¼ME1¥Ñ”4(€€€€€€€µ•ÍÍ…•Ì€ôl4(€€€€€€€€€€€ì‰É½±”ˆè€‰ÕÍ•Èˆ°€‰½¹Ñ•¹Ğˆè€‰¥ÉÍĞµ•ÍÍ…”ˆ°4(€€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÄ‰ô°4(€€€€€€€€€€€ì‰É½±”ˆè€‰…ÍÍ¥ÍÑ…¹Ğˆ°€‰½¹Ñ•¹Ğˆè€‰I•Á±äˆ°4(€€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÌ‰ô°4(€€€€€€€t4(€€€€€€€}É•…Ñ•}Ñ•ÍÑ}‘ˆ¡‘‰}Á…Ñ °Í•ÍÍ¥½¹}¥°µ•ÍÍ…•Ì¤4(4(€€€€€€€€ŒÉ•…Ñ”„µ½¬M•ÍÍ¥½¹Ñ¡…ĞÉ•…‘Ì½ÕÈÑ•ÍĞ4(€€€€€€€±…ÍÌQ•ÍÑè4(€€€€€€€€€€€‘•˜•Ñ}µ•ÍÍ…•Ì¡Í•±˜°Í¥¤è4(€€€€€€€€€€€€€€€½¹¸€ôÍÅ±¥Ñ”Ì¹½¹¹•Ğ¡ÍÑÈ¡‘‰}Á…Ñ ¤¤4(€€€€€€€€€€€€€€€½¹¸¹É½İ}™…Ñ½Éä€ôÍÅ±¥Ñ”Ì¹I½Ü4(€€€€€€€€€€€€€€€É½İÌ€ô½¹¸¹•á•ÕÑ” 4(€€€€€€€€€€€€€€€€€€€€‰M1P€¨I=4µ•ÍÍ…•Ì]!IÍ•ÍÍ¥½¹}¥€ô€ü=IH	d¥ˆ°4(€€€€€€€€€€€€€€€€€€€€¡Í¥°¤°4(€€€€€€€€€€€€€€€€¤¹™•Ñ¡…±° ¤4(€€€€€€€€€€€€€€€½¹¸¹±½Í” ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸m‘¥Ğ¡È¤™½ÈÈ¥¸É½İÍt4(4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹}‘ˆˆ°±…µ‰‘„èQ•ÍÑ ¤¤4(4(€€€€€€€‰É¥‘”€ôµÁ}Í•ÉÙ”¹Ù•¹Ñ	É¥‘” ¤4(€€€€€€€€ŒIÕ¸½¹”Á½±°å±”µ…¹Õ…±±ä4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡Q•ÍÑ ¤¤4(4(€€€€€€€€ŒM¡½Õ±¡…Ù”™½Õ¹Ñ¡”µ•ÍÍ…•Ì4(€€€€€€€É•ÍÕ±Ğ€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈôÀ¤4(€€€€€€€…ÍÍ•ÉĞ±•¸¡É•ÍÕ±Ñl‰•Ù•¹ÑÌ‰t¤€ôô€È4(€€€€€€€…ÍÍ•ÉĞÉ•ÍÕ±Ñl‰•Ù•¹ÑÌ‰ulÁul‰É½±”‰t€ôô€‰ÕÍ•Èˆ4(€€€€€€€…ÍÍ•ÉĞÉ•ÍÕ±Ñl‰•Ù•¹ÑÌ‰ulÁul‰½¹Ñ•¹Ğ‰t€ôô€‰¥ÉÍĞµ•ÍÍ…”ˆ4(€€€€€€€…ÍÍ•ÉĞÉ•ÍÕ±Ñl‰•Ù•¹ÑÌ‰ulÅul‰É½±”‰t€ôô€‰…ÍÍ¥ÍÑ…¹Ğˆ4(4(€€€‘•˜Ñ•ÍÑ}Á½±±}Í­¥ÁÍ}İ¡•¹}Õ¹¡…¹•¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰M•½¹Á½±°İ¥Ñ ¹¼™¥±”¡…¹•ÌÍ¡½Õ±‰”„¹¼µ½À¸ˆˆˆ4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í•ÍÍ¥½¹Ìˆ4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È¹µ­‘¥È ¤4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹Í}‘¥Èˆ°±…µ‰‘„èÍ•ÍÍ¥½¹Í}‘¥È¤4(4(€€€€€€€Í•ÍÍ¥½¹}¥€ô€ˆÈÀÈØÀÌÈå|ÄÔÀÀÀÁ}Í­¥Á}Ñ•ÍĞˆ4(€€€€€€€‘‰}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰ÍÑ…Ñ”¹‘ˆˆ4(4(€€€€€€€Í•ÍÍ¥½¹Í}‘…Ñ„€ôì4(€€€€€€€€€€€€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´éÍ­¥Àˆèì4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆè€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´éÍ­¥Àˆ°4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥°4(€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ğˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÔˆ°4(€€€€€€€€€€€€€€€€‰½É¥¥¸ˆèì‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°€‰¡…Ñ}¥ˆè€‰Í­¥À‰ô°4(€€€€€€€€€€€ô4(€€€€€€€ô4(€€€€€€€€¡Í•ÍÍ¥½¹Í}‘¥È€¼€‰Í•ÍÍ¥½¹Ì¹©Í½¸ˆ¤¹İÉ¥Ñ•}Ñ•áĞ¡©Í½¸¹‘ÕµÁÌ¡Í•ÍÍ¥½¹Í}‘…Ñ„¤¤4(€€€€€€€}É•…Ñ•}Ñ•ÍÑ}‘ˆ¡‘‰}Á…Ñ °Í•ÍÍ¥½¹}¥°l4(€€€€€€€€€€€ì‰É½±”ˆè€‰ÕÍ•Èˆ°€‰½¹Ñ•¹Ğˆè€‰!•±±¼ˆ°€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÄ‰ô°4(€€€€€€€t¤4(4(€€€€€€€±…ÍÌQ•ÍÑè4(€€€€€€€€€€€‘•˜}}¥¹¥Ñ}|¡Í•±˜¤è4(€€€€€€€€€€€€€€€Í•±˜¹…±±}½Õ¹Ğ€ô€À4(4(€€€€€€€€€€€‘•˜•Ñ}µ•ÍÍ…•Ì¡Í•±˜°Í¥¤è4(€€€€€€€€€€€€€€€Í•±˜¹…±±}½Õ¹Ğ€¬ô€Ä4(€€€€€€€€€€€€€€€½¹¸€ôÍÅ±¥Ñ”Ì¹½¹¹•Ğ¡ÍÑÈ¡‘‰}Á…Ñ ¤¤4(€€€€€€€€€€€€€€€½¹¸¹É½İ}™…Ñ½Éä€ôÍÅ±¥Ñ”Ì¹I½Ü4(€€€€€€€€€€€€€€€É½İÌ€ô½¹¸¹•á•ÕÑ” 4(€€€€€€€€€€€€€€€€€€€€‰M1P€¨I=4µ•ÍÍ…•Ì]!IÍ•ÍÍ¥½¹}¥€ô€ü=IH	d¥ˆ°4(€€€€€€€€€€€€€€€€€€€€¡Í¥°¤°4(€€€€€€€€€€€€€€€€¤¹™•Ñ¡…±° ¤4(€€€€€€€€€€€€€€€½¹¸¹±½Í” ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸m‘¥Ğ¡È¤™½ÈÈ¥¸É½İÍt4(4(€€€€€€€‘ˆ€ôQ•ÍÑ ¤4(€€€€€€€‰É¥‘”€ôµÁ}Í•ÉÙ”¹Ù•¹Ñ	É¥‘” ¤4(4(€€€€€€€€Œ¥ÉÍĞÁ½±°ƒŠPÍ¡½Õ±ÁÉ½•ÍÌ4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡‘ˆ¤4(€€€€€€€™¥ÉÍÑ}…±±Ì€ô‘ˆ¹…±±}½Õ¹Ğ4(€€€€€€€…ÍÍ•ÉĞ™¥ÉÍÑ}…±±Ì€øô€Ä4(4(€€€€€€€€ŒM•½¹Á½±°ƒŠP™¥±•ÌÕ¹¡…¹•°Í¡½Õ±Í­¥À•¹Ñ¥É•±ä4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡‘ˆ¤4(€€€€€€€…ÍÍ•ÉĞ‘ˆ¹…±±}½Õ¹Ğ€ôô™¥ÉÍÑ}…±±Ì°p4(€€€€€€€€€€€€‰M•½¹Á½±°Í¡½Õ±Í­¥ÀÅÕ•É¥•Ìİ¡•¸™¥±•ÌÕ¹¡…¹•ˆ4(4(€€€‘•˜Ñ•ÍÑ}Á½±±}‘•Ñ•ÑÍ}¹•İ}µ•ÍÍ…•}…™Ñ•É}‘‰}İÉ¥Ñ”¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰]É¥Ñ”„¹•Üµ•ÍÍ…”Ñ¼Ñ¡”…™Ñ•È™¥ÉÍĞÁ½±°°Ù•É¥™ä¥ĞÌ‘•Ñ•Ñ•¸ˆˆˆ4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í•ÍÍ¥½¹Ìˆ4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È¹µ­‘¥È ¤4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹Í}‘¥Èˆ°±…µ‰‘„èÍ•ÍÍ¥½¹Í}‘¥È¤4(4(€€€€€€€Í•ÍÍ¥½¹}¥€ô€ˆÈÀÈØÀÌÈå|ÄÔÀÀÀÁ}¹•İ}µÍœˆ4(€€€€€€€‘‰}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰ÍÑ…Ñ”¹‘ˆˆ4(4(€€€€€€€Í•ÍÍ¥½¹Í}‘…Ñ„€ôì4(€€€€€€€€€€€€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é¹•Üˆèì4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆè€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é¹•Üˆ°4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥°4(€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ğˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÔˆ°4(€€€€€€€€€€€€€€€€‰½É¥¥¸ˆèì‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°€‰¡…Ñ}¥ˆè€‰¹•Ü‰ô°4(€€€€€€€€€€€ô4(€€€€€€€ô4(€€€€€€€€¡Í•ÍÍ¥½¹Í}‘¥È€¼€‰Í•ÍÍ¥½¹Ì¹©Í½¸ˆ¤¹İÉ¥Ñ•}Ñ•áĞ¡©Í½¸¹‘ÕµÁÌ¡Í•ÍÍ¥½¹Í}‘…Ñ„¤¤4(€€€€€€€}É•…Ñ•}Ñ•ÍÑ}‘ˆ¡‘‰}Á…Ñ °Í•ÍÍ¥½¹}¥°l4(€€€€€€€€€€€ì‰É½±”ˆè€‰ÕÍ•Èˆ°€‰½¹Ñ•¹Ğˆè€‰¥ÉÍĞˆ°€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÄ‰ô°4(€€€€€€€t¤4(4(€€€€€€€±…ÍÌQ•ÍÑè4(€€€€€€€€€€€‘•˜•Ñ}µ•ÍÍ…•Ì¡Í•±˜°Í¥¤è4(€€€€€€€€€€€€€€€½¹¸€ôÍÅ±¥Ñ”Ì¹½¹¹•Ğ¡ÍÑÈ¡‘‰}Á…Ñ ¤¤4(€€€€€€€€€€€€€€€½¹¸¹É½İ}™…Ñ½Éä€ôÍÅ±¥Ñ”Ì¹I½Ü4(€€€€€€€€€€€€€€€É½İÌ€ô½¹¸¹•á•ÕÑ” 4(€€€€€€€€€€€€€€€€€€€€‰M1P€¨I=4µ•ÍÍ…•Ì]!IÍ•ÍÍ¥½¹}¥€ô€ü=IH	d¥ˆ°4(€€€€€€€€€€€€€€€€€€€€¡Í¥°¤°4(€€€€€€€€€€€€€€€€¤¹™•Ñ¡…±° ¤4(€€€€€€€€€€€€€€€½¹¸¹±½Í” ¤4(€€€€€€€€€€€€€€€É•ÑÕÉ¸m‘¥Ğ¡È¤™½ÈÈ¥¸É½İÍt4(4(€€€€€€€‘ˆ€ôQ•ÍÑ ¤4(€€€€€€€‰É¥‘”€ôµÁ}Í•ÉÙ”¹Ù•¹Ñ	É¥‘” ¤4(4(€€€€€€€€Œ¥ÉÍĞÁ½±°4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡‘ˆ¤4(€€€€€€€ÈÄ€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈôÀ¤4(€€€€€€€…ÍÍ•ÉĞ±•¸¡ÈÅl‰•Ù•¹ÑÌ‰t¤€ôô€Ä4(4(€€€€€€€€Œ‘„¹•Üµ•ÍÍ…”Ñ¼Ñ¡”4(€€€€€€€½¹¸€ôÍÅ±¥Ñ”Ì¹½¹¹•Ğ¡ÍÑÈ¡‘‰}Á…Ñ ¤¤4(€€€€€€€½¹¸¹•á•ÕÑ” 4(€€€€€€€€€€€€‰%9MIP%9Q<µ•ÍÍ…•Ì€¡Í•ÍÍ¥½¹}¥°É½±”°½¹Ñ•¹Ğ°Ñ¥µ•ÍÑ…µÀ¤Y1UL€ ü°€ü°€ü°€ü¤ˆ°4(€€€€€€€€€€€€¡Í•ÍÍ¥½¹}¥°€‰…ÍÍ¥ÍÑ…¹Ğˆ°€‰9•ÜÉ•Á±ä„ˆ°€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÄÀˆ¤°4(€€€€€€€€¤4(€€€€€€€½¹¸¹½µµ¥Ğ ¤4(€€€€€€€½¹¸¹±½Í” ¤4(€€€€€€€€ŒQ½Õ Ñ¡”™¥±”Ñ¼ÕÁ‘…Ñ”µÑ¥µ”€¡]0µ½‘”µ…ä¹½ĞÕÁ‘…Ñ”µÑ¥µ”½¸Íµ…±°İÉ¥Ñ•Ì¤4(€€€€€€€½Ì¹ÕÑ¥µ”¡‘‰}Á…Ñ °9½¹”¤4(4(€€€€€€€€ŒUÁ‘…Ñ”Í•ÍÍ¥½¹Ì¹©Í½¸ÕÁ‘…Ñ•‘}…ĞÑ¼ÑÉ¥•ÈÉ”µ¡•¬4(€€€€€€€Í•ÍÍ¥½¹Í}‘…Ñ…l‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é¹•Ü‰ul‰ÕÁ‘…Ñ•‘}…Ğ‰t€ô€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÄÀˆ4(€€€€€€€€¡Í•ÍÍ¥½¹Í}‘¥È€¼€‰Í•ÍÍ¥½¹Ì¹©Í½¸ˆ¤¹İÉ¥Ñ•}Ñ•áĞ¡©Í½¸¹‘ÕµÁÌ¡Í•ÍÍ¥½¹Í}‘…Ñ„¤¤4(4(€€€€€€€€ŒM•½¹Á½±°ƒŠPÍ¡½Õ±‘•Ñ•ĞÑ¡”¹•Üµ•ÍÍ…”4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡‘ˆ¤4(€€€€€€€ÈÈ€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈõÈÅl‰¹•áÑ}ÕÉÍ½È‰t¤4(€€€€€€€…ÍÍ•ÉĞ±•¸¡ÈÉl‰•Ù•¹ÑÌ‰t¤€ôô€Ä4(€€€€€€€…ÍÍ•ÉĞÈÉl‰•Ù•¹ÑÌ‰ulÁul‰½¹Ñ•¹Ğ‰t€ôô€‰9•ÜÉ•Á±ä„ˆ4(4(€€€‘•˜Ñ•ÍÑ}Á½±±}Á¥­Í}ÕÁ}¹•İ}½¹Ù•ÉÍ…Ñ¥½¹}½¹}‘‰}¡…¹” 4(€€€€€€€Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ 4(€€€€¤è4(€€€€€€€€ˆˆ‰‰É…¹µ¹•Ü½¹Ù•ÉÍ…Ñ¥½¸µÕÍĞ‰”Á¥­•ÕÀ½¸Ñ¡”Ñ¥¬İ¡•É”4(€€€€€€€ÍÑ…Ñ”¹‘ˆ¡…¹•Ì¸4(4(€€€€€€€M¥¹”€ŒäÀÀØÑ¡”É½ÕÑ¥¹œ¥¹‘•à±¥Ù•Ì%8ÍÑ…Ñ”¹‘ˆ€¡Í•ÍÍ¥½¸É½İÌ…ÉÉä4(€€€€€€€Í•ÍÍ¥½¹}­•ä½½É¥¥¸µ•Ñ…‘…Ñ„¤°Í¼„¹•Ü½¹Ù•ÉÍ…Ñ¥½¸ÌÉ•¥ÍÑÉ…Ñ¥½¸…¹4(€€€€€€€¥ÑÌ™¥ÉÍĞµ•ÍÍ…”±…¹¥¸Ñ¡”Í…µ”™¥±”ƒŠP„Í¥¹±”µÑ¥µ”¡•¬½Ù•ÉÌ4(€€€€€€€‰½Ñ …¹Ñ¡”½±‘Õ…°µ™¥±”€¡Í•ÍÍ¥½¹Ì¹©Í½¸€¬ÍÑ…Ñ”¹‘ˆ¤É…”€ ŒàäÈÔ¤¥Ì4(€€€€€€€ÍÑÉÕÑÕÉ…±±ä¥µÁ½ÍÍ¥‰±”¸Q¡¥ÌÑ•ÍĞ…ÍÍ•ÉÑÌÑ¡”¥¹‘•à¥ÌÉ•™É•Í¡•½¸„4(€€€€€€€‘ˆµµÑ¥µ”‰ÕµÀ°Í¼„½¹Ù•ÉÍ…Ñ¥½¸Ñ¡”‰É¥‘”¡…Ì¹•Ù•ÈÍ••¸‰•™½É”¥Ì4(€€€€€€€•µ¥ÑÑ•½¸Ñ¡”Í…µ”Ñ¥¬¸4(€€€€€€€€ˆˆˆ4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È€ôÑµÁ}Á…Ñ €¼€‰Í•ÍÍ¥½¹Ìˆ4(€€€€€€€Í•ÍÍ¥½¹Í}‘¥È¹µ­‘¥È ¤4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹Í}‘¥Èˆ°±…µ‰‘„èÍ•ÍÍ¥½¹Í}‘¥È¤4(4(€€€€€€€€Œ}Á½±±}½¹”É•…‘Ì€ñ!I5M}!=5ø½ÍÑ…Ñ”¹‘ˆ™½È¥ÑÌµÑ¥µ”…Ñ”ìÑ¡”…ÕÑ½ÕÍ”4(€€€€€€€€Œ™¥áÑÕÉ”Á½¥¹ÑÌ!I5M}!=5…ĞÑµÁ}Á…Ñ ¸4(€€€€€€€‘‰}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰ÍÑ…Ñ”¹‘ˆˆ4(€€€€€€€‘‰}Á…Ñ ¹İÉ¥Ñ•}Ñ•áĞ ‰Á±…•¡½±‘•Èˆ¤4(4(€€€€€€€Í•ÍÍ¥½¹}¥€ô€ˆÈÀÈØÀÌÈå|ÄÔÀÀÀÁ}±…Ñ•}É•¥ÍÑ•Èˆ4(€€€€€€€€ŒQ¡”É½ÕÑ¥¹œ¥¹‘•à¹½Ü½µ•Ì™É½´}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤€¡ÍÑ…Ñ”¹‘ˆ4(€€€€€€€€ŒÁÉ¥µ…Éä°Í•ÍÍ¥½¹Ì¹©Í½¸™…±±‰…¬¤¸MÑÕˆ¥ĞÑ¼É•ÑÕÉ¸Ñ¡”¹•Ü4(€€€€€€€€Œ½¹Ù•ÉÍ…Ñ¥½¸°Í¥µÕ±…Ñ¥¹œÑ¡”…Ñ•İ…ä¡…Ù¥¹œ©ÕÍĞİÉ¥ÑÑ•¸Ñ¡”4(€€€€€€€€ŒÍ•ÍÍ¥½¸É½Ü€¬™¥ÉÍĞµ•ÍÍ…”¥¸½¹”ÍÑ…Ñ”¹‘ˆÑÉ…¹Í…Ñ¥½¸¸4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ 4(€€€€€€€€€€€µÁ}Í•ÉÙ”°€‰}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•àˆ°4(€€€€€€€€€€€±…µ‰‘„èì4(€€€€€€€€€€€€€€€€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é±…Ñ”ˆèì4(€€€€€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥°4(€€€€€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€€€€€€€€€‰½É¥¥¸ˆèì‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°€‰¡…Ñ}¥ˆè€‰±…Ñ”‰ô°4(€€€€€€€€€€€€€€€ô4(€€€€€€€€€€€ô°4(€€€€€€€€¤4(4(€€€€€€€±…ÍÌè4(€€€€€€€€€€€‘•˜•Ñ}µ•ÍÍ…•Ì¡Í•±˜°Í¥¤è4(€€€€€€€€€€€€€€€É•ÑÕÉ¸mì4(€€€€€€€€€€€€€€€€€€€€‰¥ˆè€Ä°€‰É½±”ˆè€‰ÕÍ•Èˆ°4(€€€€€€€€€€€€€€€€€€€€‰½¹Ñ•¹Ğˆè€‰!•±±¼™É½´„™É•Í¡±äµÉ•¥ÍÑ•É•½¹Ù•ÉÍ…Ñ¥½¸ˆ°4(€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÀˆ°4(€€€€€€€€€€€€€€€õt4(4(€€€€€€€‰É¥‘”€ôµÁ}Í•ÉÙ”¹Ù•¹Ñ	É¥‘” ¤4(€€€€€€€€Œ	É¥‘”¡…Ì¹•Ù•ÈÍ••¸Ñ¡¥Ì‘ˆÍÑ…Ñ”€¡µÑ¥µ”‘¥™™•ÉÌ¤…¹¡…Ì…¸4(€€€€€€€€Œ•µÁÑä…¡•¥¹‘•àƒŠP•á…Ñ±äÑ¡”ÍÑ…Ñ”…™Ñ•È„¹•Ü½¹Ù•ÉÍ…Ñ¥½¸Ì4(€€€€€€€€Œ™¥ÉÍĞİÉ¥Ñ”¸4(€€€€€€€‰É¥‘”¹}ÍÑ…Ñ•}‘‰}µÑ¥µ”€ô€À¸À4(€€€€€€€…ÍÍ•ÉĞ‰É¥‘”¹}…¡•‘}Í•ÍÍ¥½¹Í}¥¹‘•à€ôôíô4(4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡ ¤¤4(4(€€€€€€€É•ÍÕ±Ğ€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈôÀ¤4(€€€€€€€…ÍÍ•ÉĞ±•¸¡É•ÍÕ±Ñl‰•Ù•¹ÑÌ‰t¤€ôô€Ä4(€€€€€€€…ÍÍ•ÉĞÉ•ÍÕ±Ñl‰•Ù•¹ÑÌ‰ulÁul‰Í•ÍÍ¥½¹}­•ä‰t€ôô€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é±…Ñ”ˆ4(€€€€€€€…ÍÍ•ÉĞÉ•ÍÕ±Ñl‰•Ù•¹ÑÌ‰ulÁul‰½¹Ñ•¹Ğ‰t¹ÍÑ…ÉÑÍİ¥Ñ  ‰!•±±¼™É½´„™É•Í¡±äˆ¤4(4(€€€‘•˜Ñ•ÍÑ}ÍÑ…ÉÑÕÁ}‰…Í•±¥¹•}ÍÕÁÁÉ•ÍÍ•Í}¡¥ÍÑ½É¥…±}É•Á±…ä¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰ÍÑ…ÉĞ ¤Ì‰…Í•±¥¹”É•½É‘Ì•á¥ÍÑ¥¹œ¡¥ÍÑ½Éäİ¥Ñ¡½ÕĞ•µ¥ÑÑ¥¹œ¥Ğ°Í¼„4(€€€€€€€™É•Í Ù•¹Ñ	É¥‘”‘½•Ì¹½ĞÉ•Á±…äÍÑ½É•µ•ÍÍ…•Ì½¸ÍÑ…ÉÑÕÀì½¹±ä4(€€€€€€€µ•ÍÍ…•ÌİÉ¥ÑÑ•¸…™Ñ•ÈÑ¡”‰…Í•±¥¹”…É”‘•±¥Ù•É•¸ˆˆˆ4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(4(€€€€€€€‘‰}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰ÍÑ…Ñ”¹‘ˆˆ4(€€€€€€€‘‰}Á…Ñ ¹İÉ¥Ñ•}Ñ•áĞ ‰Á±…•¡½±‘•Èˆ¤4(€€€€€€€Í•ÍÍ¥½¹}¥€ô€ˆÈÀÈØÀÌÈå|ÄÔÀÀÀÁ}¡¥ÍÑ½Éäˆ4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ 4(€€€€€€€€€€€µÁ}Í•ÉÙ”°€‰}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•àˆ°4(€€€€€€€€€€€±…µ‰‘„èì4(€€€€€€€€€€€€€€€€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é¡¥ÍĞˆèì4(€€€€€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ•ÍÍ¥½¹}¥°4(€€€€€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€€€€€€€€€‰½É¥¥¸ˆèì‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°€‰¡…Ñ}¥ˆè€‰¡¥ÍĞ‰ô°4(€€€€€€€€€€€€€€€ô4(€€€€€€€€€€€ô°4(€€€€€€€€¤4(€€€€€€€ÍÑ½É”€ômì4(€€€€€€€€€€€€‰¥ˆè€Ä°€‰É½±”ˆè€‰ÕÍ•Èˆ°€‰½¹Ñ•¹Ğˆè€‰ÁÉ”µ•á¥ÍÑ¥¹œ¡¥ÍÑ½Éäˆ°4(€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÀèÀÀˆ°4(€€€€€€€õt4(4(€€€€€€€±…ÍÌè4(€€€€€€€€€€€‘•˜•Ñ}µ•ÍÍ…•Ì¡Í•±˜°Í¥¤è4(€€€€€€€€€€€€€€€É•ÑÕÉ¸±¥ÍĞ¡ÍÑ½É”¤4(4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹}‘ˆˆ°±…µ‰‘„è ¤¤4(4(€€€€€€€‰É¥‘”€ôµÁ}Í•ÉÙ”¹Ù•¹Ñ	É¥‘” ¤4(€€€€€€€‰É¥‘”¹}•ÍÑ…‰±¥Í¡}‰…Í•±¥¹” ¤4(€€€€€€€€Œ5•ÍÍ…•ÌÑ¡…Ğ•á¥ÍÑ•‰•™½É”ÍÑ…ÉĞ ¤…É”¹½ĞÉ•Á±…å•¸4(€€€€€€€…ÍÍ•ÉĞ‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈôÀ¥l‰•Ù•¹ÑÌ‰t€ôômt4(4(€€€€€€€€Œµ•ÍÍ…”İÉ¥ÑÑ•¸…™Ñ•ÈÑ¡”‰…Í•±¥¹”%L‘•±¥Ù•É•½¸Ñ¡”¹•áĞÑ¥¬¸4(€€€€€€€ÍÑ½É”¹…ÁÁ•¹¡ì4(€€€€€€€€€€€€‰¥ˆè€È°€‰É½±”ˆè€‰…ÍÍ¥ÍÑ…¹Ğˆ°€‰½¹Ñ•¹Ğˆè€‰…ÉÉ¥Ù•…™Ñ•ÈÍÑ…ÉĞˆ°4(€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÀÔèÀÀˆ°4(€€€€€€€ô¤4(€€€€€€€½Ì¹ÕÑ¥µ”¡‘‰}Á…Ñ °9½¹”¤€€Œ‰ÕµÀµÑ¥µ”Í¼Ñ¡”Á½±°…Ñ”½Á•¹Ì4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡ ¤¤4(€€€€€€€•Ù•¹ÑÌ€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈôÀ¥l‰•Ù•¹ÑÌ‰t4(€€€€€€€…ÍÍ•ÉĞ±•¸¡•Ù•¹ÑÌ¤€ôô€Ä4(€€€€€€€…ÍÍ•ÉĞ•Ù•¹ÑÍlÁul‰½¹Ñ•¹Ğ‰t€ôô€‰…ÉÉ¥Ù•…™Ñ•ÈÍÑ…ÉĞˆ4(4(€€€‘•˜Ñ•ÍÑ}¹•İ}½¹Ù•ÉÍ…Ñ¥½¹}…™Ñ•É}‰…Í•±¥¹•}¥Í}‘•±¥Ù•É•¡Í•±˜°ÑµÁ}Á…Ñ °µ½¹­•åÁ…Ñ ¤è4(€€€€€€€€ˆˆ‰½¹Ù•ÉÍ…Ñ¥½¸Ñ¡…Ğ™¥ÉÍĞ…ÁÁ•…ÉÌQHÑ¡”ÍÑ…ÉÑÕÀ‰…Í•±¥¹”¥ÌÍÑ¥±°4(€€€€€€€‘•±¥Ù•É•½¸¥ÑÌÍÑ…Ñ”¹‘ˆµ¡…¹”Ñ¥¬ƒŠPÍ•ÍÍ¥½¹Ì…‰Í•¹Ğ™É½´Ñ¡”4(€€€€€€€‰…Í•±¥¹”‘•™…Õ±ĞÑ¼±…ÍÑ}Í••¸ôÀ¸À¸ˆˆˆ4(€€€€€€€¥µÁ½ÉĞµÁ}Í•ÉÙ”4(4(€€€€€€€‘‰}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰ÍÑ…Ñ”¹‘ˆˆ4(€€€€€€€‘‰}Á…Ñ ¹İÉ¥Ñ•}Ñ•áĞ ‰Á±…•¡½±‘•Èˆ¤4(€€€€€€€¥¹‘•àè‘¥Ğ€ôíô4(€€€€€€€µ•ÍÍ…•Ìè‘¥Ğ€ôíô4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•àˆ°±…µ‰‘„è‘¥Ğ¡¥¹‘•à¤¤4(4(€€€€€€€±…ÍÌè4(€€€€€€€€€€€‘•˜•Ñ}µ•ÍÍ…•Ì¡Í•±˜°Í¥¤è4(€€€€€€€€€€€€€€€É•ÑÕÉ¸±¥ÍĞ¡µ•ÍÍ…•Ì¹•Ğ¡Í¥°mt¤¤4(4(€€€€€€€µ½¹­•åÁ…Ñ ¹Í•Ñ…ÑÑÈ¡µÁ}Í•ÉÙ”°€‰}•Ñ}Í•ÍÍ¥½¹}‘ˆˆ°±…µ‰‘„è ¤¤4(4(€€€€€€€‰É¥‘”€ôµÁ}Í•ÉÙ”¹Ù•¹Ñ	É¥‘” ¤4(€€€€€€€‰É¥‘”¹}•ÍÑ…‰±¥Í¡}‰…Í•±¥¹” ¤€€Œ¹¼½¹Ù•ÉÍ…Ñ¥½¹Ì•á¥ÍĞå•Ğ4(4(€€€€€€€€ŒQ¡”…Ñ•İ…äÉ•¥ÍÑ•ÉÌ„‰É…¹µ¹•Ü½¹Ù•ÉÍ…Ñ¥½¸€¬¥ÑÌ™¥ÉÍĞµ•ÍÍ…”¸4(€€€€€€€Í¥€ô€ˆÈÀÈØÀÌÈå|ÄÔÀÀÀÁ}™É•Í ˆ4(€€€€€€€¥¹‘•ál‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é™É•Í ‰t€ôì4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèÍ¥°4(€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°4(€€€€€€€€€€€€‰½É¥¥¸ˆèì‰Á±…Ñ™½É´ˆè€‰Ñ•±•É…´ˆ°€‰¡…Ñ}¥ˆè€‰™É•Í ‰ô°4(€€€€€€€ô4(€€€€€€€µ•ÍÍ…•ÍmÍ¥‘t€ômì4(€€€€€€€€€€€€‰¥ˆè€Ä°€‰É½±”ˆè€‰ÕÍ•Èˆ°€‰½¹Ñ•¹Ğˆè€‰¡•±±¼…™Ñ•È‰…Í•±¥¹”ˆ°4(€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆè€ˆÈÀÈØ´ÀÌ´ÈåPÄÔèÄÀèÀÀˆ°4(€€€€€€€õt4(€€€€€€€½Ì¹ÕÑ¥µ”¡‘‰}Á…Ñ °9½¹”¤4(€€€€€€€‰É¥‘”¹}Á½±±}½¹”¡ ¤¤4(4(€€€€€€€•Ù•¹ÑÌ€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ¡…™Ñ•É}ÕÉÍ½ÈôÀ¥l‰•Ù•¹ÑÌ‰t4(€€€€€€€…ÍÍ•ÉĞ±•¸¡•Ù•¹ÑÌ¤€ôô€Ä4(€€€€€€€…ÍÍ•ÉĞ•Ù•¹ÑÍlÁul‰Í•ÍÍ¥½¹}­•ä‰t€ôô€‰…•¹Ğéµ…¥¸éÑ•±•É…´é‘´é™É•Í ˆ4(€€€€€€€…ÍÍ•ÉĞ•Ù•¹ÑÍlÁul‰½¹Ñ•¹Ğ‰t€ôô€‰¡•±±¼…™Ñ•È‰…Í•±¥¹”ˆ4(4(€€€‘•˜Ñ•ÍÑ}Á½±±}¥¹Ñ•ÉÙ…±}¥Í|ÈÀÁµÌ¡Í•±˜¤è4(€€€€€€€€ˆˆ‰Y•É¥™äÑ¡”Á½±°¥¹Ñ•ÉÙ…°½¹ÍÑ…¹Ğ¸ˆˆˆ4(€€€€€€€™É½´µÁ}Í•ÉÙ”¥µÁ½ÉĞA=11}%9QIY04(€€€€€€€…ÍÍ•ÉĞA=11}%9QIY0€ôô€À¸È4(