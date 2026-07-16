"""Tests for the canonical markdown memstore (file-backed, provider-agnostic)."""

import json

import pytest

from agent.memstore_files import (
    CanonicalMemstore,
    MarkdownDoc,
    group_transcript_by_day,
    _parse_date,
)
from agent.memstore_seeding import (
    CATEGORY_IDENTITY,
    CATEGORY_PROJECT,
    CATEGORY_TOOL,
    CATEGORY_USER,
    FactCorpus,
    SeedFact,
    parse_persona_doc,
)


# ---------------------------------------------------------------------------
# MarkdownDoc — idempotent managed blocks
# ---------------------------------------------------------------------------


class TestMarkdownDoc:
    def test_creates_file_with_title_and_block(self, tmp_path):
        doc = MarkdownDoc(tmp_path / "USER.md")
        added = doc.upsert_bullets("Preferences", ["likes Python"])
        doc.save()
        assert added == 1
        text = (tmp_path / "USER.md").read_text()
        assert "# User" in text
        assert "## Preferences" in text
        assert "hermes:seed:begin preferences" in text
        assert "- likes Python" in text

    def test_reseed_is_idempotent(self, tmp_path):
        p = tmp_path / "USER.md"
        MarkdownDoc(p).upsert_bullets("Preferences", ["likes Python"])
        d = MarkdownDoc(p)
        d.upsert_bullets("Preferences", ["likes Python"])
        d.save()
        MarkdownDoc(p).save()  # no-op reload
        # Re-open and add same again → no duplicate.
        d2 = MarkdownDoc(p)
        added = d2.upsert_bullets("Preferences", ["likes Python"])
        d2.save()
        assert added == 0
        assert p.read_text().count("- likes Python") == 1

    def test_merge_adds_new_bullets(self, tmp_path):
        p = tmp_path / "USER.md"
        d = MarkdownDoc(p)
        d.upsert_bullets("Preferences", ["likes Python"])
        d.save()
        d2 = MarkdownDoc(p)
        added = d2.upsert_bullets("Preferences", ["likes Python", "uses Neovim"])
        d2.save()
        assert added == 1
        text = p.read_text()
        assert "- likes Python" in text and "- uses Neovim" in text

    def test_preserves_content_outside_block(self, tmp_path):
        p = tmp_path / "USER.md"
        p.write_text("# User\n\nHand-written intro line.\n")
        d = MarkdownDoc(p)
        d.upsert_bullets("Preferences", ["likes Python"])
        d.save()
        text = p.read_text()
        assert "Hand-written intro line." in text
        assert "- likes Python" in text

    def test_bullets_in_reads_back(self, tmp_path):
        p = tmp_path / "USER.md"
        d = MarkdownDoc(p)
        d.upsert_bullets("Preferences", ["a", "b"])
        d.save()
        assert MarkdownDoc(p).bullets_in("Preferences") == ["a", "b"]

    def test_separate_sections_independent(self, tmp_path):
        p = tmp_path / "AGENTS.md"
        d = MarkdownDoc(p)
        d.upsert_bullets("Operating Notes", ["use uv"])
        d.upsert_bullets("Tools", ["ripgrep"])
        d.save()
        d2 = MarkdownDoc(p)
        assert d2.bullets_in("Operating Notes") == ["use uv"]
        assert d2.bullets_in("Tools") == ["ripgrep"]


# ---------------------------------------------------------------------------
# Day grouping
# ---------------------------------------------------------------------------


class TestDayGrouping:
    def test_parse_iso(self):
        assert _parse_date("2026-07-16T11:20:00Z") == "2026-07-16"

    def test_parse_bare_date(self):
        assert _parse_date("2026-07-16") == "2026-07-16"

    def test_parse_epoch_seconds(self):
        # 2026-07-16 ~ 1784000000
        assert _parse_date(1784200000) == _parse_date(1784200000)  # stable
        assert _parse_date(1784200000).startswith("2026-")

    def test_parse_epoch_millis(self):
        assert _parse_date(1784200000000).startswith("2026-")

    def test_parse_garbage_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_group_by_day(self):
        msgs = [
            {"role": "user", "content": "a", "timestamp": "2026-07-15T09:00:00Z"},
            {"role": "user", "content": "b", "timestamp": "2026-07-16T09:00:00Z"},
            {"role": "user", "content": "c", "timestamp": "2026-07-16T10:00:00Z"},
        ]
        by_day = group_transcript_by_day(msgs)
        assert set(by_day) == {"2026-07-15", "2026-07-16"}
        assert len(by_day["2026-07-16"]) == 2

    def test_undated_falls_back(self):
        msgs = [{"role": "user", "content": "x"}]
        by_day = group_transcript_by_day(msgs, default_date="2020-01-01")
        assert by_day == {"2020-01-01": msgs}


# ---------------------------------------------------------------------------
# Persona identity routing
# ---------------------------------------------------------------------------


class TestIdentityRouting:
    def test_about_the_agent_is_identity(self):
        doc = "## About the Agent\n- The assistant is named Hermes.\n"
        facts = parse_persona_doc(doc)
        assert facts[0].category == CATEGORY_IDENTITY

    def test_about_the_user_is_user(self):
        doc = "## About the User\n- The user's name is Ade.\n"
        facts = parse_persona_doc(doc)
        assert facts[0].category == CATEGORY_USER


# ---------------------------------------------------------------------------
# CanonicalMemstore — seeding into files
# ---------------------------------------------------------------------------


class TestCanonicalMemstore:
    def test_routes_facts_to_files(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        corpus = FactCorpus([
            SeedFact("the user prefers concise answers", category=CATEGORY_USER),
            SeedFact("the project uses uv", category=CATEGORY_PROJECT),
            SeedFact("uses ripgrep", category=CATEGORY_TOOL),
            SeedFact("the assistant is named Hermes", category=CATEGORY_IDENTITY),
        ])
        report = store.seed_facts(corpus)
        # 4 facts, but the identity fact is mirrored into SOUL.md too → 5 writes.
        assert report.total_added == 5
        assert report.files_touched["SOUL.md"] == 1
        assert (tmp_path / "memories" / "USER.md").exists()
        assert (tmp_path / "memories" / "AGENTS.md").exists()
        assert (tmp_path / "memories" / "TOOLS.md").exists()
        assert (tmp_path / "memories" / "IDENTITY.md").exists()
        assert "concise answers" in (tmp_path / "memories" / "USER.md").read_text()

    def test_soul_at_root_gets_identity_mirror(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        store.seed_facts(FactCorpus([
            SeedFact("the assistant is named Hermes", category=CATEGORY_IDENTITY),
        ]))
        soul = tmp_path / "SOUL.md"
        assert soul.exists()
        assert "Hermes" in soul.read_text()

    def test_reseed_idempotent(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        corpus = FactCorpus([SeedFact("prefers Python", category=CATEGORY_USER)])
        store.seed_facts(corpus)
        report2 = store.seed_facts(corpus)
        assert report2.total_added == 0
        assert (tmp_path / "memories" / "USER.md").read_text().count("prefers Python") == 1

    def test_agents_md_kept_out_of_root(self, tmp_path):
        """The memstore's AGENTS.md must live under memories/, not clobber a
        project AGENTS.md at the root."""
        store = CanonicalMemstore(root=tmp_path)
        store.seed_facts(FactCorpus([SeedFact("uses uv", category=CATEGORY_PROJECT)]))
        assert (tmp_path / "memories" / "AGENTS.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# Daily digests + roll-up
# ---------------------------------------------------------------------------


class TestDailyAndRollup:
    MESSAGES = [
        {"role": "user", "content": "My name is Ade and I prefer dark mode.",
         "timestamp": "2026-07-15T09:00:00Z"},
        {"role": "user", "content": "We decided to use PostgreSQL for the service.",
         "timestamp": "2026-07-16T09:00:00Z"},
        {"role": "user", "content": "I always run pytest before pushing.",
         "timestamp": "2026-07-16T10:00:00Z"},
    ]

    def test_daily_digests_written_per_day(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        written = store.write_daily_digests(self.MESSAGES)
        assert set(written) == {"2026-07-15", "2026-07-16"}
        d15 = tmp_path / "memories" / "daily" / "2026-07-15.md"
        assert d15.exists()
        assert "Daily Memory — 2026-07-15" in d15.read_text()
        assert "dark mode" in d15.read_text()

    def test_daily_digest_has_summary(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        store.write_daily_digests(self.MESSAGES)
        text = (tmp_path / "memories" / "daily" / "2026-07-16.md").read_text()
        assert "## Summary" in text

    def test_load_daily_corpus_roundtrips(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        store.write_daily_digests(self.MESSAGES)
        corpus, days = store.load_daily_corpus()
        assert days == 2
        contents = [f.content for f in corpus]
        assert any("PostgreSQL" in c for c in contents)

    def test_load_daily_corpus_limits_days(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        store.write_daily_digests(self.MESSAGES)
        _, days = store.load_daily_corpus(days=1)
        assert days == 1

    def test_roll_up_folds_into_memory_files(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        store.write_daily_digests(self.MESSAGES)
        report = store.roll_up()
        assert report.days_read == 2
        assert report.facts_in >= 1
        # Roll-up folds facts into the canonical files.
        assert report.seed.total_added >= 1
        # User preference from the daily tree should reach USER.md.
        user_md = tmp_path / "memories" / "USER.md"
        assert user_md.exists()

    def test_roll_up_empty_tree(self, tmp_path):
        store = CanonicalMemstore(root=tmp_path)
        report = store.roll_up()
        assert report.days_read == 0


# ---------------------------------------------------------------------------
# End-to-end via the bundled sample files
# ---------------------------------------------------------------------------


class TestBundledSamples:
    def test_seed_from_sample_persona_and_transcript(self, tmp_path):
        import pathlib
        repo = pathlib.Path(__file__).resolve().parents[2]
        persona = repo / "docs" / "memstore-seed" / "persona.md"
        transcript = repo / "docs" / "memstore-seed" / "transcript.json"
        if not persona.exists():
            pytest.skip("sample files not present")

        from agent.memstore_seeding import build_corpus_from_sources, load_transcript

        corpus = build_corpus_from_sources(
            persona_paths=[persona], transcript_paths=[transcript]
        )
        store = CanonicalMemstore(root=tmp_path)
        store.seed_facts(corpus)
        store.write_daily_digests(load_transcript(transcript))

        # Identity routed to IDENTITY.md / SOUL.md.
        assert "Hermes" in (tmp_path / "memories" / "IDENTITY.md").read_text()
        # User profile to USER.md.
        assert "Ade" in (tmp_path / "memories" / "USER.md").read_text()
        # Two days of digests from the dated transcript.
        daily = list((tmp_path / "memories" / "daily").glob("*.md"))
        assert len(daily) == 2
