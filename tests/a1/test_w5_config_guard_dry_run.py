"""W5 P0-2: Config Guard protected-mutation dry-run proof.

These tests are intentionally tempdir-only. They prove a protected route-affecting
mutation can be observed, planned, resolved to approval-required denial, recorded
with rollback evidence, and leave the protected target unchanged.
"""

import json
from pathlib import Path

from hermes_cli.w5_config_guard import run_protected_mutation_dry_run


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_protected_profile_provider_mutation_denies_without_current_approval_and_noops(tmp_path):
    fixtures_dir = tmp_path / "fixtures"
    evidence_dir = tmp_path / "evidence"
    protected_dir = fixtures_dir / "protected_targets"
    protected_dir.mkdir(parents=True)
    target = protected_dir / "profile_config_route_fields.fixture.yaml"
    original = """
model:
  provider: custom:local-ollama
  model: qwen3.5:9b
fallbacks: []
""".lstrip()
    proposed = """
model:
  provider: custom:headroom-openrouter-litellm
  model: frontier-value
fallbacks:
  - custom:headroom-openrouter-litellm
""".lstrip()
    target.write_text(original, encoding="utf-8")

    result = run_protected_mutation_dry_run(
        target_path=target,
        proposed_content=proposed,
        surface_id="profile_config_route_fields.fixture",
        mutation_class="provider_profile_route_change",
        actor="pennyworth-architect-test",
        profile_id="concierge-fixture",
        evidence_dir=evidence_dir,
        approval_ref=None,
    )

    assert result.decision == "deny"
    assert result.rule_id == "w5.config_guard.protected_mutation.approval_required"
    assert result.required_approval == "pep_current_session"
    assert result.before_hash == result.after_hash
    assert result.before_after_hash_equal is True
    assert result.target_write_count == 0
    assert result.live_config_touched is False
    assert result.secret_values_read is False
    assert target.read_text(encoding="utf-8") == original

    rollback = Path(result.rollback_artifact)
    assert rollback.exists()
    rollback_doc = json.loads(rollback.read_text(encoding="utf-8"))
    assert rollback_doc["target_path"] == str(target)
    assert rollback_doc["restore_sha256"] == result.before_hash
    assert rollback_doc["restore_content"] == original

    evidence_path = evidence_dir / "w5_config_guard_dry_run.jsonl"
    rows = _read_jsonl(evidence_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["case_id"] == "W5-P0-2-PROTECTED-MUTATION-DRY-RUN-001"
    assert row["decision"] == "deny"
    assert row["rule_id"] == result.rule_id
    assert row["mutation_plan_capture"]["target_path"] == str(target)
    assert "custom:headroom-openrouter-litellm" in row["mutation_plan_capture"]["planned_diff"]
    assert row["source_config_hash_before"] == row["source_config_hash_after"]
    assert row["before_after_hash_equal"] is True
    assert row["target_write_count"] == 0
    assert row["live_config_touched"] is False
    assert row["secret_values_read"] is False
