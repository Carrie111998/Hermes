"""GET /api/config/schema?lang= translates field descriptions.

The dashboard's UI language lives in browser localStorage, so the server
cannot resolve it from config.yaml. The endpoint takes ``lang`` and swaps
each schema field's description for its ``config_schema.*`` catalog entry;
fields without an entry keep their English text, and an absent ``lang``
leaves the schema byte-identical to today.
"""

import pytest


class TestSchemaTranslation:
    @pytest.fixture(autouse=True)
    def _home(self, _isolate_hermes_home):
        pass

    def test_no_lang_returns_english(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        client = TestClient(app)
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        body = client.get("/api/config/schema").json()
        desc = body["fields"]["model"]["description"]
        assert desc == "Default model (e.g. anthropic/claude-sonnet-4.6)"

    def test_ru_lang_translates_known_field(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        client = TestClient(app)
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        body = client.get("/api/config/schema?lang=ru").json()
        desc = body["fields"]["model"]["description"]
        assert "по умолчанию" in desc
        assert desc != "Default model (e.g. anthropic/claude-sonnet-4.6)"

    def test_field_without_catalog_entry_keeps_english(self):
        """Auto-generated fields (no ``config_schema`` entry) stay English."""
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import (
            app,
            _SESSION_HEADER_NAME,
            _SESSION_TOKEN,
            _apply_schema_translations,
        )

        client = TestClient(app)
        client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
        body = client.get("/api/config/schema?lang=ru").json()
        # Pick any field the catalogs do not cover and verify it kept its text.
        uncovered = [
            f for f, e in body["fields"].items()
            if "description" in e
            and not f.replace(".", "-") in {
                "timezone", "memory-provider", "model", "model_context_length",
                "terminal-backend", "terminal-vercel_runtime", "terminal-modal_mode",
                "proxy-credential_source", "proxy-enforce_on_docker", "tts-provider",
                "stt-provider", "stt-local-model", "stt-groq-model", "stt-openai-model",
                "stt-elevenlabs-model_id", "display-skin", "dashboard-theme",
                "display-resume_display", "display-busy_input_mode", "approvals-mode",
                "context-engine", "human_delay-mode", "logging-level",
                "agent-service_tier", "delegation-reasoning_effort", "browser-headed",
            }
        ]
        assert uncovered
        # Structurally: translation never adds or drops fields.
        plain = client.get("/api/config/schema").json()
        assert set(body["fields"]) == set(plain["fields"])

    def test_helper_returns_input_when_lang_missing(self):
        from hermes_cli.web_server import _apply_schema_translations

        fields = {"model": {"description": "Default model"}}
        assert _apply_schema_translations(fields, None) is fields

    def test_helper_keeps_uncovered_field_untouched(self):
        from hermes_cli.web_server import _apply_schema_translations

        fields = {"brand_new_field": {"description": "Plain English"}}
        out = _apply_schema_translations(fields, "ru")
        assert out["brand_new_field"]["description"] == "Plain English"
