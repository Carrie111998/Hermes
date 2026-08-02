"""Deterministic candidate-versus-baseline evaluation contracts."""

from __future__ import annotations

import json

import pytest


def test_candidate_improves_baseline_on_fixture_manifest():
    from agent.learning_evaluation import compare_texts

    manifest = {
        "version": 1,
        "cases": [
            {"name": "documents retry", "must_contain": ["retry", "verify"]},
            {"name": "rejects secret", "must_not_contain": ["API_KEY=plaintext"]},
        ],
    }

    result = compare_texts(
        baseline="Retry the command.",
        candidate="Retry the command, then verify the result.",
        manifest=manifest,
    )

    assert result["baseline"]["passed"] == 1
    assert result["candidate"]["passed"] == 2
    assert result["verdict"] == "improved"


def test_candidate_regression_is_detected_without_model_judge():
    from agent.learning_evaluation import compare_texts

    manifest = {
        "version": 1,
        "cases": [{"name": "keeps safety", "must_contain": ["approval", "rollback"]}],
    }

    result = compare_texts(
        baseline="Require approval and keep rollback.",
        candidate="Apply automatically.",
        manifest=manifest,
    )

    assert result["verdict"] == "regressed"
    assert result["candidate"]["failures"][0]["case"] == "keeps safety"


def test_simulate_patch_requires_exact_reviewed_baseline():
    from agent.learning_evaluation import simulate_candidate_text

    payload = {
        "action": "patch",
        "old_string": "old procedure",
        "new_string": "new verified procedure",
    }
    assert simulate_candidate_text("Use the old procedure.", payload) == "Use the new verified procedure."

    try:
        simulate_candidate_text("The target changed.", payload)
    except ValueError as exc:
        assert "reviewed baseline" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("stale baseline must fail")


def test_evaluated_skill_can_rollback_through_existing_mutation_handler(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    skill_dir = home / "skills" / "demo"
    (skill_dir / "evals").mkdir(parents=True)
    baseline = "---\nname: demo\ndescription: demo skill\n---\n\nRequire approval.\n"
    candidate_text = baseline.replace("Require approval.", "Require approval and rollback.")
    (skill_dir / "SKILL.md").write_text(baseline)
    (skill_dir / "evals" / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {"name": "keeps approval", "must_contain": ["approval"]},
                    {"name": "adds rollback", "must_contain": ["rollback"]},
                ],
            }
        )
    )
    from agent import learning_ledger
    from agent.learning_evaluation import evaluate_pending_skill
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.SKILLS,
        {"action": "edit", "name": "demo", "content": candidate_text},
        summary="add rollback contract",
        origin="foreground",
    )
    assert evaluate_pending_skill(record)["verdict"] == "improved"
    approved = handle_pending_subcommand(
        wa.SKILLS, ["approve", record["id"]]
    )
    assert approved is not None
    assert "Approved 1" in approved
    assert (skill_dir / "SKILL.md").read_text() == candidate_text

    rolled_back = handle_pending_subcommand(
        wa.SKILLS, ["rollback", record["id"]]
    )

    assert rolled_back == f"Rolled back evaluated skill candidate '{record['id']}'."
    assert (skill_dir / "SKILL.md").read_text() == baseline
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None
    assert candidate["status"] == "rolled_back"


def test_evaluated_memory_can_rollback_through_existing_mutation_handler(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    from agent import learning_ledger
    from agent.learning_evaluation import evaluate_pending_memory
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa
    from tools.memory_tool import MemoryStore

    store = MemoryStore()
    baseline = "User prefers concise technical answers"
    replacement = "User prefers concise verified answers"
    assert store.add("memory", baseline)["success"]
    record = wa.stage_write(
        wa.MEMORY,
        {
            "action": "replace",
            "target": "memory",
            "old_text": "concise technical",
            "content": replacement,
        },
        summary="replace preference",
        origin="foreground",
    )
    assert evaluate_pending_memory(record, store)["verdict"] == "passed"
    approved = handle_pending_subcommand(
        wa.MEMORY, ["approve", record["id"]], memory_store=store
    )
    assert approved is not None and "Approved 1" in approved
    assert store.memory_entries == [replacement]

    rolled_back = handle_pending_subcommand(
        wa.MEMORY, ["rollback", record["id"]], memory_store=store
    )

    assert rolled_back == f"Rolled back evaluated memory candidate '{record['id']}'."
    assert store.memory_entries == [baseline]
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None
    assert candidate["status"] == "rolled_back"


def test_evaluated_support_file_patch_rolls_back_the_reviewed_file(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    skill_dir = home / "skills" / "demo"
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n"
    )
    baseline = "Retry the operation.\n"
    candidate_text = "Retry and verify the operation.\n"
    support_path = skill_dir / "references" / "guide.md"
    support_path.write_text(baseline)
    (skill_dir / "evals" / "manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cases": [
                    {"name": "keeps retry", "must_contain": ["retry"]},
                    {"name": "adds verification", "must_contain": ["verify"]},
                ],
            }
        )
    )
    from agent.learning_evaluation import evaluate_pending_skill
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.SKILLS,
        {
            "action": "patch",
            "name": "demo",
            "file_path": "references/guide.md",
            "old_string": "Retry the operation.",
            "new_string": "Retry and verify the operation.",
        },
        summary="verify support-file procedure",
        origin="foreground",
    )

    assert evaluate_pending_skill(record)["verdict"] == "improved"
    approved = handle_pending_subcommand(wa.SKILLS, ["approve", record["id"]])
    assert approved is not None and "Approved 1" in approved
    assert support_path.read_text() == candidate_text
    rolled_back = handle_pending_subcommand(wa.SKILLS, ["rollback", record["id"]])
    assert rolled_back == f"Rolled back evaluated skill candidate '{record['id']}'."
    assert support_path.read_text() == baseline


def test_skill_rollback_rejects_retargeted_snapshot_metadata(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("a", "b"):
        skill_dir = home / "skills" / name
        (skill_dir / "evals").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Demo skill {name}\n---\n\nRequire approval for {name}.\n"
        )
        (skill_dir / "evals" / "manifest.json").write_text(
            json.dumps({"version": 1, "cases": [{"name": "rollback", "must_contain": ["rollback"]}]})
        )
    from agent.learning_evaluation import evaluate_pending_skill, prepare_evaluated_skill_rollback
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.SKILLS,
        {
            "action": "patch",
            "name": "a",
            "old_string": "Require approval for a.",
            "new_string": "Require approval and rollback for a.",
        },
        summary="add rollback",
        origin="foreground",
    )
    assert evaluate_pending_skill(record)["verdict"] == "improved"
    approved = handle_pending_subcommand(wa.SKILLS, ["approve", record["id"]])
    assert approved is not None and "Approved 1" in approved
    metadata_path = home / "learning" / "snapshots" / record["id"] / "snapshot.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["skill_name"] = "b"
    metadata_path.write_text(json.dumps(metadata))

    with pytest.raises(ValueError, match="snapshot.*candidate|identity|manifest"):
        prepare_evaluated_skill_rollback(record["id"])


def test_evaluation_rejects_symlinked_learning_parent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "learning").symlink_to(outside, target_is_directory=True)
    skill_dir = home / "skills" / "demo"
    (skill_dir / "evals").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("Retry.\n")
    (skill_dir / "evals" / "manifest.json").write_text(
        json.dumps({"version": 1, "cases": [{"name": "verify", "must_contain": ["verify"]}]})
    )
    from agent.learning_evaluation import evaluate_pending_skill

    record = {
        "id": "snapshot-symlink",
        "candidate_id": "snapshot-symlink",
        "payload": {
            "action": "patch",
            "name": "demo",
            "old_string": "Retry.",
            "new_string": "Retry and verify.",
        },
    }

    with pytest.raises(ValueError, match="snapshot|symlink|profile"):
        evaluate_pending_skill(record)
    assert list(outside.iterdir()) == []


def test_skill_rollback_receipt_failure_requires_explicit_reconciliation(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    skill_dir = home / "skills" / "demo"
    (skill_dir / "evals").mkdir(parents=True)
    baseline = "---\nname: demo\ndescription: Demo skill\n---\n\nRequire approval.\n"
    candidate_text = "---\nname: demo\ndescription: Demo skill\n---\n\nRequire approval and rollback.\n"
    (skill_dir / "SKILL.md").write_text(baseline)
    (skill_dir / "evals" / "manifest.json").write_text(
        json.dumps({"version": 1, "cases": [{"name": "rollback", "must_contain": ["rollback"]}]})
    )
    from agent import learning_ledger
    from agent.learning_evaluation import evaluate_pending_skill
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools import write_approval as wa

    record = wa.stage_write(
        wa.SKILLS,
        {"action": "edit", "name": "demo", "content": candidate_text},
        summary="add rollback",
        origin="foreground",
    )
    assert evaluate_pending_skill(record)["verdict"] == "improved"
    approved = handle_pending_subcommand(wa.SKILLS, ["approve", record["id"]])
    assert approved is not None and "Approved 1" in approved
    original_record_outcome = learning_ledger.record_outcome
    monkeypatch.setattr(
        learning_ledger,
        "record_outcome",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("receipt unavailable")),
    )

    first = handle_pending_subcommand(wa.SKILLS, ["rollback", record["id"]])

    assert first is not None and "needs reconciliation" in first
    assert (skill_dir / "SKILL.md").read_text() == baseline
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None and candidate["status"] == "rolling_back"
    monkeypatch.setattr(learning_ledger, "record_outcome", original_record_outcome)

    reconciled = handle_pending_subcommand(
        wa.SKILLS, ["rollback", record["id"], "mark-rolled-back"]
    )

    assert reconciled is not None and "Marked" in reconciled
    candidate = learning_ledger.get_candidate(record["id"])
    assert candidate is not None and candidate["status"] == "rolled_back"
