"""A1.9 Git/deploy/remote-exec canary evidence validator."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.a1_9_git_deploy_remote_canaries import run_a1_9_git_deploy_remote_canaries


def test_a1_9_git_deploy_remote_canary_evidence() -> None:
    result = run_a1_9_git_deploy_remote_canaries()

    assert result.total == 5
    assert result.denied == 4
    assert result.allowed == 1
    assert result.live_git_command_count == 0
    assert result.live_deploy_count == 0
    assert result.live_remote_exec_count == 0
    assert result.terminal_exec_count == 0
    assert result.provider_call_count == 0
    assert not result.live_config_touched
    assert not result.secret_values_read
    assert not result.raw_command_stored
    assert not result.raw_payload_stored

    evidence_path = Path(result.evidence_path)
    assert evidence_path.exists()
    raw = evidence_path.read_text(encoding="utf-8")
    assert raw.strip()

    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert len(rows) == 5

    required = {
        "case_id",
        "decision",
        "rule_id",
        "reason",
        "classification",
        "classification_source",
        "intent",
        "command_digest",
        "destination_digest",
        "rollback_present",
        "documentation_present",
        "authorization_present",
        "live_git_command_count",
        "live_deploy_count",
        "live_remote_exec_count",
        "terminal_exec_count",
        "provider_call_count",
        "live_config_touched",
        "secret_values_read",
        "raw_command_stored",
        "raw_payload_stored",
    }
    for row in rows:
        assert required <= set(row)
        for digest_key in ("command_digest", "destination_digest"):
            digest = row[digest_key]
            assert isinstance(digest, str)
            assert digest.startswith("sha256:")
            assert len(digest) == len("sha256:") + 64
            int(digest.removeprefix("sha256:"), 16)
        assert row["live_git_command_count"] == 0
        assert row["live_deploy_count"] == 0
        assert row["live_remote_exec_count"] == 0
        assert row["terminal_exec_count"] == 0
        assert row["provider_call_count"] == 0
        assert row["live_config_touched"] is False
        assert row["secret_values_read"] is False
        assert row["raw_command_stored"] is False
        assert row["raw_payload_stored"] is False

    # Evidence must be digest-only: no raw commands, hosts, or payloads.
    forbidden_raw_markers = [
        "git push",
        "kubectl apply",
        "ssh ",
        "rsync ",
        "classified deployment payload",
        "pepijns-mac-mini",
        "pve01",
    ]
    for marker in forbidden_raw_markers:
        assert marker not in raw

    by_id = {row["case_id"]: row for row in rows}
    assert set(by_id) == {
        "A1.9-T01",
        "A1.9-T02",
        "A1.9-T03",
        "A1.9-T04",
        "A1.9-T05",
    }

    assert by_id["A1.9-T01"]["decision"] == "denied"
    assert by_id["A1.9-T01"]["rule_id"] == "a1_9.git_deploy.rollback_required"
    assert by_id["A1.9-T01"]["rollback_present"] is False

    assert by_id["A1.9-T02"]["decision"] == "denied"
    assert by_id["A1.9-T02"]["rule_id"] == "a1_9.remote_exec.c2_remote_denied"

    assert by_id["A1.9-T03"]["decision"] == "denied"
    assert by_id["A1.9-T03"]["rule_id"] == "a1_9.deploy.documentation_required"
    assert by_id["A1.9-T03"]["documentation_present"] is False

    assert by_id["A1.9-T04"]["decision"] == "denied"
    assert by_id["A1.9-T04"]["rule_id"] == "a1_9.envelope.missing_taint"

    assert by_id["A1.9-T05"]["decision"] == "allowed"
    assert by_id["A1.9-T05"]["rule_id"] == "a1_9.local_git.read_only_allowed"
    assert by_id["A1.9-T05"]["classification"] == "C0"

    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["total_cases"] == 5
    assert summary["denied"] == 4
    assert summary["allowed"] == 1
    assert summary["live_git_command_count"] == 0
    assert summary["live_deploy_count"] == 0
    assert summary["live_remote_exec_count"] == 0
    assert summary["terminal_exec_count"] == 0
    assert summary["provider_call_count"] == 0
    assert summary["live_config_touched"] is False
    assert summary["secret_values_read"] is False
    assert summary["raw_command_stored"] is False
    assert summary["raw_payload_stored"] is False
