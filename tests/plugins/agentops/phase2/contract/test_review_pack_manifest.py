from pathlib import Path

import yaml


def test_runtime_core_review_pack_is_declarative_and_observe_only():
    manifest = Path("plugins/agentops/review_packs/runtime_core/manifest.yaml")
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    assert data["schema_version"] == 1
    assert data["authority_mode"] == "observe_only"
    assert data["execution"] == "disabled"
    assert data["actions"] == []
    assert set(data["inputs"]["collectors"]) == {
        "logs",
        "processes",
        "launchd",
        "cron",
        "sqlite_health",
        "git_state",
    }
