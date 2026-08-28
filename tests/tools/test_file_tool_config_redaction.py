"""Regression tests for path-aware secret redaction at file-tool boundaries."""

import json

import pytest

from agent.redact import is_config_like_path
from tools.file_tools import patch_tool, read_file_tool, search_tool


@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "config.yml",
        "providers.json",
        "settings.toml",
        ".env",
        ".env.local",
        "service-settings",
        "runtime.config.local",
    ],
)
def test_config_like_path_recognizes_config_and_settings_surfaces(name):
    assert is_config_like_path(name)


@pytest.mark.parametrize("name", ["settings.py", "config.ts", "config.md"])
def test_config_like_path_keeps_source_and_docs_conservative(name):
    assert not is_config_like_path(name)


def test_read_file_redacts_opaque_secret_values_from_yaml_config(tmp_path):
    secret = "syntheticOpaqueYamlCredential12345"
    config = tmp_path / "service-settings.yml"
    config.write_text(f"model:\n  api_key: {secret}\n  name: public-model\n", encoding="utf-8")

    result = json.loads(read_file_tool(str(config), task_id="config-redaction-read"))

    assert secret not in result["content"]
    assert "«redacted-secret»" in result["content"]
    assert "name: public-model" in result["content"]


def test_read_file_keeps_nonreusable_vendor_sentinel_in_config(tmp_path):
    synthetic_vendor_shape = "ghp_" + "A" * 30
    config = tmp_path / "vendor-config.yml"
    config.write_text(
        f"api_key: {synthetic_vendor_shape}\n", encoding="utf-8"
    )

    result = json.loads(
        read_file_tool(str(config), task_id="config-redaction-vendor-sentinel")
    )

    assert synthetic_vendor_shape not in result["content"]
    assert "«redacted:ghp_…»" in result["content"]


def test_read_file_redacts_extensionless_settings_surface(tmp_path):
    secret = "syntheticExtensionlessSettingsCredential86420"
    settings = tmp_path / "service-settings"
    settings.write_text(f"api_key={secret}\nmode=public\n", encoding="utf-8")

    result = json.loads(
        read_file_tool(str(settings), task_id="config-redaction-extensionless")
    )

    assert secret not in result["content"]
    assert "«redacted-secret»" in result["content"]
    assert "mode=public" in result["content"]


def test_read_file_redacts_quoted_yaml_secret_value(tmp_path):
    secret = "syntheticQuotedYamlCredential97531"
    config = tmp_path / "quoted-config.yaml"
    config.write_text(f'api_key: "{secret}"\nmode: public\n', encoding="utf-8")

    result = json.loads(
        read_file_tool(str(config), task_id="config-redaction-quoted-yaml")
    )

    assert secret not in result["content"]
    assert 'api_key: "«redacted-secret»"' in result["content"]
    assert "mode: public" in result["content"]


def test_search_redacts_mcp_and_provider_credentials_only_in_config(tmp_path):
    config_secrets = {
        "exaApiKey": "syntheticOpaqueExaCredential24680",
        "braveSearchApiKey": "syntheticOpaqueBraveCredential10293",
        "SERVICE_TOKEN": "syntheticOpaqueMcpEnvCredential38475",
        "x-api-key": "syntheticOpaqueMcpHeaderCredential65748",
    }
    source_example = "syntheticSourceExampleValue13579"
    (tmp_path / "providers.json").write_text(
        json.dumps(
            {
                "exaApiKey": config_secrets["exaApiKey"],
                "braveSearchApiKey": config_secrets["braveSearchApiKey"],
                "mcp": {
                    "env": {"SERVICE_TOKEN": config_secrets["SERVICE_TOKEN"]},
                    "headers": {"x-api-key": config_secrets["x-api-key"]},
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "provider_settings.py").write_text(
        f'EXAMPLE = {{"exaApiKey": "{source_example}"}}\n', encoding="utf-8"
    )

    result = json.loads(
        search_tool(
            pattern="exaApiKey",
            target="content",
            path=str(tmp_path),
            task_id="config-redaction-search",
        )
    )
    rendered = json.dumps(result, ensure_ascii=False)

    assert all(secret not in rendered for secret in config_secrets.values())
    assert "«redacted-secret»" in rendered
    assert source_example in rendered


def test_patch_redacts_config_diff_without_redacting_source_example(tmp_path):
    old_secret = "syntheticOldPatchCredential11223"
    new_secret = "syntheticNewPatchCredential44556"
    config = tmp_path / "settings.yml"
    config.write_text(f"secret_key: {old_secret}\n", encoding="utf-8")

    config_result = json.loads(
        patch_tool(
            path=str(config),
            old_string=old_secret,
            new_string=new_secret,
            task_id="config-redaction-patch",
        )
    )

    assert config_result["success"] is True
    assert old_secret not in config_result["diff"]
    assert new_secret not in config_result["diff"]
    assert "«redacted-secret»" in config_result["diff"]
    assert config.read_text(encoding="utf-8") == f"secret_key: {new_secret}\n"

    source_example = "syntheticSourcePatchExample77889"
    source = tmp_path / "provider_settings.py"
    source.write_text(
        f'EXAMPLE = {{"secret_key": "{source_example}"}}\n', encoding="utf-8"
    )
    source_result = json.loads(
        patch_tool(
            path=str(source),
            old_string="EXAMPLE =",
            new_string="UPDATED_EXAMPLE =",
            task_id="source-example-patch",
        )
    )

    assert source_result["success"] is True
    assert source_example in source_result["diff"]
