from pathlib import Path

import yaml


def test_runtime_core_review_pack_is_declarative_and_observe_only():
    manifest = Path("plugins/agentops/review_packs/runtime_core/manifest.yaml")
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    assert data["schema_version"] == 2
    assert data["pack"]["id"] == "runtime_core"
    assert data["pack"]["version"] == "1.0.0"
    assert data["pack"]["description"]
    assert data["authority_mode"] == "observe_only"
    assert data["execution"] == {
        "production_read": True,
        "dry_run": True,
        "no_write": True,
        "action_execution": "disabled",
    }
    assert data["actions"] == []
    assert set(data["target_kinds"]) == {"gateway", "cron", "repository", "sqlite"}
    assert {collector["id"] for collector in data["inputs"]["collectors"]} == {
        "logs",
        "processes",
        "launchd",
        "cron",
        "sqlite_health",
        "git_state",
    }
    for collector in data["inputs"]["collectors"]:
        assert collector["source_binding"]
        assert collector["deadline_seconds"] > 0
        assert collector["rate_limit_seconds"] > 0
    assert data["inputs"]["classification"] == "operational_metadata"
    assert data["inputs"]["retention_days"] > 0
    assert all({"id", "severity", "mandatory"}.issubset(probe) for probe in data["probes"])
    assert all({"id", "severity", "mandatory"}.issubset(assertion) for assertion in data["assertions"])
    assert data["failure_runbook"]["mode"] == "manual_only"
    assert data["failure_runbook"]["steps"][-1] == "do_not_change_target"
