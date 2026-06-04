import json
import subprocess
from unittest import mock

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


def test_merge_does_not_substring_dedup():
    # "fact" is a substring of "Existing compiled fact one." but is a distinct
    # fact and must NOT be dropped.
    out = bridge.merge_compiled_truth(PAGE, ["fact"])
    assert "- fact" in out


def test_merge_idempotent_across_reruns():
    once = bridge.merge_compiled_truth(PAGE, ["[source:honcho] new fact"])
    twice = bridge.merge_compiled_truth(once, ["[source:honcho] new fact"])
    assert once == twice  # re-adding the same bulleted fact is a no-op


def test_gbrain_get_returns_stdout():
    gb = bridge.GBrainAdapter()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="PAGE", stderr="")
    with mock.patch("subprocess.run", return_value=completed) as run:
        assert gb.get_page("hindsight/diego") == "PAGE"
        run.assert_called_once()
        assert run.call_args.args[0] == ["gbrain", "get", "hindsight/diego"]


def test_gbrain_get_missing_cli_returns_none():
    gb = bridge.GBrainAdapter()
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert gb.get_page("hindsight/diego") is None


def test_gbrain_timeline_add_invokes_cli():
    gb = bridge.GBrainAdapter()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=completed) as run:
        assert gb.add_timeline("hindsight/diego", "2026-06-04", "[source:honcho] x") is True
        assert run.call_args.args[0] == [
            "gbrain", "timeline-add", "hindsight/diego", "2026-06-04", "[source:honcho] x",
        ]


def test_gbrain_put_passes_markdown_via_content_argv():
    # Content goes via --content (argv), NOT piped stdin: `gbrain put` reads
    # stdin by opening '/dev/stdin', which does not exist on Windows (ENOENT).
    gb = bridge.GBrainAdapter()
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with mock.patch("subprocess.run", return_value=completed) as run:
        assert gb.put_page("hindsight/diego", "# Diego\n") is True
        assert "input" not in run.call_args.kwargs  # never piped on stdin
        assert run.call_args.args[0] == [
            "gbrain", "put", "hindsight/diego", "--content", "# Diego\n",
        ]


def _fake_manager():
    m = mock.Mock()
    m.get_peer_card.return_value = ["fact a", "[source:gbrain] fact b"]
    m.dialectic_query.return_value = "a synthesized conclusion"
    m.create_conclusion.return_value = True
    return m


def test_honcho_adapter_read_user_facts_calls_get_or_create_first():
    m = _fake_manager()
    ha = bridge.HonchoAdapter(manager=m, session_key="hermes-autonomous")
    facts = ha.read_user_facts()
    m.get_or_create.assert_called_once_with("hermes-autonomous")
    m.get_peer_card.assert_called_once_with("hermes-autonomous", peer="user")
    assert facts == ["fact a", "[source:gbrain] fact b"]


def test_honcho_adapter_write_conclusion_targets_named_peer():
    m = _fake_manager()
    ha = bridge.HonchoAdapter(manager=m, session_key="hermes-autonomous")
    assert ha.write_conclusion("[source:gbrain] x", peer="user") is True
    m.create_conclusion.assert_called_once_with(
        "hermes-autonomous", "[source:gbrain] x", peer="user",
    )
    m.get_or_create.assert_called_once_with("hermes-autonomous")


def test_honcho_adapter_run_dialectic():
    m = _fake_manager()
    ha = bridge.HonchoAdapter(manager=m, session_key="hermes-autonomous")
    assert ha.run_dialectic("what changed?") == "a synthesized conclusion"
    m.dialectic_query.assert_called_once_with("hermes-autonomous", "what changed?", peer="user")
    m.get_or_create.assert_called_once_with("hermes-autonomous")


def test_export_skips_gbrain_sourced_and_writes_new(tmp_path, monkeypatch):
    ha = mock.Mock()
    ha.read_user_facts.return_value = ["new pref", "[source:gbrain] echo"]
    ha.run_dialectic.return_value = ""  # no low-conf conclusions this run
    gb = mock.Mock()
    gb.get_page.return_value = PAGE
    gb.put_page.return_value = True
    gb.add_timeline.return_value = True
    drawers = []
    monkeypatch.setattr(bridge, "_write_mempalace_drawer",
                        lambda room, content: drawers.append((room, content)) or True)

    res = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=tmp_path / "exp.json", dry_run=False,
    )

    assert res["exported"] == 1
    assert res["loop_skipped"] == 1
    tl_texts = [c.args[2] for c in gb.add_timeline.call_args_list]
    assert any(bridge.has_source(t, "honcho") and "new pref" in t for t in tl_texts)
    gb.put_page.assert_called_once()  # compiled-truth merge written
    res2 = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=tmp_path / "exp.json", dry_run=False,
    )
    assert res2["exported"] == 0


def test_export_dry_run_writes_nothing(tmp_path):
    ha = mock.Mock()
    ha.read_user_facts.return_value = ["new pref"]
    ha.run_dialectic.return_value = ""
    gb = mock.Mock()
    gb.get_page.return_value = PAGE
    res = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=tmp_path / "exp.json", dry_run=True,
    )
    assert res["exported"] == 1
    gb.add_timeline.assert_not_called()
    gb.put_page.assert_not_called()
    assert bridge.load_state(tmp_path / "exp.json") == set()


def test_export_low_conf_writes_mempalace_drawer_not_compiled(tmp_path, monkeypatch):
    ha = mock.Mock()
    ha.read_user_facts.return_value = []
    ha.run_dialectic.return_value = "synthesized insight"
    gb = mock.Mock()
    gb.get_page.return_value = PAGE
    gb.add_timeline.return_value = True
    drawers = []
    monkeypatch.setattr(bridge, "_write_mempalace_drawer",
                        lambda room, content: drawers.append((room, content)) or True)

    res = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=["what changed?"],
        state_path=tmp_path / "exp.json", dry_run=False,
    )

    assert res["exported"] == 1
    assert len(drawers) == 1
    assert drawers[0][0] == "conclusion"
    assert bridge.has_source(drawers[0][1], "honcho")
    gb.put_page.assert_not_called()  # low-conf must NOT hit compiled-truth


def test_export_failed_timeline_write_not_recorded(tmp_path):
    # add_timeline fails -> fact is NOT marked seen, retried next run.
    ha = mock.Mock()
    ha.read_user_facts.return_value = ["transient fail fact"]
    ha.run_dialectic.return_value = ""
    gb = mock.Mock()
    gb.get_page.return_value = PAGE
    gb.add_timeline.return_value = False  # simulate CLI failure
    res = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=tmp_path / "exp.json", dry_run=False,
    )
    assert res["exported"] == 0
    assert res["write_failed"] == 1
    assert bridge.load_state(tmp_path / "exp.json") == set()  # nothing recorded


def test_export_failed_put_page_retries_compiled_without_duplicate_timeline(tmp_path, monkeypatch):
    # The sticky-failure bug: a high-conf fact's timeline-add succeeds but the
    # single compiled-truth put_page fails. The compiled hash must NOT be
    # persisted (so the merge retries next run), while the timeline entry must
    # NOT be re-added (it already succeeded; gbrain appends -> would duplicate).
    ha = mock.Mock()
    ha.read_user_facts.return_value = ["sticky compiled fact"]
    ha.run_dialectic.return_value = ""
    gb = mock.Mock()
    gb.get_page.return_value = PAGE
    gb.add_timeline.return_value = True
    gb.put_page.return_value = False  # compiled-truth merge FAILS this run
    monkeypatch.setattr(bridge, "_write_mempalace_drawer", lambda room, content: True)

    sp = tmp_path / "exp.json"
    res1 = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=sp, dry_run=False,
    )
    assert res1["exported"] == 1
    assert gb.add_timeline.call_count == 1
    gb.put_page.assert_called_once()

    # Second run: put_page now succeeds. Timeline must NOT be re-added; the
    # compiled merge MUST be retried.
    gb.put_page.return_value = True
    bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=sp, dry_run=False,
    )
    assert gb.add_timeline.call_count == 1  # no duplicate timeline entry
    assert gb.put_page.call_count == 2      # compiled merge retried

    # Third run: fully settled -> nothing retried.
    bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=sp, dry_run=False,
    )
    assert gb.add_timeline.call_count == 1
    assert gb.put_page.call_count == 2  # no further compiled put


def test_export_unreadable_page_retries_compiled_without_duplicate_timeline(tmp_path, monkeypatch):
    # get_page returning None (gbrain unreadable) also leaves the compiled merge
    # undone -> the compiled hash must not be persisted, but the timeline entry
    # already landed and must not repeat.
    ha = mock.Mock()
    ha.read_user_facts.return_value = ["needs compiled merge"]
    ha.run_dialectic.return_value = ""
    gb = mock.Mock()
    gb.get_page.return_value = None  # page unreadable this run
    gb.add_timeline.return_value = True
    monkeypatch.setattr(bridge, "_write_mempalace_drawer", lambda room, content: True)

    sp = tmp_path / "exp.json"
    bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=sp, dry_run=False,
    )
    assert gb.add_timeline.call_count == 1

    gb.get_page.return_value = PAGE
    gb.put_page.return_value = True
    bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=[], state_path=sp, dry_run=False,
    )
    assert gb.add_timeline.call_count == 1  # no duplicate timeline entry
    gb.put_page.assert_called_once()        # compiled merge happened on retry


def test_export_timeline_persisted_even_when_compiled_fails(tmp_path, monkeypatch):
    # A low-conf fact (timeline only) processed in the same run as a failing
    # compiled merge must still be recorded -> not re-exported next run.
    ha = mock.Mock()
    ha.read_user_facts.return_value = ["high conf fact"]
    ha.run_dialectic.return_value = "low conf insight"
    gb = mock.Mock()
    gb.get_page.return_value = PAGE
    gb.add_timeline.return_value = True
    gb.put_page.return_value = False  # compiled merge fails
    monkeypatch.setattr(bridge, "_write_mempalace_drawer", lambda room, content: True)

    sp = tmp_path / "exp.json"
    res1 = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=["q"], state_path=sp, dry_run=False,
    )
    assert res1["exported"] == 2  # one high-conf + one low-conf

    # Next run: low-conf timeline fact is settled (deduped); only the high-conf
    # compiled retry remains.
    gb.put_page.return_value = True
    res2 = bridge.run_export(
        ha, gb, slug="hindsight/diego", date="2026-06-04",
        dialectic_queries=["q"], state_path=sp, dry_run=False,
    )
    # add_timeline ran twice total (once per fact in run1), never re-run for the
    # already-settled low-conf fact.
    assert gb.add_timeline.call_count == 2
    assert res2["deduped"] >= 1


SEED_PAGE = """# Diego

- a genuine gbrain fact
- [source:honcho] exported echo
plain compiled sentence.

<!-- timeline -->

- 2026-06-01 should-not-be-seeded timeline line
"""


def test_parse_compiled_facts_excludes_timeline_and_honcho():
    facts = bridge.parse_compiled_facts(SEED_PAGE)
    assert "a genuine gbrain fact" in facts
    assert "plain compiled sentence." in facts
    assert all(not bridge.has_source(f, "honcho") for f in facts)
    assert all("should-not-be-seeded" not in f for f in facts)


def test_seed_writes_new_user_conclusions(tmp_path):
    ha = mock.Mock()
    ha.write_conclusion.return_value = True
    gb = mock.Mock()
    gb.get_page.return_value = SEED_PAGE
    res = bridge.run_seed(
        ha, gb, slug="hindsight/diego", state_path=tmp_path / "seed.json", dry_run=False,
    )
    assert res["seeded"] == 2  # two non-honcho compiled facts
    written = [c.args[0] for c in ha.write_conclusion.call_args_list]
    assert all(bridge.has_source(w, "gbrain") for w in written)
    res2 = bridge.run_seed(
        ha, gb, slug="hindsight/diego", state_path=tmp_path / "seed.json", dry_run=False,
    )
    assert res2["seeded"] == 0


def test_seed_failed_write_not_recorded(tmp_path):
    ha = mock.Mock()
    ha.write_conclusion.return_value = False  # simulate Honcho write failure
    gb = mock.Mock()
    gb.get_page.return_value = SEED_PAGE
    res = bridge.run_seed(
        ha, gb, slug="hindsight/diego", state_path=tmp_path / "seed.json", dry_run=False,
    )
    assert res["seeded"] == 0
    assert res["write_failed"] == 2  # both non-honcho facts failed to write
    assert bridge.load_state(tmp_path / "seed.json") == set()


def test_seed_missing_page_returns_zero(tmp_path):
    ha = mock.Mock()
    gb = mock.Mock()
    gb.get_page.return_value = None  # page unreadable / gbrain unavailable
    res = bridge.run_seed(
        ha, gb, slug="hindsight/diego", state_path=tmp_path / "seed.json", dry_run=False,
    )
    assert res == {"seeded": 0, "deduped": 0, "loop_skipped": 0, "write_failed": 0}
    ha.write_conclusion.assert_not_called()


def test_run_bridge_disabled_returns_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_load_bridge_config", lambda: {"enabled": False})
    out = bridge.run_bridge(dry_run=False)
    assert out["status"] == "disabled"


def test_two_cycle_no_echo(tmp_path, monkeypatch):
    """Full export+seed run twice must not loop facts across the boundary."""
    state = {"page": "# Diego\n\n- seeded truth\n\n<!-- timeline -->\n"}
    gb = mock.Mock()
    gb.get_page.side_effect = lambda slug: state["page"]
    gb.add_timeline.return_value = True

    def _put(slug, md):
        state["page"] = md
        return True
    gb.put_page.side_effect = _put

    ha = mock.Mock()
    ha.read_user_facts.return_value = ["honcho-derived fact"]
    ha.run_dialectic.return_value = ""
    ha.write_conclusion.return_value = True
    monkeypatch.setattr(bridge, "_write_mempalace_drawer", lambda room, content: True)

    cfg = {"enabled": True, "diegoPageSlug": "hindsight/diego", "dialecticQueries": [],
           "export": {"enabled": True}, "seed": {"enabled": True}}
    monkeypatch.setattr(bridge, "_load_bridge_config", lambda: cfg)
    monkeypatch.setattr(bridge, "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(bridge, "build_manager", lambda: object())
    monkeypatch.setattr(bridge, "HonchoAdapter", lambda manager, session_key=bridge.BRIDGE_SESSION: ha)
    monkeypatch.setattr(bridge, "GBrainAdapter", lambda: gb)
    monkeypatch.setattr(bridge, "_today", lambda: "2026-06-04")

    c1 = bridge.run_bridge(dry_run=False)
    c2 = bridge.run_bridge(dry_run=False)

    assert c1["export"]["exported"] == 1
    assert c1["seed"]["seeded"] == 1
    # The honcho fact was filtered by parse_compiled_facts (parse layer),
    # not the defensive backstop, so seed's loop_skipped stays 0.
    assert c1["seed"]["loop_skipped"] == 0
    assert c2["export"]["exported"] == 0
    assert c2["seed"]["seeded"] == 0
    seeded_contents = [c.args[0] for c in ha.write_conclusion.call_args_list]
    assert all("honcho-derived fact" not in s for s in seeded_contents)
