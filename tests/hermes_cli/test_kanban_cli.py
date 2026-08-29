"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for profile in ("implementer", "reviewer", "independent-reviewer"):
        (home / "profiles" / profile).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _source_receipt(sha: str) -> dict[str, str]:
    """Coordinator-attested provenance for scratch candidates in CLI fixtures."""
    return {
        "candidate_sha": sha,
        "subject": "test scratch artifact",
        "provenance": "test coordinator receipt",
        "producer_profile": "implementer",
    }


def _complete_bound_gate(conn, gate_id: str, *, sha: str, manifest_hash: str, monkeypatch) -> None:
    """Model the dispatched gate worker, then return to the coordinator lane."""
    gate = kb.get_task(conn, gate_id)
    assert gate is not None and gate.assignee
    claimed = kb.claim_task(conn, gate_id)
    assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_PROFILE", gate.assignee)
    assert kb.complete_task(
        conn, gate_id, result="pass", summary="validated",
        metadata={"candidate_sha": sha, "manifest_hash": manifest_hash},
        expected_run_id=claimed.current_run_id,
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------



def _parse_kanban(argv):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser.parse_args(["kanban", *argv])


def test_cli_evidence_freeze_rejects_foreign_runtime_profile(kanban_home, tmp_path, capsys, monkeypatch):
    """CLI binds a freeze to its runtime profile rather than --authority."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    payload = b"cli evidence"
    blob = tmp_path / "evidence.txt"
    blob.write_bytes(payload)
    sha = "f" * 40
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="source", assignee="implementer")
        monkeypatch.setenv("HERMES_PROFILE", "coordinator")
        kb.create_candidate(conn, task_id, sha=sha, source_receipt=_source_receipt(sha))
        attachment_id = kb.add_attachment(
            conn, task_id, filename="evidence.txt", stored_path=str(blob),
            content_type="text/plain", size=len(payload), uploaded_by="implementer",
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"required_slots": ["evidence"], "entries": [{
        "attachment_id": attachment_id, "sha256": hashlib.sha256(payload).hexdigest(),
        "content_type": "text/plain", "cardinality": 1, "slot": "evidence", "mode": "required",
    }]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "reviewer")
    args = _parse_kanban([
        "evidence-manifest-freeze", task_id, sha, str(manifest_path),
        "--verdict", "pass", "--authority", "reviewer", "--json",
    ])
    assert kc.kanban_command(args) == 1
    assert "source agent assignee or configured coordinator" in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_frozen_evidence_manifest(conn, task_id) is None


def test_cli_external_run_verbs_persist_lifecycle_and_redact_refs(kanban_home, capsys):
    """External-run CLI verbs retain identity/progress after the initiator exits."""
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="long validation", assignee="operator", owner_kind="external")

    def invoke(argv):
        assert kc.kanban_command(_parse_kanban([*argv, "--json"])) == 0
        return json.loads(capsys.readouterr().out)

    started = invoke([
        "external-run-start", task_id, "--owner", "operator",
        "--external-id", "validation-42", "--pid", "4242", "--phase", "validation",
        "--current", "3", "--total", "10", "--log-ref", "https://token:secret@example.test/log",
        "--result-ref", "Bearer result-secret", "--max-retries", "2",
        "--on-success", "complete", "--on-failure", "block",
    ])
    run_id = started["id"]
    assert started["task_id"] == task_id
    assert started["owner"] == "operator"
    assert started["external_id"] == "validation-42"
    assert (started["phase"], started["current"], started["total"]) == ("validation", 3, 10)
    assert "secret" not in json.dumps(started)
    assert invoke(["external-run-show", str(run_id)])["id"] == run_id
    assert [r["id"] for r in invoke(["external-run-list", "--task-id", task_id])["runs"]] == [run_id]
    beat = invoke(["external-run-heartbeat", str(run_id), "--owner", "operator", "--phase", "validation", "--current", "4", "--total", "10"])
    assert (beat["current"], beat["total"]) == (4, 10)
    assert invoke(["external-run-transfer", str(run_id), "--from-owner", "operator", "--to-owner", "reviewer"])["owner"] == "reviewer"
    assert invoke(["external-run-finish", str(run_id), "--owner", "reviewer", "--outcome", "completed"])["status"] == "done"
    assert invoke(["external-run-reconcile", "--stale-after", "1"])["reconciled"] == 0


def test_cli_complete_cannot_bypass_live_external_owner(kanban_home, capsys):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="external complete", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="cli-bypass")
    assert kc.kanban_command(_parse_kanban(["complete", task_id, "bypass"])) == 1
    assert "cannot complete" in capsys.readouterr().err
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running" and task.current_run_id == run.id


def test_cli_external_run_rejects_cross_board_mutation(kanban_home, capsys):
    """Same numeric run ID on another board cannot be mutated or disclosed."""
    kb.create_board("external-other")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="source", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="source-1")
    args = _parse_kanban([
        "--board", "external-other", "external-run-heartbeat", str(run.id),
        "--owner", "operator", "--phase", "spoof", "--json",
    ])
    assert kc.kanban_command(args) == 1
    assert "not found or not owned" in capsys.readouterr().err
    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).phase is None


def test_cli_wait_roundtrip_surfaces_kind_and_ref(kanban_home, capsys, monkeypatch):
    """Only the configured coordinator runtime can receipt-resume a wait."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="approval", assignee="operator", owner_kind="no_agent")
    assert kc.kanban_command(_parse_kanban([
        "wait-set", task_id, "human", "decision:ship",
    ])) == 0
    capsys.readouterr()
    assert kc.kanban_command(_parse_kanban(["wait-show", task_id, "--json"])) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {"kind": "human", "ref": "decision:ship", "task_id": task_id}
    # The command line cannot mint an approval by asserting another identity.
    assert kc.kanban_command(_parse_kanban([
        "wait-resume", task_id, "--authority", "attacker",
        "--receipt", "decision:ship", "--verdict", "approved",
    ])) == 2
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
    assert kc.kanban_command(_parse_kanban([
        "wait-resume", task_id, "--authority", "coordinator",
        "--receipt", "decision:ship", "--verdict", "approved",
    ])) == 0
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


def test_cli_review_wait_uses_canonical_gate_task_id(kanban_home, capsys, monkeypatch):
    """The CLI accepts only the canonical gate:<gate-task-id> review reference."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    sha = "1" * 40
    evidence = kanban_home / "review-wait-evidence"
    evidence.write_bytes(b"review wait")
    with kb.connect() as conn:
        source = kb.create_task(
            conn, title="source", assignee="implementer", owner_kind="agent",
        )
        attachment = kb.add_attachment(conn, source, filename="evidence.txt", stored_path=str(evidence), content_type="text/plain", size=len(b"review wait"))
        kb.create_candidate(conn, source, sha=sha, source_receipt=_source_receipt(sha))
        manifest = kb.freeze_evidence_manifest(conn, source, sha=sha, manifest={"required_slots": ["evidence"], "entries": [{"attachment_id": attachment, "sha256": hashlib.sha256(b"review wait").hexdigest(), "content_type": "text/plain", "cardinality": 1, "slot": "evidence", "mode": "required"}]}, verdict="pass", authority="coordinator")
        gate = kb.create_task(
            conn, title="review", assignee="independent-reviewer", owner_kind="agent",
            task_kind="reviewer", created_by_task_id=source,
            creation_authority="coordinator", gate_candidate_sha=sha,
            gate_manifest_hash=manifest["manifest_hash"],
        )
        _complete_bound_gate(conn, gate, sha=sha, manifest_hash=manifest["manifest_hash"], monkeypatch=monkeypatch)
    ref = f"gate:{gate}"
    assert kc.kanban_command(_parse_kanban(["wait-set", source, "review", ref])) == 0
    capsys.readouterr()
    assert kc.kanban_command(_parse_kanban(["wait-show", source, "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["ref"] == ref
    assert kc.kanban_command(
        _parse_kanban(["wait-set", source, "review", f"gate:{gate}:review"]),
    ) == 2
    assert "gate:<gate-task-id>" in capsys.readouterr().err


def test_cli_create_rejects_missing_profile(monkeypatch, kanban_home, capsys):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: False)
    args = _parse_kanban(["create", "bad", "--assignee", "researcher"])
    assert kc.kanban_command(args) == 2
    assert "does not exist" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_cli_create_allows_explicit_external_assignee(
    monkeypatch, kanban_home
):
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: False)
    args = _parse_kanban([
        "create", "external", "--assignee", "orion-research",
        "--external-assignee",
    ])
    assert kc.kanban_command(args) == 0
    with kb.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0].assignee == "orion-research"



def test_cli_create_explicit_owner_kinds_bypass_profile_lookup_only_for_manual_lanes(
    monkeypatch, kanban_home
):
    """Real command dispatch preserves explicit external and no-agent lanes."""
    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: False)
    for title, owner_kind in (("external handoff", "external"), ("manual approval", "no_agent")):
        args = _parse_kanban([
            "create", title, "--assignee", "manual-operator", "--owner-kind", owner_kind,
        ])
        assert kc.kanban_command(args) == 0
    with kb.connect_closing() as conn:
        tasks = {task.title: task for task in kb.list_tasks(conn)}
    assert tasks["external handoff"].owner_kind == "external"
    assert tasks["manual approval"].owner_kind == "no_agent"


def test_cli_create_omission_preserves_db_inference_and_explicit_no_agent_lane(
    kanban_home,
):
    """Omitting --owner-kind is distinct from deliberately selecting no_agent."""
    assert kc.kanban_command(_parse_kanban([
        "create", "assigned implicit", "--assignee", "implementer",
    ])) == 0
    assert kc.kanban_command(_parse_kanban(["create", "unassigned implicit"])) == 0
    assert kc.kanban_command(_parse_kanban([
        "create", "manual explicit", "--owner-kind", "no_agent",
    ])) == 0
    with kb.connect_closing() as conn:
        tasks = {task.title: task for task in kb.list_tasks(conn)}
        assert (tasks["assigned implicit"].owner_kind, tasks["assigned implicit"].owner_kind_explicit) == ("agent", False)
        assert (tasks["unassigned implicit"].owner_kind, tasks["unassigned implicit"].owner_kind_explicit) == ("no_agent", False)
        assert (tasks["manual explicit"].owner_kind, tasks["manual explicit"].owner_kind_explicit) == ("no_agent", True)
        result = kb.dispatch_once(conn, spawn_fn=lambda *_args, **_kwargs: 123, default_assignee="implementer")
        manual = kb.get_task(conn, tasks["manual explicit"].id)
    assert tasks["unassigned implicit"].id in result.auto_assigned_default
    assert manual is not None and manual.assignee is None


def test_cli_create_rejects_unconfigured_gate_without_traceback(kanban_home, capsys):
    """The real command rejects mandatory gates fail-closed at the CLI boundary."""
    args = _parse_kanban([
        "create", "review", "--assignee", "manual-reviewer", "--owner-kind", "no_agent",
        "--task-kind", "reviewer", "--creation-authority", "dashboard",
    ])
    assert kc.kanban_command(args) == 1
    assert "runtime HERMES_PROFILE" in capsys.readouterr().err



def test_cli_configured_authority_creates_gate_and_rejects_spoof(kanban_home, capsys, monkeypatch):
    """CLI gate provenance must be exactly the configured coordinator authority."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    authorized = _parse_kanban([
        "create", "release", "--assignee", "release-operator", "--owner-kind", "no_agent",
        "--task-kind", "release", "--creation-authority", "coordinator",
    ])
    assert kc.kanban_command(authorized) == 0
    spoofed = _parse_kanban([
        "create", "spoofed", "--assignee", "release-operator", "--owner-kind", "no_agent",
        "--task-kind", "release", "--creation-authority", "impostor",
    ])
    assert kc.kanban_command(spoofed) != 0
    assert "authority assertion" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        tasks = {task.title: task for task in kb.list_tasks(conn)}
    assert tasks["release"].task_kind == "release"
    assert tasks["release"].creation_authority == "coordinator"
    assert "spoofed" not in tasks


def test_cli_assign_rejects_owner_kind_mutation(kanban_home, capsys):
    """The assign command must not change a durable external lane into agent work."""
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="manual lane", assignee="operator", owner_kind="external",
        )
    args = _parse_kanban(["assign", task_id, "operator", "--owner-kind", "agent"])
    assert kc.kanban_command(args) == 2
    assert "owner_kind is immutable" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, task_id).owner_kind == "external"


def test_kanban_list_json_includes_session_id(kanban_home):
    """JSON output exposes `session_id` so external clients (Scarf, web
    dashboards) don't need a side query to filter by chat session."""
    from hermes_cli import kanban_db as kb
    with kb.connect() as conn:
        kb.create_task(
            conn, title="acp task", assignee="alice", owner_kind="no_agent", session_id="acp-x"
        )
    raw = kc.run_slash("list --json")
    payload = json.loads(raw)
    assert any(
        row.get("title") == "acp task"
        and row.get("session_id") == "acp-x"
        for row in payload
    )


def test_kanban_show_text_renders_graph_with_open_connection(kanban_home):
    with kb.connect_closing() as conn:
        parent_id = kb.create_task(conn, title="parent task")
        child_id = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)

    output = kc.run_slash(f"show {child_id}")

    assert f"Task {child_id}: child task" in output
    assert f"parents:   {parent_id}" in output
    assert "Cannot operate on a closed database" not in output


def test_board_override_is_isolated_per_concurrent_call(kanban_home, monkeypatch):
    kb.create_board("alpha")
    kb.create_board("beta")

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    barrier = threading.Barrier(2)
    original_init_db = kb.init_db

    def slow_init_db(*args, **kwargs):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return original_init_db(*args, **kwargs)

    monkeypatch.setattr(kb, "init_db", slow_init_db)

    failures: list[str] = []

    def worker(board: str, title: str) -> None:
        args = parser.parse_args(["kanban", "--board", board, "create", title])
        rc = kc.kanban_command(args)
        if rc != 0:
            failures.append(f"{board}:{rc}")

    t1 = threading.Thread(target=worker, args=("alpha", "alpha-task"))
    t2 = threading.Thread(target=worker, args=("beta", "beta-task"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert failures == []

    with kb.connect_closing(board="alpha") as conn:
        alpha_titles = [row.title for row in kb.list_tasks(conn, limit=100)]
    with kb.connect_closing(board="beta") as conn:
        beta_titles = [row.title for row in kb.list_tasks(conn, limit=100)]

    assert alpha_titles == ["alpha-task"]
    assert beta_titles == ["beta-task"]


def test_candidate_evidence_and_verdict_cli_json_roundtrip(kanban_home, tmp_path, capsys, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    """CLI handlers use the durable DB and return canonical structured receipts."""
    payload = b"cli evidence"
    blob = tmp_path / "evidence.txt"
    blob.write_bytes(payload)
    sha = "a" * 40
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="cli evidence", assignee="implementer", owner_kind="agent")
        attachment_id = kb.add_attachment(
            conn, task_id, filename="evidence.txt", stored_path=str(blob),
            content_type="text/plain", size=len(payload), uploaded_by="test",
        )
    source_receipt_path = tmp_path / "source-receipt.json"
    source_receipt_path.write_text(json.dumps(_source_receipt(sha)), encoding="utf-8")
    candidate = _parse_kanban(["candidate-create", task_id, sha, "--source-receipt", str(source_receipt_path), "--json"])
    assert kc.kanban_command(candidate) == 0
    created_candidate = json.loads(capsys.readouterr().out)
    assert created_candidate["task_id"] == task_id
    assert created_candidate["sha"] == sha
    assert created_candidate["source_kind"] == "attested"
    assert created_candidate["source_provenance"] == _source_receipt(sha)
    candidate_show = _parse_kanban(["candidate-show", task_id, sha, "--json"])
    assert kc.kanban_command(candidate_show) == 0
    assert json.loads(capsys.readouterr().out) == created_candidate
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"required_slots": ["report"], "entries": [{
        "attachment_id": attachment_id, "sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "content_type": "text/plain", "decoder_valid": True, "decoder_metadata": {},
        "cardinality": 1, "slot": "report", "mode": "required",
    }]}), encoding="utf-8")
    freeze = _parse_kanban(["evidence-manifest-freeze", task_id, sha, str(manifest_path), "--verdict", "pass", "--authority", "coordinator", "--json"])
    assert kc.kanban_command(freeze) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["task_id"] == task_id and receipt["sha"] == sha and receipt["manifest_hash"]
    with kb.connect_closing() as conn:
        gate_id = kb.create_task(conn, title="review", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=task_id, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=receipt["manifest_hash"])
        _complete_bound_gate(conn, gate_id, sha=sha, manifest_hash=receipt["manifest_hash"], monkeypatch=monkeypatch)
    show_manifest = _parse_kanban(["evidence-manifest-show", task_id, "--json"])
    assert kc.kanban_command(show_manifest) == 0
    assert json.loads(capsys.readouterr().out) == receipt
    record = _parse_kanban(["gate-verdict-record", task_id, gate_id, sha, receipt["manifest_hash"], "pass", "--authority", "coordinator", "--json"])
    assert kc.kanban_command(record) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["task_id"] == task_id and verdict["gate_task_id"] == gate_id and verdict["manifest_hash"] == receipt["manifest_hash"]
    show_verdict = _parse_kanban(["gate-verdict-show", task_id, gate_id, "--json"])
    assert kc.kanban_command(show_verdict) == 0
    assert json.loads(capsys.readouterr().out) == verdict


def test_candidate_evidence_cli_reports_safe_validation_errors(kanban_home, tmp_path, capsys, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    """Malformed, cross-task, and stale CLI inputs fail without a traceback."""
    payload = b"safe evidence"
    blob = tmp_path / "safe.txt"
    blob.write_bytes(payload)
    sha = "a" * 40
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="safe errors")
        other_id = kb.create_task(conn, title="other task")
        attachment_id = kb.add_attachment(
            conn, task_id, filename="safe.txt", stored_path=str(blob),
            content_type="text/plain", size=len(payload), uploaded_by="test",
        )
    malformed = _parse_kanban(["candidate-create", task_id, "not-a-sha", "--json"])
    assert kc.kanban_command(malformed) == 1
    assert "exact 40-character" in capsys.readouterr().err
    source_receipt_path = tmp_path / "safe-source-receipt.json"
    source_receipt_path.write_text(json.dumps(_source_receipt(sha)), encoding="utf-8")
    assert kc.kanban_command(_parse_kanban(["candidate-create", other_id, sha, "--source-receipt", str(source_receipt_path), "--json"])) == 0
    capsys.readouterr()
    manifest_path = tmp_path / "safe-manifest.json"
    manifest_path.write_text(json.dumps({"required_slots": ["safe"], "entries": [{
        "attachment_id": attachment_id, "sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "content_type": "text/plain", "decoder_valid": True, "decoder_metadata": {},
        "cardinality": 1, "slot": "safe", "mode": "required",
    }]}), encoding="utf-8")
    mismatch = _parse_kanban(["evidence-manifest-freeze", task_id, sha, str(manifest_path), "--verdict", "pass", "--authority", "coordinator", "--json"])
    assert kc.kanban_command(mismatch) == 1
    assert "candidate is missing" in capsys.readouterr().err
    assert kc.kanban_command(_parse_kanban(["candidate-create", task_id, sha, "--source-receipt", str(source_receipt_path), "--json"])) == 0
    capsys.readouterr()
    freeze = _parse_kanban(["evidence-manifest-freeze", task_id, sha, str(manifest_path), "--verdict", "pass", "--authority", "coordinator", "--json"])
    assert kc.kanban_command(freeze) == 0
    capsys.readouterr()
    stale = _parse_kanban(["gate-verdict-record", task_id, "t_missing", sha, "0" * 64, "pass", "--authority", "coordinator", "--json"])
    assert kc.kanban_command(stale) == 1
    assert "stale" in capsys.readouterr().err
    missing = _parse_kanban(["candidate-show", "t_missing", sha, "--json"])
    assert kc.kanban_command(missing) == 1
    assert "candidate not found" in capsys.readouterr().err

# ---------------------------------------------------------------------------







def test_release_barrier_cli_roundtrip(kanban_home, tmp_path, capsys, monkeypatch):
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    blob = tmp_path / "r"; blob.write_bytes(b"r"); sha = "d" * 40
    with kb.connect_closing() as conn:
        task = kb.create_task(conn, title="cli release", assignee="implementer", owner_kind="agent"); aid = kb.add_attachment(conn, task, filename="r", stored_path=str(blob), content_type="application/octet-stream", size=1); kb.create_candidate(conn, task, sha=sha, source_receipt=_source_receipt(sha))
        m = kb.freeze_evidence_manifest(conn, task, sha=sha, manifest={"required_slots":["r"],"entries":[{"attachment_id":aid,"sha256":__import__("hashlib").sha256(b"r").hexdigest(),"content_type":"application/octet-stream","decoder_valid":True,"decoder_metadata":{},"cardinality":1,"slot":"r","mode":"r"}]}, verdict="pass", authority="coordinator")
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=task, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=m["manifest_hash"])
        _complete_bound_gate(conn, gate, sha=sha, manifest_hash=m["manifest_hash"], monkeypatch=monkeypatch)
    def run(argv):
        assert kc.kanban_command(_parse_kanban(argv)) == 0
        return json.loads(capsys.readouterr().out)
    assert run(["release-barrier-create", task, "--sha", sha, "--manifest-hash", m["manifest_hash"], "--gate", gate, "--authority", "coordinator", "--json"])["sha"] == sha
    assert run(["release-barrier-acquire", task, "--owner-token", "o", "--json"])["acquired"]
    assert not run(["release-barrier-acquire", task, "--owner-token", "second", "--json"])["acquired"]
    assert run(["release-barrier-reconcile", task, "--owner-token", "o", "--json"])["status"] == "waiting"
    with kb.connect_closing() as conn:
        kb.record_gate_verdict(conn, task, gate_task_id=gate, sha=sha, manifest_hash=m["manifest_hash"], verdict="pass", authority="coordinator")
    assert run(["release-barrier-reconcile", task, "--owner-token", "o", "--json"])["status"] == "awaiting_receipt"
    prepare = run(["release-barrier-prepare", task, "--owner-token", "o", "--target", "target", "--sha", sha, "--idempotency-key", "cli-release", "--json"])
    assert prepare["idempotency_key"] == "cli-release"
    wrong_delivery = _parse_kanban(["release-barrier-delivery", task, "--owner-token", "o", "--target", "target", "--sha", "0" * 40, "--idempotency-key", "cli-release", "--json"])
    assert kc.kanban_command(wrong_delivery) == 1; assert "candidate SHA" in capsys.readouterr().err
    run(["release-barrier-delivery", task, "--owner-token", "o", "--target", "target", "--sha", sha, "--idempotency-key", "cli-release", "--json"])
    wrong_readback = _parse_kanban(["release-barrier-readback", task, "--owner-token", "o", "--sha", "0" * 40, "--json"])
    assert kc.kanban_command(wrong_readback) == 1; assert "readback SHA" in capsys.readouterr().err
    run(["release-barrier-readback", task, "--owner-token", "o", "--sha", sha, "--json"])
    assert run(["release-barrier-reconcile", task, "--owner-token", "o", "--json"])["status"] == "released"
    assert run(["release-barrier-reconcile", task, "--owner-token", "o", "--json"])["status"] == "released"
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='release_barrier_completed'", (task,)).fetchone()[0] == 1
    assert run(["release-barrier-show", task, "--json"])["completed_at"] is not None


def test_gate_verdict_cli_rejects_authority_spoof(kanban_home, monkeypatch, capsys):
    """CLI derives its control principal from profile/config, not --authority."""
    (kanban_home / "config.yaml").write_text("kanban:\n  coordinator_profile: coordinator\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_PROFILE", "coordinator")
    with kb.connect() as conn:
        source = kb.create_task(conn, title="source", assignee="implementer", owner_kind="agent")
        sha = "a" * 40
        blob = kanban_home / "report"; blob.write_bytes(b"")
        attachment = kb.add_attachment(conn, source, filename="report", stored_path=str(blob), content_type="text/plain", size=0)
        kb.create_candidate(conn, source, sha=sha, source_receipt=_source_receipt(sha))
        receipt = kb.freeze_evidence_manifest(conn, source, sha=sha, manifest={"required_slots": ["report"], "entries": [{"attachment_id": attachment, "sha256": hashlib.sha256(b"").hexdigest(), "content_type": "text/plain", "cardinality": 1, "slot": "report", "mode": "required"}]}, verdict="pass", authority="coordinator")
        gate = kb.create_task(conn, title="gate", assignee="reviewer", owner_kind="agent", task_kind="reviewer", created_by_task_id=source, creation_authority="coordinator", gate_candidate_sha=sha, gate_manifest_hash=receipt["manifest_hash"])
        _complete_bound_gate(conn, gate, sha=sha, manifest_hash=receipt["manifest_hash"], monkeypatch=monkeypatch)
    spoof = _parse_kanban(["gate-verdict-record", source, gate, sha, receipt["manifest_hash"], "pass", "--authority", "spoofed", "--json"])
    assert kc.kanban_command(spoof) == 1
    assert "only an assertion" in capsys.readouterr().err
    valid = _parse_kanban(["gate-verdict-record", source, gate, sha, receipt["manifest_hash"], "pass", "--json"])
    assert kc.kanban_command(valid) == 0
    assert json.loads(capsys.readouterr().out)["gate_task_id"] == gate

# ---------------------------------------------------------------------------

def test_run_slash_reclaim_running_task(kanban_home):
    import re
    import time
    import secrets
    from hermes_cli import kanban_db as kb

    out1 = kc.run_slash(
        "create 'stuck worker task' --assignee broken-model --external-assignee"
    )
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    tid = m.group(1)

    # Simulate a running claim outside TTL.
    conn = kb.connect()
    try:
        lock = secrets.token_hex(4)
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, int(time.time()) + 3600, 4242, tid),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (tid, lock, int(time.time()) + 3600, 4242, int(time.time())),
        )
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        conn.commit()
    finally:
        conn.close()

    out = kc.run_slash(f"reclaim {tid} --reason 'test'")
    assert "Reclaimed" in out, out
    # Status back to ready.
    out2 = kc.run_slash(f"show {tid}")
    assert "ready" in out2.lower()




# ---------------------------------------------------------------------------
# /kanban specify — slash surface (same entry point CLI + gateway use)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /kanban help / no-args / unknown-action UX (issue #21794)
# ---------------------------------------------------------------------------


