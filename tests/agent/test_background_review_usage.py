"""Background-review usage attribution.

Background-review forks run with ``_session_db = None`` (persistence
isolation), so their provider-billed API calls were never recorded in
``session_model_usage``. ``_record_review_usage_to_parent`` closes that gap
by snapshotting the fork's in-memory counters and recording them against the
parent session via the aux-accounting chokepoint, which writes only
``session_model_usage`` (never the transcript or the ``sessions`` summary
row).

Tests run against the real ``SessionDB`` (tmp file) wherever the wrapper's
guard logic permits, mirroring ``tests/hermes_state/test_aux_usage_accounting.py``.
"""

import pytest

from agent import background_review
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    return SessionDB(tmp_path / "state.db")


def _usage_rows(db, session_id):
    with db._lock:
        rows = db._conn.execute(
            "SELECT * FROM session_model_usage WHERE session_id = ? ORDER BY task",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


class _FakeParent:
    def __init__(self, session_db, session_id="sess-parent"):
        self._session_db = session_db
        self.session_id = session_id


class _FakeReview:
    """Minimal fork stand-in carrying the counters conversation_loop sets."""

    def __init__(self, **counters):
        for k, v in counters.items():
            setattr(self, k, v)


def _review_agent(calls=5, cache=190000, model="test-model"):
    return _FakeReview(
        model=model,
        provider="test-provider",
        base_url="https://example.invalid/v1",
        session_input_tokens=12000,
        session_output_tokens=2400,
        session_cache_read_tokens=cache,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_api_calls=calls,
        session_estimated_cost_usd=0.05,
    )


def test_records_fork_usage_against_parent_session(db):
    db.create_session("sess-parent", source="cli")

    background_review._record_review_usage_to_parent(_FakeParent(db), _review_agent())

    rows = _usage_rows(db, "sess-parent")
    assert len(rows) == 1
    r = rows[0]
    assert r["task"] == "background_review"
    assert r["model"] == "test-model"
    assert r["billing_provider"] == "test-provider"
    assert r["input_tokens"] == 12000
    assert r["output_tokens"] == 2400
    assert r["cache_read_tokens"] == 190000
    assert r.get("estimated_cost_usd") == 0.05


def test_accumulates_repeated_forks_same_model(db):
    """Same task+model upserts into ONE row with summed counters."""
    db.create_session("sess-parent", source="cli")
    parent = _FakeParent(db)

    background_review._record_review_usage_to_parent(parent, _review_agent(calls=5))
    background_review._record_review_usage_to_parent(parent, _review_agent(calls=7))

    rows = _usage_rows(db, "sess-parent")
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 24000


def test_noop_when_fork_made_no_calls(db):
    db.create_session("sess-parent", source="cli")

    background_review._record_review_usage_to_parent(
        _FakeParent(db), _FakeReview(model="m")  # no counters set -> zero usage
    )

    assert _usage_rows(db, "sess-parent") == []


def test_noop_when_parent_has_no_session_db():
    # No DB to write to — must not raise (best-effort by contract).
    background_review._record_review_usage_to_parent(_FakeParent(None), _review_agent())
    assert True


def test_noop_when_parent_has_no_session_id(db):
    db.create_session("sess-parent", source="cli")

    background_review._record_review_usage_to_parent(
        _FakeParent(db, session_id=""), _review_agent()
    )

    assert _usage_rows(db, "sess-parent") == []


def test_survives_accounting_failure():
    class _BoomDB:
        def record_auxiliary_usage(self, *args, **kwargs):
            raise RuntimeError("simulated accounting failure")

    # Must swallow the failure, never raise into the review thread.
    background_review._record_review_usage_to_parent(_FakeParent(_BoomDB()), _review_agent())
    assert True
