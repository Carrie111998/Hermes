"""``model_catalog.excluded_providers`` must also hide providers from the
model-options payload — the shape the desktop app and the dashboard render.

The desktop "Models" dialog exposes provider on/off switches that write this
config key (there is no desktop-local blocklist), so the payload builder is the
seam that makes those switches mean anything. ``build_model_options_payload``
feeds ``/api/model/options`` (dashboard + desktop REST) and the gateway's
``model.options``; a regression here would leave the switch flipped in the UI
while the provider kept showing up in every picker.
"""

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """HERMES_HOME with credentials for two providers via env vars."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-openrouter")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek")
    return home


def _write_config(home, **top_level):
    cfg = {"model": {"default": "deepseek-chat", "provider": "deepseek"}}
    cfg.update(top_level)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))


def _payload_slugs(**top_level):
    from hermes_cli.inventory import (
        build_model_options_payload,
        load_picker_context,
    )

    payload = build_model_options_payload(load_picker_context())
    return {str(row.get("slug", "")).lower() for row in payload["providers"]}


def test_model_options_payload_hides_excluded_provider(config_home):
    _write_config(config_home)
    assert "openrouter" in _payload_slugs(), (
        "sanity: a credentialled OpenRouter should be in the payload"
    )

    _write_config(
        config_home,
        **{"model_catalog": {"excluded_providers": ["openrouter"]}},
    )
    assert "openrouter" not in _payload_slugs(), (
        "excluded_providers must remove the row from the model-options payload"
    )


def test_model_options_payload_exclusion_is_case_insensitive(config_home):
    """The desktop writes back whatever case the catalog row carried, so the
    backend match must not be case-sensitive."""
    _write_config(
        config_home,
        **{"model_catalog": {"excluded_providers": ["OpenRouter"]}},
    )
    assert "openrouter" not in _payload_slugs()


def test_model_options_payload_keeps_other_providers(config_home):
    """Excluding one provider must not disturb the rest of the catalog."""
    _write_config(
        config_home,
        **{"model_catalog": {"excluded_providers": ["openrouter"]}},
    )
    assert "deepseek" in _payload_slugs()


class TestDesktopWritePath:
    """The desktop provider switch writes the blocklist through
    ``PUT /api/config``. That endpoint deep-merges the body over what is on
    disk, so switching the LAST excluded provider back on has to send an
    explicit empty list — omitting the key would leave the old list in place and
    the provider hidden forever. This pins the round trip the UI depends on."""

    @pytest.fixture(autouse=True)
    def _setup(self, config_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import (
            _SESSION_HEADER_NAME,
            _SESSION_TOKEN,
            app,
        )

        _write_config(config_home)
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _put_excluded(self, excluded):
        config = self.client.get("/api/config").json()
        config["model_catalog"] = {
            **(config.get("model_catalog") or {}),
            "excluded_providers": excluded,
        }
        resp = self.client.put("/api/config", json={"config": config})
        assert resp.status_code == 200, resp.text

    def test_switching_a_provider_off_then_on_round_trips(self):
        from hermes_cli.config import read_raw_config

        self._put_excluded(["openrouter"])
        assert read_raw_config()["model_catalog"]["excluded_providers"] == [
            "openrouter"
        ]

        # Switching it back on: an explicit empty list must clear the blocklist
        # through the deep merge.
        self._put_excluded([])
        assert read_raw_config()["model_catalog"]["excluded_providers"] == []
