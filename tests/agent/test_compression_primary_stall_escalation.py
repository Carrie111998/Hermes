"""Primary summary-route stall escalation — #95879.

A primary summary route that *stalls* (connection open, zero tokens) is
aborted by the host's progress-aware timeout and retried once on the
configured ``auxiliary.compression.fallback_chain`` (#78981/#95433). When that
retry RECOVERS, the stall path historically returned the recovered result
before ``on_timeout`` fired, so the route-agnostic cooldown ladder
(``record_timeout_failure``) never learned the primary stalled — and a
permanently half-dead primary was retried at FULL SPEED every compaction,
burning one full idle window each time, forever.

These tests pin the escalation contract:

* the fallback-recovered path fires an ``on_primary_stall`` hook (the shared
  ``on_timeout`` ladder deliberately does NOT learn about a recovery — the
  fallback just worked, #95433 ordering),
* the compressor keeps a SEPARATE, persisted ``_primary_stall_streak`` ledger
  that accumulates those recoveries,
* at ``compression.primary_stall_skip_threshold`` (default 2) a bounded
  skip-primary window arms: the host pre-pins the first fallback_chain entry
  as the FIRST attempt (no idle-window burn on the half-dead primary),
* after the window lapses the primary is re-probed; a stall re-arms the
  window, a token-producing primary clears the ledger,
* healthy routes are bit-for-bit unchanged: no hook, no streak, no skip.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import (
    ContextCompressor,
    pin_summary_route,
    take_pinned_summary_route,
)
from agent.conversation_compression import (
    CompressionCommitFence,
    resolve_compression_fallback_route,
    run_compress_context_with_progress_timeout,
)
from hermes_state import SessionDB

CHAIN_ENTRY = {
    "provider": "custom",
    "model": "backup-summarizer",
    "base_url": "https://fallback.invalid/v1",
    "api_key": "sk-fallback",
    "timeout": 45,
}


def _patch_chain(chain):
    """Pin auxiliary.compression config without touching the real config.yaml."""
    return patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value={"fallback_chain": chain},
    )


class _StalledSummaryWorker:
    """A compression worker whose first attempt streams nothing at all.

    Mirrors the #78981 fixture: the provider holds the connection open, so the
    worker never calls ``fence.touch_progress()`` and the host's idle budget
    lapses. ``stall_attempts`` controls how many attempts hang; any later
    attempt commits a real summary. ``stall_attempts=0`` commits immediately
    (the healthy-route shape).
    """

    def __init__(self, compressed, *, stall_attempts=1):
        self.compressed = compressed
        self.stall_attempts = stall_attempts
        self.routes = []
        self.fences = []
        self._lock = threading.Lock()
        self.release = threading.Event()

    @property
    def attempts(self):
        return len(self.routes)

    def __call__(self, fence: CompressionCommitFence):
        with self._lock:
            self.routes.append(take_pinned_summary_route())
            self.fences.append(fence)
            attempt = len(self.routes)
        if attempt <= self.stall_attempts:
            # Connection open, zero tokens, zero fence progress.
            self.release.wait(timeout=10)
            return ([{"role": "assistant", "content": "late"}], "late-prompt")
        if not fence.begin_commit():
            return ([{"role": "assistant", "content": "cancelled"}], "cancelled")
        try:
            return (self.compressed, "summarized-prompt")
        finally:
            fence.finish_commit()


def _run(
    worker,
    *,
    chain,
    timeouts,
    messages=None,
    idle=0.05,
    ceiling=0.2,
    on_stall=None,
    telemetry_agent=None,
):
    with _patch_chain(chain):
        return run_compress_context_with_progress_timeout(
            worker=worker,
            messages=messages
            if messages is not None
            else [{"role": "user", "content": "keep-me"}],
            system_prompt_fallback="degraded-prompt",
            idle_timeout_seconds=idle,
            total_ceiling_seconds=ceiling,
            on_timeout=lambda *args: timeouts.append(args),
            on_primary_stall=on_stall,
            telemetry_agent=telemetry_agent,
        )


def _compressor(db: SessionDB | None = None, session_id: str = "") -> ContextCompressor:
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        cc = ContextCompressor(model="test/model", quiet_mode=True)
    if db is not None:
        cc.bind_session_state(db, session_id)
    return cc


def _db(tmp_path: Path) -> SessionDB:
    return SessionDB(db_path=tmp_path / "state.db")


def _gate_tripped_compressor(
    db: SessionDB | None = None, session_id: str = ""
) -> ContextCompressor:
    """A compressor whose primary-stall ledger has already tripped the gate."""
    cc = _compressor(db, session_id)
    cc._primary_stall_streak = 2
    cc._primary_stall_skip_until = time.monotonic() + 600
    return cc


def _msgs():
    return [
        {"role": "user", "content": "u1 " + "x" * 200},
        {"role": "assistant", "content": "a1 " + "y" * 200},
        {"role": "user", "content": "u2 " + "z" * 200},
    ]


def _ok_response(content="SUMMARY BODY"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _make_compressor(summary_model="aux-summarizer"):
    with patch(
        "agent.context_compressor.get_model_context_length", return_value=100000
    ):
        return ContextCompressor(
            model="main-model",
            quiet_mode=True,
            summary_model_override=summary_model,
        )


# ---------------------------------------------------------------------------
# Stall detection: the recovered path escalates (on_primary_stall fires)
# ---------------------------------------------------------------------------


def test_fallback_recovered_stall_fires_escalation_hook():
    original = [{"role": "user", "content": "keep-me"}]
    compressed = [{"role": "user", "content": "summary of earlier turns"}]
    worker = _StalledSummaryWorker(compressed)
    timeouts = []
    stalls = []

    try:
        msgs, prompt = _run(
            worker,
            chain=[CHAIN_ENTRY],
            timeouts=timeouts,
            messages=original,
            on_stall=lambda: stalls.append("stall"),
        )
    finally:
        worker.release.set()

    assert worker.attempts == 2, "the aborted stall must be retried once"
    assert msgs == compressed, "the fallback attempt's compression is published"
    assert prompt == "summarized-prompt"
    assert stalls == ["stall"], (
        "the fallback-recovered stall must fire the route-specific hook"
    )
    assert not timeouts, (
        "on_timeout (the shared cooldown ladder) must NOT fire on a recovery"
    )


def test_fallback_recovered_stall_increments_ledger_to_skip_gate(tmp_path):
    """End-to-end: two fallback-recovered stalls trip the skip-primary gate."""
    db = _db(tmp_path)
    db.create_session("s1", source="cli")
    compressor = _compressor(db, "s1")
    timeouts = []

    def _on_stall():
        compressor.record_primary_route_stall_recovered()

    for _ in range(2):
        worker = _StalledSummaryWorker([{"role": "user", "content": "summary"}])
        try:
            _run(worker, chain=[CHAIN_ENTRY], timeouts=timeouts, on_stall=_on_stall)
        finally:
            worker.release.set()

    assert compressor._primary_stall_streak == 2
    assert compressor.should_skip_primary_route() is True, (
        "two consecutive fallback-recovered stalls must arm the skip gate"
    )
    assert not timeouts, "no degradation was ever signalled"


# ---------------------------------------------------------------------------
# Escalation firing: the skip-primary gate routes the FIRST attempt to fallback
# ---------------------------------------------------------------------------


def test_skip_primary_gate_prepins_fallback_as_first_attempt():
    """Host wiring simulation: gate tripped -> first attempt IS the fallback."""
    compressed = [{"role": "user", "content": "summary via fallback"}]
    worker = _StalledSummaryWorker(compressed, stall_attempts=0)
    stalls = []
    compressor = _gate_tripped_compressor()

    with _patch_chain([CHAIN_ENTRY]):
        assert compressor.should_skip_primary_route() is True
        route = resolve_compression_fallback_route()
    assert route is not None

    try:
        with _patch_chain([CHAIN_ENTRY]):
            with pin_summary_route(route):
                msgs, prompt = run_compress_context_with_progress_timeout(
                    worker=worker,
                    messages=[{"role": "user", "content": "keep-me"}],
                    system_prompt_fallback="degraded-prompt",
                    idle_timeout_seconds=0.05,
                    total_ceiling_seconds=0.2,
                    stall_fallback=False,
                    on_primary_stall=lambda: stalls.append("stall"),
                )
    finally:
        worker.release.set()

    assert worker.attempts == 1, (
        "the pre-pinned fallback is the only attempt — no idle burn, no retry"
    )
    pinned = worker.routes[0]
    assert pinned is not None, "the first attempt must carry the fallback route"
    assert pinned["provider"] == "custom"
    assert pinned["model"] == "backup-summarizer"
    assert msgs == compressed
    assert stalls == [], "a healthy pre-pinned attempt is not a new stall"


def test_skipped_primary_stall_degrades_without_second_retry():
    """A stall on the pre-pinned fallback must not re-run the same route."""
    original = [{"role": "user", "content": "keep-me"}]
    worker = _StalledSummaryWorker(
        [{"role": "user", "content": "unused"}], stall_attempts=1
    )
    timeouts = []
    entry = dict(CHAIN_ENTRY, timeout=0.05)

    try:
        with _patch_chain([CHAIN_ENTRY]):
            with pin_summary_route(entry):
                msgs, prompt = run_compress_context_with_progress_timeout(
                    worker=worker,
                    messages=original,
                    system_prompt_fallback="degraded-prompt",
                    idle_timeout_seconds=0.05,
                    total_ceiling_seconds=0.2,
                    stall_fallback=False,
                    on_timeout=lambda *args: timeouts.append(args),
                )
    finally:
        worker.release.set()

    assert worker.attempts == 1, "no chained retry on the same fallback route"
    assert msgs is original
    assert prompt == "degraded-prompt"
    assert len(timeouts) == 1, "degrade via the shared ladder exactly once"


# ---------------------------------------------------------------------------
# Non-regression: healthy routes behave exactly as before
# ---------------------------------------------------------------------------


def test_healthy_route_never_fires_escalation_hook():
    compressed = [{"role": "user", "content": "summary"}]
    worker = _StalledSummaryWorker(compressed, stall_attempts=0)
    stalls = []

    try:
        msgs, prompt = _run(
            worker, chain=[CHAIN_ENTRY], timeouts=[], on_stall=lambda: stalls.append("stall")
        )
    finally:
        worker.release.set()

    assert worker.attempts == 1, "a healthy route is not retried"
    assert worker.routes[0] is None, "a healthy route is never pinned"
    assert msgs == compressed
    assert prompt == "summarized-prompt"
    assert stalls == [], "no stall, no escalation hook"


def test_healthy_route_keeps_ledger_untouched(tmp_path):
    db = _db(tmp_path)
    db.create_session("s1", source="cli")
    compressor = _compressor(db, "s1")
    worker = _StalledSummaryWorker(
        [{"role": "user", "content": "summary"}], stall_attempts=0
    )
    try:
        _run(worker, chain=[CHAIN_ENTRY], timeouts=[])
    finally:
        worker.release.set()
    assert compressor._primary_stall_streak == 0
    assert compressor.should_skip_primary_route() is False
    stored = db.get_session_model_config_value("s1", "primary_stall_streak")
    assert stored in (None, 0), (
        "the durable ledger is untouched on a healthy route"
    )


# ---------------------------------------------------------------------------
# Telemetry: a half-dead primary is visible in the compression stream
# ---------------------------------------------------------------------------


def test_stall_recovery_emits_route_specific_telemetry(caplog):
    compressor = _gate_tripped_compressor()
    agent = SimpleNamespace(context_compressor=compressor)
    worker = _StalledSummaryWorker([{"role": "user", "content": "summary"}])

    with caplog.at_level(logging.INFO, logger="agent.conversation_compression"):
        try:
            _run(
                worker,
                chain=[CHAIN_ENTRY],
                timeouts=[],
                on_stall=lambda: None,
                telemetry_agent=agent,
            )
        finally:
            worker.release.set()

    stall_lines = [
        r.message
        for r in caplog.records
        if "compression attempt telemetry" in r.message
        and "primary_route_stall_recovered" in r.message
    ]
    assert stall_lines, "the recovered stall must be visible in the telemetry stream"
    assert any('"primary_stall_streak":2' in line for line in stall_lines), (
        "the telemetry line must carry the route-specific ledger count"
    )


# ---------------------------------------------------------------------------
# Compressor ledger: gate, re-probe, reset
# ---------------------------------------------------------------------------


def test_streak_below_threshold_does_not_skip():
    cc = _compressor()
    cc.record_primary_route_stall_recovered()
    assert cc._primary_stall_streak == 1
    assert cc.should_skip_primary_route() is False


def test_skip_window_expiry_reprobes_primary():
    """After the window lapses the gate drops, re-probing the real primary."""
    cc = _gate_tripped_compressor()
    assert cc.should_skip_primary_route() is True

    # Window lapses (simulated): the next compression probes the primary again.
    cc._primary_stall_skip_until = time.monotonic() - 1.0
    assert cc.should_skip_primary_route() is False, (
        "an expired skip window must re-probe the primary"
    )

    # The probe stalls and the fallback recovers it: window re-arms, gate back.
    cc.record_primary_route_stall_recovered()
    assert cc._primary_stall_streak == 3
    assert cc.should_skip_primary_route() is True, (
        "a re-probe stall must re-arm a fresh skip window"
    )


def test_unpinned_summary_success_resets_ledger():
    """A primary attempt that produces tokens clears the ledger."""
    compressor = _gate_tripped_compressor()
    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _ok_response()

    with patch("agent.context_compressor.call_llm", side_effect=_fake_call_llm):
        summary = compressor._generate_summary(_msgs())

    assert summary and "SUMMARY BODY" in summary
    assert "provider" not in calls[0], "an unpinned call is the real primary"
    assert compressor._primary_stall_streak == 0
    assert compressor._primary_stall_skip_until == 0.0
    assert compressor.should_skip_primary_route() is False


def test_pinned_summary_success_keeps_ledger():
    """A pre-pinned fallback success is NOT proof the primary recovered."""
    compressor = _gate_tripped_compressor()
    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        return _ok_response()

    with patch("agent.context_compressor.call_llm", side_effect=_fake_call_llm):
        with pin_summary_route(dict(CHAIN_ENTRY)):
            summary = compressor._generate_summary(_msgs())

    assert summary
    assert calls[0]["provider"] == "custom", "the pin reached the summary call"
    assert compressor._primary_stall_streak == 2, (
        "a fallback success must not clear the primary-stall ledger"
    )


# ---------------------------------------------------------------------------
# Persistence: restart must not disarm mid-escalation (#95879 constraint)
# ---------------------------------------------------------------------------


def test_primary_stall_ledger_roundtrips_and_rearms(tmp_path):
    db = _db(tmp_path)
    db.create_session("s1", source="cli")

    first = _compressor(db, "s1")
    first.record_primary_route_stall_recovered()
    first.record_primary_route_stall_recovered()
    assert first._primary_stall_streak == 2
    assert db.get_session_model_config_value("s1", "primary_stall_streak") == 2
    stored_until = db.get_session_model_config_value("s1", "primary_stall_skip_until")
    assert isinstance(stored_until, (int, float)) and stored_until > time.time()

    # Process restart: a fresh compressor inherits the tripped gate.
    second = _compressor(db, "s1")
    assert second._primary_stall_streak == 2
    assert second.should_skip_primary_route() is True, (
        "a fresh compressor on a resumed session must inherit the skip window"
    )


def test_primary_success_resets_ledger_durably(tmp_path):
    db = _db(tmp_path)
    db.create_session("s1", source="cli")

    cc = _compressor(db, "s1")
    cc.record_primary_route_stall_recovered()
    cc.record_primary_route_stall_recovered()
    assert cc.should_skip_primary_route() is True

    cc.record_primary_route_success()
    assert cc._primary_stall_streak == 0
    assert db.get_session_model_config_value("s1", "primary_stall_streak") == 0

    fresh = _compressor(db, "s1")
    assert fresh.should_skip_primary_route() is False


def test_rebind_to_other_session_does_not_leak_ledger(tmp_path):
    db = _db(tmp_path)
    db.create_session("hot", source="cli")
    db.create_session("cold", source="cli")

    cc = _compressor(db, "hot")
    cc.record_primary_route_stall_recovered()
    cc.record_primary_route_stall_recovered()
    assert cc.should_skip_primary_route() is True

    cc.bind_session_state(db, "cold")
    assert cc._primary_stall_streak == 0
    assert cc.should_skip_primary_route() is False


def test_session_rotation_carries_primary_ledger(tmp_path):
    """A compression rotation must not silently heal a half-dead primary."""
    db = _db(tmp_path)
    db.create_session("parent", source="cli")
    db.create_session("child", source="cli")

    cc = _compressor(db, "parent")
    cc.record_primary_route_stall_recovered()
    cc.record_primary_route_stall_recovered()
    assert cc.should_skip_primary_route() is True

    rotated = _compressor(db, "child")  # fresh child row: zeros
    rotated.on_session_start(
        "child",
        boundary_reason="compression",
        old_session_id="parent",
        session_db=db,
    )
    assert rotated._primary_stall_streak == 2
    assert rotated.should_skip_primary_route() is True

    # The carried ledger was persisted onto the child row.
    restarted = _compressor(db, "child")
    assert restarted._primary_stall_streak == 2