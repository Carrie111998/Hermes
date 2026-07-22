"""Tests for events.subscribers.memory_writer -- routes events to memory layers."""

import subprocess
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers import memory_writer as mw
from events.subscribers.memory_writer import MemoryWriter, MEMORY_ROUTING


class TestMemoryRouting:
    def test_high_score_routes_to_gbrain(self):
        assert "gbrain" in MEMORY_ROUTING[EventType.JOB_HIGH_SCORE]["targets"]

    def test_interview_routes_to_both(self):
        targets = MEMORY_ROUTING[EventType.INTERVIEW_SIGNAL]["targets"]
        assert "gbrain" in targets
        assert "mempalace" in targets

    def test_cron_failed_consecutive_routes_to_memory_md(self):
        assert "memory_md" in MEMORY_ROUTING[EventType.CRON_FAILED_CONSECUTIVE]["targets"]

    def test_cron_completed_not_in_routing(self):
        assert EventType.CRON_COMPLETED not in MEMORY_ROUTING

    def test_gateway_health_not_routed(self):
        # Dropped 2026-07-14: gateway up/down is pure churn, already retained in
        # the event bus + audit.jsonl. It must not append to MEMORY.md.
        assert EventType.GATEWAY_HEALTH not in MEMORY_ROUTING

    def test_gateway_health_not_in_subscription_filter(self):
        # event_types drives the bus-level subscribe() filter; dropping the
        # routing entry must also stop the subscriber from fetching the event.
        assert EventType.GATEWAY_HEALTH not in MemoryWriter.event_types


class TestMemoryWriter:
    def test_skips_non_routed_events(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(EventType.CRON_COMPLETED, "scout", {})
        # Should not raise
        writer.handle(event)

    def test_builds_gbrain_content(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
        )
        content = writer._build_content(event, "gbrain")
        assert "Acme" in content
        assert "VP Finance" in content


class TestGBrainWrite:
    """_write_gbrain shells out to the gbrain CLI but DISCARDS its output.

    It must therefore NOT capture stdout/stderr: a capture pipe inherited by a
    grandchild keeps the pipe's write end open and hangs subprocess.run's
    timeout on Windows (the grandchild-pipe bug). With no pipe at all there is
    nothing to inherit, so timeout=10 reliably fires. Best-effort throughout.
    """

    def test_write_gbrain_uses_devnull_not_capture_pipe(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)
        event = Event.create(EventType.JOB_HIGH_SCORE, "scout", {"company": "Acme"})

        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            writer._write_gbrain(event, "timeline content")

        run.assert_called_once()
        kwargs = run.call_args.kwargs
        assert kwargs.get("stdout") is subprocess.DEVNULL
        assert kwargs.get("stderr") is subprocess.DEVNULL
        assert "capture_output" not in kwargs  # capture pipe is what causes the hang
        assert kwargs.get("timeout") == 10
        assert run.call_args.args[0] == ["gbrain", "timeline", "add", "Acme", "timeline content"]

    def test_write_gbrain_swallows_timeout(self, tmp_path):
        # A wedged gbrain that times out must not propagate out of the
        # best-effort writer (it runs inside the gateway event loop).
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)
        event = Event.create(EventType.JOB_HIGH_SCORE, "scout", {"company": "Acme"})

        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd=["gbrain"], timeout=10)):
            writer._write_gbrain(event, "content")  # must not raise

    def test_write_gbrain_skips_when_no_company(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)
        event = Event.create(EventType.JOB_HIGH_SCORE, "scout", {})

        with patch("subprocess.run") as run:
            writer._write_gbrain(event, "content")
        run.assert_not_called()


class TestMemPalaceWrite:
    """Tests for the MemPalace integration in MemoryWriter._write_mempalace."""

    def test_interview_signal_writes_to_mempalace(self, tmp_path):
        """INTERVIEW_SIGNAL should call mempalace add_drawer with the event content."""
        pytest.importorskip("mempalace")  # mempalace ships in its own venv; skip where absent
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.INTERVIEW_SIGNAL, "tracker",
            {"company": "Acme", "detail": "Phone screen Tuesday 2pm"},
        )

        captured_call = {}

        def fake_add_drawer(collection, wing, room, content, source_file, chunk_index, agent):
            captured_call.update(
                collection=collection,
                wing=wing, room=room, content=content,
                source_file=source_file, chunk_index=chunk_index, agent=agent,
            )
            return True

        def fake_get_collection(*args, **kwargs):
            return "mock-collection"

        with patch("mempalace.miner.add_drawer", fake_add_drawer), \
             patch("mempalace.palace.get_collection", fake_get_collection), \
             patch.object(writer, "_write_gbrain"):  # don't spawn the real gbrain CLI
            writer.handle(event)

        assert captured_call, "mempalace.add_drawer should have been called"
        # Event content should flow through to the drawer
        assert "Acme" in captured_call["content"]
        assert "Phone screen" in captured_call["content"]
        # source_file should identify the event uniquely
        assert event.event_id in captured_call["source_file"]
        # Wing/room should be hermes-specific (not a user wing)
        assert captured_call["wing"].startswith("hermes")
        # Room should correspond to the event type
        assert captured_call["room"] == "interview_signal"
        assert captured_call["agent"] == "hermes-event-bus"

    def test_mempalace_missing_is_graceful(self, tmp_path):
        """If mempalace is not installed, MemoryWriter should not crash."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.OFFER_SIGNAL, "tracker",
            {"company": "Acme", "detail": "Offer letter attached"},
        )

        # Simulate mempalace not being importable
        import builtins
        real_import = builtins.__import__

        def stub_import(name, *args, **kwargs):
            if name.startswith("mempalace"):
                raise ImportError("no mempalace")
            return real_import(name, *args, **kwargs)

        # patch.object first so _write_gbrain is stubbed before __import__ is
        # swapped — guarantees no real gbrain CLI spawn even with the import stub active.
        with patch.object(writer, "_write_gbrain"), \
             patch("builtins.__import__", stub_import):
            # Should not raise
            writer.handle(event)

    def test_collection_is_cached_across_writes(self, tmp_path):
        """Subsequent writes should reuse the same collection object."""
        pytest.importorskip("mempalace")  # mempalace ships in its own venv; skip where absent
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        calls = {"count": 0}

        def fake_get_collection(*args, **kwargs):
            calls["count"] += 1
            return "shared-collection"

        def noop_add_drawer(*args, **kwargs):
            return True

        with patch("mempalace.miner.add_drawer", noop_add_drawer), \
             patch("mempalace.palace.get_collection", fake_get_collection):
            writer._write_mempalace(
                Event.create(EventType.INTERVIEW_SIGNAL, "t", {"company": "A"}),
                "content-1",
            )
            writer._write_mempalace(
                Event.create(EventType.OFFER_SIGNAL, "t", {"company": "B"}),
                "content-2",
            )

        assert calls["count"] == 1, "get_collection should only be called once (cached)"


class TestCorrelationIdDedup:
    """Tests for correlation_id deduplication in MemoryWriter."""

    def test_same_correlation_id_processed_once(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event1 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
            correlation_id="corr-abc-123",
        )
        event2 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
            correlation_id="corr-abc-123",
        )

        # Stub the gbrain write so handle() never spawns the real CLI; the rate
        # counters we assert on are bumped by _check_rate_limit(), independent of it.
        with patch.object(writer, "_write_gbrain"):
            # First call should process
            writer.handle(event1)
            assert "corr-abc-123" in writer._seen_correlation_ids

            # Second call with same correlation_id should be skipped
            # We verify by checking rate counters: only one write should have been attempted
            gbrain_count_before = len(writer._rate_counters.get("gbrain", []))

            writer.handle(event2)

            gbrain_count_after = len(writer._rate_counters.get("gbrain", []))
            assert gbrain_count_after == gbrain_count_before  # no new write

    def test_different_correlation_ids_both_processed(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event1 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
            correlation_id="corr-111",
        )
        event2 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Beta Inc", "title": "Engineer", "platform": "Lever"},
            correlation_id="corr-222",
        )

        # Stub the gbrain write so handle() never spawns the real CLI.
        with patch.object(writer, "_write_gbrain"):
            writer.handle(event1)
            writer.handle(event2)

        # Both correlation_ids should be recorded as seen
        assert "corr-111" in writer._seen_correlation_ids
        assert "corr-222" in writer._seen_correlation_ids

        # Both should have triggered writes (2 gbrain entries in rate counters)
        assert len(writer._rate_counters.get("gbrain", [])) == 2


# ---------------------------------------------------------------------------
# Honcho routing tests
# ---------------------------------------------------------------------------


def _make_writer(tmp_path):
    """Construct a MemoryWriter with a temporary EventBus (matches existing pattern)."""
    bus = EventBus(db_path=tmp_path / "events" / "test.db")
    return mw.MemoryWriter(bus)


def test_honcho_is_routed_for_meaningful_events():
    assert "honcho" in mw.MEMORY_ROUTING[EventType.JOB_HIGH_SCORE]["targets"]
    assert "honcho" in mw.MEMORY_ROUTING[EventType.INTERVIEW_SIGNAL]["targets"]
    assert EventType.GATEWAY_HEALTH not in mw.MEMORY_ROUTING  # dropped 2026-07-14 (churn)
    assert "honcho" not in mw.MEMORY_ROUTING[EventType.CRON_FAILED_CONSECUTIVE]["targets"]


def test_honcho_rate_limit_present():
    assert mw.RATE_LIMITS["honcho"]["max_per_hour"] == 30


def test_write_honcho_targets_events_peer_not_user(tmp_path):
    w = _make_writer(tmp_path)
    fake_mgr = mock.Mock()
    fake_mgr.create_conclusion.return_value = True
    with mock.patch("plugins.memory.honcho.bridge._load_capture_config",
                    return_value={"enabled": True}):
        with mock.patch.object(w, "_get_honcho_manager", return_value=fake_mgr):
            w._write_honcho(content="High-score job at Acme")
    fake_mgr.get_or_create.assert_called_once_with("hermes-autonomous")
    fake_mgr.create_conclusion.assert_called_once_with(
        "hermes-autonomous", "High-score job at Acme", peer="hermes-events",
    )


def test_write_honcho_respects_capture_flag(tmp_path):
    w = _make_writer(tmp_path)
    with mock.patch("plugins.memory.honcho.bridge._load_capture_config",
                    return_value={"enabled": False}):
        with mock.patch.object(w, "_get_honcho_manager") as gm:
            w._write_honcho("x")
            gm.assert_not_called()


def test_write_honcho_unavailable_is_silent(tmp_path):
    w = _make_writer(tmp_path)
    with mock.patch("plugins.memory.honcho.bridge._load_capture_config",
                    return_value={"enabled": True}):
        with mock.patch.object(w, "_get_honcho_manager", return_value=None):
            w._write_honcho(content="x")  # must not raise


def test_write_honcho_false_return_is_silent(tmp_path):
    w = _make_writer(tmp_path)
    fake_mgr = mock.Mock()
    fake_mgr.create_conclusion.return_value = False
    with mock.patch("plugins.memory.honcho.bridge._load_capture_config",
                    return_value={"enabled": True}):
        with mock.patch.object(w, "_get_honcho_manager", return_value=fake_mgr):
            w._write_honcho(content="x")  # must not raise
    fake_mgr.create_conclusion.assert_called_once()


def test_build_content_honcho_uses_rich_phrasing(tmp_path):
    """_build_content for honcho should use the same rich phrasing as gbrain, not the raw fallback."""
    w = _make_writer(tmp_path)
    ev = Event.create(
        EventType.JOB_HIGH_SCORE, "scout",
        {"title": "Staff Eng", "company": "Acme", "score": 95},
    )
    content = w._build_content(ev, "honcho")
    assert "Staff Eng" in content and "Acme" in content
    assert "High-score job" in content            # rich gbrain phrasing reused
    assert not content.startswith("JOB_HIGH_SCORE:")  # NOT the raw fallback dump


# ---------------------------------------------------------------------------
# Defensive MEMORY.md size cap (bound MEMORY.md growth) — 2026-07-14
# ---------------------------------------------------------------------------


class TestMemoryMdCap:
    def test_cap_drops_oldest_event_sections_only(self):
        head = "# Jaum Memory\n\n## Orchestration Rules\n- keep me\n\n"
        events = "".join(
            f"## Event 2026-06-{d:02d}\n" + "".join(f"- filler {i}\n" for i in range(40))
            for d in range(1, 16)
        )
        newest = "## Event 2026-07-14\n- newest entry\n"
        capped = mw._cap_memory_md(head + events + newest, max_bytes=3000)
        assert len(capped.encode("utf-8")) <= 3000
        assert "# Jaum Memory" in capped and "- keep me" in capped   # protected head kept
        assert "2026-06-01" not in capped                            # oldest evicted
        assert "- newest entry" in capped                            # newest kept

    def test_cap_never_trims_protected_content(self):
        # Over cap but ZERO '## Event' sections -> returned unchanged (best-effort).
        content = "# Jaum Memory\n" + "".join(f"- rule {i}\n" for i in range(500))
        assert mw._cap_memory_md(content, max_bytes=100) == content

    def test_write_memory_md_applies_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(mw, "MEMORY_MD_MAX_BYTES", 2000)
        mem = tmp_path / "memories" / "MEMORY.md"
        mem.parent.mkdir(parents=True)
        head = "# Jaum Memory\n- keep me\n"
        events = "".join(
            f"## Event 2026-06-{d:02d}\n" + "".join(f"- filler {i}\n" for i in range(40))
            for d in range(1, 12)
        )
        mem.write_text(head + events, encoding="utf-8")
        w = _make_writer(tmp_path)
        ev = Event.create(
            EventType.CRON_FAILED_CONSECUTIVE, "cron",
            {"job_name": "x", "consecutive_errors": 3, "error": "boom"},
        )
        w._write_memory_md(ev, "x failing since 2026-07-14 (3 consecutive) — boom")
        out = mem.read_text(encoding="utf-8")
        assert len(out.encode("utf-8")) <= 2000
        assert "# Jaum Memory" in out and "- keep me" in out
        assert "boom" in out                     # freshly-appended note survived
        assert "2026-06-01" not in out           # oldest event evicted

    def test_write_memory_md_under_cap_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
        mem = tmp_path / "memories" / "MEMORY.md"
        mem.parent.mkdir(parents=True)
        mem.write_text("# Jaum Memory\n- small\n", encoding="utf-8")
        w = _make_writer(tmp_path)
        ev = Event.create(
            EventType.CRON_FAILED_CONSECUTIVE, "cron",
            {"job_name": "y", "consecutive_errors": 2, "error": "eek"},
        )
        w._write_memory_md(ev, "y failing since 2026-07-14 (2 consecutive) — eek")
        out = mem.read_text(encoding="utf-8")
        assert "# Jaum Memory" in out and "- small" in out and "eek" in out
