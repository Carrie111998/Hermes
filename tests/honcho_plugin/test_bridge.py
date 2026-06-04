import json
from plugins.memory.honcho import bridge


def test_tag_and_detect_source():
    tagged = bridge.tag_fact("prefers email over slack", "honcho")
    assert tagged == "[source:honcho] prefers email over slack"
    assert bridge.has_source(tagged, "honcho") is True
    assert bridge.has_source(tagged, "gbrain") is False
    assert bridge.has_source("plain fact", "honcho") is False


def test_fact_hash_is_tag_insensitive():
    h1 = bridge.fact_hash("[source:honcho] prefers email")
    h2 = bridge.fact_hash("prefers email")
    assert h1 == h2


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    bridge.save_state(p, {"a", "b"})
    assert bridge.load_state(p) == {"a", "b"}
    assert bridge.load_state(tmp_path / "missing.json") == set()


def test_fact_hash_stable_for_tagged_empty_body():
    assert bridge.fact_hash("[source:honcho] ") == bridge.fact_hash("")
    assert bridge.fact_hash("[source:gbrain-v2] x") == bridge.fact_hash("x")
