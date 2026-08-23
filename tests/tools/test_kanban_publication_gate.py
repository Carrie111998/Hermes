from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools import kanban_tools as kt


META = {"custom_fact": {"nested": True}}
PROOF = {"repo_path": "/repo", "branch": "feature/x", "expected_base": "main", "pr_number": 42}
TASK = SimpleNamespace(assignee="forge", publication_required=True)
OPTIONAL_TASK = SimpleNamespace(assignee="forge", publication_required=False)


def _run(monkeypatch, responses, *, metadata=META, profile="forge", **proof):
    monkeypatch.setenv("HERMES_PROFILE", profile)
    monkeypatch.setattr(kt.Path, "is_dir", lambda _self: True)
    values = iter(responses)
    monkeypatch.setattr(kt, "_publication_command", lambda *_args: next(values))
    return kt._publication_gate(TASK, metadata, **proof)


def _valid_prefix():
    return [(True, "/repo"), (True, ""), (True, "abc"), (True, "feature/x"), (True, "abc remote")]


def test_schema_has_explicit_proof_fields_and_handler():
    props = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert props["metadata"]["additionalProperties"] is True
    assert {name: props[name]["type"] for name in PROOF} == {
        "repo_path": "string", "branch": "string", "expected_base": "string", "pr_number": "integer",
    }
    entry = next(item for item in kt.registry.get_all_entries() if item.name == "kanban_complete")
    assert entry.schema is kt.KANBAN_COMPLETE_SCHEMA
    assert entry.handler is kt._handle_complete


def test_gate_accepts_matching_open_pr(monkeypatch):
    responses = _valid_prefix() + [(True, '{"state":"OPEN","headRefOid":"abc","baseRefName":"main"}')]
    assert _run(monkeypatch, responses, **PROOF) is None


@pytest.mark.parametrize("field", tuple(PROOF))
def test_missing_explicit_field_rejected(monkeypatch, field):
    proof = {key: value for key, value in PROOF.items() if key != field}
    rejection = _run(monkeypatch, [], **proof)
    assert field in (rejection or "")


def test_metadata_cannot_substitute_for_explicit_proof(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb

    out = json.loads(kt._handle_complete({"summary": "finished", "metadata": {**META, **PROOF}}))
    assert "missing explicit field" in out["error"]

    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_metadata_cannot_disable_persisted_policy(worker_env, monkeypatch):
    out = json.loads(
        kt._handle_complete(
            {
                "summary": "finished",
                "metadata": {**META, "publication_required": False, **PROOF},
            }
        )
    )
    assert "missing explicit field" in out["error"]

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_explicit_false_skips_proof(monkeypatch):
    monkeypatch.setattr(kt, "_publication_command", lambda *_: pytest.fail("proof should be skipped"))
    assert kt._publication_gate(OPTIONAL_TASK, {"repo_path": "/bad"}) is None


def test_non_forge_skips_proof(monkeypatch):
    assert _run(monkeypatch, [], profile="steward") is None


def test_existing_rejections_remain(monkeypatch):
    assert "not clean" in (_run(monkeypatch, [(True, "/repo"), (True, " M file.py")], **PROOF) or "")
    responses = _valid_prefix()[:4] + [(True, "")]
    assert "origin branch" in (_run(monkeypatch, responses, **PROOF) or "")
    responses = _valid_prefix() + [(True, '{"state":"OPEN","headRefOid":"def","baseRefName":"main"}')]
    assert "PR head" in (_run(monkeypatch, responses, **PROOF) or "")


def test_handler_passes_explicit_fields_and_keeps_metadata(worker_env, monkeypatch):
    captured = {}

    def reject(_task, metadata, **proof):
        captured["metadata"] = metadata
        captured["proof"] = proof
        return "publication proof rejected: test"

    monkeypatch.setattr(kt, "_publication_gate", reject)
    out = json.loads(kt._handle_complete({"summary": "finished", "metadata": META, **PROOF}))
    assert captured["metadata"]["custom_fact"] == META["custom_fact"]
    assert captured["proof"] == PROOF
    assert "publication proof rejected" in out["error"]


def test_completion_rejection_leaves_task_running(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "forge")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="publication-test", assignee="forge")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setattr(kt, "_publication_gate", lambda *args, **kwargs: "publication proof rejected: dirty")
    out = json.loads(kt._handle_complete({"summary": "finished"}))
    assert "publication proof rejected" in out["error"]
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "forge")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="publication-test", assignee="forge")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_metadata_only_does_not_invoke_git(worker_env, monkeypatch):
    monkeypatch.setattr(kt, "_publication_command", lambda *_: pytest.fail("metadata fallback"))
    out = json.loads(kt._handle_complete({"summary": "finished", "metadata": PROOF}))
    assert "missing explicit field" in out["error"]


def test_pr_number_must_be_integer(monkeypatch):
    rejection = _run(monkeypatch, _valid_prefix(), repo_path="/repo", branch="feature/x", expected_base="main", pr_number="bad")
    assert "pr_number" in (rejection or "")


def test_schema_description_separates_metadata_from_proof():
    description = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]["metadata"]["description"]
    assert "explicit" in description
    assert "metadata field" not in description


def test_null_policy_preserves_legacy_required_behavior(worker_env, monkeypatch):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET publication_required = NULL WHERE id = ?", (worker_env,))
        conn.commit()
    finally:
        conn.close()

    out = json.loads(kt._handle_complete({"summary": "finished", "metadata": META}))
    assert "missing explicit field" in out["error"]


def test_normal_metadata_is_preserved_as_free_form():
    assert kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]["metadata"]["additionalProperties"] is True
    assert "repo_path" not in kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]["metadata"].get("properties", {})


def test_repo_path_validation_uses_explicit_value(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "forge")
    monkeypatch.setattr(kt.Path, "is_dir", lambda _self: False)
    assert "existing directory" in (kt._publication_gate(TASK, META, **PROOF) or "")


def test_expected_base_validation_uses_explicit_value(monkeypatch):
    responses = _valid_prefix() + [(True, '{"state":"OPEN","headRefOid":"abc","baseRefName":"develop"}')]
    assert "PR base" in (_run(monkeypatch, responses, **{**PROOF, "expected_base": "main"}) or "")


def test_branch_validation_uses_explicit_value(monkeypatch):
    responses = _valid_prefix() + [(True, '{"state":"OPEN","headRefOid":"abc","baseRefName":"main"}')]
    assert "branch" in (_run(monkeypatch, responses, **{**PROOF, "branch": "other"}) or "")


def test_pr_number_explicit_value_reaches_gh(monkeypatch):
    seen = []
    responses = _valid_prefix() + [(True, '{"state":"OPEN","headRefOid":"abc","baseRefName":"main"}')]
    def command(args, repo):
        seen.append(args)
        return responses.pop(0)
    monkeypatch.setenv("HERMES_PROFILE", "forge")
    monkeypatch.setattr(kt.Path, "is_dir", lambda _self: True)
    monkeypatch.setattr(kt, "_publication_command", command)
    assert kt._publication_gate(TASK, META, **PROOF) is None
    assert "42" in seen[-1]


def test_publication_schema_has_no_required_fields_for_false_mode():
    assert kt.KANBAN_COMPLETE_SCHEMA["parameters"]["required"] == []


def test_metadata_only_rejection_is_recoverable(worker_env):
    out = json.loads(kt._handle_complete({"summary": "finished", "metadata": PROOF}))
    assert "rejected" in out["error"]

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, worker_env)
        assert task is not None
        assert task.status == "running"
    finally:
        conn.close()


def test_non_forge_does_not_need_proof(monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "steward")
    assert kt._publication_gate(SimpleNamespace(assignee="steward"), META) is None


def test_explicit_fields_are_top_level_schema_properties():
    props = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert all(field in props for field in PROOF)


def test_handler_forwards_absent_fields(worker_env, monkeypatch):
    captured = {}
    monkeypatch.setattr(kt, "_publication_gate", lambda task, metadata, **proof: captured.update(proof) or "reject")
    kt._handle_complete({"summary": "finished"})
    assert captured == {field: None for field in PROOF}


def test_artifacts_and_normal_handoff_schema_remain_present():
    props = kt.KANBAN_COMPLETE_SCHEMA["parameters"]["properties"]
    assert "artifacts" in props and "created_cards" in props and "metadata" in props


def test_proof_metadata_does_not_mutate_input(monkeypatch):
    metadata = dict(META)
    _run(monkeypatch, _valid_prefix() + [(True, '{"state":"OPEN","headRefOid":"abc","baseRefName":"main"}')], metadata=metadata, **PROOF)
    assert metadata == META


def test_completion_handler_uses_summary_or_result_independently(worker_env, monkeypatch):
    monkeypatch.setattr(kt, "_publication_gate", lambda *args, **kwargs: "reject")
    out = json.loads(kt._handle_complete({"result": "finished"}))
    assert "error" in out


def test_explicit_proof_contract_is_exact():
    assert set(PROOF) == {"repo_path", "branch", "expected_base", "pr_number"}
