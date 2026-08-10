"""Tests for the single-shape session_search tool.

Three calling shapes:
  1. DISCOVERY β€” pass query β†’ FTS5 + anchored window + bookends per hit
  2. SCROLL    β€” pass session_id + around_message_id β†’ just the window
  3. BROWSE    β€” no args β†’ recent sessions chronologically

All run zero LLM calls.
"""
import json
import time

import pytest

from hermes_state import SessionDB
from tools.session_search_tool import (
    SESSION_SEARCH_SCHEMA,
    _format_timestamp,
    _is_compacted_message,
    _is_compression_ended,
    _resolve_to_parent,
    _session_link,
    session_search,
)


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _seed_modpack_sessions(db):
    """Create three sessions about a modpack so FTS5 has hits to dedupe."""
    now = int(time.time())
    # Older session β€” modpack origin
    db.create_session("s_oldest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 30000, "Building the Modpack", "s_oldest"))
    db.append_message("s_oldest", role="user", content="Let's build a Minecraft modpack")
    db.append_message("s_oldest", role="assistant", content="Great. Let me scaffold the modpack repo.")
    db.append_message("s_oldest", role="user", content="Use NeoForge 1.21.1")
    db.append_message("s_oldest", role="assistant", content="Done. Modpack repo created with NeoForge 1.21.1.")
    db.append_message("s_oldest", role="assistant", content="Tier-0 mods installed; modpack smoke test passes.")

    # Middle session β€” modpack quest coverage
    db.create_session("s_middle", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 15000, "Modpack Quest Coverage", "s_middle"))
    db.append_message("s_middle", role="user", content="Deep-dive every modpack reference quest guide")
    db.append_message("s_middle", role="assistant", content="Surveying ATM10 questbook for modpack inspiration.")
    db.append_message("s_middle", role="user", content="Update the modpack version too")
    db.append_message("s_middle", role="assistant", content="Modpack version bumped 0.4 β†’ 0.8.5; quest coverage page added.")

    # Newest session β€” modpack mob spawn fix
    db.create_session("s_newest", source="cli")
    db._conn.execute("UPDATE sessions SET started_at = ?, title = ? WHERE id = ?",
                     (now - 1000, "Modpack Mob Spawn Fix", "s_newest"))
    db.append_message("s_newest", role="user", content="Fix the modpack mob spawning")
    db.append_message("s_newest", role="assistant", content="Investigating elite mob gating in the modpack KubeJS.")
    db.append_message("s_newest", role="assistant", content="Shipped commit b850442. Modpack alternator nerfed too.")
    db._conn.commit()


# =========================================================================
# Schema invariants
# =========================================================================

class TestSchema:
    def test_schema_params_cover_every_shape(self):
        params = SESSION_SEARCH_SCHEMA["parameters"]["properties"]
        # Discovery shape
        assert "query" in params
        assert "limit" in params
        assert params["sort"]["enum"] == ["newest", "oldest"]
        # Scroll shape
        assert "session_id" in params
        assert "around_message_id" in params
        assert "window" in params
        # Shared
        assert "role_filter" in params
        # Mode is inferred from which args are set β€” no explicit mode param
        assert "mode" not in params


class TestFormatTimestamp:
    def test_formats_unix_and_passes_through_the_rest(self):
        assert "2023" in _format_timestamp(1700000000)
        assert _format_timestamp(None) == "unknown"
        assert _format_timestamp("not-a-number-string") == "not-a-number-string"


# =========================================================================
# Browse shape (no args)
# =========================================================================

class TestBrowseShape:
    def test_lazy_database_is_closed_after_search(self, monkeypatch):
        class _DB:
            closed = 0

            def list_sessions_rich(self, **_kwargs):
                return []

            def close(self):
                self.closed += 1

        db = _DB()
        monkeypatch.setattr("hermes_state.SessionDB", lambda: db)

        result = json.loads(session_search())

        assert result["success"] is True
        assert db.closed == 1

    def test_cross_profile_database_is_closed_but_shared_database_is_not(
        self, monkeypatch
    ):
        class _DB:
            def __init__(self):
                self.closed = 0

            def list_sessions_rich(self, **_kwargs):
                return []

            def close(self):
                self.closed += 1

        shared_db = _DB()
        profile_db = _DB()
        monkeypatch.setattr(
            "tools.session_search_tool._resolve_profile_db",
            lambda _profile: profile_db,
        )

        result = json.loads(session_search(db=shared_db, profile="work"))

        assert result["success"] is True
        assert profile_db.closed == 1
        assert shared_db.closed == 0

    def test_no_args_returns_recent_sessions(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db))
        assert result["success"] is True
        assert result["mode"] == "browse"
        assert result["count"] >= 3

    def test_browse_excludes_current_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids


# =========================================================================
# Discovery shape (with query)
# =========================================================================

class TestDiscoveryShape:
    def test_discovery_field_plan_preserves_full_default_result(self, db, monkeypatch):
        _seed_modpack_sessions(db)
        original = db.search_messages
        requested_fields = None

        def search_spy(*args, **kwargs):
            nonlocal requested_fields
            requested_fields = kwargs.get("fields")
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "search_messages", search_spy)

        result = json.loads(session_search(query="modpack", limit=1, db=db))

        assert result["success"] is True
        assert requested_fields is not None
        assert "context" not in requested_fields
        assert len(result["results"]) == 1
        hit = result["results"][0]
        assert "bookend_start" in hit
        assert hit["messages"]
        assert "bookend_end" in hit

    def test_discovery_result_has_bookends_and_window(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, db=db))
        assert result["success"] is True
        assert result["mode"] == "discover"
        assert result["count"] >= 1
        for hit in result["results"]:
            assert "bookend_start" in hit
            assert "messages" in hit
            assert "bookend_end" in hit
            assert "match_message_id" in hit
            assert "snippet" in hit
            assert "messages_before" in hit
            assert "messages_after" in hit


    def test_current_session_filtered_out(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", db=db, current_session_id="s_newest"))
        sids = [r["session_id"] for r in result["results"]]
        assert "s_newest" not in sids


class TestDiscoverySort:
    def test_sort_newest_orders_by_recency(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="newest", db=db))
        # First result should be the most recent session
        first = result["results"][0]
        assert first["session_id"] == "s_newest" or "Newest" in (first.get("title") or "")

    def test_sort_oldest_orders_by_age(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(query="modpack", limit=3, sort="oldest", db=db))
        first = result["results"][0]
        assert first["session_id"] == "s_oldest"


# =========================================================================
# Scroll shape (session_id + around_message_id)
# =========================================================================

class TestScrollShape:
    def test_scroll_returns_anchored_window_without_bookends(self, db):
        _seed_modpack_sessions(db)
        # Get an anchor first via discovery
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]

        # Now scroll
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=2, db=db
        ))
        assert result["success"] is True
        assert result["mode"] == "scroll"
        # Scroll shape has no bookends
        assert "bookend_start" not in result
        assert "bookend_end" not in result
        # The anchor is in the window and flagged
        anchor_in_window = [m for m in result["messages"] if m["id"] == anchor_mid]
        assert len(anchor_in_window) == 1
        assert anchor_in_window[0].get("anchor") is True

    def test_scroll_window_clamped_to_20(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        result = json.loads(session_search(
            session_id=anchor_sid, around_message_id=anchor_mid, window=999, db=db
        ))
        assert result["window"] == 20


    def test_scroll_rejects_active_delegation_child_in_current_lineage(self, db):
        db.create_session("s_current", source="cli")
        db.create_session(
            "s_delegate", source="delegate", parent_session_id="s_current"
        )
        mid = db.append_message(
            "s_delegate", role="assistant", content="live delegated result"
        )

        result = json.loads(session_search(
            session_id="s_delegate", around_message_id=mid, db=db,
            current_session_id="s_current",
        ))

        assert result["success"] is False
        assert "current session" in result.get("error", "").lower()


class TestScrollPattern:
    """The forward/backward scroll loop using tool output."""

    def test_scroll_forward_from_last_id(self, db):
        # Long session
        db.create_session("s_long", source="cli")
        ids = []
        for i in range(20):
            ids.append(db.append_message("s_long", role="user" if i % 2 == 0 else "assistant",
                                         content=f"long session msg {i}"))

        v1 = json.loads(session_search(
            session_id="s_long", around_message_id=ids[5], window=3, db=db
        ))
        last_id = v1["messages"][-1]["id"]
        v2 = json.loads(session_search(
            session_id="s_long", around_message_id=last_id, window=3, db=db
        ))
        # Forward scroll: v2 should reach further than v1
        assert max(m["id"] for m in v2["messages"]) > max(m["id"] for m in v1["messages"])
        # Boundary id appears in both
        assert last_id in [m["id"] for m in v1["messages"]]
        assert last_id in [m["id"] for m in v2["messages"]]


# =========================================================================
# Shape precedence
# =========================================================================

class TestShapePrecedence:
    def test_scroll_args_beat_query(self, db):
        _seed_modpack_sessions(db)
        disc = json.loads(session_search(query="modpack", limit=1, db=db))
        anchor_sid = disc["results"][0]["session_id"]
        anchor_mid = disc["results"][0]["match_message_id"]
        # Pass both query and scroll args β€” scroll should win
        result = json.loads(session_search(
            query="modpack",  # would normally trigger discovery
            session_id=anchor_sid, around_message_id=anchor_mid, db=db,
        ))
        assert result["mode"] == "scroll"


    def test_session_id_without_anchor_reads(self, db):
        _seed_modpack_sessions(db)
        # session_id alone (no anchor, no query) β†’ read shape, not browse.
        result = json.loads(session_search(session_id="s_oldest", db=db))
        assert result["mode"] == "read"


# =========================================================================
# Read shape β€” dump a whole session by id (serves @session links)
# =========================================================================

class TestReadShape:
    def test_read_returns_full_session(self, db):
        _seed_modpack_sessions(db)
        result = json.loads(session_search(session_id="s_oldest", db=db))
        assert result["success"] is True
        assert result["mode"] == "read"
        assert result["session_id"] == "s_oldest"
        assert result["message_count"] == 5
        assert result["truncated"] is False
        assert len(result["messages"]) == 5
        assert result["session_meta"]["title"] == "Building the Modpack"

    def test_read_strips_ansi_sequences_from_messages(self, db):
        db.create_session("s_ansi", source="cli")
        db.append_message("s_ansi", role="user", content="plain")
        db.append_message(
            "s_ansi", role="assistant", content="\u001b[31mred text\u001b[0m and more"
        )
        db._conn.commit()
        result = json.loads(session_search(session_id="s_ansi", db=db))
        assert result["success"] is True
        rendered = [m["content"] for m in result["messages"] if m.get("content")]
        assert any(text == "red text and more" for text in rendered)
        assert all("\u001b" not in text for text in rendered)

    def test_read_truncates_large_session(self, db):
        db.create_session("s_big", source="cli")
        for i in range(50):
            db.append_message("s_big", role="user" if i % 2 == 0 else "assistant", content=f"m{i}")
        db._conn.commit()
        result = json.loads(session_search(session_id="s_big", db=db))
        assert result["mode"] == "read"
        assert result["message_count"] == 50
        assert result["truncated"] is True
        assert len(result["messages"]) == 30  # head ίw¶‰ΛkΊwµηQΌΡ΅”Α…Ι•ΉΠ…•ΉΠΈ4(€τττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττ4(4)±…ΝΜQ•ΝΡI•Ν½±Ω•Q½A…Ι•ΉΠθ4(€€€€‰UΉ¥ΠΡ•ΝΡΜ™½Θ}Ι•Ν½±Ω•}Ρ½}Α…Ι•ΉΠΜ½µΑΙ•ΝΝ¥½Έµ…έ…Ι”ΡΥΑ±”Ι•ΡΥΙΈΈ4(4(€€€‘•Ρ•ΝΡ}±•…ε}Ι½Ρ…Ρ¥½Ή}‘•Ρ•ΡΝ}½µΑΙ•ΝΝ¥½Έ΅Ν•±°‘¤θ4(€€€€€€€€‰A…Ι•ΉΠ•Ή‘•έ¥Ρ •Ή‘}Ι•…Ν½Έτ½µΑΙ•ΝΝ¥½Έ°΅¥±΅…ΜΑ…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°€‰½µΑΙ•ΝΝ¥½Έ¤4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}΅¥±°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}Α…Ι•ΉΠ¤4(€€€€€€€Ι½½Π°΅…Ν}½µΑΙ•ΝΝ¥½Έ€τ}Ι•Ν½±Ω•}Ρ½}Α…Ι•ΉΠ΅‘°€‰Ν}΅¥±¤4(€€€€€€€…ΝΝ•ΙΠΙ½½Π€ττ€‰Ν}Α…Ι•ΉΠ4(€€€€€€€…ΝΝ•ΙΠ΅…Ν}½µΑΙ•ΝΝ¥½Έ¥ΜQΙΥ”4(4(4(€€€‘•Ρ•ΝΡ}΅…¥Ή}έ¥Ρ΅}µ¥α•‘}•‘•Μ΅Ν•±°‘¤θ4(€€€€€€€€‰½µΑΙ•ΝΝ¥½ΈΙ…Ή‘Α…Ι•ΉΠƒHΑ…Ι•ΉΠƒH΅¥±€΅ΉΌ•Ή‘}Ι•…Ν½Έ½ΈΑ…Ι•ΉΠ¤Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}ΐ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰Ν}ΐ°€‰½µΑΙ•ΝΝ¥½Έ¤4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}ΐ°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}ΐ¤4(€€€€€€€€Ν}ΐ‘½•Μ9=P•Ήέ¥Ρ ½µΑΙ•ΝΝ¥½ΈƒP‰ΥΠ…Ή•ΝΡ½ΘΝ}ΐ‘½•Μ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}ΐ¤4(€€€€€€€Ι½½Π°΅…Ν}½µΑΙ•ΝΝ¥½Έ€τ}Ι•Ν½±Ω•}Ρ½}Α…Ι•ΉΠ΅‘°€‰Ν}¤4(€€€€€€€…ΝΝ•ΙΠΙ½½Π€ττ€‰Ν}ΐ4(€€€€€€€…ΝΝ•ΙΠ΅…Ν}½µΑΙ•ΝΝ¥½Έ¥ΜQΙΥ”4(4(4)±…ΝΜQ•ΝΡ%Ν½µΑ…Ρ•‘5•ΝΝ…”θ4(€€€€‰UΉ¥ΠΡ•ΝΡΜ™½ΘΡ΅”}¥Ν}½µΑ…Ρ•‘}µ•ΝΝ…”΅•±Α•ΘΈ4(4(€€€‘•Ρ•ΝΡ}…Ρ¥Ω•}µ•ΝΝ…•}Ι•ΡΥΙΉΝ}™…±Ν”΅Ν•±°‘¤θ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰ΜΔ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€µ¥€τ‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰ΜΔ°Ι½±”τ‰ΥΝ•Θ°½ΉΡ•ΉΠτ‰΅•±±Ό¤4(€€€€€€€…ΝΝ•ΙΠ}¥Ν}½µΑ…Ρ•‘}µ•ΝΝ…”΅‘°µ¥¤¥Μ…±Ν”4(4(€€€‘•Ρ•ΝΡ}½µΑ…Ρ•‘}µ•ΝΝ…•}Ι•ΡΥΙΉΝ}ΡΙΥ”΅Ν•±°‘¤θ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰ΜΔ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€µ¥€τ‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰ΜΔ°Ι½±”τ‰ΥΝ•Θ°½ΉΡ•ΉΠτ‰…Ι΅¥Ω•½ΉΡ•ΉΠ¤4(€€€€€€€‘Ή…Ι΅¥Ω•}…Ή‘}½µΑ…Π ‰ΜΔ°l4(€€€€€€€€€€€μ‰Ι½±”θ€‰…ΝΝ¥ΝΡ…ΉΠ°€‰½ΉΡ•ΉΠθ€‰½µΑ…Ρ•ΝΥµµ…Ιδ‰τ°4(€€€€€€€t¤4(€€€€€€€€µ¥¥ΜΉ½ά…Ρ¥Ω”τΐ°½µΑ…Ρ•τΔ4(€€€€€€€…ΝΝ•ΙΠ}¥Ν}½µΑ…Ρ•‘}µ•ΝΝ…”΅‘°µ¥¤¥ΜQΙΥ”4(4(4)±…ΝΜQ•ΝΡ%ΉA±…•½µΑ…Ρ¥½Ή¥Ν½Ω•Ιδθ4(€€€€‰%ΈµΑ±…”½µΑ…Ρ¥½Έθ…Ι΅¥Ω•ΡΥΙΉΜ½ΈΡ΅”M5Ν•ΝΝ¥½Ή}¥µΥΝΠ‰”4(€€€‘¥Ν½Ω•Ι…‰±”™Ι½΄Ρ΅”ΥΙΙ•ΉΠΝ•ΝΝ¥½ΈΈ4(4(€€€‘•Ρ•ΝΡ}…Ι΅¥Ω•‘}½ΉΡ•ΉΡ}‘¥Ν½Ω•Ι…‰±•}…™Ρ•Ι}½µΑ…Ρ¥½Έ΅Ν•±°‘¤θ4(€€€€€€€€‰Q΅”½Ι”Ι•Ι•ΝΝ¥½ΈθΑΙ”µ½µΑ…Ρ¥½Έ½ΉΡ•ΉΠ½ΈΡ΅”ΥΙΙ•ΉΠΝ•ΝΝ¥½Έ4(€€€€€€€µΥΝΠΝΥΙ™…”¥Έ‘¥Ν½Ω•Ιδ•Ω•ΈΡ΅½Υ Ι…έ}Ν¥€ττΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}½µΑ…Π°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}½µΑ…Π°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Q΅”ΝΑ•ΡΙ…°Α΅½•Ή¥ΰ½Ή±δΝΑ…έΉΜ‘ΥΙ¥Ή™Υ±°µ½½ΉΜ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}½µΑ…Π°Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰MΑ•ΡΙ…°Α΅½•Ή¥ΰΙ•ΕΥ¥Ι•Μµ½½ΉΝΡ½Ή”‰…¥Π¤4(€€€€€€€‘Ή…Ι΅¥Ω•}…Ή‘}½µΑ…Π ‰Ν}½µΑ…Π°l4(€€€€€€€€€€€μ‰Ι½±”θ€‰ΥΝ•Θ°€‰½ΉΡ•ΉΠθ€‰MΥµµ…ΙδθΝΑ•ΡΙ…°Α΅½•Ή¥ΰ‘¥ΝΥΝΝ•‰τ°4(€€€€€€€€€€€μ‰Ι½±”θ€‰…ΝΝ¥ΝΡ…ΉΠ°€‰½ΉΡ•ΉΠθ€‰­Ή½έ±•‘•ΝΑ•ΡΙ…°Α΅½•Ή¥ΰ¥Ή™Ό‰τ°4(€€€€€€€t¤4(4(€€€€€€€Ι•ΝΥ±Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰ΝΑ•ΡΙ…°Α΅½•Ή¥ΰ°‘υ‘°ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}½µΑ…Π°4(€€€€€€€€¤¤4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰ΝΥ•ΝΜ‰t¥ΜQΙΥ”4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰½ΥΉΠ‰t€ψτ€Δ4(€€€€€€€€Q΅”΅¥ΠΝ΅½Υ±‰”™Ι½΄Ρ΅”Ν…µ”Ν•ΝΝ¥½Έ€΅…Ι΅¥Ω•Ι½έΜ¤4(€€€€€€€΅¥Π€τΙ•ΝΥ±Ρl‰Ι•ΝΥ±ΡΜ‰ulΑt4(€€€€€€€…ΝΝ•ΙΠ΅¥Ρl‰Ν•ΝΝ¥½Ή}¥‰t€ττ€‰Ν}½µΑ…Π4(4(€€€‘•Ρ•ΝΡ}±¥Ω•}½ΉΡ•ΉΡ}ΝΡ¥±±}™¥±Ρ•Ι•‘}½Ή}ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Έ΅Ν•±°‘¤θ4(€€€€€€€€‰9½Έµ½µΑ…Ρ•€΅…Ρ¥Ω”¤½ΉΡ•ΉΠ½ΈΡ΅”ΥΙΙ•ΉΠΝ•ΝΝ¥½ΈΝΡ…εΜ™¥±Ρ•Ι•Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}±¥Ω”°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}±¥Ω”°Ι½±”τ‰ΥΝ•Θ°½ΉΡ•ΉΠτ‰ΙεΝΡ…°½±•΄™…Ιµ¥ΉΙ½ΥΡ”¤4(€€€€€€€Ι•ΝΥ±Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰ΙεΝΡ…°½±•΄°‘υ‘°ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}±¥Ω”°4(€€€€€€€€¤¤4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰½ΥΉΠ‰t€ττ€ΐ4(4(4)±…ΝΜQ•ΝΡ1•…εI½Ρ…Ρ¥½Ή¥Ν½Ω•Ιδθ4(€€€€‰1•…δΙ½Ρ…Ρ¥½ΈθΑ…Ι•ΉΠΝ•ΝΝ¥½Έ•Ή‘•έ¥Ρ •Ή‘}Ι•…Ν½Έτ½µΑΙ•ΝΝ¥½Έ°4(€€€΅¥±Ν•ΝΝ¥½ΈΙ•…Ρ•ΈA…Ι•ΉΠΜΑΙ”µ½µΑ…Ρ¥½Έ½ΉΡ•ΉΠµΥΝΠ‰”‘¥Ν½Ω•Ι…‰±”4(€€€™Ι½΄Ρ΅”΅¥±Έ4(4(€€€‘•Ρ•ΝΡ}½µΑΙ•ΝΝ¥½Ή}Α…Ι•ΉΡ}‘¥Ν½Ω•Ι…‰±•}™Ι½µ}΅¥±΅Ν•±°‘¤θ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}Α…Ι•ΉΠ°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Q΅”Ω½¥ΙεΝΡ…°µ¥Ή¥ΉΙ•ΕΥ¥Ι•Μ‘¥…µ½ΉΑ¥­…α”¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}Α…Ι•ΉΠ°Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Y½¥ΙεΝΡ…°™½ΥΉ¥ΈΡ΅”‘••ΐ…Ω•ΙΉΜ¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°€‰½µΑΙ•ΝΝ¥½Έ¤4(4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}΅¥±°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}Α…Ι•ΉΠ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}΅¥±°Ι½±”τ‰ΥΝ•Θ°½ΉΡ•ΉΠτ‰½ΉΡ¥ΉΥ”Ω½¥ΙεΝΡ…°έ½Ι¬¤4(4(€€€€€€€Ι•ΝΥ±Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰Ω½¥ΙεΝΡ…°°‘υ‘°ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}΅¥±°4(€€€€€€€€¤¤4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰ΝΥ•ΝΜ‰t¥ΜQΙΥ”4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰½ΥΉΠ‰t€ψτ€Δ4(€€€€€€€Ν¥‘Μ€τmΙl‰Ν•ΝΝ¥½Ή}¥‰t™½ΘΘ¥ΈΙ•ΝΥ±Ρl‰Ι•ΝΥ±ΡΜ‰ut4(€€€€€€€…ΝΝ•ΙΠ€‰Ν}Α…Ι•ΉΠ¥ΈΝ¥‘Μ4(4(4)±…ΝΜQ•ΝΡ•±•…Ρ¥½Ήα±ΥΝ¥½Έθ4(€€€€‰•±•…Ρ¥½Έ΅¥±‘Ι•Έ€΅‘•±•…Ρ•}Ρ…Ν¬¤µΥΝΠMQd•α±Υ‘•ƒPΡ΅•¥Θ½ΉΡ•ΉΠ4(€€€¥ΜΝΡ¥±°Ω¥Ν¥‰±”ΡΌΡ΅”Α…Ι•ΉΠ…•ΉΠΈΑ…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥¥ΜΝ•Π‰ΥΠΡ΅”4(€€€Α…Ι•ΉΠ‘½•Μ9=P΅…Ω”•Ή‘}Ι•…Ν½Έτ½µΑΙ•ΝΝ¥½ΈΈ4(4(€€€‘•Ρ•ΝΡ}‘•±•…Ρ¥½Ή}Α…Ι•ΉΡ}•α±Υ‘•‘}™Ι½µ}΅¥±΅Ν•±°‘¤θ4(€€€€€€€€‰΅¥±…ΈΝ•”¥ΡΜ½έΈ½ΉΡ•ΉΠ‰ΥΠΑ…Ι•ΉΠΜ±¥Ω”½ΉΡ•ΉΠΝΡ…εΜ4(€€€€€€€•α±Υ‘•€΅¥ΠΜ¥Έ½ΉΡ•αΠΩ¥„‘•±•…Ρ¥½Έ¤Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}Α…Ι•ΉΠ°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Ή•‰Υ±„‘•Α±½εµ•ΉΠ¥Ή™Ι…ΝΡΙΥΡΥΙ”Ν•ΡΥΐ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}Α…Ι•ΉΠ°Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰9•‰Υ±„‘•Α±½εµ•ΉΠ½Ή™¥ΥΙ•ΝΥ•ΝΝ™Υ±±δ¤4(4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}΅¥±°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}Α…Ι•ΉΠ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}΅¥±°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰‘•±•…Ρ•Ή•‰Υ±„‘•Α±½εµ•ΉΠΝΥ‰Ρ…Ν¬¤4(4(€€€€€€€Ι•ΝΥ±Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰Ή•‰Υ±„‘•Α±½εµ•ΉΠ°‘υ‘°ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}΅¥±°4(€€€€€€€€¤¤4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰½ΥΉΠ‰t€ττ€ΐ4(4(4(€τττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττ4(	½Ρ ±…ε•ΙΜΡ½•Ρ΅•Θθ‘¥Ν½Ω•ΙδΝ½Α”€ ΨΜΔΠΠ¤ƒ\‰½½­•Ή‰½ΥΉ‘¥Ή€ ΨδΜΜΠ¤4(4(½µΑ…Ρ¥½ΈΡ½Υ΅•ΜΡέΌ¥Ή‘•Α•Ή‘•ΉΠ±…ε•ΙΜ½Ν•ΝΝ¥½Ή}Ν•…Ι θ4(€€€ΔΈ¥Ν½Ω•ΙδΝ½Α”ƒP½µΑ…Ρ¥½Έµ…Ι΅¥Ω•Ι½έΜ½ΈΡ΅”ΥΙΙ•ΉΠΝ•ΝΝ¥½ΈµΥΝΠ4(€€€€€ΝΥΙ™…”¥Έ‘¥Ν½Ω•Ιδ€΅Ρ΅¥ΜAH¤Έ4(€€€ΘΈ½ΉΡ•ΉΠ‰½ΥΉ‘¥ΉƒP‰½½­•Ή‘ΜµΥΝΠ•α±Υ‘”•Ή•Ι…Ρ•½µΑ…Ρ¥½Έ΅…Ή‘½™4(€€€€€ΝΥµµ…Ι¥•Μ…Ή…ΐµ•ΝΝ…”½ΉΡ•ΉΠ±•ΉΡ € ΠΜΔάΤ€Ό€ΨδΜΜΠ¤Έ4(½µΑ…Ρ•Ν•ΝΝ¥½Έ•α•Ι¥Ν•Μ‰½Ρ …Π½Ή”θ¥ΡΜ…Ι΅¥Ω•½ΉΡ•ΉΠ¥ΜΡ΅”QL4(΅¥Π°έ΅¥±”Ρ΅”½µΑ…Ρ¥½ΈΝΥµµ…ΙδΙ½ά¥ΠΑΙ½‘Υ•Ν¥ΡΜ…ΠΡ΅”Ν•ΝΝ¥½ΈΡ…¥°°4(•α…Ρ±δέ΅•Ι”‰½½­•Ή‘}•Ή¥ΜΝ…µΑ±•Έ4(€τττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττ4(4)±…ΝΜQ•ΝΡ½µΑ…Ρ¥½Ή¥Ν½Ω•Ιε	½Ρ΅1…ε•ΙΜθ4(€€€€‰½µΑ…Ρ•µΝ•ΝΝ¥½Έ½ΉΡ•ΉΠ¥Μ‘¥Ν½Ω•Ι…‰±”9¥ΡΜ‰½½­•Ή‘ΜΝΡ¥±°4(€€€•α±Υ‘”½µΑ…Ρ¥½ΈΝΥµµ…Ι¥•Μ€Ό…ΐ½ΉΡ•ΉΠ±•ΉΡ Έ4(4(€€€‘•}Ν••‘}½µΑ…Ρ•‘}Ν•ΝΝ¥½Έ΅Ν•±°‘¤θ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}‰½Ρ °Ν½ΥΙ”τ‰±¤¤4(€€€€€€€€1½ΉΉ½Ιµ…°½Α•Ή¥ΉƒP•α•Ι¥Ν•ΜΡ΅”€ΔΘΐΐµ΅…Θ‰½½­•Ή…ΐΈ4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰-¥¬½™Ρ΅”½‰Ν¥‘¥…Έ…Ρ•έ…δµ¥Ι…Ρ¥½ΈΈ€€¬€‰Ό€¨€Τΐΐΐ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰MΡ…ΙΡ¥ΉΡ΅”½‰Ν¥‘¥…Έ…Ρ•έ…δµ¥Ι…Ρ¥½ΈΑ±…ΈΈ¤4(€€€€€€€€A…‘‘¥ΉΝΌΡ΅”…Ή΅½Ι•έ¥Ή‘½ά‘½•ΝΈΠΝέ…±±½άΡ΅”‰½½­•Ή‘ΜΈ4(€€€€€€€™½Θ¤¥ΈΙ…Ή” Δΐ¤θ4(€€€€€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰ΥΝ•Θ°½ΉΡ•ΉΠυ‰µ¥Ι…Ρ¥½ΈΝΡ•ΐν¥τ¤4(€€€€€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°½ΉΡ•ΉΠυ‰µ¥Ι…Ρ¥½ΈΝΡ•ΐν¥τ‘½Ή”¤4(€€€€€€€€Q΅”QLµ…Ρ Ρ…Ι•ΠƒPέ¥±°‰”…Ι΅¥Ω•‰δ½µΑ…Ρ¥½Έ‰•±½άΈ4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Ρ΅”½‰Ν¥‘¥…Έ…Ρ•έ…δΉ••‘Μ„ΕΥ…ΙΡθ­•εΝΡ½Ή”ΡΌ…Ρ¥Ω…Ρ”¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰9½Ρ•θΕΥ…ΙΡθ­•εΝΡ½Ή”Ι•ΕΥ¥Ι•™½ΘΡ΅”½‰Ν¥‘¥…Έ…Ρ•έ…δΈ¤4(€€€€€€€™½Θ¤¥ΈΙ…Ή” Τ¤θ4(€€€€€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰ΥΝ•Θ°½ΉΡ•ΉΠυ‰έΙ…ΐµΥΐν¥τ¤4(€€€€€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‰½Ρ °Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°½ΉΡ•ΉΠυ‰έΙ…ΑΑ•ν¥τ¤4(€€€€€€€€½µΑ…Π¥ΈΑ±…”θ•Ω•ΙεΡ΅¥Ή…‰½Ω”‰•½µ•Μ…Ρ¥Ω”τΐ½½µΑ…Ρ•τΔ…Ή4(€€€€€€€€Ρ΅”΅…Ή‘½™ΝΥµµ…Ιδ¥Μ¥ΉΝ•ΙΡ•…ΜΡ΅”Ή•ά±¥Ω”Ρ…¥°Έ4(€€€€€€€‘Ή…Ι΅¥Ω•}…Ή‘}½µΑ…Π ‰Ν}‰½Ρ °l4(€€€€€€€€€€€μ‰Ι½±”θ€‰ΥΝ•Θ°4(€€€€€€€€€€€€€‰½ΉΡ•ΉΠθ€‰m=9QaP=5AQ%=8ƒPII9=91et€4(€€€€€€€€€€€€€€€€€€€€€€€€‰…Ι±¥•ΘΡΥΙΉΜέ•Ι”½µΑ…Ρ•¥ΉΡΌΡ΅¥ΜΝΥµµ…ΙδΈ€€¬€‰Μ€¨€ΤΐΐΐΑτ°4(€€€€€€€€€€€μ‰Ι½±”θ€‰…ΝΝ¥ΝΡ…ΉΠ°€‰½ΉΡ•ΉΠθ€‰½ΉΡ¥ΉΥ¥Ή…™Ρ•Θ½µΑ…Ρ¥½ΈΈ‰τ°4(€€€€€€€t¤4(€€€€€€€‘Ή}½ΉΈΉ½µµ¥Π ¤4(4(€€€‘•Ρ•ΝΡ}…Ι΅¥Ω•‘}΅¥Ρ}ΝΥΙ™…•Ν}έ¥Ρ΅}‰½ΥΉ‘•‘}ΝΥµµ…Ιε}™Ι••}‰½½­•Ή‘Μ΅Ν•±°‘¤θ4(€€€€€€€Ν•±Ή}Ν••‘}½µΑ…Ρ•‘}Ν•ΝΝ¥½Έ΅‘¤4(4(€€€€€€€Ι•ΝΥ±Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰ΕΥ…ΙΡθ­•εΝΡ½Ή”°‘υ‘°ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}‰½Ρ °4(€€€€€€€€¤¤4(4(€€€€€€€€1…ε•Θ€ΔƒP‘¥Ν½Ω•ΙδΝ½Α”θΡ΅”…Ι΅¥Ω•€΅…Ρ¥Ω”τΐ°½µΑ…Ρ•τΔ¤4(€€€€€€€€½ΉΡ•ΉΠ½ΈΡ΅”UII9PΝ•ΝΝ¥½ΈµΥΝΠΝΥΙ™…”Έ4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰ΝΥ•ΝΜ‰t¥ΜQΙΥ”4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρl‰½ΥΉΠ‰t€ψτ€Δ4(€€€€€€€•ΉΡΙδ€τΙ•ΝΥ±Ρl‰Ι•ΝΥ±ΡΜ‰ulΑt4(€€€€€€€…ΝΝ•ΙΠ•ΉΡΙεl‰Ν•ΝΝ¥½Ή}¥‰t€ττ€‰Ν}‰½Ρ 4(4(€€€€€€€€1…ε•Θ€Ι„ƒPΝΥµµ…Ιδ•α±ΥΝ¥½ΈθΡ΅”½µΑ…Ρ¥½Έ΅…Ή‘½™Ι½άΝ¥ΡΜ…ΠΡ΅”4(€€€€€€€€Ν•ΝΝ¥½ΈΡ…¥°€΅™Ι•Ν΅±δ¥ΉΝ•ΙΡ•‰δ…Ι΅¥Ω•}…Ή‘}½µΑ…Π¤°•α…Ρ±δέ΅•Ι”4(€€€€€€€€‰½½­•Ή‘}•ΉΝ…µΑ±•ΜƒP¥ΠµΥΝΠ‰”™¥±Ρ•Ι•½ΥΠΈ4(€€€€€€€™½ΘµΝ¥Έ•ΉΡΙδΉ•Π ‰‰½½­•Ή‘}ΝΡ…ΙΠ°mt¤€¬•ΉΡΙδΉ•Π ‰‰½½­•Ή‘}•Ή°mt¤θ4(€€€€€€€€€€€…ΝΝ•ΙΠ€‰m=9QaP=5AQ%=8Ή½Π¥Έ€΅µΝΉ•Π ‰½ΉΡ•ΉΠ¤½Θ€¤4(4(€€€€€€€€1…ε•Θ€ΙƒP½ΉΡ•ΉΠ…ΑΜθ‰½½­•Ή‘Μƒ&ΔΘΐΐ΅…ΙΜ°έ¥Ή‘½άƒ&Πΐΐΐ΅…ΙΜΈ4(€€€€€€€™½ΘµΝ¥Έ•ΉΡΙδΉ•Π ‰‰½½­•Ή‘}ΝΡ…ΙΠ°mt¤€¬•ΉΡΙδΉ•Π ‰‰½½­•Ή‘}•Ή°mt¤θ4(€€€€€€€€€€€…ΝΝ•ΙΠ±•Έ΅µΝΉ•Π ‰½ΉΡ•ΉΠ¤½Θ€¤€πτ€ΔΘΔΐ4(€€€€€€€™½ΘµΝ¥Έ•ΉΡΙδΉ•Π ‰µ•ΝΝ…•Μ°mt¤θ4(€€€€€€€€€€€…ΝΝ•ΙΠ±•Έ΅µΝΉ•Π ‰½ΉΡ•ΉΠ¤½Θ€¤€πτ€ΠΐΔΐ4(4(€€€€€€€€Q΅”±½Ήµ‰ΥΠµ±•¥Ρ¥µ…Ρ”½Α•Ή¥ΉΝΥΙΩ¥Ω•Μ€΅…ΑΑ•°Ή½Π‘Ι½ΑΑ•¤Έ4(€€€€€€€‰½½­•Ή‘}½ΉΡ•ΉΡΜ€τm΄Ή•Π ‰½ΉΡ•ΉΠ¤½Θ€™½Θ΄¥Έ•ΉΡΙδΉ•Π ‰‰½½­•Ή‘}ΝΡ…ΙΠ°mt¥t4(€€€€€€€…ΝΝ•ΙΠ…Ήδ ‰½‰Ν¥‘¥…Έ…Ρ•έ…δµ¥Ι…Ρ¥½Έ¥Έ™½Θ¥Έ‰½½­•Ή‘}½ΉΡ•ΉΡΜ¤4(4(4(€τττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττ4(Q•­Ή¥Υ΄Ι•Ω¥•άΙ½ΥΉ€ΘθΙ•έ¥Ή•α±ΥΝ¥½Έ€¬‘•±•…Ρ¥½ΈµΥΉ‘•Θµ½µΑΙ•ΝΝ¥½Έ4(€τττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττττ4(4)±…ΝΜQ•ΝΡI•έ¥Ή‘α±ΥΝ¥½Έθ4(€€€€‰I•έ¥Ή½ΥΉ‘ΌΙ½έΜ€΅…Ρ¥Ω”τΐ°½µΑ…Ρ•τΐ¤µΥΝΠMQd΅¥‘‘•ΈƒP½Ή±δ4(€€€½µΑ…Ρ¥½Έ…Ι΅¥Ω•Μ€΅…Ρ¥Ω”τΐ°½µΑ…Ρ•τΔ¤Ν΅½Υ±ΝΥΙ™…”Έ4(4(€€€‘•Ρ•ΝΡ}½µΑ…Ρ•‘}µ•ΝΝ…•Ν}ΝΡ¥±±}ΝΥΙ™…•}…±½ΉΝ¥‘•}Ι•έ¥Ή΅Ν•±°‘¤θ4(€€€€€€€€‰=ΈΡ΅”Ν…µ”Ν•ΝΝ¥½Έθ½µΑ…Ρ•Ι½έΜΝΥΙ™…”°Ι•έ¥ΉΙ½έΜ‘½ΈΠΈ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}µ¥α•°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€€5•ΝΝ…”Ρ΅…Πέ¥±°‰”½µΑ…Ρ•4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}µ¥α•°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰½µΑ…Ρ¥½Έ…Ι΅¥Ω•½ΉΡ•ΉΠ‰•Ρ„¤4(€€€€€€€‘Ή…Ι΅¥Ω•}…Ή‘}½µΑ…Π ‰Ν}µ¥α•°l4(€€€€€€€€€€€μ‰Ι½±”θ€‰…ΝΝ¥ΝΡ…ΉΠ°€‰½ΉΡ•ΉΠθ€‰MΥµµ…Ιδ½‰•Ρ„‰τ°4(€€€€€€€t¤4(€€€€€€€€9½ά…‘„Α½ΝΠµ½µΑ…Ρ¥½Έµ•ΝΝ…”…ΉΙ•έ¥Ή¥Π4(€€€€€€€µ¥Θ€τ‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}µ¥α•°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Ι•έ½ΥΉ½ΉΡ•ΉΠ…µµ„¤4(€€€€€€€‘Ή}½ΉΈΉ•α•ΥΡ” 4(€€€€€€€€€€€€‰UAQµ•ΝΝ…•ΜMP…Ρ¥Ω”€τ€ΐ°½µΑ…Ρ•€τ€ΐ]!I¥€τ€ό°4(€€€€€€€€€€€€΅µ¥Θ°¤°4(€€€€€€€€¤4(€€€€€€€‘Ή}½ΉΈΉ½µµ¥Π ¤4(4(€€€€€€€€½µΑ…Ρ•½ΉΡ•ΉΠΝ΅½Υ±‰”‘¥Ν½Ω•Ι…‰±”4(€€€€€€€Ι•ΝΥ±Ρ}½µΑ…Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰½µΑ…Ρ¥½Έ…Ι΅¥Ω•½ΉΡ•ΉΠ‰•Ρ„°‘υ‘°4(€€€€€€€€€€€ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}µ¥α•°4(€€€€€€€€¤¤4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρ}½µΑ…Ρl‰½ΥΉΠ‰t€ψτ€Δ4(4(€€€€€€€€I•έ½ΥΉ½ΉΡ•ΉΠΝ΅½Υ±9=P‰”‘¥Ν½Ω•Ι…‰±”4(€€€€€€€Ι•ΝΥ±Ρ}Ι•έ¥Ή€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰Ι•έ½ΥΉ½ΉΡ•ΉΠ…µµ„°‘υ‘°4(€€€€€€€€€€€ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}µ¥α•°4(€€€€€€€€¤¤4(€€€€€€€…ΝΝ•ΙΠΙ•ΝΥ±Ρ}Ι•έ¥Ή‘l‰½ΥΉΠ‰t€ττ€ΐ4(4(4)±…ΝΜQ•ΝΡ½µΑΙ•ΝΝ¥½ΉΉ‘•‘!•±Α•Θθ4(€€€€‰UΉ¥ΠΡ•ΝΡΜ™½Θ}¥Ν}½µΑΙ•ΝΝ¥½Ή}•Ή‘•Έ4(4(€€€‘•Ρ•ΝΡ}½µΑΙ•ΝΝ¥½Ή}•Ή‘•‘}Ν•ΝΝ¥½Έ΅Ν•±°‘¤θ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰ΜΔ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰ΜΔ°€‰½µΑΙ•ΝΝ¥½Έ¤4(€€€€€€€…ΝΝ•ΙΠ}¥Ν}½µΑΙ•ΝΝ¥½Ή}•Ή‘•΅‘°€‰ΜΔ¤¥ΜQΙΥ”4(4(€€€‘•Ρ•ΝΡ}‘•±•…Ρ¥½Ή}΅¥±‘}Ή½Ρ}•Ή‘•΅Ν•±°‘¤θ4(€€€€€€€€‰‘•±•…Ρ¥½Έ΅¥±ΥΉ‘•Θ„½µΑΙ•ΝΝ¥½Έ½ΉΡ¥ΉΥ…Ρ¥½Έ‘½•Μ9=P΅…Ω”4(€€€€€€€•Ή‘}Ι•…Ν½Έτ½µΑΙ•ΝΝ¥½Έ¥ΡΝ•±Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰Ν}Α…Ι•ΉΠ°€‰½µΑΙ•ΝΝ¥½Έ¤4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}½ΉΡ¥ΉΥ…Ρ¥½Έ°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}Α…Ι•ΉΠ¤4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}‘•±•…Ρ•}΅¥±°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}½ΉΡ¥ΉΥ…Ρ¥½Έ¤4(€€€€€€€…ΝΝ•ΙΠ}¥Ν}½µΑΙ•ΝΝ¥½Ή}•Ή‘•΅‘°€‰Ν}‘•±•…Ρ•}΅¥±¤¥Μ…±Ν”4(4(4)±…ΝΜQ•ΝΡ1•…ε½ΉΡ¥ΉΥ…Ρ¥½ΉA±ΥΝ•±•…Ρ¥½Έθ4(€€€€‰I•Ι•ΝΝ¥½Έθ„‘•±•…Ρ¥½Έ΅¥±Ι•…Ρ•ΥΉ‘•Θ„½µΑΙ•ΝΝ¥½Έ½ΉΡ¥ΉΥ…Ρ¥½Έ4(€€€µΥΝΠΝΡ…δ•α±Υ‘•ƒP¥ΡΜ½ΉΡ•ΉΠ¥ΜΝΡ¥±°±¥Ω”ΡΌΡ΅”Α…Ι•ΉΠ…•ΉΠΈ4(€€€=Ή±δΡ΅”½µΑΙ•ΝΝ¥½Έµ•Ή‘•…Ή•ΝΡ½ΘΜ½ΉΡ•ΉΠΝ΅½Υ±ΝΥΙ™…”Έ4(4(€€€‘•Ρ•ΝΡ}½µΑΙ•ΝΝ¥½Ή}Α…Ι•ΉΡ}ΝΥΙ™…•Ν}‰ΥΡ}‘•±•…Ρ•}΅¥±‘}•α±Υ‘•΅Ν•±°‘¤θ4(€€€€€€€€‰M•ΡΥΐθΙ…Ή‘Α…Ι•ΉΠ€΅½µΑΙ•ΝΝ¥½Έ¤ƒHΑ…Ι•ΉΠ€΅½µΑΙ•ΝΝ¥½Έ¤ƒH΅¥±4(€€€€€€€€΅…Ρ¥Ω”°ΥΙΙ•ΉΠΝ•ΝΝ¥½Έ¤Έ‘•±•…Ρ¥½ΈΙ…Ή‘΅¥±¥ΜΙ•…Ρ•ΥΉ‘•Θ4(€€€€€€€Ρ΅”Α…Ι•ΉΠΈM•…Ι΅¥Ή™Ι½΄Ρ΅”΅¥±Ν΅½Υ±™¥ΉΙ…Ή‘Α…Ι•ΉΠ½Α…Ι•ΉΠ4(€€€€€€€½ΉΡ•ΉΠ‰ΥΠ9=PΡ΅”‘•±•…Ρ¥½ΈΙ…Ή‘΅¥±Μ½ΉΡ•ΉΠΈ4(€€€€€€€€Ι…Ή‘Α…Ι•ΉΠθ½µΑΙ•ΝΝ¥½Έµ•Ή‘•°΅…ΜΝ•…Ι΅…‰±”½ΉΡ•ΉΠ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}ΐ°Ν½ΥΙ”τ‰±¤¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}ΐ°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Ι…Ή‘Α…Ι•ΉΠ½Νµ¥…Ή½µ…±δΙ•Ν•…Ι ‘…Ρ„¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰Ν}ΐ°€‰½µΑΙ•ΝΝ¥½Έ¤4(4(€€€€€€€€A…Ι•ΉΠθ½µΑΙ•ΝΝ¥½Έµ•Ή‘•½ΉΡ¥ΉΥ…Ρ¥½Έ4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}ΐ°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}ΐ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}ΐ°Ι½±”τ‰ΥΝ•Θ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰Α…Ι•ΉΠ½Νµ¥…Ή½µ…±δ™½±±½άµΥΐΉ½Ρ•Μ¤4(€€€€€€€‘Ή•Ή‘}Ν•ΝΝ¥½Έ ‰Ν}ΐ°€‰½µΑΙ•ΝΝ¥½Έ¤4(4(€€€€€€€€ΥΙΙ•ΉΠΝ•ΝΝ¥½Έθ…Ρ¥Ω”΅¥±4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}ΥΙΙ•ΉΠ°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}ΐ¤4(4(€€€€€€€€•±•…Ρ¥½Έ΅¥±ΥΉ‘•ΘΝ}ΐ€΅Ή½Π½µΑΙ•ΝΝ¥½Έµ•Ή‘•¤4(€€€€€€€‘ΉΙ•…Ρ•}Ν•ΝΝ¥½Έ ‰Ν}‘•±•…Ρ”°Ν½ΥΙ”τ‰±¤°Α…Ι•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}ΐ¤4(€€€€€€€‘Ή…ΑΑ•Ή‘}µ•ΝΝ…” ‰Ν}‘•±•…Ρ”°Ι½±”τ‰…ΝΝ¥ΝΡ…ΉΠ°4(€€€€€€€€€€€€€€€€€€€€€€€€€½ΉΡ•ΉΠτ‰‘•±•…Ρ•½Νµ¥…Ή½µ…±δΝΥ‰Ρ…Ν¬Ι•ΝΥ±ΡΜ¤4(4(€€€€€€€Ι•ΝΥ±Π€τ©Ν½ΈΉ±½…‘Μ΅Ν•ΝΝ¥½Ή}Ν•…Ι  4(€€€€€€€€€€€ΕΥ•Ιδτ‰½Νµ¥…Ή½µ…±δ°‘υ‘°4(€€€€€€€€€€€ΥΙΙ•ΉΡ}Ν•ΝΝ¥½Ή}¥τ‰Ν}ΥΙΙ•ΉΠ°4(€€€€€€€€¤¤4(4(€€€€€€€€½µΑΙ•ΝΝ¥½Έµ•Ή‘•…Ή•ΝΡ½ΙΜΝ΅½Υ±‰”‘¥Ν½Ω•Ι…‰±”4(€€€€€€€Ν¥‘Μ€τmΙl‰Ν•ΝΝ¥½Ή}¥‰t™½ΘΘ¥ΈΙ•ΝΥ±Ρl‰Ι•ΝΥ±ΡΜ‰ut4(€€€€€€€…ΝΝ•ΙΠ€‰Ν}ΐ¥ΈΝ¥‘Μ½Θ€‰Ν}ΐ¥ΈΝ¥‘Μ4(4(€€€€€€€€•±•…Ρ¥½Έ΅¥±µΥΝΠ9=P…ΑΑ•…Θ4(€€€€€€€…ΝΝ•ΙΠ€‰Ν}‘•±•…Ρ”Ή½Π¥ΈΝ¥‘Μ4(