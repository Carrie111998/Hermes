"""Release contracts for fail-closed native Responses compaction."""

from __future__ import annotations

import sqlite3
import sys
import types
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent
from agent.codex_responses_adapter import _classify_responses_issuer
from agent.responses_compaction import (
    COMPACTION_LEDGER_VERSION,
    NativeCompactionLedger,
    NativeCompactionPolicy,
    NativeCompactionReadError,
    NativeCompactionRoute,
    NativeCompactionStateError,
    advance_policy_after_success,
    build_native_request_overrides,
    compaction_route_key,
    load_policy_for_route,
    persist_policy_compare_and_set,
    route_for_request,
)
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _isolated_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run_agent, "_hermes_home", tmp_path)


def _route(model: str) -> NativeCompactionRoute:
    return route_for_request(
        provider="openai-codex",
        endpoint="https://chatgpt.com/backend-api/codex",
        model=model,
    )


def _observed_policy(route: NativeCompactionRoute, encrypted: str) -> NativeCompactionPolicy:
    return advance_policy_after_success(
        NativeCompactionPolicy(route=route),
        codex_output_items=[
            {
                "type": "compaction",
                "encrypted_content": encrypted,
                "_issuer_kind": route.issuer_kind,
                "_compaction_route": route.to_dict(),
            }
        ],
        replay_attempted=False,
    )


def test_v3_route_ledger_retains_independent_route_state(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("ledger", "test", model="gpt-5.6-sol")
    route_a = _route("gpt-5.6-sol")
    route_b = _route("gpt-5.6-mini")

    receipt_a = persist_policy_compare_and_set(
        db,
        session_id,
        NativeCompactionPolicy(route=route_a).transition(
            "unsupported", error="unsupported"
        ),
    )
    policy_a = receipt_a.policy
    assert receipt_a.committed is True
    policy_b = replace(
        _observed_policy(route_b, "opaque-b"), revision=policy_a.revision
    )
    sidecar_b = [
        {
            "type": "compaction",
            "encrypted_content": "opaque-b",
            "_issuer_kind": route_b.issuer_kind,
            "_compaction_route": route_b.to_dict(),
        }
    ]
    db.append_message(
        session_id,
        role="assistant",
        content="",
        finish_reason="incomplete",
        codex_output_items=sidecar_b,
        codex_responses_compaction_policy=policy_b.to_dict(),
        expected_codex_responses_compaction_revision=policy_a.revision,
    )

    assert policy_a.capability == "unsupported"
    assert policy_b.capability == "item_observed"
    assert load_policy_for_route(db, session_id, route_a).capability == "unsupported"
    assert load_policy_for_route(db, session_id, route_b).capability == "item_observed"
    raw = db.get_codex_responses_compaction_state(session_id)
    assert raw["version"] == COMPACTION_LEDGER_VERSION == 3
    assert len(raw["routes"]) == 2
    db.close()


def test_ordinary_cas_cannot_publish_checkpoint_state_without_message(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("cas-guard", "test", model="gpt-5.6-sol")
    policy = _observed_policy(_route("gpt-5.6-sol"), "orphaned")
    candidate = NativeCompactionLedger.empty().with_policy(policy).to_dict()

    with pytest.raises(ValueError, match="atomic message append"):
        db.compare_and_set_codex_responses_compaction_state(
            session_id,
            expected_revision=0,
            state=candidate,
        )

    assert db.get_codex_responses_compaction_state(session_id) == (
        NativeCompactionLedger.empty().to_dict()
    )
    assert db.get_messages_as_conversation(session_id) == []
    db.close()


def test_ordinary_cas_cannot_drop_or_regress_unrelated_routes(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("cas-routes", "test", model="gpt-5.6-sol")
    route_a = _route("gpt-5.6-sol")
    route_b = NativeCompactionRoute(
        issuer_kind="codex_backend",
        endpoint="https://chatgpt.com/backend-api/codex",
        model="gpt-5.6-codex",
    )
    initial = NativeCompactionLedger.empty().with_policy(
        NativeCompactionPolicy(route=route_a).transition("shape_accepted")
    )
    assert db.compare_and_set_codex_responses_compaction_state(
        session_id,
        expected_revision=0,
        state=initial.to_dict(),
    )

    persisted = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    dropping = persisted.with_policy(NativeCompactionPolicy(route=route_b)).to_dict()
    dropping["routes"].pop(compaction_route_key(route_a))
    with pytest.raises(ValueError, match="cannot drop unrelated routes"):
        db.compare_and_set_codex_responses_compaction_state(
            session_id,
            expected_revision=persisted.revision,
            state=dropping,
        )

    regressed = persisted.with_policy(
        NativeCompactionPolicy(
            route=route_a,
            capability="unknown",
            revision=persisted.revision,
        )
    )
    with pytest.raises(ValueError, match="not monotonic"):
        db.compare_and_set_codex_responses_compaction_state(
            session_id,
            expected_revision=persisted.revision,
            state=regressed.to_dict(),
        )

    unchanged = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    assert unchanged.to_dict() == persisted.to_dict()
    db.close()


def test_route_identity_is_canonical_and_never_persists_url_credentials():
    route = route_for_request(
        provider="openai",
        endpoint="HTTPS://user:secret@API.OPENAI.COM/v1/?token=also-secret#fragment",
        model="gpt-5.6-sol",
    )

    assert route.endpoint == "https://api.openai.com/v1"
    assert "secret" not in str(route.to_dict())
    assert NativeCompactionRoute.from_dict(route.to_dict()) == route
    assert _classify_responses_issuer(
        base_url="HTTPS://user:secret@API.OPENAI.COM/v1/?token=also-secret"
    ) == route.issuer_kind


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(version=2),
        lambda value: value.update(revision="1"),
        lambda value: value["routes"].update({"not-a-route-key": {}}),
        lambda value: value.update(unexpected=True),
        lambda value: next(iter(value["routes"].values())).update(
            last_compaction_digest=None
        ),
        lambda value: next(iter(value["routes"].values())).update(
            compaction_count=0
        ),
    ],
)
def test_v3_route_ledger_rejects_noncanonical_state(mutation):
    ledger = NativeCompactionLedger.empty().with_policy(
        _observed_policy(_route("gpt-5.6-sol"), "opaque")
    )
    raw = ledger.to_dict()
    mutation(raw)

    with pytest.raises(NativeCompactionStateError):
        NativeCompactionLedger.from_dict(raw)


def test_malformed_durable_state_fails_closed_without_inventing_route_state(
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("corrupt", "test", model="gpt-5.6-sol")
    db._conn.execute(
        "UPDATE sessions SET codex_responses_compaction_state = ? WHERE id = ?",
        ('{"version":3,"revision":"not-an-int","routes":{}}', session_id),
    )
    db._conn.commit()

    with pytest.raises(NativeCompactionReadError):
        load_policy_for_route(db, session_id, _route("gpt-5.6-sol"))
    db.close()


def test_checkpoint_message_and_v3_ledger_commit_atomically(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("atomic", "test", model="gpt-5.6-sol")
    policy = _observed_policy(_route("gpt-5.6-sol"), "opaque-atomic")
    sidecar = [
        {
            "type": "compaction",
            "encrypted_content": "opaque-atomic",
            "_issuer_kind": policy.route.issuer_kind,
            "_compaction_route": policy.route.to_dict(),
        }
    ]
    db._conn.execute(
        """CREATE TRIGGER abort_compaction_state
           BEFORE UPDATE OF codex_responses_compaction_state ON sessions
           BEGIN SELECT RAISE(ABORT, 'state write failed'); END"""
    )
    db._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="state write failed"):
        db.append_message(
            session_id,
            role="assistant",
            content="",
            finish_reason="incomplete",
            codex_output_items=sidecar,
            codex_responses_compaction_policy=policy.to_dict(),
            expected_codex_responses_compaction_revision=0,
        )

    assert db.get_messages_as_conversation(session_id) == []
    assert db.get_codex_responses_compaction_state(session_id) == (
        NativeCompactionLedger.empty().to_dict()
    )
    db.close()


def test_successful_checkpoint_publishes_message_and_route_state_together(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("atomic-success", "test", model="gpt-5.6-sol")
    policy = _observed_policy(_route("gpt-5.6-sol"), "opaque-success")
    sidecar = [
        {
            "type": "compaction",
            "encrypted_content": "opaque-success",
            "_issuer_kind": policy.route.issuer_kind,
            "_compaction_route": policy.route.to_dict(),
        }
    ]

    db.append_message(
        session_id,
        role="assistant",
        content="",
        finish_reason="incomplete",
        codex_output_items=sidecar,
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )

    messages = db.get_messages_as_conversation(session_id)
    ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    assert messages[-1]["codex_output_items"] == sidecar
    assert ledger.revision == 1
    assert ledger.policy_for(policy.route).capability == "item_observed"
    assert ledger.policy_for(policy.route).last_compaction_digest == (
        policy.last_compaction_digest
    )
    db.close()


def test_atomic_checkpoint_rejects_sidecar_policy_mismatch(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("atomic-mismatch", "test", model="gpt-5.6-sol")
    policy = _observed_policy(_route("gpt-5.6-sol"), "committed")
    mismatched_sidecar = [
        {
            "type": "compaction",
            "encrypted_content": "different",
            "_issuer_kind": policy.route.issuer_kind,
            "_compaction_route": policy.route.to_dict(),
        }
    ]

    with pytest.raises(ValueError, match="must match"):
        db.append_message(
            session_id,
            role="assistant",
            content="",
            finish_reason="incomplete",
            codex_output_items=mismatched_sidecar,
            codex_responses_compaction_policy=policy.to_dict(),
            expected_codex_responses_compaction_revision=0,
        )

    assert db.get_messages_as_conversation(session_id) == []
    assert db.get_codex_responses_compaction_state(session_id) == (
        NativeCompactionLedger.empty().to_dict()
    )
    db.close()


def test_atomic_checkpoint_rejects_counter_jump_and_rolls_back(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("counter-jump", "test", model="gpt-5.6-sol")
    route = _route("gpt-5.6-sol")
    policy = _observed_policy(route, "opaque-counter")
    jumped = replace(policy, compaction_count=policy.compaction_count + 5)
    sidecar = [
        {
            "type": "compaction",
            "encrypted_content": "opaque-counter",
            "_issuer_kind": route.issuer_kind,
            "_compaction_route": route.to_dict(),
        }
    ]

    with pytest.raises(ValueError, match="counter is inconsistent"):
        db.append_message(
            session_id,
            role="assistant",
            content="",
            finish_reason="incomplete",
            codex_output_items=sidecar,
            codex_responses_compaction_policy=jumped.to_dict(),
            expected_codex_responses_compaction_revision=0,
        )

    assert db.get_messages_as_conversation(session_id) == []
    assert db.get_codex_responses_compaction_state(session_id) == (
        NativeCompactionLedger.empty().to_dict()
    )
    db.close()


def test_import_rejects_malformed_native_compaction_ledger(tmp_path):
    source = SessionDB(db_path=tmp_path / "source.db")
    session_id = source.create_session("source", "test", model="gpt-5.6-sol")
    blob = source.export_session(session_id)
    source.close()
    assert blob is not None
    blob["codex_responses_compaction_state"] = {
        "version": 2,
        "revision": 0,
        "routes": {},
    }

    imported = SessionDB(db_path=tmp_path / "imported.db")
    result = imported.import_sessions([blob])

    assert result["ok"] is False
    assert result["imported"] == 0
    assert imported.get_session(session_id) is None
    assert any("ledger version" in error["error"] for error in result["errors"])
    imported.close()


def _runtime_agent(monkeypatch):
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    agent = run_agent.AIAgent(
        model="gpt-5.6-sol",
        provider="openai-codex",
        api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="test-token",
        quiet_mode=True,
        max_iterations=5,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cleanup_task_resources = lambda task_id: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    agent.request_overrides = {}
    agent._session_db = SessionDB(
        db_path=run_agent._hermes_home / f"runtime-{uuid.uuid4().hex}.db"
    )
    agent.session_id = f"runtime-{uuid.uuid4().hex}"
    agent._session_db.create_session(
        agent.session_id, "test", model=agent.model
    )
    agent._session_db_created = True
    agent._native_compaction_policy = NativeCompactionPolicy(
        route=_route("gpt-5.6-sol")
    )
    agent.codex_responses_auto_compaction = "native"
    agent.compression_enabled = True
    agent.codex_responses_compact_threshold = 200_000
    return agent


def _compaction_only(encrypted: str):
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="compaction",
                id=f"cmp_{encrypted}",
                encrypted_content=encrypted,
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1, total_tokens=6),
    )


def test_compaction_only_continuation_is_bounded_to_one(monkeypatch):
    agent = _runtime_agent(monkeypatch)
    responses = [_compaction_only("first"), _compaction_only("second")]
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)
    summary_calls = []
    monkeypatch.setattr(
        agent,
        "_run_codex_stream",
        lambda kwargs, **_extra: (
            summary_calls.append(kwargs)
            or _text_response("Bounded summary from the newest checkpoint.")
        ),
    )
    result = agent.run_conversation("Do not loop forever")

    assert result["completed"] is False
    assert result["partial"] is True
    assert result["final_response"] == (
        "Native compaction could not complete after the single checkpoint continuation."
    )
    assert len(calls) == 2
    assert summary_calls == []
    assert agent._native_compaction_policy.capability == "quarantined"


def test_only_sidecar_matching_committed_digest_is_replayed(monkeypatch):
    agent = _runtime_agent(monkeypatch)
    policy = _observed_policy(_route("gpt-5.6-sol"), "committed")

    def _history(encrypted: str):
        return [
            {
                "role": "assistant",
                "content": "checkpoint",
                "codex_output_items": [
                    {
                        "type": "compaction",
                        "encrypted_content": encrypted,
                        "_issuer_kind": policy.route.issuer_kind,
                        "_compaction_route": policy.route.to_dict(),
                    }
                ],
            }
        ]

    committed_history = _history("committed")
    agent._session_db.append_message(
        agent.session_id,
        role="assistant",
        content="checkpoint",
        codex_output_items=committed_history[0]["codex_output_items"],
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    agent._native_compaction_policy = policy

    mismatched = agent._build_api_kwargs(_history("uncommitted"))
    assert all(item.get("type") != "compaction" for item in mismatched["input"])
    assert agent._native_compaction_replay_attempted is False

    matching = agent._build_api_kwargs(committed_history)
    assert matching["input"][0]["type"] == "compaction"
    assert matching["input"][0]["encrypted_content"] == "committed"
    assert agent._native_compaction_replay_attempted is True

    mixed_history = _history("committed") + [
        {"role": "user", "content": "after committed"},
        _history("uncommitted")[0],
    ]
    mixed = agent._build_api_kwargs(mixed_history)
    replayed_compactions = [
        item for item in mixed["input"] if item.get("type") == "compaction"
    ]
    assert replayed_compactions == [
        {"type": "compaction", "encrypted_content": "committed"}
    ]
    assert all(
        item.get("encrypted_content") != "uncommitted"
        for item in mixed["input"]
    )

    altered_same_blob = _history("committed")
    altered_same_blob[0]["codex_output_items"].append(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "not committed"}],
            "_issuer_kind": policy.route.issuer_kind,
            "_compaction_route": policy.route.to_dict(),
        }
    )
    altered = agent._build_api_kwargs(altered_same_blob)
    assert all(item.get("type") != "compaction" for item in altered["input"])


class _InvalidEncryptedContentError(Exception):
    status_code = 400
    body = {
        "error": {
            "code": "invalid_encrypted_content",
            "message": "encrypted state rejected",
        }
    }


def test_rejected_compaction_replay_quarantines_without_deleting_evidence(
    monkeypatch,
):
    agent = _runtime_agent(monkeypatch)
    policy = _observed_policy(_route("gpt-5.6-sol"), "rejected")
    agent._native_compaction_policy = policy
    checkpoint = {
        "role": "assistant",
        "content": "checkpoint",
        "codex_output_items": [
            {
                "type": "compaction",
                "encrypted_content": "rejected",
                "_issuer_kind": policy.route.issuer_kind,
                "_compaction_route": policy.route.to_dict(),
            }
        ],
    }
    agent._session_db.append_message(
        agent.session_id,
        role="assistant",
        content="checkpoint",
        codex_output_items=checkpoint["codex_output_items"],
        codex_responses_compaction_policy=policy.to_dict(),
        expected_codex_responses_compaction_revision=0,
    )
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _InvalidEncryptedContentError("rejected")
        return _text_response("recovered")

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)
    result = agent.run_conversation(
        "Recover safely", conversation_history=[checkpoint]
    )

    assert result["completed"] is True
    assert calls[0]["input"][0]["type"] == "compaction"
    assert all(item.get("type") != "compaction" for item in calls[1]["input"])
    assert agent._native_compaction_policy.capability == "quarantined"
    preserved = [
        message
        for message in result["messages"]
        if message.get("codex_output_items")
    ]
    assert preserved == [checkpoint]


def _text_response(text: str):
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="message",
                id="msg_after_compaction",
                role="assistant",
                status="completed",
                phase=None,
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1, total_tokens=6),
    )


def test_runtime_commits_checkpoint_before_single_continuation(monkeypatch, tmp_path):
    agent = _runtime_agent(monkeypatch)
    db = SessionDB(db_path=tmp_path / "runtime.db")
    session_id = db.create_session("runtime", "test", model=agent.model)
    agent._session_db = db
    agent.session_id = session_id
    responses = [_compaction_only("runtime"), _text_response("done")]
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)
    result = agent.run_conversation("Commit before replay")

    assert result["completed"] is True
    assert len(calls) == 2
    assert calls[1]["input"][0]["type"] == "compaction"
    messages = db.get_messages_as_conversation(session_id)
    checkpoints = [message for message in messages if message.get("codex_output_items")]
    assert len(checkpoints) == 1
    ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    assert ledger.policy_for(_route(agent.model)).capability == "replay_verified"
    db.close()


def test_runtime_checkpoint_failure_stops_before_continuation(monkeypatch, tmp_path):
    agent = _runtime_agent(monkeypatch)
    db = SessionDB(db_path=tmp_path / "runtime-failure.db")
    session_id = db.create_session("runtime-failure", "test", model=agent.model)
    agent._session_db = db
    agent.session_id = session_id
    db._conn.execute(
        """CREATE TRIGGER abort_runtime_compaction_state
           BEFORE UPDATE OF codex_responses_compaction_state ON sessions
           BEGIN SELECT RAISE(ABORT, 'runtime state write failed'); END"""
    )
    db._conn.commit()
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        return _compaction_only("runtime-failure")

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)
    result = agent.run_conversation("Fail closed")

    assert result["completed"] is False
    assert result["error"] == "Native compaction checkpoint persistence failed"
    assert len(calls) == 1
    assert all(
        not message.get("codex_output_items")
        for message in db.get_messages_as_conversation(session_id)
    )
    assert db.get_codex_responses_compaction_state(session_id) == (
        NativeCompactionLedger.empty().to_dict()
    )
    assert agent._native_compaction_policy.capability == "quarantined"
    db.close()


def test_checkpoint_flush_reconciles_one_concurrent_unrelated_route(
    monkeypatch, tmp_path
):
    agent = _runtime_agent(monkeypatch)
    db = SessionDB(db_path=tmp_path / "checkpoint-conflict.db")
    session_id = db.create_session("checkpoint-conflict", "test", model=agent.model)
    agent._session_db = db
    agent.session_id = session_id
    checkpoint_policy = _observed_policy(_route(agent.model), "route-a")
    checkpoint = {
        "role": "assistant",
        "content": "",
        "finish_reason": "incomplete",
        "codex_output_items": [
            {
                "type": "compaction",
                "encrypted_content": "route-a",
                "_issuer_kind": checkpoint_policy.route.issuer_kind,
                "_compaction_route": checkpoint_policy.route.to_dict(),
            }
        ],
    }
    agent._native_compaction_pending_policy = checkpoint_policy
    agent._native_compaction_pending_commits = {
        id(checkpoint): checkpoint_policy
    }

    unrelated = NativeCompactionPolicy(route=_route("gpt-5.6-mini")).transition(
        "unsupported", error="not-supported"
    )
    persist_policy_compare_and_set(db, session_id, unrelated)

    assert agent._flush_messages_to_session_db([checkpoint], []) is True
    ledger = NativeCompactionLedger.from_dict(
        db.get_codex_responses_compaction_state(session_id)
    )
    assert ledger.revision == 2
    assert ledger.policy_for(checkpoint_policy.route).capability == "item_observed"
    assert ledger.policy_for(unrelated.route).capability == "unsupported"
    assert len(db.get_messages_as_conversation(session_id)) == 1
    db.close()
