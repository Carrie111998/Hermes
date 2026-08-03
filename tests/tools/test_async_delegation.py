"""Tests for async (background) delegation — tools/async_delegation.py.

Covers the dispatch handle, non-blocking behavior, completion-event delivery
onto the shared process_registry.completion_queue, the rich re-injection block
formatting, capacity rejection, and crash handling.
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tools import async_delegation as ad
from tools.process_registry import (
    ProcessRegistry,
    format_process_notification,
    process_registry,
)


@pytest.fixture(autouse=True)
def _clean_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    # Give just-released workers a beat to finalize BEFORE draining, so their
    # completion events land now instead of leaking into the next test's
    # queue (worker threads push events asynchronously; a drain that races an
    # in-flight _finalize misses it).
    deadline = time.monotonic() + 2.0
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(0.02)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def _drain_one(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def _drain_for(delegation_id, timeout=5.0):
    """Drain until the event for *delegation_id* appears (discarding others).

    Completion events are pushed asynchronously by worker threads, so a
    straggler from a PREVIOUS test can land after that test's teardown drain
    and leak into the current test's queue. Matching on delegation_id makes
    the assertion immune to that cross-test leak.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            evt = process_registry.completion_queue.get_nowait()
            if evt.get("delegation_id") == delegation_id:
                return evt
            continue
        time.sleep(0.02)
    return None


def _runtime_effect(
    *,
    authority="conversation-root-runtime-effect-test",
    baseline=41,
):
    return {
        "schema": "hermes.runtime-effect.v1",
        "kind": "isolated_workspace_may_have_changed.v1",
        "workspace_lease_authority": authority,
        "baseline_edit_generation": baseline,
    }


def _api_execution_context():
    from gateway.api_execution_context import transport_semantic_digest

    route_digest = transport_semantic_digest(
        model="openai/gpt-5",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="",
    )
    effective_digest = transport_semantic_digest(
        model="openai/gpt-5",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_mode="chat_completions",
    )
    return {
        "schema": "hermes.api-detached-execution-context.v1",
        "gateway_session_key": "memory:stable:42",
        "request_model": "alias-42",
        "request_provider": "",
        "model_options": {
            "reasoning": {"enabled": True, "effort": "high"},
            "service_tier": "priority",
        },
        "route_alias": "alias-42",
        "route_model": "openai/gpt-5",
        "route_provider": "openai",
        "route_semantic_sha256": route_digest,
        "session_model": "",
        "confirmed_runtime_lock": False,
        "requested_runtime": {
            "model": "alias-42",
            "provider": "",
        },
        "route_source": "model_routes",
        "effective_model": "openai/gpt-5",
        "effective_provider": "openai",
        "effective_transport_sha256": effective_digest,
    }


def test_api_execution_context_requires_api_origin_session():
    """Only a session-continuable API parent may attach the trusted envelope."""

    with pytest.raises(
        ValueError,
        match="requires the currently bound originating API session",
    ):
        ad.dispatch_async_delegation(
            goal="must not detach",
            context=None,
            toolsets=None,
            role="leaf",
            model=None,
            session_key="not-an-api-origin",
            origin_session_id="",
            runner=lambda: {"status": "completed", "summary": "never"},
            api_execution_context=_api_execution_context(),
        )
    assert ad.active_count() == 0


def test_api_origin_cannot_detach_without_execution_context():
    from gateway.session_context import reset_session_vars, set_session_vars

    set_session_vars(
        platform="api_server",
        chat_id="trusted-api-origin",
    )
    try:
        with pytest.raises(
            ValueError,
            match="requires a durable API execution context",
        ):
            ad.dispatch_async_delegation(
                goal="must stay synchronous",
                context=None,
                toolsets=None,
                role="leaf",
                model=None,
                session_key="trusted-api-origin",
                origin_session_id="trusted-api-origin",
                runner=lambda: {
                    "status": "completed",
                    "summary": "never",
                },
                api_execution_context=None,
            )
    finally:
        reset_session_vars()
    assert ad.active_count() == 0


def test_non_api_origin_may_detach_without_execution_context():
    from gateway.session_context import reset_session_vars, set_session_vars

    set_session_vars(platform="telegram", chat_id="push-chat")
    try:
        result = ad.dispatch_async_delegation(
            goal="push completion",
            context=None,
            toolsets=None,
            role="leaf",
            model=None,
            session_key="push-chat",
            runner=lambda: {
                "status": "completed",
                "summary": "done",
            },
            api_execution_context=None,
        )
    finally:
        reset_session_vars()

    assert result["status"] == "dispatched"


@pytest.mark.parametrize(
    ("platform", "bound_origin", "supplied_origin"),
    [
        ("telegram", "push-chat-id", "push-chat-id"),
        ("api_server", "trusted-api-origin", "different-api-origin"),
    ],
)
def test_api_execution_context_rejects_push_or_mismatched_origin(
    platform,
    bound_origin,
    supplied_origin,
):
    from gateway.session_context import reset_session_vars, set_session_vars

    set_session_vars(platform=platform, chat_id=bound_origin)
    try:
        with pytest.raises(
            ValueError,
            match="requires the currently bound originating API session",
        ):
            ad.dispatch_async_delegation(
                goal="must not detach",
                context=None,
                toolsets=None,
                role="leaf",
                model=None,
                session_key=supplied_origin,
                origin_session_id=supplied_origin,
                runner=lambda: {
                    "status": "completed",
                    "summary": "never",
                },
                api_execution_context=_api_execution_context(),
            )
    finally:
        reset_session_vars()
    assert ad.active_count() == 0


def _durable_in_home(home, delegation_id):
    with ad._delivery_home_scope(home):
        return ad.get_durable_delegation(delegation_id)


def _persist_terminal_in_home(
    home,
    delegation_id,
    *,
    delivery_store=None,
):
    record = {
        "delegation_id": delegation_id,
        "session_key": "owner",
        "origin_ui_session_id": "",
        "parent_session_id": "parent",
        "dispatched_at": 1.0,
    }
    if delivery_store is not None:
        record["_delivery_store"] = delivery_store
    event = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": "owner",
        "parent_session_id": "parent",
        "status": "completed",
        "completed_at": 2.0,
    }
    with ad._delivery_home_scope(home):
        ad._persist_dispatch(record)
        ad._persist_completion(
            event,
            {"status": "completed", "summary": "done"},
        )
    return event


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ([], "error"),
        ([{"status": "completed"}, {"status": "success"}], "completed"),
        ([{"status": "completed"}, {"status": "timeout"}], "partial"),
        ([{"status": "completed"}, {"status": "failed"}], "partial"),
        ([{"status": "partial"}, {"status": "failed"}], "partial"),
        ([{"status": "partial"}, {"status": "partial"}], "partial"),
        ([{"status": "interrupted"}, {"status": "interrupted"}], "interrupted"),
        ([{"status": "interrupted"}, {"status": "failed"}], "error"),
        ([{"status": "failed"}, {"status": "timeout"}], "partial"),
        ([{"status": "completed", "failed": True}], "error"),
        ([{"status": "interrupted", "failed": True}], "error"),
    ],
)
def test_aggregate_batch_status_requires_every_child_for_completion(
    results, expected
):
    assert ad._aggregate_batch_status(results) == expected


@pytest.mark.parametrize(
    "runner_result",
    [
        {
            "status": "failed",
            "completed": True,
            "summary": "diagnostic completion-shaped text",
        },
        {
            "status": "interrupted",
            "failed": True,
            "summary": "diagnostic interruption-shaped text",
        },
    ],
)
def test_async_single_normalizes_contradictory_terminal_outcome(
    tmp_path,
    monkeypatch,
    runner_result,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    dispatched = ad.dispatch_async_delegation(
        goal="contradictory terminal result",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: dict(runner_result),
        max_async_children=1,
    )
    event = _drain_for(dispatched["delegation_id"])

    assert event is not None
    assert event["status"] == "failed"
    assert event["terminal_outcome_contradictory"] is True
    assert event["summary"] == runner_result["summary"]
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable is not None
    assert durable["state"] == "failed"
    assert durable["event"]["terminal_outcome_contradictory"] is True
    assert durable["result"]["summary"] == runner_result["summary"]
    assert durable["result"]["status"] == runner_result["status"]


def test_async_batch_persists_canonical_per_child_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    combined = {
        "results": [
            {
                "task_index": 0,
                "status": "completed",
                "completed": True,
                "failed": True,
                "summary": "failure diagnostic",
            },
            {
                "task_index": 1,
                "status": "completed",
                "summary": "verified result",
            },
        ],
        "total_duration_seconds": 0.2,
    }

    dispatched = ad.dispatch_async_delegation_batch(
        goals=["first", "second"],
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: combined,
        max_async_children=1,
    )
    event = _drain_for(dispatched["delegation_id"])

    assert event is not None
    assert event["status"] == "partial"
    assert event["terminal_outcome_contradictory"] is True
    assert event["results"][0]["status"] == "failed"
    assert (
        event["results"][0]["terminal_outcome_contradictory"]
        is True
    )
    assert event["results"][0]["summary"] == "failure diagnostic"
    assert event["results"][1]["status"] == "completed"
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable is not None
    assert (
        durable["event"]["results"][0]["terminal_outcome_contradictory"]
        is True
    )
    # The raw child result remains available for diagnostics; only the event
    # status consumed by delivery/rendering is canonicalized.
    assert durable["result"]["results"][0]["status"] == "completed"


def test_mixed_batch_is_partial_in_event_persistence_and_formatter(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    combined = {
        "results": [
            {
                "task_index": 0,
                "status": "completed",
                "summary": "first task complete",
            },
            {
                "task_index": 1,
                "status": "partial",
                "summary": "second task evidence",
            },
        ],
        "total_duration_seconds": 0.2,
    }

    dispatched = ad.dispatch_async_delegation_batch(
        goals=["first", "second"],
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: combined,
        max_async_children=1,
    )
    event = _drain_for(dispatched["delegation_id"])

    assert event is not None
    assert event["status"] == "partial"
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable is not None
    assert durable["state"] == "partial"

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["status"] == "partial"

    text = format_process_notification(event)
    expected_header = (
        "[ASYNC DELEGATION BATCH PARTIALLY COMPLETE — "
        f"{dispatched['delegation_id']}]"
    )
    assert expected_header in text
    assert "Status: partial" in text
    assert "--- ✓ TASK 1/2" in text
    assert "--- ⚠ TASK 2/2" in text


def test_single_partial_formatter_does_not_claim_completion():
    text = format_process_notification(
        {
            "type": "async_delegation",
            "delegation_id": "deleg_partial",
            "status": "partial",
            "goal": "inspect",
            "summary": "useful evidence",
        }
    )

    assert "[ASYNC DELEGATION PARTIALLY COMPLETE — deleg_partial]" in text
    assert "[ASYNC DELEGATION COMPLETE — deleg_partial]" not in text


def test_runtime_effect_durable_restore_round_trip(tmp_path, monkeypatch):
    """The host-only effect survives task/event persistence and restart restore."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    effect = _runtime_effect()
    dispatched = ad.dispatch_async_delegation(
        goal="edit isolated workspace",
        context=None,
        toolsets=["terminal"],
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {"status": "completed", "summary": "done"},
        runtime_effect=effect,
    )
    event = _drain_for(dispatched["delegation_id"])

    assert event is not None
    assert event["runtime_effect"] == effect
    with ad._DB_LOCK, ad._transaction() as conn:
        task_json, event_json = conn.execute(
            """SELECT task_json, event_json FROM async_delegations
               WHERE delegation_id=?""",
            (dispatched["delegation_id"],),
        ).fetchone()
    assert json.loads(task_json)["runtime_effect"] == effect
    assert json.loads(event_json)["runtime_effect"] == effect

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    restored_event = restored.get_nowait()
    assert restored_event["restored"] is True
    assert restored_event["runtime_effect"] == effect


def test_restore_quarantines_malformed_task_json_and_continues(
    tmp_path, monkeypatch
):
    """One corrupt abandoned task cannot abort recovery of its valid sibling."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = time.time()
    for delegation_id in ("deleg_bad_task", "deleg_valid_task"):
        ad._persist_dispatch(
            {
                "delegation_id": delegation_id,
                "session_key": "owner",
                "origin_ui_session_id": "",
                "parent_session_id": "parent",
                "dispatched_at": now,
                "goal": delegation_id,
            }
        )
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            """UPDATE async_delegations
               SET owner_pid=999999999, owner_started_at=NULL"""
        )
        conn.execute(
            """UPDATE async_delegations SET task_json='{'
               WHERE delegation_id='deleg_bad_task'"""
        )

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["delegation_id"] == "deleg_valid_task"
    assert restored.empty()

    bad = ad.get_durable_delegation("deleg_bad_task")
    valid = ad.get_durable_delegation("deleg_valid_task")
    assert bad is not None and bad["state"] == "unknown"
    assert bad["delivery_state"] == "dropped"
    assert valid is not None and valid["state"] == "unknown"
    assert valid["delivery_state"] == "pending"


def test_restore_quarantines_malformed_event_json_and_continues(
    tmp_path, monkeypatch
):
    """One corrupt completion remains inspectable while valid rows restore."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for delegation_id in ("deleg_bad_event", "deleg_valid_event"):
        record = {
            "delegation_id": delegation_id,
            "session_key": "owner",
            "origin_ui_session_id": "",
            "parent_session_id": "parent",
            "dispatched_at": 1.0,
        }
        event = {
            "type": "async_delegation",
            "delegation_id": delegation_id,
            "session_key": "owner",
            "status": "completed",
            "completed_at": 2.0,
        }
        ad._persist_dispatch(record)
        ad._persist_completion(
            event,
            {"status": "completed", "summary": "done"},
        )
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET event_json='['
               WHERE delegation_id='deleg_bad_event'"""
        )

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["delegation_id"] == "deleg_valid_event"
    assert restored.empty()
    assert (
        ad.get_durable_delegation("deleg_bad_event")["delivery_state"]
        == "dropped"
    )
    assert (
        ad.get_durable_delegation("deleg_valid_event")["delivery_state"]
        == "pending"
    )


def test_runtime_effect_is_not_rendered_into_model_notification_text():
    """Trusted transport metadata must never become model-visible prose."""
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_runtime_effect_non_leak",
        "status": "completed",
        "goal": "edit isolated workspace",
        "summary": "done",
        "runtime_effect": _runtime_effect(
            authority="opaque-root-authority-never-render",
            baseline=987654321,
        ),
    }

    rendered = format_process_notification(event)
    without_effect = dict(event)
    without_effect.pop("runtime_effect")

    assert rendered == format_process_notification(without_effect)
    assert "hermes.runtime-effect.v1" not in rendered
    assert "isolated_workspace_may_have_changed.v1" not in rendered
    assert "opaque-root-authority-never-render" not in rendered
    assert "baseline_edit_generation" not in rendered


def test_malformed_persisted_runtime_effect_is_dropped_on_restore(
    tmp_path, monkeypatch,
):
    """A forged/corrupt durable envelope fails closed instead of re-entering."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="edit isolated workspace",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {"status": "completed", "summary": "done"},
        runtime_effect=_runtime_effect(),
    )
    assert _drain_for(dispatched["delegation_id"]) is not None

    with ad._DB_LOCK, ad._transaction() as conn:
        payload = conn.execute(
            """SELECT event_json FROM async_delegations
               WHERE delegation_id=?""",
            (dispatched["delegation_id"],),
        ).fetchone()[0]
        event = json.loads(payload)
        event["runtime_effect"]["forged_extra_field"] = True
        conn.execute(
            """UPDATE async_delegations SET event_json=?
               WHERE delegation_id=?""",
            (json.dumps(event), dispatched["delegation_id"]),
        )

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 0
    assert restored.empty()
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable["delivery_state"] == "dropped"
def test_active_for_session_counts_every_live_delegation_state():
    with ad._records_lock:
        ad._records.update(
            {
                "running": {
                    "status": "running",
                    "origin_ui_session_id": "desktop-sid",
                },
                "stalling": {
                    "status": "stalling",
                    "origin_ui_session_id": "desktop-sid",
                },
                "finalizing": {
                    "status": "finalizing",
                    "origin_ui_session_id": "desktop-sid",
                },
                "completed": {
                    "status": "completed",
                    "origin_ui_session_id": "desktop-sid",
                },
                "other-session": {
                    "status": "running",
                    "origin_ui_session_id": "other-sid",
                },
            }
        )

    assert ad.active_for_session("desktop-sid") == 3
    assert ad.active_for_session("other-sid") == 1
    assert ad.active_for_session("") == 0


def test_dispatch_returns_immediately_without_blocking():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done", "api_calls": 1,
                "duration_seconds": 0.1, "model": "m"}

    t0 = time.monotonic()
    res = ad.dispatch_async_delegation(
        goal="g", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=3,
    )
    elapsed = time.monotonic() - t0

    assert res["status"] == "dispatched"
    assert res["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: dispatch returned while the runner is still
    # gated (active), so it cannot have waited on the gate. The active_count
    # check is the environment-independent proof; the generous wall-clock
    # bound is a loose sanity backstop, not the primary assertion (a loaded
    # CI runner can be slow but never anywhere near the runner's 5s gate).
    assert ad.active_count() == 1
    assert elapsed < 4.0, f"dispatch blocked {elapsed:.2f}s (gate is 5s)"
    gate.set()


def test_async_executor_workers_are_daemon_threads():
    gate = threading.Event()

    def runner():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "done"}

    res = ad.dispatch_async_delegation(
        goal="daemon check", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=runner, max_async_children=1,
    )
    assert res["status"] == "dispatched"

    deadline = time.monotonic() + 2
    worker = None
    while time.monotonic() < deadline:
        worker = next(
            (t for t in threading.enumerate() if t.name.startswith("async-delegate")),
            None,
        )
        if worker is not None:
            break
        time.sleep(0.02)
    assert worker is not None
    assert worker.daemon is True
    gate.set()
    assert _drain_one() is not None


def test_completion_event_lands_on_shared_queue_with_session_key():
    def runner():
        return {"status": "completed", "summary": "the result",
                "api_calls": 3, "duration_seconds": 2.0, "model": "test-model"}

    res = ad.dispatch_async_delegation(
        goal="compute X", context="some context", toolsets=["web", "file"],
        role="leaf", model="test-model", session_key="agent:main:cli:dm:local",
        parent_session_id="20260703_parent_sid",
        runner=runner, max_async_children=3,
    )
    assert res["status"] == "dispatched"

    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["summary"] == "the result"
    assert evt["session_key"] == "agent:main:cli:dm:local"
    assert evt["parent_session_id"] == "20260703_parent_sid"
    assert evt["delegation_id"] == res["delegation_id"]


def test_rich_reinjection_block_is_self_contained():
    def runner():
        return {"status": "completed", "summary": "The answer is 42.",
                "api_calls": 7, "duration_seconds": 3.5, "model": "test-model"}

    ad.dispatch_async_delegation(
        goal="Compute the meaning of life",
        context="User is a philosopher. Respond tersely.",
        toolsets=["web"], role="leaf", model="test-model",
        session_key="", runner=runner, max_async_children=3,
    )
    evt = _drain_one()
    assert evt is not None
    text = format_process_notification(evt)
    assert text is not None
    for needle in [
        "ASYNC DELEGATION COMPLETE",
        "Compute the meaning of life",
        "User is a philosopher",
        "Toolsets: web",
        "The answer is 42.",
        "Status: completed",
        "API calls: 7",
    ]:
        assert needle in text, f"missing {needle!r}"


def test_dispatch_rejected_at_capacity():
    ev = threading.Event()

    def blocker():
        ev.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    for i in range(2):
        r = ad.dispatch_async_delegation(
            goal=f"task{i}", context=None, toolsets=None, role="leaf",
            model="m", session_key="", runner=blocker, max_async_children=2,
        )
        assert r["status"] == "dispatched"

    r3 = ad.dispatch_async_delegation(
        goal="task3", context=None, toolsets=None, role="leaf", model="m",
        session_key="", runner=blocker, max_async_children=2,
    )
    assert r3["status"] == "rejected"
    assert "capacity reached" in r3["error"]
    ev.set()


def test_delegation_ids_use_full_uuid_entropy():
    delegation_id = ad._new_delegation_id()

    assert delegation_id.startswith("deleg_")
    assert len(delegation_id.removeprefix("deleg_")) == 32
    int(delegation_id.removeprefix("deleg_"), 16)


@pytest.mark.parametrize(
    "weak_id",
    [
        "deleg_deadbeef",
        "deleg_" + "g" * 32,
        "caller-controlled",
        "",
    ],
)
def test_batch_rejects_weak_or_malformed_supplied_delegation_id(
    tmp_path,
    monkeypatch,
    weak_id,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ran = threading.Event()
    result = ad.dispatch_async_delegation_batch(
        goals=["must not run"],
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        delegation_id=weak_id,
        runner=lambda: ran.set() or {"results": []},
    )

    assert result["status"] == "rejected"
    assert "128-bit" in result["error"]
    assert not ran.is_set()
    assert ad.active_count() == 0
    assert ad.list_async_delegations() == []


@pytest.mark.parametrize("batch", [False, True])
def test_duplicate_durable_id_is_rejected_without_overwrite(
    tmp_path,
    monkeypatch,
    batch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    duplicate_id = "deleg_" + "a" * 32
    original = {
        "delegation_id": duplicate_id,
        "session_key": "original-owner",
        "origin_ui_session_id": "",
        "parent_session_id": "original-parent",
        "dispatched_at": 1.0,
        "goal": "original",
    }
    ad._persist_dispatch(original)
    ran = threading.Event()

    if batch:
        result = ad.dispatch_async_delegation_batch(
            goals=["replacement"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="replacement-owner",
            delegation_id=duplicate_id,
            runner=lambda: ran.set() or {"results": []},
        )
    else:
        monkeypatch.setattr(ad, "_new_delegation_id", lambda: duplicate_id)
        result = ad.dispatch_async_delegation(
            goal="replacement",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="replacement-owner",
            runner=lambda: ran.set() or {
                "status": "completed",
                "summary": "wrong",
            },
        )

    assert result["status"] == "rejected"
    assert not ran.is_set()
    assert ad.active_count() == 0
    durable = ad.get_durable_delegation(duplicate_id)
    assert durable is not None
    assert durable["origin_session"] == "original-owner"


@pytest.mark.parametrize("batch", [False, True])
def test_dispatch_persistence_failure_rolls_back_exact_admission(
    tmp_path,
    monkeypatch,
    batch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        ad,
        "_persist_dispatch",
        lambda _record: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk unavailable")
        ),
    )
    ran = threading.Event()

    if batch:
        result = ad.dispatch_async_delegation_batch(
            goals=["one", "two"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=lambda: ran.set() or {"results": []},
        )
    else:
        result = ad.dispatch_async_delegation(
            goal="one",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=lambda: ran.set() or {
                "status": "completed",
                "summary": "wrong",
            },
        )

    assert result["status"] == "rejected"
    assert "persist" in result["error"].lower()
    assert not ran.is_set()
    assert ad.active_count() == 0
    assert ad.list_async_delegations() == []


@pytest.mark.parametrize("batch", [False, True])
def test_transient_finalize_persistence_failure_retries_exact_result(
    tmp_path,
    monkeypatch,
    batch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        ad,
        "_FINALIZE_PERSIST_RETRY_DELAYS_SECONDS",
        (0.0, 0.0, 0.0),
    )
    original_persist = ad._persist_completion
    calls = {"count": 0}

    def flaky_persist(event, result):
        calls["count"] += 1
        if calls["count"] == 1:
            raise sqlite3.OperationalError("transient busy")
        return original_persist(event, result)

    monkeypatch.setattr(ad, "_persist_completion", flaky_persist)
    if batch:
        terminal_result = {
            "results": [
                {
                    "status": "completed",
                    "summary": "exact batch result",
                }
            ],
            "total_duration_seconds": 0.1,
        }
        dispatched = ad.dispatch_async_delegation_batch(
            goals=["one"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=lambda: terminal_result,
        )
    else:
        terminal_result = {
            "status": "completed",
            "summary": "exact single result",
        }
        dispatched = ad.dispatch_async_delegation(
            goal="one",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=lambda: terminal_result,
        )

    event = _drain_for(dispatched["delegation_id"])
    assert event is not None
    assert calls["count"] == 2
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable is not None
    assert durable["result"] == terminal_result


@pytest.mark.parametrize("batch", [False, True])
def test_permanent_finalize_failure_is_explicit_and_retryable_without_rerun(
    tmp_path,
    monkeypatch,
    batch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        ad,
        "_FINALIZE_PERSIST_RETRY_DELAYS_SECONDS",
        (0.0, 0.0, 0.0),
    )
    original_persist = ad._persist_completion
    runner_calls = {"count": 0}

    def fail_persist(_event, _result):
        raise sqlite3.OperationalError("persistent disk failure")

    monkeypatch.setattr(ad, "_persist_completion", fail_persist)
    if batch:
        terminal_result = {
            "results": [
                {
                    "status": "completed",
                    "summary": "retained batch result",
                }
            ],
            "total_duration_seconds": 0.1,
        }

        def runner():
            runner_calls["count"] += 1
            return terminal_result

        dispatched = ad.dispatch_async_delegation_batch(
            goals=["one"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=runner,
        )
    else:
        terminal_result = {
            "status": "completed",
            "summary": "retained single result",
        }

        def runner():
            runner_calls["count"] += 1
            return terminal_result

        dispatched = ad.dispatch_async_delegation(
            goal="one",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=runner,
        )

    deadline = time.monotonic() + 3
    state = None
    while time.monotonic() < deadline:
        rows = ad.list_async_delegations()
        state = rows[0] if rows else None
        if state and state["status"] == "finalize_failed":
            break
        time.sleep(0.01)

    assert state is not None
    assert state["status"] == "finalize_failed"
    assert "persistent disk failure" in state["finalize_error"]
    assert state["finalize_spooled"] is True
    assert ad.active_count() == 1
    assert runner_calls["count"] == 1
    assert process_registry.completion_queue.empty()
    with ad._records_lock:
        retained = ad._records[dispatched["delegation_id"]]
        assert retained["_finalize_result"] == terminal_result

    monkeypatch.setattr(ad, "_persist_completion", original_persist)
    assert ad.retry_failed_finalization(dispatched["delegation_id"]) is True
    event = _drain_for(dispatched["delegation_id"])
    assert event is not None
    assert runner_calls["count"] == 1
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable is not None
    assert durable["result"] == terminal_result


@pytest.mark.parametrize("batch", [False, True])
def test_finalize_spool_survives_process_boundary_and_restores_exact_result(
    tmp_path,
    monkeypatch,
    batch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        ad,
        "_FINALIZE_PERSIST_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )
    original_persist = ad._persist_completion
    runner_calls = {"count": 0}

    def unavailable_db(_event, _result):
        raise sqlite3.OperationalError("simulated terminal DB outage")

    monkeypatch.setattr(ad, "_persist_completion", unavailable_db)
    terminal_result = (
        {
            "results": [
                {
                    "status": "completed",
                    "summary": "exact batch after restart",
                }
            ],
            "total_duration_seconds": 0.25,
        }
        if batch
        else {
            "status": "completed",
            "summary": "exact single after restart",
            "api_calls": 7,
        }
    )

    def runner():
        runner_calls["count"] += 1
        return terminal_result

    if batch:
        dispatched = ad.dispatch_async_delegation_batch(
            goals=["spool batch"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=runner,
        )
    else:
        dispatched = ad.dispatch_async_delegation(
            goal="spool single",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="owner",
            runner=runner,
        )

    deadline = time.monotonic() + 3
    retained = None
    while time.monotonic() < deadline:
        with ad._records_lock:
            retained = ad._records.get(dispatched["delegation_id"])
            if retained and retained.get("status") == "finalize_failed":
                retained = dict(retained)
                break
        time.sleep(0.01)
    assert retained is not None
    assert retained["status"] == "finalize_failed"
    spool_path = retained["_finalize_spool_path"]
    assert os.path.isfile(spool_path)
    assert runner_calls["count"] == 1
    assert ad.active_count() == 1

    # Simulate a new process: no in-memory result survives.  Only the
    # host-owned sidecar plus the original running row remain.
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    monkeypatch.setattr(ad, "_persist_completion", original_persist)
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    event = restored.get_nowait()

    assert event["delegation_id"] == dispatched["delegation_id"]
    assert event["status"] == (
        "completed" if batch else terminal_result["status"]
    )
    assert runner_calls["count"] == 1
    assert not os.path.exists(spool_path)
    durable = ad.get_durable_delegation(dispatched["delegation_id"])
    assert durable is not None
    assert durable["result"] == terminal_result
    assert ad.active_count() == 0


def test_durable_wake_claim_commits_and_replays_exact_canonical_response(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="durable wake",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {
            "status": "completed",
            "summary": "ready",
        },
    )
    event = _drain_for(dispatched["delegation_id"])
    assert event is not None
    store = ad.get_event_delivery_store(event)
    assert store is not None

    claim = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="hermes-internal-wake-v1-fingerprint",
        store=store,
    )
    assert claim.state == "claimed"
    concurrent = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="hermes-internal-wake-v1-fingerprint",
        store=store,
    )
    assert concurrent.state == "in_progress"
    assert "already running" in concurrent.reason

    response = {
        "choices": [{"message": {"content": "exact"}}],
        "usage": {"completion_tokens": 3, "prompt_tokens": 7},
    }
    assert ad.complete_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="hermes-internal-wake-v1-fingerprint",
        claim_id=claim.claim_id,
        response=response,
        store=store,
    )
    replay = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="hermes-internal-wake-v1-fingerprint",
        store=store,
    )
    assert replay.state == "completed"
    assert replay.response == response
    assert replay.response is not response

    mismatch = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="hermes-internal-wake-v1-other",
        store=store,
    )
    assert mismatch.state == "uncertain"
    assert "fingerprint mismatch" in mismatch.reason


def test_dead_durable_wake_owner_becomes_uncertain_and_never_reclaims(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="durable wake crash",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {
            "status": "completed",
            "summary": "ready",
        },
    )
    event = _drain_for(dispatched["delegation_id"])
    assert event is not None
    store = ad.get_event_delivery_store(event)
    assert store is not None
    claim = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="wake-crash-fingerprint",
        store=store,
    )
    assert claim.state == "claimed"

    # Simulate a process boundary after agent execution began but before its
    # response commit.  The next process has no right to execute tools again.
    with ad._ACTIVE_WAKE_CLAIMS_LOCK:
        ad._ACTIVE_WAKE_CLAIMS.clear()
    monkeypatch.setattr(ad, "_WAKE_PROCESS_INSTANCE_ID", "replacement-process")
    monkeypatch.setattr(ad, "_WAKE_RUNNING_STALE_SECONDS", 0.0)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    after_crash = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="wake-crash-fingerprint",
        store=store,
    )
    assert after_crash.state == "uncertain"
    assert "may include effects" in after_crash.reason
    again = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="wake-crash-fingerprint",
        store=store,
    )
    assert again.state == "uncertain"
    assert not again.claim_id


def test_old_but_positively_live_foreign_wake_owner_stays_in_progress(
    monkeypatch,
):
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        "gateway.status.get_process_start_time",
        lambda _pid: 4242,
    )

    assert not ad._wake_owner_is_stale(
        claim_id="foreign-live-claim",
        owner_pid=123,
        owner_started_at=4242,
        owner_instance="foreign-process-instance",
        claimed_at=0.0,
        now=ad._WAKE_RUNNING_STALE_SECONDS * 10,
    )


@pytest.mark.parametrize(
    ("pid_exists", "current_started"),
    [
        (False, 4242),
        (True, 9999),
    ],
)
def test_dead_or_pid_reused_foreign_wake_owner_is_stale(
    monkeypatch,
    pid_exists,
    current_started,
):
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda _pid: pid_exists,
    )
    monkeypatch.setattr(
        "gateway.status.get_process_start_time",
        lambda _pid: current_started,
    )

    assert ad._wake_owner_is_stale(
        claim_id="foreign-stale-claim",
        owner_pid=123,
        owner_started_at=4242,
        owner_instance="foreign-process-instance",
        claimed_at=time.time(),
        now=time.time(),
    )


def test_unused_durable_wake_claim_can_be_released_and_reclaimed(
    tmp_path,
    monkeypatch,
):
    """A pre-execution capacity denial reopens only its own exact claim."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="durable wake capacity denial",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {
            "status": "completed",
            "summary": "ready",
        },
    )
    event = _drain_for(dispatched["delegation_id"])
    assert event is not None
    store = ad.get_event_delivery_store(event)
    assert store is not None
    fingerprint = "wake-capacity-fingerprint"
    claim = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key=fingerprint,
        store=store,
    )
    assert claim.state == "claimed"

    assert not ad.release_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key=fingerprint,
        claim_id="stale-owner-claim",
        store=store,
    )
    assert ad.release_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key=fingerprint,
        claim_id=claim.claim_id,
        store=store,
    )
    with ad._ACTIVE_WAKE_CLAIMS_LOCK:
        assert claim.claim_id not in ad._ACTIVE_WAKE_CLAIMS

    replacement = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key=fingerprint,
        store=store,
    )
    assert replacement.state == "claimed"
    assert replacement.claim_id != claim.claim_id


@pytest.mark.parametrize(
    "failure_reason",
    [
        "model execution raised after tools may have run",
        "request task was cancelled after execution began",
        "response serialization failed after execution",
    ],
)
def test_abandoned_durable_wake_is_terminal_uncertain_without_second_run(
    tmp_path,
    monkeypatch,
    failure_reason,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="durable wake exception",
        context=None,
        toolsets=None,
        role="leaf",
        model="m",
        session_key="owner",
        runner=lambda: {
            "status": "completed",
            "summary": "ready",
        },
    )
    event = _drain_for(dispatched["delegation_id"])
    store = ad.get_event_delivery_store(event)
    assert store is not None
    model_calls = 0
    claim = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="wake-exception-fingerprint",
        store=store,
    )
    assert claim.state == "claimed"
    model_calls += 1
    assert ad.abandon_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="wake-exception-fingerprint",
        claim_id=claim.claim_id,
        reason=failure_reason,
        store=store,
    )

    retry = ad.claim_durable_wake_execution(
        delegation_id=dispatched["delegation_id"],
        idempotency_key="wake-exception-fingerprint",
        store=store,
    )
    if retry.state == "claimed":
        model_calls += 1
    assert retry.state == "uncertain"
    assert retry.reason == failure_reason
    assert model_calls == 1
    with ad._ACTIVE_WAKE_CLAIMS_LOCK:
        assert claim.claim_id not in ad._ACTIVE_WAKE_CLAIMS


def test_durable_wake_missing_row_is_uncertain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = ad.resolve_event_delivery_store(hermes_home=tmp_path)
    claim = ad.claim_durable_wake_execution(
        delegation_id="missing-delegation",
        idempotency_key="missing-fingerprint",
        store=store,
    )
    assert claim.state == "uncertain"
    assert "missing" in claim.reason


def test_interrupt_all_signals_running_children():
    ev = threading.Event()
    interrupted = {"count": 0}
    # No short internal timeout: the blocker holds until interrupt_fn fires.
    # The old ev.wait(timeout=5) made this test a change-detector for CI
    # worker load — on a CPU-starved runner the 5s expired before
    # interrupt_all() ran, the record finalized, and interrupt_all() found
    # nothing running (n == 0). The pytest-level timeout is the real
    # runaway guard.

    def blocker():
        ev.wait(timeout=60)
        return {"status": "interrupted", "summary": None}

    def interrupt_fn():
        interrupted["count"] += 1
        ev.set()

    r = ad.dispatch_async_delegation(
        goal="long task", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=blocker,
        interrupt_fn=interrupt_fn, max_async_children=3,
    )
    n = ad.interrupt_all(reason="test")
    assert n == 1
    assert interrupted["count"] == 1
    # child still emits a completion event after interrupt. Match on THIS
    # delegation's id — straggler 'completed' events from a previous test's
    # workers can finalize after that test's teardown drain and leak into
    # this queue (observed on loaded CI workers).
    evt = _drain_for(r["delegation_id"])
    assert evt is not None
    assert evt["status"] == "interrupted"


def _fast_stale_monitor(monkeypatch, *, idle=0.15, in_tool=0.3, grace=0.15):
    """Shrink the stale-monitor cadence so tests run in milliseconds."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    monkeypatch.setattr(ad, "_STALE_IDLE_SECONDS", idle)
    monkeypatch.setattr(ad, "_STALE_IN_TOOL_SECONDS", in_tool)
    monkeypatch.setattr(ad, "_STALL_GRACE_SECONDS", grace)


def test_stalled_runner_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation(
        goal="stuck child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_runner,
        interrupt_fn=interrupt_fn, max_async_children=1,
        # Frozen progress token: the child never advances an API call.
        progress_fn=lambda: ((0, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["delegation_id"] == res["delegation_id"]
        assert evt["api_calls"] == 0
        assert "stalled" in evt["error"]
        # Interrupt was requested BEFORE force-finalization (grace window).
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    # If the ignored runner eventually returns, it must not enqueue a second
    # completion for a delegation the monitor already finalized.
    assert _drain_one(timeout=0.5) is None


def test_progressing_runner_is_never_stalled(monkeypatch):
    """A child that keeps advancing is left alone no matter how long it runs."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    ticks = {"n": 0}

    def slow_but_alive_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "done", "api_calls": 7}

    def progress_fn():
        # Token advances on every sample — simulates a child making steady
        # API-call progress.
        ticks["n"] += 1
        return (ticks["n"], None), False

    res = ad.dispatch_async_delegation(
        goal="slow child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=slow_but_alive_runner,
        max_async_children=1, progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    # Run well past the (shrunk) idle threshold — several monitor sweeps.
    time.sleep(0.6)
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"
    assert evt["summary"] == "done"


def test_stalling_runner_that_honors_interrupt_keeps_its_result(monkeypatch):
    """Interrupt-responsive children finalize through the NORMAL path.

    The monitor's interrupt gives a wedged-looking child a grace window; if
    the runner returns during it, the real result (partial work, api_calls)
    is delivered instead of a synthetic stalled event.
    """
    _fast_stale_monitor(monkeypatch, grace=5.0)
    interrupted = threading.Event()

    def runner():
        # "Wedged" until interrupted, then unwinds and reports partial work.
        interrupted.wait(timeout=10)
        return {
            "status": "interrupted",
            "summary": "partial work saved",
            "api_calls": 3,
        }

    res = ad.dispatch_async_delegation(
        goal="responsive child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1,
        progress_fn=lambda: ((3, None), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "interrupted"
    assert evt["summary"] == "partial work saved"
    assert evt["api_calls"] == 3
    assert ad.active_count() == 0


def test_streaming_child_counts_as_alive(monkeypatch):
    """A child mid-stream (api_call_count frozen, last_activity_ts ticking)
    must never be stalled — streamed chunks tick _touch_activity, and the
    progress token includes that timestamp (same liveness signal as the
    compaction inactivity budget, PR #71508)."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    now = {"ts": 1000.0}

    def progress_fn():
        # api_call_count and current_tool frozen (long streaming response in
        # flight), but the activity timestamp advances with every chunk.
        now["ts"] += 1.0
        return ((1, None, now["ts"]),), False

    res = ad.dispatch_async_delegation(
        goal="streaming child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: (gate.wait(timeout=10), {"status": "completed", "summary": "streamed"})[1],
        progress_fn=progress_fn,
    )
    assert res["status"] == "dispatched"

    time.sleep(0.6)  # several sweeps past the shrunk idle threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stalled_event_carries_structured_stall_metadata(monkeypatch):
    """The terminal stalled event must expose machine-readable stall context
    (#51690) — quiet duration, tripped threshold, phase, grace — mirroring
    the sync path's timeout_seconds/timed_out_after_seconds/timeout_phase."""
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()

    res = ad.dispatch_async_delegation(
        goal="stall metadata", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: ((0, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["status"] == "stalled"
        assert evt["stalled_after_quiet_seconds"] >= 0.3  # in-tool threshold
        assert evt["stall_threshold_seconds"] == ad._STALE_IN_TOOL_SECONDS
        assert evt["stall_phase"] == "in_tool"
        assert evt["stall_grace_seconds"] == ad._STALL_GRACE_SECONDS
    finally:
        gate.set()


def test_list_async_delegations_exposes_live_activity(monkeypatch):
    """list_async_delegations must expose per-child live activity sampled
    from progress_fn plus seconds_since_progress, for /agents UIs (#51690)."""
    monkeypatch.setattr(ad, "_STALE_CHECK_INTERVAL", 0.03)
    gate = threading.Event()
    base_ts = time.time() - 12.0

    res = ad.dispatch_async_delegation(
        goal="live listing", context=None, toolsets=None, role="leaf",
        model="m", session_key="", max_async_children=1,
        runner=lambda: {} if gate.wait(timeout=10) else {},
        progress_fn=lambda: (((3, "web_search", base_ts),), True),
    )
    try:
        time.sleep(0.1)  # let the monitor stamp _progress_ts at least once
        item = next(
            d for d in ad.list_async_delegations()
            if d["delegation_id"] == res["delegation_id"]
        )
        assert item["status"] == "running"
        assert item["in_tool"] is True
        assert "seconds_since_progress" in item
        (child,) = item["children_activity"]
        assert child["api_calls"] == 3
        assert child["current_tool"] == "web_search"
        assert 10.0 <= child["seconds_since_activity"] <= 20.0
        # Callables and private bookkeeping must never leak.
        assert "progress_fn" not in item
        assert "interrupt_fn" not in item
        assert not any(k.startswith("_") for k in item)
    finally:
        gate.set()


def test_stalled_batch_is_interrupted_then_finalized(monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    interrupted = {"count": 0}

    def stuck_batch():
        gate.wait(timeout=10)
        return {"results": [{"status": "completed", "summary": "too late"}]}

    def interrupt_fn():
        interrupted["count"] += 1

    res = ad.dispatch_async_delegation_batch(
        goals=["a", "b"], context="ctx", toolsets=None, role="leaf",
        model="m", session_key="", runner=stuck_batch,
        interrupt_fn=interrupt_fn, max_async_children=1,
        progress_fn=lambda: (((0, None), (0, None)), False),
    )
    assert res["status"] == "dispatched"

    evt = _drain_for(res["delegation_id"], timeout=5.0)
    try:
        assert evt is not None
        assert evt["type"] == "async_delegation"
        assert evt["status"] == "stalled"
        assert evt["is_batch"] is True
        assert evt["goals"] == ["a", "b"]
        assert evt["results"] == []
        assert "stalled" in evt["error"]
        assert interrupted["count"] >= 1
        assert ad.active_count() == 0
    finally:
        gate.set()

    assert _drain_one(timeout=0.5) is None


def test_in_tool_stall_uses_higher_threshold(monkeypatch):
    """A frozen child inside a tool gets the in-tool ceiling, not the idle one."""
    _fast_stale_monitor(monkeypatch, idle=0.1, in_tool=10.0, grace=0.1)
    gate = threading.Event()

    def runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "long tool finished"}

    res = ad.dispatch_async_delegation(
        goal="long tool child", context=None, toolsets=None, role="leaf",
        model="m", session_key="", runner=runner, max_async_children=1,
        # Frozen token but in_tool=True — a legitimately slow terminal
        # command / web fetch. Must NOT be stalled at the idle threshold.
        progress_fn=lambda: ((1, "terminal"), True),
    )
    assert res["status"] == "dispatched"

    time.sleep(0.5)  # far past idle threshold, well under in-tool threshold
    assert ad.active_count() == 1
    assert process_registry.completion_queue.empty()

    gate.set()
    evt = _drain_for(res["delegation_id"], timeout=5.0)
    assert evt is not None
    assert evt["status"] == "completed"


def test_stall_stays_finalizing_until_durable_persistence(tmp_path, monkeypatch):
    _fast_stale_monitor(monkeypatch)
    gate = threading.Event()
    persist_entered = threading.Event()
    allow_persist = threading.Event()
    real_persist = ad._persist_completion

    def blocking_persist(event, result):
        persist_entered.set()
        allow_persist.wait(timeout=5)
        real_persist(event, result)

    def stuck_runner():
        gate.wait(timeout=10)
        return {"status": "completed", "summary": "too late"}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_persist_completion", blocking_persist)
    dispatched = ad.dispatch_async_delegation(
        goal="durable stall", context=None, toolsets=None, role="leaf",
        model="m", session_key="owner", runner=stuck_runner,
        max_async_children=1, progress_fn=lambda: ((0, None), False),
    )

    try:
        assert persist_entered.wait(timeout=5)
        assert ad.active_count() == 1
        record = next(
            item for item in ad.list_async_delegations()
            if item["delegation_id"] == dispatched["delegation_id"]
        )
        assert record["status"] == "finalizing"
        assert process_registry.completion_queue.empty()

        allow_persist.set()
        evt = _drain_for(dispatched["delegation_id"])
        assert evt is not None
        assert evt["status"] == "stalled"
        assert ad.active_count() == 0
        durable = ad.get_durable_delegation(dispatched["delegation_id"])
        assert durable["state"] == "stalled"
        assert durable["delivery_state"] == "pending"
    finally:
        allow_persist.set()
        gate.set()


def test_stalled_completion_restores_once_after_process_restart(tmp_path):
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import json
import threading
import time
from tools import async_delegation as ad
ad._STALE_CHECK_INTERVAL = 0.03
ad._STALE_IDLE_SECONDS = 0.1
ad._STALL_GRACE_SECONDS = 0.1
gate = threading.Event()
r = ad.dispatch_async_delegation(
    goal="restart stall", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: gate.wait(timeout=60),
    progress_fn=lambda: ((0, None), False),
)
deadline = time.time() + 10
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
row = ad.get_durable_delegation(r["delegation_id"])
print(json.dumps({"delegation_id": r["delegation_id"], "row": row}, sort_keys=True))
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=30, check=True,
    )
    produced = json.loads(first.stdout.strip().splitlines()[-1])
    delegation_id = produced["delegation_id"]
    assert produced["row"]["state"] == "stalled"
    assert produced["row"]["delivery_state"] == "pending"

    consumer = r'''
import json
from tools.process_registry import process_registry
process_registry.restore_async_delegation_completions(once=True)
evt = process_registry.completion_queue.get_nowait()
print(json.dumps({"event": evt, "remaining": process_registry.completion_queue.qsize()}, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    restored = json.loads(second.stdout.strip().splitlines()[-1])
    assert restored["remaining"] == 0
    assert restored["event"]["delegation_id"] == delegation_id
    assert restored["event"]["status"] == "stalled"
    assert restored["event"]["restored"] is True

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tools.process_registry import process_registry; "
                "process_registry.restore_async_delegation_completions(once=True); "
                "print(process_registry.completion_queue.qsize())"
            ),
        ],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


def test_completed_records_pruned_to_cap():
    # Run more than the retention cap quickly; ensure list doesn't grow forever.
    for i in range(ad._MAX_RETAINED_COMPLETED + 10):
        ad.dispatch_async_delegation(
            goal=f"t{i}", context=None, toolsets=None, role="leaf", model="m",
            session_key="", runner=lambda: {"status": "completed", "summary": "ok"},
            max_async_children=ad._MAX_RETAINED_COMPLETED + 20,
        )
    # let workers finish
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and ad.active_count() > 0:
        time.sleep(0.05)
    assert len(ad.list_async_delegations()) <= ad._MAX_RETAINED_COMPLETED


def test_completion_is_persisted_and_delivery_can_be_acknowledged(tmp_path, monkeypatch):
    """A finished child remains pending on disk until its queue consumer acks it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dispatched = ad.dispatch_async_delegation(
        goal="durable", context="ctx", toolsets=["terminal"], role="leaf",
        model="m", session_key="owner", parent_session_id="parent",
        runner=lambda: {"status": "completed", "summary": "survived"},
    )
    assert _drain_one() is not None

    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    row = ad.get_durable_delegation(dispatched["delegation_id"])
    assert row["origin_session"] == "owner"
    assert row["state"] == "completed"
    assert row["result"]["summary"] == "survived"
    assert row["delivery_state"] == "pending"
    # Queue publication/restoration is not a destination delivery attempt.
    assert row["delivery_attempts"] == 0

    assert ad.mark_completion_delivered(dispatched["delegation_id"])
    assert ad.restore_undelivered_completions(queue.Queue()) == 0
    assert ad.get_durable_delegation(dispatched["delegation_id"])["delivery_state"] == "delivered"


def test_record_owned_store_wins_over_ambient_home_during_finalization(
    tmp_path,
    monkeypatch,
):
    """The raw stale-monitor thread must finalize into the dispatch profile."""

    default_home = tmp_path
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    store = ad.resolve_event_delivery_store(
        hermes_home=alpha_home,
        profile="alpha",
    )
    record = {
        "delegation_id": "deleg_stalled_alpha",
        "session_key": "agent:alpha:discord:channel:123",
        "origin_ui_session_id": "",
        "parent_session_id": "alpha-parent",
        "goal": "finish from a raw monitor thread",
        "status": "finalizing",
        "dispatched_at": 1.0,
        "completed_at": 2.0,
        "_delivery_store": store,
    }
    # Both calls intentionally run with DEFAULT as the ambient home. The
    # dispatch-captured store is the only authority available to a raw monitor
    # thread after the originating ContextVar has disappeared.
    ad._persist_dispatch(record)
    ad._push_completion_event(
        record,
        {
            "status": "stalled",
            "summary": None,
            "error": "stalled",
        },
        "stalled",
    )
    event = _drain_for("deleg_stalled_alpha")

    assert event is not None
    event_store = ad.get_event_delivery_store(event)
    assert event_store == store
    assert _durable_in_home(default_home, "deleg_stalled_alpha") is None
    alpha = _durable_in_home(alpha_home, "deleg_stalled_alpha")
    assert alpha is not None
    assert alpha["state"] == "stalled"

    with ad._delivery_home_scope(alpha_home):
        with ad._DB_LOCK, ad._transaction() as conn:
            persisted = json.loads(
                conn.execute(
                    """SELECT event_json FROM async_delegations
                       WHERE delegation_id='deleg_stalled_alpha'"""
                ).fetchone()[0]
            )
    assert ad._EVENT_DELIVERY_STORE_KEY not in persisted
    assert "restored" not in persisted


def test_restore_overwrites_forged_private_store_and_validates_profile_home(
    tmp_path,
    monkeypatch,
):
    default_home = tmp_path
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _persist_terminal_in_home(alpha_home, "deleg_forged_store")

    # Simulate a row written by an old/compromised producer. The current
    # persistence path strips this field, but restore must still distrust data
    # already present on disk and overwrite it.
    forged_home = tmp_path / "attacker-controlled"
    with ad._delivery_home_scope(alpha_home):
        with ad._DB_LOCK, ad._transaction() as conn:
            raw = conn.execute(
                """SELECT event_json FROM async_delegations
                   WHERE delegation_id='deleg_forged_store'"""
            ).fetchone()[0]
            payload = json.loads(raw)
            payload[ad._EVENT_DELIVERY_STORE_KEY] = str(forged_home)
            conn.execute(
                """UPDATE async_delegations SET event_json=?
                   WHERE delegation_id='deleg_forged_store'""",
                (json.dumps(payload),),
            )

    restored = queue.Queue()
    assert (
        ad.restore_undelivered_completions(
            restored,
            hermes_home=alpha_home,
            profile="alpha",
        )
        == 1
    )
    event = restored.get_nowait()
    assert ad.event_has_delivery_store_stamp(event)
    assert event[ad._EVENT_DELIVERY_STORE_KEY] != str(forged_home)
    restored_store = ad.get_event_delivery_store(event)
    assert restored_store is not None
    assert restored_store.hermes_home == str(alpha_home.resolve())
    assert restored_store.profile == "alpha"
    assert restored_store.profile_generation
    assert not ad.event_has_delivery_store_stamp(
        {"type": "async_delegation", "delegation_id": "legacy"}
    )

    with pytest.raises(ValueError, match="resolves to"):
        ad.restore_undelivered_completions(
            queue.Queue(),
            hermes_home=default_home,
            profile="alpha",
        )
    with pytest.raises(ValueError, match="does not exist"):
        ad.restore_undelivered_completions(
            queue.Queue(),
            profile="missing-profile",
        )


def test_invalid_named_profile_never_downgrades_to_unlabelled_home_authority(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda key, default="": (
            "definitely-missing-profile"
            if key == "HERMES_SESSION_PROFILE"
            else default
        ),
    )

    with pytest.raises(ValueError, match="does not exist"):
        ad._current_event_delivery_store()
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize(
    ("settle", "expected_state"),
    [
        ("complete", "delivered"),
        ("release", "pending"),
        ("drop", "dropped"),
    ],
)
def test_event_delivery_operations_are_scoped_to_stamped_profile(
    tmp_path,
    monkeypatch,
    settle,
    expected_state,
):
    """Same id in two profile DBs must only mutate the event's stamped store."""

    default_home = tmp_path
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    delegation_id = f"deleg_scoped_{settle}"
    _persist_terminal_in_home(default_home, delegation_id)
    _persist_terminal_in_home(alpha_home, delegation_id)

    restored = queue.Queue()
    assert (
        ad.restore_undelivered_completions(
            restored,
            hermes_home=alpha_home,
            profile="alpha",
        )
        == 1
    )
    event = restored.get_nowait()
    claim = ad.claim_event_delivery(event, "profile-test")
    assert claim
    assert _durable_in_home(default_home, delegation_id)["delivery_attempts"] == 0
    assert _durable_in_home(alpha_home, delegation_id)["delivery_attempts"] == 1

    if settle == "complete":
        assert ad.complete_event_delivery(event, claim)
    elif settle == "release":
        assert ad.release_event_delivery(event, claim)
    else:
        assert ad.drop_event_delivery(event, claim)

    default = _durable_in_home(default_home, delegation_id)
    alpha = _durable_in_home(alpha_home, delegation_id)
    assert default["delivery_state"] == "pending"
    assert default["delivery_attempts"] == 0
    assert alpha["delivery_state"] == expected_state


def test_event_delivery_retry_delay_tracks_claim_lease_and_terminal_state(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    delegation_id = "deleg_retry_delay"
    _persist_terminal_in_home(tmp_path, delegation_id)
    restored = queue.Queue()
    assert (
        ad.restore_undelivered_completions(
            restored,
            hermes_home=tmp_path,
        )
        == 1
    )
    event = restored.get_nowait()

    claim = ad.claim_event_delivery(event, "retry-delay-owner")
    assert claim
    fresh_delay = ad.event_delivery_retry_delay(
        event,
        minimum_seconds=0.1,
    )
    assert fresh_delay is not None
    assert 299.0 < fresh_delay <= (
        ad._DELIVERY_CLAIM_LEASE_SECONDS
        + ad._DELIVERY_RETRY_EPSILON_SECONDS
    )

    assert ad.release_event_delivery(event, claim)
    assert ad.event_delivery_retry_delay(
        event,
        minimum_seconds=0.1,
    ) == pytest.approx(0.1)

    retry_claim = ad.claim_event_delivery(event, "retry-delay-next")
    assert retry_claim
    assert ad.complete_event_delivery(event, retry_claim)
    assert ad.event_delivery_retry_delay(event) is None


def test_delivery_claim_with_missing_timestamp_is_reclaimable(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    delegation_id = "deleg_null_claim_timestamp"
    _persist_terminal_in_home(tmp_path, delegation_id)
    assert ad.claim_completion_delivery(delegation_id, "stale-owner")
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            """UPDATE async_delegations SET delivery_claimed_at=NULL
               WHERE delegation_id=?""",
            (delegation_id,),
        )

    assert ad.claim_completion_delivery(delegation_id, "replacement-owner")
    assert ad.complete_completion_delivery(
        delegation_id,
        "replacement-owner",
    )


def test_raw_renewal_thread_renews_claim_in_stamped_profile(
    tmp_path,
    monkeypatch,
):
    default_home = tmp_path
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    delegation_id = "deleg_scoped_renewal"
    _persist_terminal_in_home(default_home, delegation_id)
    _persist_terminal_in_home(alpha_home, delegation_id)
    restored = queue.Queue()
    ad.restore_undelivered_completions(
        restored,
        hermes_home=alpha_home,
        profile="alpha",
    )
    event = restored.get_nowait()
    claim = ad.claim_event_delivery(event, "renewal-test")
    assert claim

    with ad._delivery_home_scope(alpha_home):
        with ad._DB_LOCK, ad._transaction() as conn:
            before = conn.execute(
                """SELECT delivery_claimed_at FROM async_delegations
                   WHERE delegation_id=?""",
                (delegation_id,),
            ).fetchone()[0]

    handle = ad.begin_event_delivery_renewal(
        event,
        claim,
        interval_seconds=0.01,
    )
    deadline = time.monotonic() + 2.0
    after = before
    while after <= before and time.monotonic() < deadline:
        time.sleep(0.02)
        with ad._delivery_home_scope(alpha_home):
            with ad._DB_LOCK, ad._transaction() as conn:
                after = conn.execute(
                    """SELECT delivery_claimed_at FROM async_delegations
                       WHERE delegation_id=?""",
                    (delegation_id,),
                ).fetchone()[0]
    handle()

    assert after > before
    assert not handle.ownership_lost
    assert _durable_in_home(default_home, delegation_id)["delivery_attempts"] == 0
    assert ad.complete_event_delivery(event, claim)
    assert _durable_in_home(alpha_home, delegation_id)["delivery_state"] == "delivered"


def test_stamped_event_fails_closed_when_authoritative_row_is_missing(
    tmp_path,
    monkeypatch,
):
    default_home = tmp_path
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    delegation_id = "deleg_missing_stamped_row"
    _persist_terminal_in_home(alpha_home, delegation_id)
    restored = queue.Queue()
    ad.restore_undelivered_completions(
        restored,
        hermes_home=alpha_home,
        profile="alpha",
    )
    event = restored.get_nowait()
    with ad._delivery_home_scope(alpha_home):
        ad._delete_durable_delegation(delegation_id)

    assert ad.claim_event_delivery(event, "strict-event") is None
    assert not ad.complete_event_delivery(event, "strict-event:claim")
    # Direct id APIs retain their pre-contract legacy admission semantics.
    assert ad.claim_completion_delivery("legacy-no-row", "legacy-claim")
    assert ad.complete_completion_delivery("legacy-no-row", "legacy-claim")


def test_process_registry_explicit_restore_entrypoint_is_home_idempotent(
    tmp_path,
    monkeypatch,
):
    default_home = tmp_path
    alpha_home = tmp_path / "profiles" / "alpha"
    alpha_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    _persist_terminal_in_home(default_home, "deleg_registry_default")
    _persist_terminal_in_home(alpha_home, "deleg_registry_alpha")

    registry = ProcessRegistry()
    assert (
        registry.restore_async_delegation_completions(
            hermes_home=alpha_home,
            profile="alpha",
            once=True,
        )
        == 1
    )
    assert (
        registry.restore_async_delegation_completions(
            hermes_home=alpha_home,
            profile="alpha",
            once=True,
        )
        == 0
    )
    events = [
        registry.completion_queue.get_nowait(),
        registry.completion_queue.get_nowait(),
    ]
    assert {event["delegation_id"] for event in events} == {
        "deleg_registry_default",
        "deleg_registry_alpha",
    }
    alpha_event = next(
        event
        for event in events
        if event["delegation_id"] == "deleg_registry_alpha"
    )
    assert ad.get_event_delivery_store(alpha_event).profile == "alpha"
    with pytest.raises(ValueError, match="cannot be combined"):
        registry.restore_async_delegation_completions(
            hermes_home=alpha_home,
            profile="alpha",
            event_filter=lambda _event: True,
            once=True,
        )


class _RecoveryTimer:
    instances = []
    fail_start = False

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.started = False
        self.cancelled = False
        self.__class__.instances.append(self)

    def start(self):
        if self.__class__.fail_start:
            raise RuntimeError("timer start failed")
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


def _persist_foreign_running(home, delegation_id, *, owner_pid, session_key="owner"):
    Path(home).mkdir(parents=True, exist_ok=True)
    with ad._delivery_home_scope(home):
        ad._persist_dispatch(
            {
                "delegation_id": delegation_id,
                "session_key": session_key,
                "origin_ui_session_id": "",
                "parent_session_id": "parent",
                "dispatched_at": 1.0,
            }
        )
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                """UPDATE async_delegations
                   SET owner_pid=?, owner_started_at=?
                   WHERE delegation_id=?""",
                (owner_pid, owner_pid * 10, delegation_id),
            )


def _install_owner_liveness(monkeypatch, alive):
    import gateway.status as gateway_status

    monkeypatch.setattr(
        gateway_status,
        "_pid_exists",
        lambda pid: bool(alive.get(int(pid), False)),
    )
    monkeypatch.setattr(
        gateway_status,
        "get_process_start_time",
        lambda pid: int(pid) * 10,
    )


def test_registry_rescans_owner_observed_live_before_recovery(
    tmp_path,
    monkeypatch,
):
    """Death after the initial recovery check must not strand RUNNING forever."""

    import tools.process_registry as registry_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _persist_foreign_running(
        tmp_path,
        "deleg-overlap",
        owner_pid=4242,
    )
    alive = {4242: True}
    _install_owner_liveness(monkeypatch, alive)
    _RecoveryTimer.instances = []
    _RecoveryTimer.fail_start = False
    monkeypatch.setattr(registry_module.threading, "Timer", _RecoveryTimer)

    registry = ProcessRegistry()
    assert registry.completion_queue.empty()
    assert len(_RecoveryTimer.instances) == 1
    timer = _RecoveryTimer.instances[0]
    assert timer.started and timer.daemon

    # The old process dies only after recovery already observed it live.
    alive[4242] = False
    timer.fire()

    event = registry.completion_queue.get_nowait()
    assert event["delegation_id"] == "deleg-overlap"
    assert event["status"] == "unknown"
    assert event["restored"] is True
    assert registry._async_abandoned_rescan_timers == {}
    assert registry._async_abandoned_watch_ids == {}
    registry.cancel_async_delegation_recovery_rescans()


def test_registry_rescan_preserves_graceful_old_owner_completion(
    tmp_path,
    monkeypatch,
):
    import tools.process_registry as registry_module

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _persist_foreign_running(
        tmp_path,
        "deleg-overlap-graceful",
        owner_pid=4343,
    )
    alive = {4343: True}
    _install_owner_liveness(monkeypatch, alive)
    _RecoveryTimer.instances = []
    _RecoveryTimer.fail_start = False
    monkeypatch.setattr(registry_module.threading, "Timer", _RecoveryTimer)
    registry = ProcessRegistry()
    assert len(_RecoveryTimer.instances) == 1

    with ad._delivery_home_scope(tmp_path):
        ad._persist_completion(
            {
                "type": "async_delegation",
                "delegation_id": "deleg-overlap-graceful",
                "session_key": "owner",
                "parent_session_id": "parent",
                "status": "completed",
                "summary": "old owner finished cleanly",
                "completed_at": 2.0,
            },
            {
                "status": "completed",
                "summary": "old owner finished cleanly",
            },
        )
    _RecoveryTimer.instances[0].fire()

    event = registry.completion_queue.get_nowait()
    assert event["status"] == "completed"
    assert event["summary"] == "old owner finished cleanly"
    assert registry._async_abandoned_rescan_timers == {}
    registry.cancel_async_delegation_recovery_rescans()


def test_registry_recovery_rescans_are_home_scoped_and_cancel_cleanly(
    tmp_path,
    monkeypatch,
):
    import tools.process_registry as registry_module

    launch_home = tmp_path / "launch"
    alpha_home = tmp_path / "alpha"
    beta_home = tmp_path / "beta"
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    _persist_foreign_running(
        alpha_home,
        "same-id",
        owner_pid=1001,
        session_key="alpha-owner",
    )
    _persist_foreign_running(
        beta_home,
        "same-id",
        owner_pid=1002,
        session_key="beta-owner",
    )
    alive = {1001: True, 1002: True}
    _install_owner_liveness(monkeypatch, alive)
    _RecoveryTimer.instances = []
    _RecoveryTimer.fail_start = False
    monkeypatch.setattr(registry_module.threading, "Timer", _RecoveryTimer)
    registry = ProcessRegistry()

    registry.restore_async_delegation_completions(
        hermes_home=alpha_home,
        once=True,
    )
    registry.restore_async_delegation_completions(
        hermes_home=beta_home,
        once=True,
    )
    assert set(registry._async_abandoned_rescan_timers) == {
        str(alpha_home.resolve()),
        str(beta_home.resolve()),
    }

    alive[1001] = False
    alpha_timer = registry._async_abandoned_rescan_timers[
        str(alpha_home.resolve())
    ]
    beta_timer = registry._async_abandoned_rescan_timers[
        str(beta_home.resolve())
    ]
    alpha_timer.fire()

    event = registry.completion_queue.get_nowait()
    assert ad.get_event_delivery_store(event).hermes_home == str(
        alpha_home.resolve()
    )
    assert _durable_in_home(alpha_home, "same-id")["state"] == "unknown"
    assert _durable_in_home(beta_home, "same-id")["state"] == "running"
    assert str(beta_home.resolve()) in registry._async_abandoned_rescan_timers

    registry.cancel_async_delegation_recovery_rescans()
    assert beta_timer.cancelled is True
    assert registry._async_abandoned_rescan_timers == {}
    beta_timer.fire()
    assert registry.completion_queue.empty()


def test_registry_filtered_rescan_preserves_tui_session_ownership(
    tmp_path,
    monkeypatch,
):
    import tools.process_registry as registry_module

    launch_home = tmp_path / "launch"
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    _persist_foreign_running(
        profile_home,
        "deleg-session-a",
        owner_pid=2001,
        session_key="session-a",
    )
    _persist_foreign_running(
        profile_home,
        "deleg-session-b",
        owner_pid=2002,
        session_key="session-b",
    )
    alive = {2001: True, 2002: True}
    _install_owner_liveness(monkeypatch, alive)
    _RecoveryTimer.instances = []
    _RecoveryTimer.fail_start = False
    monkeypatch.setattr(registry_module.threading, "Timer", _RecoveryTimer)
    registry = ProcessRegistry()

    registry.restore_async_delegation_completions(
        hermes_home=profile_home,
        event_filter=lambda event: event.get("session_key") == "session-a",
    )
    timer = registry._async_abandoned_rescan_timers[
        str(profile_home.resolve())
    ]
    alive[2001] = False
    alive[2002] = False
    timer.fire()

    event = registry.completion_queue.get_nowait()
    assert event["delegation_id"] == "deleg-session-a"
    assert registry.completion_queue.empty()
    assert (
        _durable_in_home(profile_home, "deleg-session-b")["delivery_state"]
        == "pending"
    )

    # The rejected sibling was not put into the de-dup set; its own TUI
    # session can restore it later.
    assert (
        registry.restore_async_delegation_completions(
            hermes_home=profile_home,
            event_filter=lambda candidate: candidate.get("session_key")
            == "session-b",
        )
        == 1
    )
    assert (
        registry.completion_queue.get_nowait()["delegation_id"]
        == "deleg-session-b"
    )
    registry.cancel_async_delegation_recovery_rescans()


def test_registry_rescan_retries_storage_error_with_bounded_backoff(
    tmp_path,
    monkeypatch,
):
    import tools.process_registry as registry_module

    launch_home = tmp_path / "launch"
    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    _persist_foreign_running(
        profile_home,
        "deleg-storage-retry",
        owner_pid=5050,
    )
    alive = {5050: True}
    _install_owner_liveness(monkeypatch, alive)
    _RecoveryTimer.instances = []
    _RecoveryTimer.fail_start = False
    monkeypatch.setattr(registry_module.threading, "Timer", _RecoveryTimer)
    monkeypatch.setattr(
        registry_module,
        "ASYNC_ABANDONED_RESCAN_INITIAL_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        registry_module,
        "ASYNC_ABANDONED_RESCAN_MAX_SECONDS",
        4.0,
    )
    registry = ProcessRegistry()
    registry.restore_async_delegation_completions(
        hermes_home=profile_home,
        once=True,
    )
    first = registry._async_abandoned_rescan_timers[
        str(profile_home.resolve())
    ]

    original_live_ids = ad.live_foreign_delegation_ids
    monkeypatch.setattr(
        ad,
        "live_foreign_delegation_ids",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("state.db unavailable")),
    )
    first.fire()

    replacement = registry._async_abandoned_rescan_timers[
        str(profile_home.resolve())
    ]
    assert replacement is not first
    assert replacement.delay == 2.0
    assert first not in registry._async_abandoned_rescan_timers.values()

    monkeypatch.setattr(ad, "live_foreign_delegation_ids", original_live_ids)
    alive[5050] = False
    replacement.fire()
    assert (
        registry.completion_queue.get_nowait()["delegation_id"]
        == "deleg-storage-retry"
    )
    assert registry._async_abandoned_rescan_timers == {}
    registry.cancel_async_delegation_recovery_rescans()


def test_real_process_restart_restores_owned_completion_once(tmp_path):
    """Real-import E2E: a fresh interpreter restores a prior process's result."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": repo}
    producer = r'''
import time
from tools import async_delegation as ad
r = ad.dispatch_async_delegation(
    goal="restart", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "after restart"},
)
deadline = time.time() + 5
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''
    first = subprocess.run(
        [sys.executable, "-c", producer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    delegation_id = first.stdout.strip().splitlines()[-1]

    consumer = r'''
import json
from tools.process_registry import process_registry
process_registry.restore_async_delegation_completions(once=True)
evt = process_registry.completion_queue.get_nowait()
print(json.dumps(evt, sort_keys=True))
'''
    second = subprocess.run(
        [sys.executable, "-c", consumer], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    evt = json.loads(second.stdout.strip().splitlines()[-1])
    assert evt["delegation_id"] == delegation_id
    assert evt["session_key"] == "owner-session"
    assert evt["parent_session_id"] == "durable-parent"
    assert evt["summary"] == "after restart"

    acker = f'''
from tools import async_delegation as ad
assert ad.mark_completion_delivered({delegation_id!r})
'''
    subprocess.run(
        [sys.executable, "-c", acker], cwd=repo, env=env,
        text=True, capture_output=True, timeout=15, check=True,
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tools.process_registry import process_registry; "
                "process_registry.restore_async_delegation_completions(once=True); "
                "print(process_registry.completion_queue.qsize())"
            ),
        ],
        cwd=repo, env=env, text=True, capture_output=True, timeout=15, check=True,
    )
    assert probe.stdout.strip().splitlines()[-1] == "0"


def test_submit_failure_removes_durable_running_record(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class _BrokenExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("submit failed")

    monkeypatch.setattr(ad, "_get_executor", lambda _max_workers: _BrokenExecutor())
    result = ad.dispatch_async_delegation(
        goal="never ran", context=None, toolsets=None, role="leaf", model="m",
        session_key="owner", runner=lambda: {},
    )

    assert result["status"] == "rejected"
    with ad._DB_LOCK, ad._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0] == 0


def test_history_retention_never_counts_pending_obligations(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 2)
    for index, delivery_state in enumerate(("pending", "delivered", "pending")):
        delegation_id = f"deleg_{index}"
        record = {
            "delegation_id": delegation_id,
            "session_key": "owner",
            "origin_ui_session_id": "",
            "parent_session_id": None,
            "dispatched_at": float(index + 1),
        }
        ad._persist_dispatch(record)
        ad._persist_completion(
            {
                "delegation_id": delegation_id,
                "status": "completed",
                "completed_at": float(index + 1),
            },
            {"status": "completed", "summary": delegation_id},
        )
        if delivery_state == "delivered":
            ad.mark_completion_delivered(delegation_id)

    ad._prune_durable_records()

    assert ad.get_durable_delegation("deleg_0") is not None
    assert ad.get_durable_delegation("deleg_1") is not None
    assert ad.get_durable_delegation("deleg_2") is not None


def test_history_cap_prunes_only_old_delivered_or_dropped_rows(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 2)
    monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 10)
    monkeypatch.setattr(ad, "_DURABLE_RETENTION_SECONDS", 10**12)
    states = {
        "pending-old": "pending",
        "history-old": "delivered",
        "history-mid": "dropped",
        "history-new": "delivered",
        "pending-new": "pending",
    }
    for index, (delegation_id, delivery_state) in enumerate(states.items()):
        _persist_terminal_in_home(tmp_path, delegation_id)
        if delivery_state == "delivered":
            ad.mark_completion_delivered(delegation_id)
        elif delivery_state == "dropped":
            with ad._DB_LOCK, ad._transaction() as conn:
                conn.execute(
                    """UPDATE async_delegations
                       SET delivery_state='dropped'
                       WHERE delegation_id=?""",
                    (delegation_id,),
                )
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                "UPDATE async_delegations SET updated_at=? WHERE delegation_id=?",
                (float(index + 1), delegation_id),
            )

    ad._prune_durable_records()

    assert ad.get_durable_delegation("history-old") is None
    for delegation_id in (
        "pending-old",
        "history-mid",
        "history-new",
        "pending-new",
    ):
        assert ad.get_durable_delegation(delegation_id) is not None


def test_pending_overflow_is_explicit_quarantine_without_payload_loss(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The history cap must not eat an undelivered obligation first.
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 1)
    monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 10)
    for index in range(4):
        delegation_id = f"pending-{index}"
        event = _persist_terminal_in_home(tmp_path, delegation_id)
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                "UPDATE async_delegations SET updated_at=? WHERE delegation_id=?",
                (float(index + 1), delegation_id),
            )
        durable = ad.get_durable_delegation(delegation_id)
        assert durable["event"]["delegation_id"] == event["delegation_id"]

    ad._prune_durable_records()
    assert all(
        ad.get_durable_delegation(f"pending-{index}")["delivery_state"]
        == "pending"
        for index in range(4)
    )

    monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 2)
    ad._prune_durable_records()

    rows = {
        f"pending-{index}": ad.get_durable_delegation(f"pending-{index}")
        for index in range(4)
    }
    assert all(row is not None for row in rows.values())
    assert {
        delegation_id
        for delegation_id, row in rows.items()
        if row["delivery_state"] == "dropped"
    } == {"pending-0", "pending-1"}
    assert {
        delegation_id
        for delegation_id, row in rows.items()
        if row["delivery_state"] == "pending"
    } == {"pending-2", "pending-3"}
    for delegation_id in ("pending-0", "pending-1"):
        row = rows[delegation_id]
        assert (
            row["delivery_disposition_reason"]
            == ad._PENDING_OVERFLOW_DISPOSITION
        )
        assert row["event"]["delegation_id"] == delegation_id
        assert row["result"]["summary"] == "done"


def test_pending_overflow_never_revokes_a_live_delivery_lease(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_MAX_DURABLE_PENDING", 0)
    _persist_terminal_in_home(tmp_path, "pending-live-claim")
    assert ad.claim_completion_delivery("pending-live-claim", "active-consumer")

    ad._prune_durable_records()

    row = ad.get_durable_delegation("pending-live-claim")
    assert row["delivery_state"] == "pending"
    assert row["delivery_disposition_reason"] == ""
    assert ad.complete_completion_delivery(
        "pending-live-claim",
        "active-consumer",
    )


def test_delivery_history_age_and_cap_boundaries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_MAX_RETAINED_COMPLETED", 2)
    monkeypatch.setattr(ad, "_DURABLE_RETENTION_SECONDS", 100.0)
    monkeypatch.setattr(ad.time, "time", lambda: 1000.0)
    for delegation_id, updated_at in (
        ("older-than-age", 899.999),
        ("exact-age-boundary", 900.0),
        ("newer-history", 901.0),
    ):
        _persist_terminal_in_home(tmp_path, delegation_id)
        ad.mark_completion_delivered(delegation_id)
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute(
                "UPDATE async_delegations SET updated_at=? WHERE delegation_id=?",
                (updated_at, delegation_id),
            )

    ad._prune_durable_records()

    assert ad.get_durable_delegation("older-than-age") is None
    assert ad.get_durable_delegation("exact-age-boundary") is not None
    assert ad.get_durable_delegation("newer-history") is not None


def test_recover_marks_abandoned_running_record_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = {
        "delegation_id": "deleg_abandoned",
        "session_key": "owner",
        "origin_ui_session_id": "",
        "parent_session_id": None,
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)
    with ad._DB_LOCK, ad._connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, owner_started_at=NULL WHERE delegation_id=?",
            (99999999, "deleg_abandoned"),
        )

    assert ad.recover_abandoned_delegations() == 1
    durable = ad.get_durable_delegation("deleg_abandoned")
    assert durable["state"] == "unknown"
    assert durable["delivery_state"] == "pending"
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    assert restored.get_nowait()["status"] == "unknown"


def test_origin_session_id_survives_persistence_round_trip(tmp_path, monkeypatch):
    """origin_session_id (the api_server wake self-post target) must be
    persisted with the durable dispatch record and restored on recovery —
    otherwise completions recovered after a process restart are unroutable
    to api_server sessions (in-memory record is gone)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = {
        "delegation_id": "deleg_wake_target",
        "session_key": "owner",
        "origin_ui_session_id": "",
        "origin_session_id": "raw-api-sid-42",
        "api_execution_context": _api_execution_context(),
        "parent_session_id": None,
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)

    # Durable record carries the wake target.
    durable = ad.get_durable_delegation("deleg_wake_target")
    assert durable["origin_session_id"] == "raw-api-sid-42"

    # Simulate the owning process dying, then recovery after restart: the
    # regenerated completion event must still carry the wake target.
    with ad._DB_LOCK, ad._connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET owner_pid=?, owner_started_at=NULL WHERE delegation_id=?",
            (99999999, "deleg_wake_target"),
        )
    restored = queue.Queue()
    assert ad.restore_undelivered_completions(restored) == 1
    evt = restored.get_nowait()
    assert evt["delegation_id"] == "deleg_wake_target"
    assert evt["origin_session_id"] == "raw-api-sid-42"
    assert evt["api_execution_context"] == _api_execution_context()
    assert evt["restored"] is True


def test_origin_session_id_migration_backfills_legacy_rows(tmp_path, monkeypatch):
    """Rows written by a pre-origin_session_id build must survive the ALTER
    TABLE migration and read back as an empty wake target."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Create a legacy-schema DB (no origin_session_id column).
    import sqlite3

    db_path = ad._db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(db_path))
    legacy.execute(
        """CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL
        )"""
    )
    legacy.execute(
        """INSERT INTO async_delegations
           (delegation_id, origin_session, state, dispatched_at, updated_at)
           VALUES ('deleg_legacy', 'owner', 'running', 1.0, 1.0)"""
    )
    legacy.commit()
    legacy.close()

    durable = ad.get_durable_delegation("deleg_legacy")
    assert durable is not None
    assert durable["origin_session_id"] == ""


def test_durable_delivery_claim_is_exclusive_and_retryable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = {
        "delegation_id": "deleg_claim", "session_key": "owner",
        "origin_ui_session_id": "", "parent_session_id": None,
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)
    ad._persist_completion(
        {"delegation_id": "deleg_claim", "status": "completed", "completed_at": 2.0},
        {"status": "completed", "summary": "done"},
    )

    assert ad.claim_completion_delivery("deleg_claim", "consumer-a")
    assert not ad.claim_completion_delivery("deleg_claim", "consumer-b")
    assert ad.release_completion_delivery("deleg_claim", "consumer-a")
    assert ad.claim_completion_delivery("deleg_claim", "consumer-b")
    assert ad.complete_completion_delivery("deleg_claim", "consumer-b")
    assert not ad.claim_completion_delivery("deleg_claim", "consumer-c")
    assert ad.get_durable_delegation("deleg_claim")["delivery_state"] == "delivered"


def test_delivery_renewal_exception_is_observable_and_refuses_ack(
    tmp_path, monkeypatch
):
    """A renewal exception relinquishes ownership instead of silently ACKing."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    record = {
        "delegation_id": "deleg_renew_error",
        "session_key": "owner",
        "origin_ui_session_id": "",
        "parent_session_id": None,
        "dispatched_at": 1.0,
    }
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_renew_error",
        "status": "completed",
        "completed_at": 2.0,
    }
    ad._persist_dispatch(record)
    ad._persist_completion(
        event,
        {"status": "completed", "summary": "done"},
    )
    assert ad.claim_completion_delivery("deleg_renew_error", "consumer-a")

    def _raise_renewal(_evt, _claim_id):
        raise OSError("simulated renewal storage failure")

    monkeypatch.setattr(ad, "renew_event_delivery", _raise_renewal)
    handle = ad.begin_event_delivery_renewal(
        event,
        "consumer-a",
        interval_seconds=0.01,
    )
    deadline = time.monotonic() + 2.0
    while not handle.ownership_lost:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    handle()

    assert not ad.complete_completion_delivery(
        "deleg_renew_error",
        "consumer-a",
    )
    assert ad.get_durable_delegation("deleg_renew_error")["delivery_state"] == "pending"
    assert ad.claim_completion_delivery("deleg_renew_error", "consumer-b")


# ---------------------------------------------------------------------------
# Integration: delegate_task(background=True) routing
# ---------------------------------------------------------------------------

def test_delegate_task_background_routes_async_and_does_not_block(monkeypatch):
    """delegate_task(background=True) returns a handle without running the
    child synchronously, and the child completes on the background thread.
    A single task is dispatched as a one-item background batch unit."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)  # a sync impl would hang delegate_task here
        return {
            "task_index": 0, "status": "completed", "summary": f"done: {goal}",
            "api_calls": 1, "duration_seconds": 0.1, "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    # monkeypatch (not `with`) so patches outlive delegate_task's return and
    # remain active while the background worker runs.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", slow_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        goal="the real task", context="ctx",
        background=True, parent_agent=parent,
    )

    import json
    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["delegation_id"].startswith("deleg_")
    # Non-blocking invariant: delegate_task returned while the child is STILL
    # blocked on the closed gate, so no completion event exists yet.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1  # one background batch unit, not finished

    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    # Single task rides the batch path → carries a 1-item results list.
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 1
    assert evt["results"][0]["summary"] == "done: the real task"
    text = format_process_notification(evt)
    assert text is not None
    assert "the real task" in text


def test_delegate_task_background_waits_inside_kanban_worker(monkeypatch):
    """A dispatcher-spawned Kanban worker is a finite process, so a required
    delegated result must return in-turn instead of becoming an orphaned
    background completion after the parent exits."""
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "kanban-worker-session"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    started = threading.Event()
    release = threading.Event()

    def delayed_child(task_index, goal, child=None, parent_agent=None, **kw):
        started.set()
        release.wait(timeout=5)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": "review approved",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", delayed_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)

    captured = {}

    def call_delegate():
        captured["output"] = dt.delegate_task(
            goal="independent review",
            background=True,
            parent_agent=parent,
        )

    caller = threading.Thread(target=call_delegate)
    caller.start()
    assert started.wait(timeout=2)
    assert caller.is_alive(), "Kanban delegate_task returned before its child finished"
    assert ad.active_count() == 0

    release.set()
    caller.join(timeout=5)
    assert not caller.is_alive()

    parsed = json.loads(captured["output"])
    assert parsed["results"][0]["summary"] == "review approved"
    assert "SYNCHRONOUSLY" in parsed["note"]
    assert process_registry.completion_queue.empty()


def test_delegate_task_background_uses_live_tui_agent_session_id(monkeypatch):
    """TUI async delegation must route to the live/compressed agent id.

    Regression: delegate_task captured the stale approval/session context key
    after compression rotated parent_agent.session_id. The resulting completion
    was orphaned and could be consumed by an unrelated desktop session poller.
    """
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools.approval import reset_current_session_key, set_current_session_key

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "post-compress-tip"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda *a, **k: {
            "task_index": 0,
            "status": "completed",
            "summary": "done",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
            "exit_reason": "completed",
        },
    )

    approval_token = set_current_session_key("pre-compress-parent")
    session_tokens = set_session_vars(
        source="tui",
        session_key="pre-compress-parent",
        ui_session_id="origin-tab",
    )
    try:
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)
        assert json.loads(out)["status"] == "dispatched"
        evt = _drain_one()
    finally:
        reset_current_session_key(approval_token)
        clear_session_vars(session_tokens)

    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt["session_key"] == "post-compress-tip"
    assert evt["origin_ui_session_id"] == "origin-tab"


def test_delegate_task_background_batch_runs_as_one_unit(monkeypatch):
    """A multi-item batch with background=True dispatches the WHOLE fan-out as
    ONE background unit (one handle, one async slot). The children run in
    parallel and join; the consolidated results come back as a single
    completion event when ALL of them finish."""
    import json
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"

    gate = threading.Event()

    def _blocking_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)
        return {
            "task_index": task_index, "status": "completed",
            "summary": f"done: {goal}", "api_calls": 1,
            "duration_seconds": 0.1, "model": "m", "exit_reason": "completed",
        }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }

    # Use monkeypatch (not a `with` block) so the patches stay active while the
    # background worker thread runs _execute_and_aggregate AFTER delegate_task
    # has already returned.
    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_run_single_child", _blocking_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    out = dt.delegate_task(
        tasks=[{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
        background=True,
        parent_agent=parent,
    )

    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["mode"] == "background"
    assert parsed["count"] == 3
    assert parsed["delegation_id"].startswith("deleg_")
    assert parsed["goals"] == ["a", "b", "c"]
    # ONE background unit for the whole fan-out (not three), and the call
    # returned while all children are still blocked → chat not blocked.
    assert process_registry.completion_queue.empty()
    assert ad.active_count() == 1

    # Release the children; the whole batch joins and emits ONE event.
    gate.set()
    evt = _drain_one()
    assert evt is not None
    assert evt["type"] == "async_delegation"
    assert evt.get("is_batch") is True
    assert len(evt["results"]) == 3
    summaries = sorted(r["summary"] for r in evt["results"])
    assert summaries == ["done: a", "done: b", "done: c"]
    # The consolidated notification names all three tasks in one block.
    text = format_process_notification(evt)
    assert text is not None
    assert "TASK 1/3" in text and "TASK 2/3" in text and "TASK 3/3" in text
    assert "done: a" in text and "done: b" in text and "done: c" in text
    # No more events — it's a single combined completion, not N of them.
    assert _drain_one() is None


def test_delegate_task_background_passes_progress_fn_to_async_registry(monkeypatch):
    import json
    from unittest.mock import MagicMock
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._interrupt_requested = False
    parent._active_children = []
    parent._active_children_lock = None

    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"
    fake_child.get_activity_summary.return_value = {
        "api_call_count": 4,
        "current_tool": "terminal",
        "last_activity_ts": 1234.5,
    }

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    captured = {}

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return {"status": "dispatched", "delegation_id": "deleg_progress"}

    monkeypatch.setattr(dt, "_build_child_agent", lambda **kw: fake_child)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *a, **k: creds)
    monkeypatch.setattr(ad, "dispatch_async_delegation_batch", fake_dispatch)

    out = dt.delegate_task(goal="background stall guard", background=True, parent_agent=parent)

    parsed = json.loads(out)
    assert parsed["status"] == "dispatched"
    assert parsed["delegation_id"] == "deleg_progress"
    # The dispatch wires a live progress sampler over the child agents so the
    # async registry's stale monitor can watch the detached batch. The token
    # includes last_activity_ts so streamed chunks count as liveness (each
    # chunk ticks _touch_activity), not just completed API calls.
    progress_fn = captured["progress_fn"]
    assert callable(progress_fn)
    token, in_tool = progress_fn()
    assert token == ((4, "terminal", 1234.5),)
    assert in_tool is True


def test_model_dispatch_forces_background():
    """The MODEL-facing dispatch path forces background=True for any top-level
    delegation (single task OR batch), and keeps it off for an orchestrator
    subagent (depth > 0). Direct delegate_task() callers are unaffected (they
    keep the synchronous default)."""
    import tools.delegate_tool as dt
    from unittest.mock import MagicMock

    top = MagicMock()
    top._delegate_depth = 0
    sub = MagicMock()
    sub._delegate_depth = 1

    # Registry-fallback helper: top-level always background, regardless of
    # single vs batch; subagent never.
    assert dt._model_background_value({"goal": "x"}, top) is True
    assert dt._model_background_value(
        {"tasks": [{"goal": "a"}, {"goal": "b"}]}, top
    ) is True
    assert dt._model_background_value({"tasks": [{"goal": "a"}]}, top) is True
    assert dt._model_background_value({"goal": "x"}, sub) is False
    assert dt._model_background_value(
        {"tasks": [{"goal": "a"}, {"goal": "b"}]}, sub
    ) is False


def test_run_agent_dispatch_forces_background():
    """run_agent._dispatch_delegate_task — the live model path — forces
    background on for any top-level delegation (single OR batch) and off for a
    subagent."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        agent = _FakeAgent()
        run_agent.AIAgent._dispatch_delegate_task(agent, {"goal": "x"})
        assert captured["background"] is True

        run_agent.AIAgent._dispatch_delegate_task(
            agent, {"tasks": [{"goal": "a"}, {"goal": "b"}]}
        )
        assert captured["background"] is True

        sub = _FakeAgent()
        sub._delegate_depth = 1
        run_agent.AIAgent._dispatch_delegate_task(sub, {"goal": "x"})
        assert captured["background"] is False


def test_dispatch_never_forwards_model_toolsets():
    """The model has no toolsets argument — subagents always inherit the
    parent's toolsets. Even if a model smuggles a `toolsets` key into the
    tool-call args, the live dispatch path must NOT forward it to
    delegate_task (which no longer accepts it) and must not crash."""
    from unittest.mock import patch
    import run_agent

    class _FakeAgent:
        _delegate_depth = 0

    captured = {}

    def _fake_delegate(**kwargs):
        captured.update(kwargs)
        return "{}"

    with patch("tools.delegate_tool.delegate_task", _fake_delegate):
        run_agent.AIAgent._dispatch_delegate_task(
            _FakeAgent(), {"goal": "x", "toolsets": ["web", "terminal"]}
        )
    assert "toolsets" not in captured


def test_delegate_task_background_detaches_child_from_parent(monkeypatch):
    """A background child must NOT remain in parent._active_children —
    otherwise parent-turn interrupts / cache evicts / session close would
    kill the detached subagent mid-run."""
    from unittest.mock import MagicMock, patch
    import tools.delegate_tool as dt

    parent = MagicMock()
    parent._delegate_depth = 0
    parent.session_id = "sess"
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    fake_child = MagicMock()
    fake_child._delegate_role = "leaf"
    fake_child._subagent_id = "s1"

    gate = threading.Event()

    def slow_child(task_index, goal, child=None, parent_agent=None, **kw):
        gate.wait(timeout=60)
        return {"task_index": 0, "status": "completed", "summary": "ok"}

    def build_and_register(**kw):
        # Mirror what the real _build_child_agent does: register the child
        # for interrupt propagation.
        parent._active_children.append(fake_child)
        return fake_child

    creds = {
        "model": "m", "provider": None, "base_url": None, "api_key": None,
        "api_mode": None, "command": None, "args": None,
    }
    with patch.object(dt, "_build_child_agent", side_effect=build_and_register), \
         patch.object(dt, "_run_single_child", side_effect=slow_child), \
         patch.object(dt, "_resolve_delegation_credentials", return_value=creds):
        out = dt.delegate_task(goal="bg task", background=True, parent_agent=parent)

    import json
    assert json.loads(out)["status"] == "dispatched"
    # Child detached immediately at dispatch, while it is still running.
    assert fake_child not in parent._active_children
    gate.set()
    assert _drain_one() is not None


def test_concurrent_dispatch_respects_capacity():
    """Two threads racing dispatch with cap=1 must yield exactly one accept
    (capacity check and record insert are atomic under the records lock)."""
    gate = threading.Event()

    def blocker():
        gate.wait(timeout=60)
        return {"status": "completed", "summary": "x"}

    results = []
    barrier = threading.Barrier(2)

    def racer():
        barrier.wait(timeout=5)
        results.append(
            ad.dispatch_async_delegation(
                goal="race", context=None, toolsets=None, role="leaf",
                model="m", session_key="", runner=blocker,
                max_async_children=1,
            )
        )

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["dispatched", "rejected"]
    gate.set()


# ---------------------------------------------------------------------------
# Gateway routing: session_key -> platform/chat_id, rich formatting, injection
# ---------------------------------------------------------------------------

def _make_async_evt(**over):
    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_x1",
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "context": "repo /tmp/p",
        "toolsets": ["terminal"],
        "role": "leaf",
        "model": "m",
        "status": "completed",
        "summary": "Found the bug in test_foo",
        "api_calls": 4,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
    }
    evt.update(over)
    return evt


def test_gateway_enriches_routing_from_session_key():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt()
    runner._enrich_async_delegation_routing(evt)
    assert evt["platform"] == "telegram"
    assert evt["chat_id"] == "12345"
    assert evt["thread_id"] == "678"


def test_gateway_formatter_renders_async_block():
    from gateway.run import _format_gateway_process_notification

    txt = _format_gateway_process_notification(_make_async_evt())
    assert txt is not None
    assert "ASYNC DELEGATION COMPLETE" in txt
    assert "Found the bug in test_foo" in txt
    assert "Investigate flaky test" in txt


def test_gateway_watch_drain_requeues_async_without_looping():
    from gateway.run import _drain_gateway_watch_events

    q = queue.Queue()
    async_evt = _make_async_evt()
    watch_evt = {
        "type": "watch_match",
        "session_id": "proc_1",
        "command": "pytest",
        "pattern": "READY",
        "output": "READY",
    }
    q.put(async_evt)
    q.put(watch_evt)

    watch_events = _drain_gateway_watch_events(q)

    assert watch_events == [watch_evt]
    assert q.qsize() == 1
    assert q.get_nowait() == async_evt


def test_gateway_builds_routable_source_from_enriched_event():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt()
    runner._enrich_async_delegation_routing(evt)
    src = runner._build_process_event_source(evt)
    assert src is not None
    assert src.platform.value == "telegram"
    assert src.chat_id == "12345"


def test_gateway_cli_origin_event_left_unrouted():
    """An empty session_key (CLI origin) is left without routing fields."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    evt = _make_async_evt(session_key="")
    runner._enrich_async_delegation_routing(evt)
    assert "platform" not in evt
