import json

from tools.skill_authority import (
    build_manifest,
    classify_skill_identifier,
    validate_runtime_authority,
)


def _skill(root, category, name, body="body"):
    path = root / category / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_manifest_fingerprint_matches_declared_source_and_runtime(tmp_path):
    source = _skill(tmp_path / "source", "software-development", "lah-repo-router")
    runtime = _skill(tmp_path / "runtime", "software-development", "lah-repo-router")
    manifest = build_manifest(
        runtime,
        {"lah-repo-router": {"source_path": str(source), "source_repo": "test"}},
    )
    result = validate_runtime_authority(runtime, manifest, critical=("lah-repo-router",))
    assert result["valid"] is True


def test_manifest_detects_runtime_drift(tmp_path):
    source = _skill(tmp_path / "source", "software-development", "lah-repo-router")
    runtime = _skill(tmp_path / "runtime", "software-development", "lah-repo-router", "changed")
    manifest = build_manifest(
        runtime,
        {"lah-repo-router": {"source_path": str(source), "source_repo": "test"}},
    )
    result = validate_runtime_authority(runtime, manifest, critical=("lah-repo-router",))
    assert result["valid"] is False
    assert "content drift" in result["errors"][0]


def test_missing_critical_manifest_entry_blocks(tmp_path):
    runtime = _skill(tmp_path / "runtime", "software-development", "lah-repo-router")
    manifest = {"schema_version": 1, "skills": {}}
    result = validate_runtime_authority(runtime, manifest, critical=("lah-repo-router",))
    assert result["valid"] is False
    assert "missing manifest entry" in result["errors"][0]


def test_declared_fingerprint_cannot_be_stale(tmp_path):
    source = _skill(tmp_path / "source", "software-development", "lah-repo-router")
    runtime = _skill(tmp_path / "runtime", "software-development", "lah-repo-router")
    manifest = build_manifest(
        runtime,
        {"lah-repo-router": {"source_path": str(source), "source_repo": "test"}},
    )
    manifest["skills"]["lah-repo-router"]["runtime_content_sha256"] = "stale"
    result = validate_runtime_authority(runtime, manifest, critical=("lah-repo-router",))
    assert result["valid"] is False
    assert any("declared runtime fingerprint mismatch" in error for error in result["errors"])


def test_identifier_classifier_keeps_plugin_and_local_contracts_distinct():
    names = {"lah-repo-router"}
    assert classify_skill_identifier("lah-repo-router", names) == "VALID_CANONICAL_NAME"
    assert classify_skill_identifier("lah-stack/lah-repo-router", names) == "CATEGORY_PATH_USED_AS_IDENTIFIER"
    assert classify_skill_identifier("superpowers:writing-plans", names) == "VALID_PLUGIN_NAMESPACE"
