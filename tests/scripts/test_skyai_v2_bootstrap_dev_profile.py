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


def test_bootstrap_config_has_no_generic_database_url_fallback() -> None:
    config_text = bootstrap.dump_profile_config(bootstrap.build_profile_config())

    assert "DATABASE_URL" not in config_text
    assert "SKYAI_CI_DATABASE_URL" not in config_text


def test_env_template_mentions_only_skyai_specific_future_db_secret() -> None:
    assert "SKYAI_CI_DATABASE_URL" in bootstrap.ENV_TEMPLATE
    assert "DATABASE_URL=" not in bootstrap.ENV_TEMPLATE.replace("SKYAI_CI_DATABASE_URL=", "")
