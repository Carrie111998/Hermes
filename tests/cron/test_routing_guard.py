"""Behavior tests for the cron routing-integrity manifest."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
import pytest


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    jobs_path = tmp_path / "jobs.json"
    config_path = tmp_path / "config.yaml"
    manifest_path = tmp_path / "cron-routing-manifest.yaml"
    jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "agent001",
                        "name": "agent report",
                        "enabled": True,
                        "no_agent": False,
                        "provider": "provider-a",
                        "model": "model-a",
                        "deliver": "telegram:1",
                    },
                    {
                        "id": "script001",
                        "name": "pure script",
                        "enabled": True,
                        "no_agent": True,
                        "provider": "stale-provider",
                        "model": "stale-model",
                    },
                    {
                        "id": "paused001",
                        "name": "paused agent",
                        "enabled": False,
                        "no_agent": False,
                        "provider": "provider-z",
                        "model": "model-z",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        yaml.safe_dump(
            {
                "fallback_providers": [
                    {"provider": "provider-b", "model": "model-b"},
                    {"provider": "provider-c", "model": "model-c"},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return jobs_path, config_path, manifest_path


def test_check_manifest_detects_agent_pin_drift_without_including_no_agent_jobs(tmp_path):
    from cron.routing_guard import capture_manifest, check_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)

    captured = capture_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
    )
    assert captured["agent_job_count"] == 1

    same = check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda route: None,
    )
    assert same["ok"] is True
    assert same["problems"] == []

    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0]["model"] = "model-drifted"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    drifted = check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda route: None,
    )
    assert drifted["ok"] is False
    assert any(problem["kind"] == "manifest_mismatch" for problem in drifted["problems"])


def test_check_manifest_fails_when_policy_hash_does_not_match_manifest_policy(tmp_path):
    from cron.routing_guard import capture_manifest, check_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["policy"]["agent_jobs"][0]["model"] = "silently-rebased-model"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0]["model"] = "silently-rebased-model"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    checked = check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda _route: None,
    )

    assert checked["ok"] is False
    assert any(problem["kind"] == "manifest_hash_mismatch" for problem in checked["problems"])


def test_check_manifest_rejects_an_unsupported_schema_version(tmp_path):
    from cron.routing_guard import capture_manifest, check_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    checked = check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda _route: None,
    )

    assert checked["ok"] is False
    assert any(problem["kind"] == "unsupported_schema" for problem in checked["problems"])


def test_capture_treats_agent_job_with_missing_enabled_field_as_active(tmp_path):
    from cron.routing_guard import capture_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0].pop("enabled")
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    captured = capture_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
    )

    assert captured["agent_job_count"] == 1
    assert captured["policy"]["agent_jobs"][0]["job_id"] == "agent001"


def test_check_manifest_ignores_delivery_intent_changes(tmp_path):
    from cron.routing_guard import capture_manifest, check_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0]["deliver"] = "discord:human-approved-target"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    checked = check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda _route: None,
    )

    assert checked["ok"] is True


def test_restore_preserves_effective_legacy_fallback_and_key_env_metadata(tmp_path):
    from cron.routing_guard import capture_manifest, restore_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    original_fallback_providers = [
        {
            "provider": "provider-b",
            "model": "model-b",
            "key_env": "PROVIDER_B_KEY",
            "api_mode": "chat_completions",
        }
    ]
    original_fallback_model = {
        "provider": "provider-legacy",
        "model": "legacy-model",
        "api_key_env": "LEGACY_PROVIDER_KEY",
    }
    config_path.write_text(
        yaml.safe_dump(
            {
                "fallback_providers": original_fallback_providers,
                "fallback_model": original_fallback_model,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    captured = capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    assert captured["policy"]["fallback_routes"] == [
        {"provider": "provider-b", "model": "model-b", "base_url": "", "key_env": "PROVIDER_B_KEY", "api_mode": "chat_completions"},
        {"provider": "provider-legacy", "model": "legacy-model", "base_url": "", "api_key_env": "LEGACY_PROVIDER_KEY"},
    ]

    config_path.write_text(yaml.safe_dump({"fallback_providers": []}), encoding="utf-8")
    restore_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)

    restored_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert restored_config["fallback_providers"] == original_fallback_providers
    assert restored_config["fallback_model"] == original_fallback_model


def test_check_manifest_marks_an_unresolvable_runtime_route_as_failed(tmp_path):
    from cron.routing_guard import capture_manifest, check_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)

    checked = check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda route: "provider is unavailable" if route["provider"] == "provider-b" else None,
    )

    assert checked["ok"] is False
    assert checked["problems"] == [
        {
            "kind": "unresolvable_route",
            "route": {"source": "fallback:0", "provider": "provider-b", "model": "model-b", "base_url": ""},
            "detail": "provider is unavailable",
        }
    ]


def test_restore_manifest_only_changes_managed_routing_fields(tmp_path):
    from cron.routing_guard import capture_manifest, restore_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)

    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0].update(
        provider="drifted-provider",
        model="drifted-model",
        deliver="discord:keep-this-human-change",
    )
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    config_path.write_text(yaml.safe_dump({"fallback_providers": []}), encoding="utf-8")

    restored = restore_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
    )

    assert restored["restored_job_ids"] == ["agent001"]
    restored_jobs = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
    assert restored_jobs[0]["provider"] == "provider-a"
    assert restored_jobs[0]["model"] == "model-a"
    assert restored_jobs[0]["deliver"] == "discord:keep-this-human-change"
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["fallback_providers"] == [
        {"provider": "provider-b", "model": "model-b"},
        {"provider": "provider-c", "model": "model-c"},
    ]


def test_runtime_route_resolver_uses_scheduler_provider_resolution_without_inference(monkeypatch):
    from cron.routing_guard import resolve_runtime_route

    seen = {}

    def fake_resolve_runtime_provider(**kwargs):
        seen.update(kwargs)
        return {"provider": "provider-a", "api_key": "not-inspected"}

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_resolve_runtime_provider,
    )

    assert resolve_runtime_route(
        {"provider": "provider-a", "model": "model-a", "base_url": "https://example.test/v1"}
    ) is None
    assert seen == {"requested": "provider-a", "explicit_base_url": "https://example.test/v1"}


def test_cli_check_runs_runtime_resolver_for_every_distinct_route(monkeypatch, tmp_path):
    import cron.routing_guard as guard

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    guard.capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    resolved = []
    monkeypatch.setattr(
        guard,
        "resolve_runtime_route",
        lambda route: resolved.append((route["provider"], route["model"])) or None,
    )

    exit_code = guard.main(
        [
            "check",
            "--jobs-path", str(jobs_path),
            "--config-path", str(config_path),
            "--manifest-path", str(manifest_path),
        ]
    )

    assert exit_code == 0
    assert resolved == [
        ("provider-a", "model-a"),
        ("provider-b", "model-b"),
        ("provider-c", "model-c"),
    ]


def test_no_agent_routing_drift_is_excluded_from_manifest_check(tmp_path):
    from cron.routing_guard import capture_manifest, check_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][1].update(provider="changed-script-provider", model="changed-script-model")
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    assert check_manifest(
        jobs_path=jobs_path,
        config_path=config_path,
        manifest_path=manifest_path,
        resolver=lambda _route: None,
    )["ok"] is True


def test_fallback_runtime_resolver_passes_target_model_and_key_env(monkeypatch):
    from cron.routing_guard import resolve_runtime_route

    monkeypatch.setenv("FALLBACK_TEST_KEY", "fallback-secret")
    seen = {}
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: seen.update(kwargs) or {"provider": "provider-b"},
    )

    assert resolve_runtime_route(
        {
            "source": "fallback:0",
            "provider": "provider-b",
            "model": "model-b",
            "base_url": "https://fallback.example/v1",
            "key_env": "FALLBACK_TEST_KEY",
        }
    ) is None
    assert seen == {
        "requested": "provider-b",
        "target_model": "model-b",
        "explicit_base_url": "https://fallback.example/v1",
        "explicit_api_key": "fallback-secret",
    }


@pytest.mark.parametrize("inline_field", ["api_key", "access_token", "apiKey"])
def test_capture_refuses_inline_fallback_secret(tmp_path, inline_field):
    from cron.routing_guard import capture_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    config_path.write_text(
        yaml.safe_dump(
            {"fallback_providers": [{"provider": "provider-b", "model": "model-b", inline_field: "secret"}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inline credential|Unsupported fallback metadata"):
        capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)


def test_capture_rejects_fallback_source_metadata_that_could_collide_with_guard(tmp_path):
    from cron.routing_guard import capture_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    config_path.write_text(
        yaml.safe_dump(
            {"fallback_providers": [{"provider": "provider-b", "model": "model-b", "source": "user-metadata"}]}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Unsupported fallback metadata"):
        capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)


def test_restore_rolls_back_jobs_if_config_write_fails(monkeypatch, tmp_path):
    import cron.routing_guard as guard

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    guard.capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0]["model"] = "drifted-model"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    config_path.write_text(yaml.safe_dump({"fallback_providers": []}), encoding="utf-8")
    before_jobs = jobs_path.read_bytes()
    before_config = config_path.read_bytes()
    monkeypatch.setattr(guard, "_atomic_yaml_write", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        guard.restore_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)

    assert jobs_path.read_bytes() == before_jobs
    assert config_path.read_bytes() == before_config


def test_restore_preserves_top_level_list_registry_schema(tmp_path):
    from cron.routing_guard import capture_manifest, restore_manifest

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    jobs[0]["model"] = "drifted-model"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    restore_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)

    restored = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert isinstance(restored, list)
    assert restored[0]["model"] == "model-a"


def test_cli_restore_repairs_routing_drift(tmp_path):
    import cron.routing_guard as guard

    jobs_path, config_path, manifest_path = _write_fixture(tmp_path)
    guard.capture_manifest(jobs_path=jobs_path, config_path=config_path, manifest_path=manifest_path)
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs["jobs"][0]["model"] = "drifted-model"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    exit_code = guard.main(
        [
            "restore",
            "--jobs-path", str(jobs_path),
            "--config-path", str(config_path),
            "--manifest-path", str(manifest_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"][0]["model"] == "model-a"
