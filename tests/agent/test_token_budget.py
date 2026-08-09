"""Behavior tests for the global daily LLM token budget ledger.

Covers the reserve → settle / release contract on a real SQLite ``state.db``
(no mocks — the whole point of the ledger is that it is shared across
processes, so it is exercised through two independent ledger handles on the
same file, which is what two Hermes processes look like from SQLite's side).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from agent import token_budget as tb


@pytest.fixture
def settings():
    return tb.BudgetSettings(daily_tokens=1000, timezone="UTC")


@pytest.fixture
def ledger(tmp_path):
    return tb.DailyTokenBudget(db_path=tmp_path / "state.db")


# ── Settings resolution ─────────────────────────────────────────────────────


def test_missing_budget_section_disables_the_feature(tmp_path, monkeypatch):
    """No `budget:` in config.yaml → disabled, and disabled means untouched DB."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    resolved = tb.load_budget_settings()
    assert resolved.enabled is False
    assert resolved.daily_tokens == 0


def test_settings_read_from_config_yaml(tmp_path, monkeypatch):
    """`budget.daily_tokens` / `budget.timezone` come off config.yaml."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "budget:\n  daily_tokens: 2000000\n  timezone: 'America/Sao_Paulo'\n",
        encoding="utf-8",
    )
    resolved = tb.load_budget_settings()
    assert resolved.daily_tokens == 2_000_000
    assert resolved.timezone == "America/Sao_Paulo"
    assert resolved.enabled is True


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 0),
        (0, 0),
        (-5, 0),
        (True, 0),
        ("2_000_000", 2_000_000),
        ("1,500", 1_500),
        ("not-a-number", 0),
        ({"nested": 1}, 0),
        (1500.9, 1500),
        # Non-finite values: YAML's `.inf` / `.nan` parse into real floats, and
        # int() raises OverflowError / ValueError on them. "No limit" is 0 here.
        (float("inf"), 0),
        (float("-inf"), 0),
        (float("nan"), 0),
        ("inf", 0),
        ("-Infinity", 0),
        ("nan", 0),
        ("1e400", 0),  # overflows to inf while parsing
    ],
)
def test_daily_tokens_coercion_never_raises(raw, expected):
    """A malformed budget value disables the ceiling; it never breaks a turn."""
    assert tb._coerce_daily_tokens(raw) == expected


def test_a_huge_int_budget_is_not_rounded_through_float():
    """An absurd-but-valid int must survive exactly, not overflow to disabled."""
    huge = 10**30
    assert tb._coerce_daily_tokens(huge) == huge


def test_invalid_timezone_falls_back_to_the_hermes_clock():
    """A bogus IANA zone must not crash the day-key computation."""
    day = tb.current_day(tb.BudgetSettings(daily_tokens=10, timezone="Mars/Olympus"))
    assert len(day) == 10 and day.count("-") == 2


def test_day_key_follows_the_configured_timezone():
    """Two zones on opposite sides of the date line get different day keys."""
    a = tb.current_day(tb.BudgetSettings(daily_tokens=1, timezone="Pacific/Kiritimati"))
    b = tb.current_day(tb.BudgetSettings(daily_tokens=1, timezone="Pacific/Niue"))
    assert a >= b  # Kiritimati (+14) is never behind Niue (-11)


# ── Reserve / settle / release ──────────────────────────────────────────────


def test_disabled_budget_short_circuits_without_touching_the_db(tmp_path):
    """The default install must pay nothing on the request path."""
    db = tmp_path / "state.db"
    ledger = tb.DailyTokenBudget(db_path=db)
    outcome = ledger.reserve(500, settings=tb.BudgetSettings(daily_tokens=0))
    assert outcome.status == "disabled"
    assert outcome.reservation is None
    assert not db.exists()


def test_settle_charges_the_actual_not_the_estimate(ledger, settings):
    """A generous estimate is corrected by the provider's real count."""
    granted = ledger.reserve(900, settings=settings)
    assert granted.status == "granted"

    ledger.settle(granted.reservation, 120, settings=settings)

    snap = ledger.snapshot(settings=settings)
    assert snap.used == 120
    assert snap.reserved == 0
    assert snap.remaining == 880


def test_settle_charges_an_underestimate_in_full(ledger, settings):
    """Spending more than reserved is recorded honestly, even past the limit."""
    granted = ledger.reserve(100, settings=settings)
    ledger.settle(granted.reservation, 1400, settings=settings)

    snap = ledger.snapshot(settings=settings)
    assert snap.used == 1400
    assert snap.remaining == 0
    assert snap.exhausted is True


def test_release_frees_the_claim_without_charging(ledger, settings):
    """A failed call gives its tokens back — an unsettled reservation is not spend."""
    granted = ledger.reserve(800, settings=settings)
    assert ledger.snapshot(settings=settings).reserved == 800

    ledger.release(granted.reservation)

    snap = ledger.snapshot(settings=settings)
    assert snap.used == 0
    assert snap.reserved == 0
    assert snap.remaining == 1000


def test_reserve_is_denied_when_the_estimate_does_not_fit(ledger, settings):
    granted = ledger.reserve(600, settings=settings)
    ledger.settle(granted.reservation, 600, settings=settings)

    denied = ledger.reserve(500, settings=settings)

    assert denied.denied is True
    assert denied.reservation is None
    assert "600" in denied.message and "1,000" in denied.message
    # A denial must not consume anything.
    assert ledger.snapshot(settings=settings).reserved == 0


def test_an_in_flight_reservation_blocks_a_concurrent_process(tmp_path, settings):
    """Two handles on one state.db cannot both be granted the last tokens.

    This is the cross-process invariant: the second Hermes process sees the
    first one's outstanding reservation, not just its committed spend.
    """
    db = tmp_path / "state.db"
    process_a = tb.DailyTokenBudget(db_path=db)
    process_b = tb.DailyTokenBudget(db_path=db)

    a = process_a.reserve(700, settings=settings)
    b = process_b.reserve(700, settings=settings)

    assert a.status == "granted"
    assert b.denied is True

    # Once A settles cheaply, B fits.
    process_a.settle(a.reservation, 10, settings=settings)
    assert process_b.reserve(700, settings=settings).status == "granted"


def test_an_old_reservation_is_never_freed_by_a_timer(tmp_path, ledger, settings):
    """No reclamation timer: an unreleased claim keeps holding today's budget.

    This is the deliberate tradeoff. Freeing a claim we cannot prove is
    finished would let that call settle into a row that no longer exists and,
    with exactly-once settle, drop its spend — so the day would keep admitting
    attempts against budget already gone. Losing capacity until midnight is
    the safe failure.
    """
    import sqlite3

    ledger.reserve(900, settings=settings)
    # Backdate the row well past any plausible call duration.
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "UPDATE llm_budget_reservations SET created_at = ?",
        (time.time() - tb.ABANDONED_ROW_MAX_AGE_SECONDS * 10,),
    )
    conn.commit()
    conn.close()

    assert ledger.snapshot(settings=settings).reserved == 900
    assert ledger.reserve(900, settings=settings).denied is True
    assert ledger.snapshot(settings=settings).reserved == 900


def test_a_status_read_never_mutates_the_ledger(tmp_path, ledger, settings):
    """`/usage` is a read. Rendering it must not free anyone's live claim."""
    import sqlite3

    granted = ledger.reserve(900, settings=settings)
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "UPDATE llm_budget_reservations SET created_at = ?",
        (time.time() - tb.ABANDONED_ROW_MAX_AGE_SECONDS * 10,),
    )
    conn.commit()
    conn.close()

    for _ in range(3):
        assert ledger.snapshot(settings=settings).reserved == 900
        tb.daily_budget_lines(snapshot=ledger.snapshot(settings=settings))

    # The claim is still settleable — the reads did not destroy it.
    ledger.settle(granted.reservation, 300, settings=settings)
    assert ledger.snapshot(settings=settings).used == 300


def test_abandoned_rows_from_past_days_are_swept(tmp_path, ledger, settings):
    """Table hygiene: a day-old row from another day is dropped on the next write.

    It counts against no live budget (wrong day) and cannot be in flight (a
    day old), so deleting it is free. Bounded growth without touching today.
    """
    import sqlite3

    ledger.reserve(1, settings=settings)  # let the ledger create the schema

    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "INSERT INTO llm_budget_reservations VALUES (?, ?, ?, ?, ?)",
        ("ancient", "1999-12-31", 500, time.time() - tb.ABANDONED_ROW_MAX_AGE_SECONDS - 60, 1),
    )
    # Same past day, but recent — a call that just crossed midnight. Keep it.
    conn.execute(
        "INSERT INTO llm_budget_reservations VALUES (?, ?, ?, ?, ?)",
        ("midnight-crosser", "1999-12-31", 500, time.time(), 1),
    )
    conn.commit()
    conn.close()

    ledger.reserve(100, settings=settings)  # a write path — sweeps

    conn = sqlite3.connect(tmp_path / "state.db")
    ids = {r[0] for r in conn.execute("SELECT id FROM llm_budget_reservations")}
    conn.close()
    assert "ancient" not in ids
    assert "midnight-crosser" in ids


def test_settle_is_exactly_idempotent(ledger, settings):
    """Settling twice must charge once. This is the double-billing regression.

    Every repeat shape is covered: the same reservation settled again, a
    reservation that was released first, and an id that was never in the
    table at all. Only the call that removes the claim row may add usage.
    """
    granted = ledger.reserve(400, settings=settings)

    first = ledger.settle(granted.reservation, 300, settings=settings)
    assert first.snapshot.used == 300

    # Same reservation, settled again — and again with a different count.
    second = ledger.settle(granted.reservation, 300, settings=settings)
    third = ledger.settle(granted.reservation, 999, settings=settings)

    assert ledger.snapshot(settings=settings).used == 300
    assert second.snapshot.used == 300
    assert third.snapshot.used == 300
    # A no-op settle must not announce thresholds either.
    assert second.crossed_marks == []
    assert third.crossed_marks == []


def test_settle_after_release_charges_nothing(ledger, settings):
    """A released claim is spent-nothing by definition; settling it adds nothing."""
    granted = ledger.reserve(100, settings=settings)
    ledger.release(granted.reservation)

    ledger.settle(granted.reservation, 250, settings=settings)

    assert ledger.snapshot(settings=settings).used == 0


def test_settle_of_an_unknown_reservation_id_charges_nothing(ledger, settings):
    """A fabricated or stale id must never be able to inflate a day."""
    ledger.settle(
        tb.Reservation(id="never-existed", day=tb.current_day(settings), tokens=0),
        5000,
        settings=settings,
    )
    assert ledger.snapshot(settings=settings).used == 0


def test_repeat_settles_cannot_be_used_to_inflate_a_day(tmp_path, settings):
    """Cross-process: a re-settle from another process is a no-op there too."""
    db = tmp_path / "state.db"
    process_a = tb.DailyTokenBudget(db_path=db)
    process_b = tb.DailyTokenBudget(db_path=db)

    granted = process_a.reserve(500, settings=settings)
    process_a.settle(granted.reservation, 500, settings=settings)

    for _ in range(5):
        process_b.settle(granted.reservation, 500, settings=settings)

    assert process_b.snapshot(settings=settings).used == 500


def test_ledger_survives_the_day_boundary(ledger, settings):
    """Yesterday's spend does not eat today's budget."""
    yesterday = tb.Reservation(id="old", day="1999-12-31", tokens=0)
    ledger.settle(yesterday, 999, settings=settings)

    snap = ledger.snapshot(settings=settings)
    assert snap.used == 0
    assert snap.remaining == 1000


# ── Threshold notifications ─────────────────────────────────────────────────


def test_thresholds_fire_once_per_day_across_processes(tmp_path, settings):
    """50%/75% are claimed in the settle transaction — exactly one announcer."""
    db = tmp_path / "state.db"
    process_a = tb.DailyTokenBudget(db_path=db)
    process_b = tb.DailyTokenBudget(db_path=db)

    first = process_a.settle(
        process_a.reserve(500, settings=settings).reservation, 500, settings=settings
    )
    assert first.crossed_marks == [50]

    # A different process pushes past 75%: it announces 75, never 50 again.
    second = process_b.settle(
        process_b.reserve(300, settings=settings).reservation, 300, settings=settings
    )
    assert second.crossed_marks == [75]

    third = process_a.settle(
        process_a.reserve(50, settings=settings).reservation, 50, settings=settings
    )
    assert third.crossed_marks == []


def test_a_single_jump_claims_every_mark_it_passed(ledger, settings):
    """One big call that vaults from 0% to 80% still reports both marks."""
    granted = ledger.reserve(800, settings=settings)
    outcome = ledger.settle(granted.reservation, 800, settings=settings)
    assert outcome.crossed_marks == [50, 75]


def test_threshold_message_names_the_mark_and_the_remainder(settings):
    snap = tb.BudgetSnapshot(day="2026-08-09", limit=1000, used=760, reserved=0, timezone="UTC")
    msg = tb.threshold_message(snap, 75)
    assert "75%" in msg
    assert "760" in msg
    assert "240" in msg


# ── Rendering ───────────────────────────────────────────────────────────────


def test_no_budget_configured_renders_nothing(tmp_path, monkeypatch):
    """`/usage` surfaces extend unconditionally, so this must be empty."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    tb.reset_ledger_cache()
    assert tb.daily_budget_lines() == []


def test_rendered_block_reports_used_remaining_and_reset_zone():
    snap = tb.BudgetSnapshot(
        day="2026-08-09", limit=2000, used=500, reserved=100, timezone="America/Sao_Paulo"
    )
    lines = tb.daily_budget_lines(snapshot=snap)
    body = "\n".join(lines)

    assert "Daily LLM Budget" in lines[0] and "2026-08-09" in lines[0]
    assert "25%" in body
    assert "500" in body and "2,000" in body
    assert "1,400" in body  # remaining excludes the in-flight reservation
    assert "100" in body  # the in-flight figure is disclosed, not hidden
    assert "America/Sao_Paulo" in body


def test_markdown_rendering_only_changes_emphasis():
    snap = tb.BudgetSnapshot(day="2026-08-09", limit=100, used=10, reserved=0)
    plain = tb.daily_budget_lines(snapshot=snap)
    md = tb.daily_budget_lines(snapshot=snap, markdown=True)
    assert len(plain) == len(md)
    assert "**Daily LLM Budget**" in md[0]
    assert "**" not in plain[0]


# ── Agent-loop wiring ───────────────────────────────────────────────────────


def test_release_helper_frees_the_agents_claim(tmp_path, monkeypatch):
    """``_release_agent_budget_reservation`` drops the claim and clears the slot.

    This is the helper the loop calls at every attempt and iteration boundary
    and that ``AIAgent.run_conversation``'s ``finally`` calls on abort paths.
    Since the ledger runs no reclamation timer, owner-release is the only way
    a dead turn's budget ever comes back before midnight.
    """
    from agent.conversation_loop import _release_agent_budget_reservation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "budget:\n  daily_tokens: 1000\n  timezone: 'UTC'\n", encoding="utf-8"
    )
    tb.reset_ledger_cache()

    class _Agent:
        pass

    agent = _Agent()
    agent._budget_reservation = tb.reserve(400).reservation
    assert tb.snapshot().reserved == 400

    _release_agent_budget_reservation(agent)

    assert agent._budget_reservation is None
    assert tb.snapshot().reserved == 0
    # Idempotent: a second release (loop boundary + turn end) is a no-op.
    _release_agent_budget_reservation(agent)
    assert tb.snapshot().used == 0
    tb.reset_ledger_cache()


def test_conversation_loop_reserves_before_the_call_and_settles_on_the_response():
    """Wiring pin: reserve precedes the provider call, settle follows it."""
    import inspect

    import agent.conversation_loop as cl

    src = inspect.getsource(cl.run_conversation)
    reserve_idx = src.index("from agent.token_budget import reserve as _budget_reserve")
    call_idx = src.index("api_kwargs = agent._build_api_kwargs(api_messages)")
    settle_idx = src.index("_settle_agent_budget_reservation(agent, response)")
    assert reserve_idx < call_idx < settle_idx
    # Released at the attempt, iteration, and turn boundaries, so a call that
    # never got a response cannot carry its claim into the next request.
    assert src.count("_release_agent_budget_reservation(agent)") >= 3


def test_the_settle_precedes_every_response_driven_exit():
    """Wiring pin: the charge lands before the loop can bail on the response.

    The truncation, refusal, invalid-response, and tool-call-retry branches all
    ``return``/``continue``/``break`` out of the attempt. If settle sat after
    them (it used to sit with the usage bookkeeping, far below), each of those
    responses would reach a release instead and be booked as free.
    """
    import inspect

    import agent.conversation_loop as cl

    src = inspect.getsource(cl.run_conversation)
    settle_idx = src.index("_settle_agent_budget_reservation(agent, response)")
    for response_driven_branch in (
        "if _redirect_crossed_response:",
        "if response_invalid:",
        'if finish_reason == "content_filter":',
        'if finish_reason == "length":',
        "if hasattr(response, 'usage') and response.usage:",
    ):
        assert settle_idx < src.index(response_driven_branch), response_driven_branch


def test_the_reservation_is_scoped_to_each_physical_provider_attempt():
    """Wiring pin: reserve/release sit INSIDE the retry loop, not around it.

    A retry is another real request to the provider and burns real tokens.
    Reserving once around ``while retry_count < max_retries`` would let N
    attempts spend against a single claim, so the estimate must be re-taken
    (and the previous claim released) on every pass.
    """
    import inspect

    import agent.conversation_loop as cl

    src = inspect.getsource(cl.run_conversation)
    retry_loop_idx = src.index("while retry_count < max_retries:")
    reserve_idx = src.index("from agent.token_budget import reserve as _budget_reserve")
    # The per-attempt release must precede the reserve inside that loop.
    release_idx = src.index("_release_agent_budget_reservation(agent)", retry_loop_idx)
    assert retry_loop_idx < release_idx < reserve_idx

    # And the reserve must be inside the loop body, i.e. indented deeper than
    # the `while` itself — a same-level reserve would be after the loop.
    reserve_line = src[: src.index("\n", reserve_idx)].rsplit("\n", 1)[-1]
    while_line = src[: src.index("\n", retry_loop_idx)].rsplit("\n", 1)[-1]
    indent = lambda line: len(line) - len(line.lstrip())  # noqa: E731
    assert indent(reserve_line) > indent(while_line)


@pytest.fixture
def budget_profile(tmp_path, monkeypatch):
    """A profile with a 10,000-token/day budget, addressed through the wrappers.

    The loop helpers go through the module-level ``reserve``/``settle`` API,
    which resolves the ledger from ``HERMES_HOME`` — so the wiring is exercised
    exactly as the agent loop exercises it, on a real state.db.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "budget:\n  daily_tokens: 10000\n  timezone: 'UTC'\n", encoding="utf-8"
    )
    tb.reset_ledger_cache()
    yield tmp_path
    tb.reset_ledger_cache()


class _LoopAgent:
    """The slice of the agent that the budget helpers actually touch."""

    provider = "openai"
    api_mode = "chat_completions"

    def __init__(self) -> None:
        self._budget_reservation = None
        self.status: list[str] = []

    def _emit_status(self, message: str) -> None:
        self.status.append(message)


def _usage_response(prompt_tokens: int, completion_tokens: int):
    """A provider response carrying OpenAI-shaped usage."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    )


def test_a_truncated_response_with_usage_is_charged_before_the_retry(budget_profile):
    """A truncated answer is spend, and the continuation retry must not undo it.

    ``finish_reason="length"`` sends the loop back for a continuation without
    ever reaching the usage bookkeeping, so the charge has to land the moment
    the response arrives. Otherwise the per-attempt release at the top of the
    next attempt hands the truncated call's tokens back and a model that
    truncates on every pass can run all day for free.
    """
    from agent.conversation_loop import (
        _release_agent_budget_reservation,
        _settle_agent_budget_reservation,
    )

    agent = _LoopAgent()

    # Attempt 1 — reserve generously, get a truncated response reporting 1,000.
    agent._budget_reservation = tb.reserve(4000).reservation
    assert tb.snapshot().reserved == 4000
    _settle_agent_budget_reservation(agent, _usage_response(900, 100))

    assert agent._budget_reservation is None
    assert tb.snapshot().used == 1000  # the actual, not the 4,000 estimate
    assert tb.snapshot().reserved == 0

    # Attempt 2 — the continuation. The loop's per-attempt release runs first
    # and must find nothing to give back; the new claim stacks on real spend.
    _release_agent_budget_reservation(agent)
    assert tb.snapshot().used == 1000

    agent._budget_reservation = tb.reserve(4000).reservation
    _settle_agent_budget_reservation(agent, _usage_response(1500, 500))

    assert tb.snapshot().used == 3000
    assert tb.snapshot().reserved == 0


def test_a_response_without_usage_is_charged_at_the_reservation(budget_profile):
    """No usage reported is not a free call — charge the estimate, never zero.

    Plenty of providers and streaming stubs answer without a usage block. If
    those settled at zero (or were released), an unmetered provider would spend
    the whole day unmetered.
    """
    from agent.conversation_loop import _settle_agent_budget_reservation

    agent = _LoopAgent()

    # A perfectly successful response that simply carries no usage block.
    agent._budget_reservation = tb.reserve(2500).reservation
    _settle_agent_budget_reservation(
        agent, SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")])
    )

    assert agent._budget_reservation is None
    assert tb.snapshot().used == 2500
    assert tb.snapshot().reserved == 0

    # A usage block that reports nothing is the same case, not a free call.
    agent._budget_reservation = tb.reserve(1500).reservation
    _settle_agent_budget_reservation(agent, _usage_response(0, 0))

    assert tb.snapshot().used == 4000
    assert tb.snapshot().reserved == 0


def test_only_an_attempt_without_a_response_gives_its_tokens_back(budget_profile):
    """``response is None`` is the one shape that leaves the claim releasable."""
    from agent.conversation_loop import (
        _release_agent_budget_reservation,
        _settle_agent_budget_reservation,
    )

    agent = _LoopAgent()
    agent._budget_reservation = tb.reserve(3000).reservation

    # The transport raised / the request was cancelled before any answer.
    _settle_agent_budget_reservation(agent, None)
    assert agent._budget_reservation is not None  # still the owner's to release
    assert tb.snapshot().used == 0

    _release_agent_budget_reservation(agent)
    assert tb.snapshot().used == 0
    assert tb.snapshot().reserved == 0


def test_agent_forwarder_releases_the_claim_on_abort_paths():
    """Wiring pin: the turn's ``finally`` releases, covering raises/early returns."""
    import inspect

    import run_agent

    src = inspect.getsource(run_agent.AIAgent.run_conversation)
    assert "_release_agent_budget_reservation(self)" in src
    assert src.index("finally:") < src.index("_release_agent_budget_reservation(self)")


# ── Fail-open ───────────────────────────────────────────────────────────────


def test_an_unusable_ledger_never_blocks_a_call(tmp_path, settings, monkeypatch):
    """A budget we cannot read must fail OPEN — never brick the agent."""
    ledger = tb.DailyTokenBudget(db_path=tmp_path / "state.db")

    def _boom(*_a, **_kw):
        raise OSError("disk gone")

    monkeypatch.setattr(ledger, "_connect", _boom)

    outcome = ledger.reserve(100, settings=settings)
    assert outcome.status == "disabled"
    assert outcome.denied is False
    # Settle/release/snapshot degrade quietly rather than raising.
    assert ledger.settle(tb.Reservation("x", "2026-08-09", 1), 5, settings=settings).snapshot is None
    ledger.release(tb.Reservation("x", "2026-08-09", 1))
    assert ledger.snapshot(settings=settings) is None
