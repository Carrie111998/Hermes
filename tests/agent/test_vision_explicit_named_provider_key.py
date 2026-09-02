"""Regression test: explicit auxiliary.vision.api_key must reach the client (#96232).

`resolve_vision_provider_client` with an explicitly configured named provider
(e.g. ``auxiliary.vision.provider: my-vision-provider``) dropped the resolved
per-task ``api_key`` on its final ``_get_cached_client`` call, so the
credential-pool / auth.json key for the provider was sent instead — a 401 on
named custom providers whose pool credential differs from the explicit
``auxiliary.vision.api_key``. The zai branch above already threaded the key;
the generic named-provider path now does too.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: test-model\n"
        "custom_providers:\n"
        "  - name: my-vision-provider\n"
        "    base_url: https://example.invalid/v1\n"
        "    api_mode: chat_completions\n"
        "    model: my-vision-model\n"
    )


def _capture_client(captured):
    def _fake_cached_client(provider, model=None, async_mode=False,
                            base_url=None, api_key=None, api_mode=None,
                            main_runtime=None, is_vision=False, task=None):
        captured["provider"] = provider
        captured["api_key"] = api_key
        return MagicMock(), model

    return _fake_cached_client


class TestVisionExplicitNamedProviderKey:
    def test_explicit_api_key_reaches_the_client_builder(self, monkeypatch):
        """The explicitly configured per-task key must be what the client is
        built with — not the pool/auth.json credential."""
        from agent import auxiliary_client as ac

        # Non-secret test fixture value via env (never a usable credential).
        monkeypatch.setenv("HERMES_TEST_VISION_KEY", "test-key-not-a-credential")
        explicit_key = os.environ["HERMES_TEST_VISION_KEY"]

        captured = {}
        with patch.object(ac, "_get_cached_client", _capture_client(captured)):
            provider, client, final_model = ac.resolve_vision_provider_client(
                provider="my-vision-provider",
                model="my-vision-model",
                api_key=explicit_key,
            )

        assert provider == "my-vision-provider"
        assert client is not None
        assert captured["api_key"] == explicit_key
        assert captured["provider"] == "my-vision-provider"

    def test_absent_api_key_stays_none(self):
        """No explicit key → None passes through so the pool/env resolution
        path inside the builder is unchanged."""
        from agent import auxiliary_client as ac

        captured = {}
        with patch.object(ac, "_get_cached_client", _capture_client(captured)):
            ac.resolve_vision_provider_client(
                provider="my-vision-provider",
                model="my-vision-model",
            )

        assert captured["api_key"] is None
