"""Tests for the sillytavern ST-native features (stdlib + sqlite)."""

import importlib.util
import os
import tempfile


def _load_native(tmp_home):
    os.environ["HERMES_HOME"] = str(tmp_home)
    spec = importlib.util.spec_from_file_location(
        "st_native",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "plugins", "sillytavern", "st_native.py",
        ),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_character_and_session_roundtrip(tmp_path):
    n = _load_native(tmp_path)
    cid = n.create_character("Hakua", first_mes="Hi!", personality="cheerful")
    assert cid > 0
    sid = n.create_session(cid, title="s1")
    msgs = n.get_messages(sid)
    # first_mes should be seeded
    assert any(m["content"] == "Hi!" for m in msgs)
    n.add_message(sid, "user", "hello")
    assert len(n.get_messages(sid)) == 2


def test_lore_keyword_match(tmp_path):
    n = _load_native(tmp_path)
    n.add_lore("world", ["dragon", "wyrm"], "Dragons rule the north.")
    n.add_lore("world", ["ocean"], "The sea is vast.")
    hits = n.match_lore("world", "I saw a DRAGON today")
    assert len(hits) == 1
    assert "Dragons" in hits[0]["content"]
    assert n.match_lore("world", "nothing relevant") == []


def test_build_prompt_assembles_blocks(tmp_path):
    n = _load_native(tmp_path)
    cid = n.create_character("Hakua", description="an AI", personality="loyal",
                             scenario="a lab", first_mes="Hi")
    n.create_persona("Bob", description="a dev", is_default=True)
    n.add_lore("w", ["lab"], "The lab is secret.")
    sid = n.create_session(cid, title="s")
    p = n.build_prompt(sid, "tell me about the lab", lore_book="w")
    assert "Hakua" in p["system"]
    assert "an AI" in p["system"]
    assert "Bob" in p["system"]
    assert p["lore_hits"] == 1
    assert "The lab is secret" in p["system"]
    assert p["messages"][-1]["content"] == "tell me about the lab"


def test_summary_and_memory_bridge(tmp_path):
    n = _load_native(tmp_path)
    cid = n.create_character("Hakua", first_mes="Hi")
    sid = n.create_session(cid, title="s")
    n.add_message(sid, "user", "hello there")
    n.set_summary(sid, "greeted each other")
    recs = n.session_to_memory_records(sid)
    # one summary record + one chat record
    assert len(recs) == 2
    assert any("summary" in r["tags"] for r in recs)
    assert any("chat" in r["tags"] for r in recs)
    for r in recs:
        assert 0 < r["salience"] <= 1


def test_import_memory_to_lore(tmp_path):
    n = _load_native(tmp_path)
    added = n.import_memory_to_lore(
        "mem",
        [{"content": "Bob lives in Ueda", "tags": "bob,location"},
         {"content": "", "tags": "skip"}],  # empty content skipped
    )
    assert added == 1
    hits = n.match_lore("mem", "where does bob live")
    assert len(hits) == 1
    assert "Ueda" in hits[0]["content"]


def test_default_persona_switch(tmp_path):
    n = _load_native(tmp_path)
    n.create_persona("A", is_default=True)
    n.create_persona("B", is_default=True)
    # only the latest default remains
    assert n.get_default_persona()["name"] == "B"
