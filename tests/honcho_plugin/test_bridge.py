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


PAGE = """---
type: concept
title: Diego
---

# Diego

Existing compiled fact one.

<!-- timeline -->

- 2026-06-01 old timeline entry
"""


def test_merge_inserts_above_timeline_marker():
    out = bridge.merge_compiled_truth(PAGE, ["[source:honcho] new fact"])
    above, _, below = out.partition("<!-- timeline -->")
    assert "[source:honcho] new fact" in above
    assert "new fact" not in below  # not duplicated into timeline section
    assert "old timeline entry" in below


def test_merge_dedups_existing_fact():
    out = bridge.merge_compiled_truth(PAGE, ["Existing compiled fact one."])
    assert out.count("Existing compiled fact one.") == 1


def test_merge_without_marker_appends_marker_then_fact():
    out = bridge.merge_compiled_truth("# Diego\n\nbody\n", ["[source:honcho] x"])
    assert "<!-- timeline -->" in out
    above, _, _ = out.partition("<!-- timeline -->")
    assert "[source:honcho] x" in above
