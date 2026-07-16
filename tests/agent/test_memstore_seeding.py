"""Tests for provider-agnostic memstore seeding and dreams consolidation."""

import json

import pytest

from agent.memory_manager import MemoryManager
from agent.memstore_seeding import (
    CATEGORY_BOOTSTRAP,
    CATEGORY_INSIGHT,
    CATEGORY_PROJECT,
    CATEGORY_TOOL,
    CATEGORY_USER,
    DreamConsolidator,
    FactCorpus,
    MemstoreSeeder,
    SeedFact,
    build_corpus_from_sources,
    extract_entities,
    load_transcript,
    parse_persona_doc,
    parse_transcript,
    seed_and_dream,
)


# ---------------------------------------------------------------------------
# Test sink — captures provider-agnostic writes
# ---------------------------------------------------------------------------


class RecordingProvider:
    """Minimal provider-like sink capturing on_memory_write calls."""

    name = "recording"

    def __init__(self):
        self.writes = []

    def on_memory_write(self, action, target, content, metadata=None):
        self.writes.append((action, target, content, metadata or {}))


class LegacyProvider:
    """Sink with the legacy 3-arg memory-write signature (no metadata)."""

    name = "legacy"

    def __init__(self):
        self.writes = []

    def on_memory_write(self, action, target, content):
        self.writes.append((action, target, content))


# ---------------------------------------------------------------------------
# SeedFact / FactCorpus
# ---------------------------------------------------------------------------


class TestSeedFact:
    def test_target_derived_from_category(self):
        assert SeedFact("x", category=CATEGORY_USER).target == "user"
        assert SeedFact("x", category=CATEGORY_PROJECT).target == "memory"

    def test_explicit_target_wins(self):
        assert SeedFact("x", category=CATEGORY_USER, target="memory").target == "memory"

    def test_trust_clamped(self):
        assert SeedFact("x", trust=5.0).trust == 1.0
        assert SeedFact("x", trust=-1.0).trust == 0.0

    def test_content_stripped(self):
        assert SeedFact("  hello  ").content == "hello"

    def test_dedup_key_normalizes(self):
        a = SeedFact("The User likes Python!")
        b = SeedFact("the user likes python")
        assert a.key == b.key

    def test_roundtrip_dict(self):
        f = SeedFact("hi", category=CATEGORY_USER, tags=("a", "b"), trust=0.7, source="s")
        assert SeedFact.from_dict(f.to_dict()) == f


class TestFactCorpus:
    def test_dedup_merges(self):
        corpus = FactCorpus()
        assert corpus.add(SeedFact("User likes Python", tags=("x",), trust=0.5))
        # Same normalised content → merged, not added.
        assert not corpus.add(SeedFact("user likes python", tags=("y",), trust=0.9))
        assert len(corpus) == 1
        merged = corpus.facts()[0]
        assert merged.trust == 0.9  # max trust wins
        assert set(merged.tags) == {"x", "y"}  # tags unioned

    def test_empty_content_ignored(self):
        corpus = FactCorpus()
        assert not corpus.add(SeedFact("   "))
        assert len(corpus) == 0

    def test_jsonl_roundtrip(self):
        corpus = FactCorpus([SeedFact("a fact"), SeedFact("another fact", category=CATEGORY_USER)])
        restored = FactCorpus.from_jsonl(corpus.to_jsonl())
        assert len(restored) == 2

    def test_from_jsonl_skips_garbage(self):
        corpus = FactCorpus.from_jsonl('{"content": "ok"}\nnot-json\n\n')
        assert len(corpus) == 1


# ---------------------------------------------------------------------------
# Entity extraction
# ---------------------------------------------------------------------------


class TestEntityExtraction:
    def test_capitalized_phrases(self):
        ents = extract_entities("The project uses PostgreSQL and Redis")
        assert "PostgreSQL" in ents and "Redis" in ents

    def test_drops_leading_stopword(self):
        ents = extract_entities("The user prefers things")
        assert "The" not in ents

    def test_quoted_terms(self):
        ents = extract_entities('use "pytest" for tests')
        assert "pytest" in ents


# ---------------------------------------------------------------------------
# Persona doc parsing
# ---------------------------------------------------------------------------


class TestPersonaParsing:
    DOC = (
        "# About the User\n"
        "- The user's name is Ade.\n"
        "- Prefers concise answers.\n"
        "\n"
        "## Project: Hermes\n"
        "The project uses Python 3.11. We chose pytest for tests.\n"
    )

    def test_bullets_become_facts(self):
        facts = parse_persona_doc(self.DOC)
        contents = [f.content for f in facts]
        assert "The user's name is Ade." in contents
        assert "Prefers concise answers." in contents

    def test_heading_sets_category(self):
        facts = parse_persona_doc(self.DOC)
        user_facts = [f for f in facts if "name is Ade" in f.content]
        assert user_facts[0].category == CATEGORY_USER
        project_facts = [f for f in facts if "Python 3.11" in f.content]
        assert project_facts[0].category == CATEGORY_PROJECT

    def test_paragraph_split_into_sentences(self):
        facts = parse_persona_doc(self.DOC)
        contents = [f.content for f in facts]
        assert any("Python 3.11" in c for c in contents)
        assert any("pytest" in c for c in contents)

    def test_bootstrap_heading_routes_to_bootstrap(self):
        doc = "## Getting Started\n- Activate the virtualenv before commands.\n"
        facts = parse_persona_doc(doc)
        assert facts[0].category == CATEGORY_BOOTSTRAP

    def test_bootstrap_wins_over_tool_setup_hint(self):
        # "Onboarding" mentions setup-like content but must land in bootstrap,
        # not be swallowed by the tool "setup"/"environment" hints.
        doc = "## Onboarding\n- Install prerequisites first.\n"
        facts = parse_persona_doc(doc)
        assert facts[0].category == CATEGORY_BOOTSTRAP

    def test_plain_tools_heading_still_tool(self):
        doc = "## Tools\n- Uses ripgrep and fd.\n"
        facts = parse_persona_doc(doc)
        assert facts[0].category == CATEGORY_TOOL

    def test_short_lines_skipped(self):
        facts = parse_persona_doc("# H\n- ok\n- a\n")
        assert facts == []

    def test_provenance_recorded(self):
        facts = parse_persona_doc(self.DOC, source="persona")
        assert all(f.source.startswith("persona:L") for f in facts)

    def test_paragraph_provenance_points_to_start_line(self):
        # Paragraph starts on line 3; a fact from it must cite L3, not the
        # blank/heading line that flushed the paragraph.
        doc = "# H\n\nThe project uses Python 3.11. We chose pytest.\n\n"
        facts = parse_persona_doc(doc)
        assert facts, "expected at least one fact"
        assert all(f.source == "persona:L3" for f in facts)


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


class TestTranscriptParsing:
    MESSAGES = [
        {"role": "user", "content": "Remember that I deploy on Fridays only."},
        {"role": "assistant", "content": "Got it."},
        {"role": "user", "content": "My name is Ade and I prefer dark mode."},
        {"role": "user", "content": "We decided to use PostgreSQL for the service."},
        {"role": "user", "content": "I always run pytest before pushing."},
        {"role": "user", "content": "thanks"},
    ]

    def test_explicit_remember_extracted(self):
        facts = parse_transcript(self.MESSAGES)
        assert any("deploy on Fridays" in f.content for f in facts)

    def test_identity_extracted_as_user(self):
        facts = parse_transcript(self.MESSAGES)
        ident = [f for f in facts if "name is Ade" in f.content]
        assert ident and ident[0].category == CATEGORY_USER

    def test_decision_extracted_as_project(self):
        facts = parse_transcript(self.MESSAGES)
        dec = [f for f in facts if "PostgreSQL" in f.content]
        assert dec and dec[0].category == CATEGORY_PROJECT

    def test_assistant_turns_ignored(self):
        facts = parse_transcript(self.MESSAGES)
        assert not any("Got it" in f.content for f in facts)

    def test_trivial_turns_skipped(self):
        facts = parse_transcript(self.MESSAGES)
        assert not any(f.content.strip().lower() == "thanks" for f in facts)

    def test_multimodal_content_parts(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "I prefer Rust over Go."}]}]
        facts = parse_transcript(msgs)
        assert any("Rust" in f.content for f in facts)

    def test_provenance_records_message_index(self):
        facts = parse_transcript(self.MESSAGES)
        assert all(f.source.startswith("transcript:msg") for f in facts)


class TestLoadTranscript:
    def test_messages_wrapper(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}]}))
        assert load_transcript(p) == [{"role": "user", "content": "hi"}]

    def test_bare_list(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text(json.dumps([{"role": "user", "content": "hi"}]))
        assert load_transcript(p)[0]["content"] == "hi"

    def test_jsonl(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text('{"role": "user", "content": "a"}\n{"role": "user", "content": "b"}\n')
        assert len(load_transcript(p)) == 2

    def test_empty_file(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text("")
        assert load_transcript(p) == []


# ---------------------------------------------------------------------------
# Seeder — provider-agnostic writes
# ---------------------------------------------------------------------------


class TestMemstoreSeeder:
    def test_writes_through_on_memory_write(self):
        sink = RecordingProvider()
        corpus = FactCorpus([
            SeedFact("User likes Python", category=CATEGORY_USER),
            SeedFact("Project uses uv", category=CATEGORY_PROJECT),
        ])
        report = MemstoreSeeder(sink).seed(corpus)
        assert report.written == 2
        assert len(sink.writes) == 2
        actions = {w[0] for w in sink.writes}
        assert actions == {"add"}

    def test_routes_user_vs_memory_target(self):
        sink = RecordingProvider()
        MemstoreSeeder(sink).seed(FactCorpus([
            SeedFact("a pref", category=CATEGORY_USER),
            SeedFact("a project fact", category=CATEGORY_PROJECT),
        ]))
        targets = {content: target for (_, target, content, _) in sink.writes}
        assert targets["a pref"] == "user"
        assert targets["a project fact"] == "memory"

    def test_metadata_carries_category_and_trust(self):
        sink = RecordingProvider()
        MemstoreSeeder(sink).seed(FactCorpus([SeedFact("x", trust=0.8, tags=("t",))]))
        meta = sink.writes[0][3]
        assert meta["category"] == "general"
        assert meta["trust"] == 0.8
        assert meta["tags"] == ["t"]
        assert meta["write_origin"] == "memstore_seeding"

    def test_legacy_3arg_sink_supported(self):
        sink = LegacyProvider()
        report = MemstoreSeeder(sink).seed(FactCorpus([SeedFact("x")]))
        assert report.written == 1
        assert sink.writes == [("add", "memory", "x")]

    def test_dry_run_writes_nothing(self):
        sink = RecordingProvider()
        report = MemstoreSeeder(sink).seed(FactCorpus([SeedFact("x")]), dry_run=True)
        assert report.written == 1 and report.dry_run
        assert sink.writes == []

    def test_provider_failure_isolated(self):
        class Boom:
            name = "boom"

            def on_memory_write(self, action, target, content, metadata=None):
                raise RuntimeError("nope")

        report = MemstoreSeeder(Boom()).seed(FactCorpus([SeedFact("x"), SeedFact("y")]))
        assert report.failed == 2 and report.written == 0
        assert len(report.errors) == 2

    def test_bad_sink_rejected(self):
        with pytest.raises(TypeError):
            MemstoreSeeder(object())

    def test_seeds_through_memory_manager(self):
        """End-to-end provider-agnostic path via MemoryManager fan-out."""
        provider = RecordingProvider()
        # Give it the ABC surface MemoryManager expects.
        provider.get_tool_schemas = lambda: []
        manager = MemoryManager()
        manager.add_provider(provider)
        MemstoreSeeder(manager).seed(FactCorpus([SeedFact("via manager")]))
        assert any("via manager" in w[2] for w in provider.writes)


# ---------------------------------------------------------------------------
# Dreams — consolidation
# ---------------------------------------------------------------------------


class TestDreamConsolidator:
    def test_dedupe_near_identical(self):
        corpus = FactCorpus([
            SeedFact("The user prefers concise technical answers"),
            SeedFact("User prefers concise, technical answers please"),
        ])
        refined, report = DreamConsolidator().consolidate(corpus)
        assert report.merged >= 1
        assert len(refined) < 2 + report.insights

    def test_prune_low_trust(self):
        corpus = FactCorpus([
            SeedFact("solid fact", trust=0.6),
            SeedFact("weak fact", trust=0.1),
        ])
        refined, report = DreamConsolidator(min_trust=0.25).consolidate(corpus)
        contents = [f.content for f in refined]
        assert "solid fact" in contents
        assert "weak fact" not in contents
        assert report.pruned == 1

    def test_decay_factor_applies(self):
        corpus = FactCorpus([SeedFact("fact", trust=0.5)])
        refined, _ = DreamConsolidator(decay_factor=0.5, min_trust=0.0).consolidate(corpus)
        assert refined.facts()[0].trust == pytest.approx(0.25)

    def test_contradiction_detected_and_demoted(self):
        corpus = FactCorpus([
            SeedFact("Ade prefers PostgreSQL for the service", trust=0.7,
                     entities=("PostgreSQL",)),
            SeedFact("Ade does not want PostgreSQL anywhere", trust=0.5,
                     entities=("PostgreSQL",)),
        ])
        _, report = DreamConsolidator(synthesize=False).consolidate(corpus)
        assert report.contradictions >= 1

    def test_synthesize_insight_from_cluster(self):
        corpus = FactCorpus([
            SeedFact("Redis caches sessions", entities=("Redis",)),
            SeedFact("Redis stores rate limits", entities=("Redis",)),
            SeedFact("Redis backs the job queue", entities=("Redis",)),
        ])
        refined, report = DreamConsolidator(min_cluster=3).consolidate(corpus)
        assert report.insights >= 1
        insights = [f for f in refined if f.category == CATEGORY_INSIGHT]
        assert insights and "Redis" in insights[0].content

    def test_no_insight_below_cluster_threshold(self):
        corpus = FactCorpus([SeedFact("Redis caches things", entities=("Redis",))])
        _, report = DreamConsolidator(min_cluster=3).consolidate(corpus)
        assert report.insights == 0

    def test_substring_antonym_not_a_contradiction(self):
        # "like" is a substring of "dislike" — must not trigger a contradiction.
        corpus = FactCorpus([
            SeedFact("Ade dislikes Python for scripting", entities=("Python",)),
            SeedFact("Ade dislikes Python in general", entities=("Python",)),
        ])
        _, report = DreamConsolidator(synthesize=False, dedupe_threshold=0.99).consolidate(corpus)
        assert report.contradictions == 0

    def test_real_antonym_still_detected(self):
        corpus = FactCorpus([
            SeedFact("we enable telemetry for Redis", entities=("Redis",)),
            SeedFact("we disable telemetry for Redis", entities=("Redis",)),
        ])
        _, report = DreamConsolidator(synthesize=False).consolidate(corpus)
        assert report.contradictions >= 1

    def test_insight_label_matches_cluster_entity(self):
        # Each fact lists an unrelated entity first, then the shared one.
        corpus = FactCorpus([
            SeedFact("Redis caches sessions", entities=("Nginx", "Redis")),
            SeedFact("Redis stores rate limits", entities=("Kafka", "Redis")),
            SeedFact("Redis backs the job queue", entities=("Celery", "Redis")),
        ])
        refined, report = DreamConsolidator(min_cluster=3).consolidate(corpus)
        insight = [f for f in refined if f.category == CATEGORY_INSIGHT][0]
        assert insight.content.startswith("Redis:")
        assert insight.entities == ("Redis",)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class TestOrchestration:
    def test_build_corpus_from_sources(self, tmp_path):
        persona = tmp_path / "p.md"
        persona.write_text("# About the User\n- The user's name is Ade.\n")
        transcript = tmp_path / "t.json"
        transcript.write_text(json.dumps({
            "messages": [{"role": "user", "content": "I always use ripgrep instead of grep."}]
        }))
        corpus = build_corpus_from_sources(
            persona_paths=[persona], transcript_paths=[transcript]
        )
        contents = [f.content for f in corpus]
        assert any("Ade" in c for c in contents)
        assert any("ripgrep" in c for c in contents)

    def test_seed_and_dream_end_to_end(self, tmp_path):
        persona = tmp_path / "p.md"
        persona.write_text(
            "# About the User\n"
            "- The user prefers concise answers.\n"
            "- The user prefers concise answers please.\n"
        )
        corpus = build_corpus_from_sources(persona_paths=[persona])
        sink = RecordingProvider()
        seed_report, dream_report = seed_and_dream(sink, corpus, dream=True)
        assert dream_report is not None
        assert seed_report.written == len(sink.writes)
        # Dreaming should have merged the two near-identical preferences.
        assert dream_report.merged >= 1

    def test_seed_and_dream_skip_dream(self, tmp_path):
        corpus = FactCorpus([SeedFact("x")])
        sink = RecordingProvider()
        _, dream_report = seed_and_dream(sink, corpus, dream=False)
        assert dream_report is None
