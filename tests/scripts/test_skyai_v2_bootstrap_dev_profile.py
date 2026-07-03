from __future__ import annotations

from pathlib import Path

import yaml

from scripts import skyai_v2_bootstrap_dev_profile as bootstrap


def test_bootstrap_profile_dry_run_does_not_write(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / "skyai-v2-dev"

    result = bootstrap.bootstrap_profile(profile_home, apply=False)

    assert result["mode"] == "dry_run"
    assert not profile_home.exists()


def test_bootstrap_profile_apply_creates_dedicated_skyai_profile(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / "skyai-v2-dev"

    result = bootstrap.bootstrap_profile(profile_home, apply=True)

    assert result["mode"] == "apply"
    assert (profile_home / "config.yaml").is_file()
    assert (profile_home / ".env").is_file()
    assert (profile_home / "SOUL.md").is_file()
    assert (profile_home / "skyai_v2").is_dir()

    config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"] == ["skyai-customer"]
    assert config["toolsets"] == ["skyai_customer"]
    assert config["platform_toolsets"]["gateway"] == ["skyai_customer"]
    assert config["memory"]["memory_enabled"] is False
    assert config["skyai_v2"]["canary_gateway"]["host"] == "127.0.0.1"


def test_bootstrap_profile_can_inherit_nonsecret_model_config(tmp_path: Path) -> None:
    profile_home = tmp_path / "profiles" / "skyai-v2-dev"
    model_config = {
        "default": "gpt-5.5",
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_mode": "codex_responses",
    }

    bootstrap.bootstrap_profile(profile_home, apply=True, model_config=model_config)

    config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"] == model_config


def test_merge_model_config_explicit_values_override_inherited_fields() -> None:
    assert bootstrap.merge_model_config(
        {
            "default": "gpt-5.4-mini",
            "provider": "openai-codex",
            "api_mode": "codex_responses",
        },
        {
            "default": "gpt-5.5",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_mode": "",
        },
    ) == {
        "default": "gpt-5.5",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "base_url": "https://chatgpt.com/backend-api/codex",
    }


def test_parse_args_accepts_explicit_model_flags() -> None:
    args = bootstrap.parse_args(
        [
            "--model-default",
            "gpt-5.5",
            "--model-provider",
            "openai-codex",
            "--model-base-url",
            "https://chatgpt.com/backend-api/codex",
            "--model-api-mode",
            "codex_responses",
        ]
    )

    assert args.model_default == "gpt-5.5"
    assert args.model_provider == "openai-codex"
    assert args.model_base_url == "https://chatgpt.com/backend-api/codex"
    assert args.model_api_mode == "codex_responses"


def test_load_nonsecret_root_model_config_filters_secret_like_fields(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "gpt-5.5",
                    "provider": "openai-codex",
                    "api_mode": "codex_responses",
                    "api_key": "must-not-copy",
                    "token": "must-not-copy",
                }
            }
        ),
        encoding="utf-8",
    )

    assert bootstrap.load_nonsecret_root_model_config(root) == {
        "default": "gpt-5.5",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
    }


def test_bootstrap_config_has_no_generic_database_url_fallback() -> None:
    config_text = bootstrap.dump_profile_config(bootstrap.build_profile_config())

    assert "DATABASE_URL" not in config_text
    assert "SKYAI_CI_DATABASE_URL" not in config_text


def test_env_template_mentions_only_skyai_specific_future_db_secret() -> None:
    assert "SKYAI_CI_DATABASE_URL" in bootstrap.ENV_TEMPLATE
    assert "DATABASE_URL=" not in bootstrap.ENV_TEMPLATE.replace("SKYAI_CI_DATABASE_URL=", "")
