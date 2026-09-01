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


@pytest.mark.parametrize(
    "name",
    [
        "settings.py",
        "config.ts",
        "config.md",
        "config.mjs",
        "config.cjs",
        "config.mts",
        "config.cts",
    ],
)
def test_config_like_path_keeps_source_and_docs_conservative(name):
    assert not is_config_like_path(name)


_TOML_SECRET_ASSIGNMENTS = [
    pytest.param(
        'mcp.exaApiKey = "syntheticOpaqueDottedTomlSecret"',
        "syntheticOpaqueDottedTomlSecret",
        'mcp.exaApiKey = "«redacted-secret»"',
        id="dotted-bare-key",
    ),
    pytest.param(
        '"braveSearchApiKey" = "syntheticOpaqueQuotedTomlSecret"',
        "syntheticOpaqueQuotedTomlSecret",
        '"braveSearchApiKey" = "«redacted-secret»"',
        id="quoted-key",
    ),
    pytest.param(
        "'braveSearchApiKey' = 'syntheticOpaqueSingleQuotedTomlSecret'",
        "syntheticOpaqueSingleQuotedTomlSecret",
        "'braveSearchApiKey' = '«redacted-secret»'",
        id="single-quoted-key-and-value",
    ),
    pytest.param(
        "mcp.'exaApiKey' = 'syntheticOpaqueQuotedDottedTomlSecret'",
        "syntheticOpaqueQuotedDottedTomlSecret",
        "mcp.'exaApiKey' = '«redacted-secret»'",
        id="quoted-dotted-segment",
    ),
]


@pytest.mark.parametrize("assignment,secret,redacted", _TOML_SECRET_ASSIGNMENTS)
def test_read_file_redacts_toml_secret_key_assignment(
    tmp_path, assignment, secret, redacted
):
    config = tmp_path / "providers.toml"
    config.write_text(f"{assignment}\n", encoding="utf-8")

    result = json.loads(
        read_file_tool(str(config), task_id="config-redaction-toml-read")
    )

    assert secret not in result["content"]
    assert redacted in result["content"]


@pytest.mark.parametrize("assignment,secret,redacted", _TOML_SECRET_ASSIGNMENTS)
def test_search_redacts_toml_secret_key_assignment(
    tmp_path, assignment, secret, redacted
):
    config = tmp_path / "providers.toml"
    config.write_text(f"{assignment}\n", encoding="utf-8")

    result = json.loads(
        search_tool(
            pattern="ApiKey",
            target="content",
            path=str(tmp_path),
            task_id="config-redaction-toml-search",
        )
    )
    rendered = json.dumps(result, ensure_ascii=False)

    assert secret not in rendered
    assert any(match["content"] == redacted for match in result["matches"])


@pytest.mark.parametrize("assignment,secret,redacted", _TOML_SECRET_ASSIGNMENTS)
def test_patch_redacts_toml_secret_key_assignment(
    tmp_path, assignment, secret, redacted
):
    replacement = f"replacement{secret}"
    config = tmp_path / "providers.toml"
    config.write_text(f"{assignment}\n", encoding="utf-8")

    result = json.loads(
        patch_tool(
            path=str(config),
            old_string=secret,
            new_string=replacement,
            task_id="config-redaction-toml-patch",
        )
    )

    assert result["success"] is True
    assert secret not in result["diff"]
    assert replacement not in result["diff"]
    assert redacted in result["diff"]
    assert (
        config.read_text(encoding="utf-8")
        == f"{assignment.replace(secret, replacement)}\n"
    )


_INLINE_TOML_SECRET_ASSIGNMENT = (
    'mcp = { exaApiKey = "syntheticOpaqueInlineTomlSecret" }'
)
_INLINE_TOML_SECRET = "syntheticOpaqueInlineTomlSecret"
_INLINE_TOML_REDACTED = 'mcp = { exaApiKey = "«redacted-secret»" }'


def test_read_file_redacts_secret_in_toml_inline_table(tmp_path):
    config = tmp_path / "providers.toml"
    config.write_text(f"{_INLINE_TOML_SECRET_ASSIGNMENT}\n", encoding="utf-8")

    result = json.loads(
        read_file_tool(str(config), task_id="config-redaction-inline-toml-read")
    )

    assert _INLINE_TOML_SECRET not in result["content"]
    assert _INLINE_TOML_REDACTED in result["content"]


def test_search_redacts_secret_in_toml_inline_table(tmp_path):
    config = tmp_path / "providers.toml"
    config.write_text(f"{_INLINE_TOML_SECRET_ASSIGNMENT}\n", encoding="utf-8")

    result = json.loads(
        search_tool(
            pattern="exaApiKey",
            target="content",
            path=str(tmp_path),
            task_id="config-redaction-inline-toml-search",
        )
    )
    rendered = json.dumps(result, ensure_ascii=False)

    assert _INLINE_TOML_SECRET not in rendered
    assert any(
        match["content"] == _INLINE_TOML_REDACTED for match in result["matches"]
    )


def test_patch_redacts_secret_in_toml_inline_table(tmp_path):
    replacement = f"replacement{_INLINE_TOML_SECRET}"
    config = tmp_path / "providers.toml"
    config.write_text(f"{_INLINE_TOML_SECRET_ASSIGNMENT}\n", encoding="utf-8")

    result = json.loads(
        patch_tool(
            path=str(config),
            old_string=_INLINE_TOML_SECRET,
            new_string=replacement,
            task_id="config-redaction-inline-toml-patch",
        )
    )

    assert result["success"] is True
    assert _INLINE_TOML_SECRET not in result["diff"]
    assert replacement not in result["diff"]
    assert _INLINE_TOML_REDACTED in result["diff"]
    assert config.read_text(encoding="utf-8") == (
        f"{_INLINE_TOML_SECRET_ASSIGNMENT.replace(_INLINE_TOML_SECRET, replacement)}\n"
    )


_QUOTED_YAML_SECRET_ASSIGNMENTS = [
    pytest.param(
        '"api.key": syntheticOpaqueDoubleQuotedYamlSecret',
        "syntheticOpaqueDoubleQuotedYamlSecret",
        '"api.key": «redacted-secret»',
        id="double-quoted-dotted-key",
    ),
    pytest.param(
        "'api_key': syntheticOpaqueSingleQuotedYamlSecret",
        "syntheticOpaqueSingleQuotedYamlSecret",
        "'api_key': «redacted-secret»",
        id="single-quoted-key",
    ),
]


@pytest.mark.parametrize("assignment,secret,redacted", _QUOTED_YAML_SECRET_ASSIGNMENTS)
def test_read_file_redacts_quoted_yaml_secret_key_assignment(
    tmp_path, assignment, secret, redacted
):
    config = tmp_path / "providers.yaml"
    config.write_text(f"{assignment}\n", encoding="utf-8")

    result = json.loads(
        read_file_tool(str(config), task_id="config-redaction-quoted-yaml-read")
    )

    assert secret not in result["content"]
    assert redacted in result["content"]


@pytest.mark.parametrize("assignment,secret,redacted", _QUOTED_YAML_SECRET_ASSIGNMENTS)
def test_search_redacts_quoted_yaml_secret_key_assignment(
    tmp_path, assignment, secret, redacted
):
    config = tmp_path / "providers.yaml"
    config.write_text(f"{assignment}\n", encoding="utf-8")

    result = json.loads(
        search_tool(
            pattern="api",
            target="content",
            path=str(tmp_path),
            task_id="config-redaction-quoted-yaml-search",
        )
    )
    rendered = json.dumps(result, ensure_ascii=False)

    assert secret not in rendered
    assert any(match["content"] == redacted for match in result["matches"])


@pytest.mark.parametrize("assignment,secret,redacted", _QUOTED_YAML_SECRET_ASSIGNMENTS)
def test_patch_redacts_quoted_yaml_secret_key_assignment(
    tmp_path, assignment, secret, redacted
):
    replacement = f"replacement{secret}"
    config = tmp_path / "providers.yaml"
    config.write_text(f"{assignment}\n", encoding="utf-8")

    result = json.loads(
        patch_tool(
            path=str(config),
            old_string=secret,
            new_string=replacement,
            task_id="config-redaction-quoted-yaml-patch",
        )
    )

    assert result["success"] is True
    assert secret not in result["diff"]
    assert replacement not in result["diff"]
    assert redacted in result["diff"]
    assert (
        config.read_text(encoding="utf-8")
        == f"{assignment.replace(secret, replacement)}\n"
    )


def _escaped_json_secret_config():
    value = 'syntheticEscapedJsonHead"syntheticEscapedJsonTail'
    return value, json.dumps({"apiKey": value}, ensure_ascii=False)


def test_read_file_redacts_complete_escaped_json_string_and_keeps_json_valid(tmp_path):
    secret, serialized = _escaped_json_secret_config()
    config = tmp_path / "providers.json"
    config.write_text(f"{serialized}\n", encoding="utf-8")

    result = json.loads(
        read_file_tool(str(config), task_id="config-redaction-escaped-json-read")
    )
    redacted_line = result["content"].splitlines()[0].split("|", 1)[1]

    assert all(part not in result["content"] for part in secret.split('"'))
    assert json.loads(redacted_line) == {"apiKey": "«redacted-secret»"}


def test_search_redacts_complete_escaped_json_string_and_keeps_json_valid(tmp_path):
    secret, serialized = _escaped_json_secret_config()
    config = tmp_path / "providers.json"
    config.write_text(f"{serialized}\n", encoding="utf-8")

    result = json.loads(
        search_tool(
            pattern="apiKey",
            target="content",
            path=str(tmp_path),
            task_id="config-redaction-escaped-json-search",
        )
    )
    rendered = json.dumps(result, ensure_ascii=False)

    assert all(part not in rendered for part in secret.split('"'))
    assert len(result["matches"]) == 1
    assert json.loads(result["matches"][0]["content"]) == {
        "apiKey": "«redacted-secret»"
    }


def test_patch_redacts_complete_escaped_json_strings_and_keeps_json_valid(tmp_path):
    secret, serialized = _escaped_json_secret_config()
    replacement_head = "replacementEscapedJsonHead"
    config = tmp_path / "providers.json"
    config.write_text(f"{serialized}\n", encoding="utf-8")

    result = json.loads(
        patch_tool(
            path=str(config),
            old_string=secret.split('"', 1)[0],
            new_string=replacement_head,
            task_id="config-redaction-escaped-json-patch",
        )
    )

    assert result["success"] is True
    assert all(part not in result["diff"] for part in secret.split('"'))
    assert replacement_head not in result["diff"]
    redacted_json = json.dumps({"apiKey": "«redacted-secret»"}, ensure_ascii=False)
    assert f"-{redacted_json}" in result["diff"]
    assert f"+{redacted_json}" in result["diff"]
    assert json.loads(config.read_text(encoding="utf-8"))["apiKey"].startswith(
        replacement_head
    )


def test_read_file_preserves_editable_json_example_in_mjs_source(tmp_path):
    example = "syntheticEditableJsonExample24680"
    source = tmp_path / "config.mjs"
    source.write_text(
        f'export const EXAMPLE = {{"exaApiKey": "{example}"}};\n', encoding="utf-8"
    )

    result = json.loads(
        read_file_tool(str(source), task_id="source-mjs-redaction-read")
    )

    assert example in result["content"]


def test_search_preserves_editable_json_example_in_mjs_source(tmp_path):
    example = "syntheticEditableJsonExample24680"
    source = tmp_path / "config.mjs"
    source.write_text(
        f'export const EXAMPLE = {{"exaApiKey": "{example}"}};\n', encoding="utf-8"
    )

    result = json.loads(
        search_tool(
            pattern="exaApiKey",
            target="content",
            path=str(tmp_path),
            task_id="source-mjs-redaction-search",
        )
    )

    assert example in json.dumps(result, ensure_ascii=False)


def test_patch_preserves_editable_json_example_in_mjs_source(tmp_path):
    example = "syntheticEditableJsonExample24680"
    source = tmp_path / "config.mjs"
    source.write_text(
        f'export const EXAMPLE = {{"exaApiKey": "{example}"}};\n', encoding="utf-8"
    )

    result = json.loads(
        patch_tool(
            path=str(source),
            old_string="EXAMPLE",
            new_string="EDITABLE_EXAMPLE",
            task_id="source-mjs-redaction-patch",
        )
    )

    assert result["success"] is True
    assert example in result["diff"]


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
