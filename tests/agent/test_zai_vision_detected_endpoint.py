"""Regression test: ZAI vision resolver must honor the endpoint auto-detected
at setup time (auth.json providers.zai.detected_endpoint) instead of only
trying the hardcoded generic endpoints.

Before the fix, a Coding Lite / Coding Plan key — which is only valid on
/api/coding/paas/v4 — had its vision calls routed to the generic
/api/paas/v4 endpoint, producing error 1113 ("insufficient balance").  The
main chat model already used the detected endpoint via the credential pool
(agent/credential_pool.py -> _resolve_zai_base_url); this test locks in the
same behaviour for the vision resolver.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

import pytest


@pytest.fixture
def isolated_home(monkeypatch):
    """Temp HERMES_HOME with auth.json + clean credential env vars."""
    test_home = tempfile.mkdtemp(prefix="hermes_test_zai_vis_")
    hermes_home = os.path.join(test_home, ".hermes")
    os.makedirs(hermes_home)
    monkeypatch.setenv("HERMES_HOME", hermes_home)

    # Strip all credential-shaped env vars so each scenario starts hermetic.
    for k in list(os.environ.keys()):
        if k.endswith("_API_KEY") or k.endswith("_TOKEN"):
            monkeypatch.delenv(k, raising=False)

    yield hermes_home
    shutil.rmtree(test_home, ignore_errors=True)


def _write_auth(home: str, api_key: str, base_url: str) -> None:
    """Write an auth.json with a detected_endpoint cache for the given key."""
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    auth = {
        "version": 1,
        "providers": {
            "zai": {
                "detected_endpoint": {
                    "base_url": base_url,
                    "endpoint_id": "coding-global",
                    "model": "glm-5.2",
                    "label": "Global (Coding Plan)",
                    "key_hash": key_hash,
                }
            }
        },
    }
    with open(os.path.join(home, "auth.json"), "w") as fp:
        json.dump(auth, fp)


class TestZaiVisionDetectedEndpoint:
    def test_detected_coding_endpoint_used_for_vision(self, isolated_home, monkeypatch):
        """The detected /api/coding/paas/v4 URL must be the first candidate
        tried for vision — not the generic /api/paas/v4 that 1113s on a
        coding-plan key.
        """
        api_key = "sk-test-coding-plan-key"
        _write_auth(isolated_home, api_key, "https://api.z.ai/api/coding/paas/v4")
        monkeypatch.setenv("GLM_API_KEY", api_key)

        import agent.auxiliary_client as auxiliary_client
        monkeypatch.setattr(auxiliary_client, "_AUTH_JSON_PATH", Path(isolated_home) / "auth.json")
        resolve_vision_provider_client = auxiliary_client.resolve_vision_provider_client

        provider, client, _model = resolve_vision_provider_client(provider="zai")
        assert client is not None, (
            "vision client should resolve with the detected endpoint"
        )
        base_url = str(getattr(client, "base_url", ""))
        assert "coding/paas/v4" in base_url, (
            f"vision should use the detected coding endpoint, got {base_url!r}"
        )

    def test_detected_standard_endpoint_used_for_vision(self, isolated_home, monkeypatch):
        """A standard API key must keep its detected generic Z.AI endpoint."""
        api_key = "sk-test-standard-api-key"
        standard_url = "https://api.z.ai/api/paas/v4"
        _write_auth(isolated_home, api_key, standard_url)
        monkeypatch.setenv("GLM_API_KEY", api_key)

        import agent.auxiliary_client as auxiliary_client
        monkeypatch.setattr(
            auxiliary_client, "_AUTH_JSON_PATH", Path(isolated_home) / "auth.json"
        )

        _provider, client, _model = (
            auxiliary_client.resolve_vision_provider_client(provider="zai")
        )
        assert client is not None
        assert str(getattr(client, "base_url", "")).rstrip("/") == standard_url

    def test_credential_pool_key_selects_matching_cached_endpoint(self, isolated_home, monkeypatch):
        """A pool-only key must validate and use the cached coding endpoint."""
        api_key = "pool-only-zai-key"
        _write_auth(isolated_home, api_key, "https://api.z.ai/api/coding/paas/v4")

        from hermes_cli import auth
        import agent.auxiliary_client as auxiliary_client
        monkeypatch.setattr(auxiliary_client, "_AUTH_JSON_PATH", Path(isolated_home) / "auth.json")
        resolve_vision_provider_client = auxiliary_client.resolve_vision_provider_client

        # No *_API_KEY variables are set by isolated_home. Mock the canonical
        # secret resolver at its pool-fallback boundary, without probing.
        monkeypatch.setattr(
            auth, "_resolve_api_key_provider_secret",
            lambda provider_id, pconfig: (api_key, "credential_pool:zai"),
        )
        provider, client, _model = resolve_vision_provider_client(provider="zai")
        assert client is not None
        assert "coding/paas/v4" in str(getattr(client, "base_url", ""))

    def test_stale_key_hash_falls_back_to_hardcoded(self, isolated_home, monkeypatch):
        """When the cached detected_endpoint was recorded for a *different* key,
        the hash must not match and the cached coding endpoint must NOT be
        used — resolution falls back to the hardcoded generic URLs so a stale
        entry can never poison resolution.
        """
        _write_auth(isolated_home, "sk-old-key",
                    "https://api.z.ai/api/coding/paas/v4")
        monkeypatch.setenv("GLM_API_KEY", "sk-current-key")

        import agent.auxiliary_client as auxiliary_client
        monkeypatch.setattr(auxiliary_client, "_AUTH_JSON_PATH", Path(isolated_home) / "auth.json")
        resolve_vision_provider_client = auxiliary_client.resolve_vision_provider_client

        provider, client, _model = resolve_vision_provider_client(provider="zai")
        assert client is not None
        base_url = str(getattr(client, "base_url", ""))
        assert "coding/paas/v4" not in base_url, (
            "stale key_hash must NOT serve the cached coding endpoint; "
            f"got {base_url!r}"
        )
