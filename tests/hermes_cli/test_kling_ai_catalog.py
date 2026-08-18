from pathlib import Path

from hermes_cli.mcp_catalog import _build_server_config, _parse_manifest


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "optional-mcps" / "kling-ai" / "manifest.yaml"


def test_kling_ai_catalog_manifest() -> None:
    entry = _parse_manifest(MANIFEST)

    assert entry.name == "Plugin-Hermes-kling-ai"
    assert entry.transport.type == "http"
    assert entry.transport.url == "${KLING_AI_MCP_URL}"
    assert entry.auth.type == "oauth"
    assert len(entry.auth.env) == 1
    assert entry.auth.env[0].name == "KLING_AI_MCP_URL"
    assert entry.auth.env[0].default == "https://klingai.com/mcp"
    assert entry.auth.env[0].secret is False
    assert entry.suggest is not None
    assert entry.suggest.keywords == ["kling", "kling ai", "可灵"]
    assert entry.suggest.hosts == ["klingai.com", "kling.ai"]
    assert "https://kling.ai/mcp" in entry.post_install


def test_kling_ai_catalog_builds_one_oauth_server() -> None:
    entry = _parse_manifest(MANIFEST)

    assert _build_server_config(entry, {}) == {
        "url": "${KLING_AI_MCP_URL}",
        "auth": "oauth",
    }
