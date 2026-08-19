"""Regression for #90089 Part 2 — pinned model/provider intermittently
ignored on manual cron runs.

Root cause: when the primary provider resolution fails transiently (auth
blip, network hiccup), the fallback chain in ``run_job`` unconditionally
overwrites ``model = fb_model`` (line ~5400) — even when the job has an
explicit ``model`` pin.  This means a job pinned to ``glm-4.5-air`` /
``zai`` that hits a transient zai auth failure falls back to a different
provider AND model, silently ignoring the pin.  The intermittent nature
matches: only the runs where the primary provider resolution failed
exhibited the bug.

Fix: the fallback chain must not swap the model when the job has an
explicit ``job["model"]`` pin.  The provider can still fall back (the
credentials are gone), but the model pin is preserved so the operator's
intent is respected — the fallback provider simply receives the pinned
model name (which may or may not be available there, but at least the pin
is not silently discarded).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import run_job


def _base_job(**overrides):
    job = {
        "id": "pin-90089",
        "name": "pin test 90089",
        "prompt": "hello",
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "model_snapshot": None,
        "base_url": None,
    }
    job.update(overrides)
    return job


def _run_with_fallback(
    job,
    *,
    primary_provider,
    primary_raises,
    fallback_provider,
    fallback_model,
    tmp_path,
    config_model="global-default-model",
):
    """Drive run_job where the primary provider resolution raises and the
    fallback chain is walked.  Returns (success, output, error, model_used).

    ``model_used`` is the model string passed to AIAgent — the thing that
    actually determines which model the run uses.
    """
    config_yaml = f"model:\n  default: {config_model}\n  provider: {primary_provider}\n"
    config_yaml += (
        "fallback_providers:\n"
        f"  - provider: {fallback_provider}\n"
        f"    model: {fallback_model}\n"
    )
    (tmp_path / "config.yaml").write_text(config_yaml)

    captured_agent_kwargs: dict = {}

    class _FakeAgent:
        def __init__(self, **kwargs):
            captured_agent_kwargs.update(kwargs)
            self.session_id = None

        def run_conversation(self, *a, **kw):
            return {"final_response": "ok"}

        def close(self):
            pass

    fake_db = MagicMock()

    call_count = [0]

    def _resolve(**kwargs):
        call_count[0] += 1
        # First call (primary resolution) always raises to trigger the
        # fallback chain.  Subsequent calls (fallback entries) succeed.
        if call_count[0] == 1 and primary_raises:
            raise primary_raises
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": fallback_provider,
            "api_mode": "chat_completions",
        }

    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             side_effect=_resolve,
         ), \
         patch("run_agent.AIAgent", _FakeAgent):
        try:
            success, output, final_response, error = run_job(job)
        except Exception as exc:
            return False, "", str(exc), captured_agent_kwargs.get("model", "")

    return success, output, error, captured_agent_kwargs.get("model", "")


class TestPinnedModelNotOverwrittenByFallback:
    """When the job has an explicit ``model`` pin, the fallback chain must
    NOT replace it with the fallback entry's model."""

    def test_pinned_model_survives_primary_provider_failure(self, tmp_path):
        """Job pinned to glm-4.5-air/zai.  Primary (zai) auth fails →
        fallback chain fires.  The model passed to AIAgent must still be
        the pinned model, not the fallback's model."""
        from hermes_cli.auth import AuthError

        job = _base_job(
            model="glm-4.5-air",
            provider="zai",
        )

        success, _output, _error, model_used = _run_with_fallback(
            job,
            primary_provider="zai",
            primary_raises=AuthError("zai token expired"),
            fallback_provider="lmstudio",
            fallback_model="qwen3.8",
            tmp_path=tmp_path,
        )

        assert success is True, f"run should succeed via fallback, got error: {_error}"
        assert model_used == "glm-4.5-air", (
            f"pinned model 'glm-4.5-air' must survive fallback, "
            f"got '{model_used}'"
        )

    def test_unpinned_model_is_replaced_by_fallback(self, tmp_path):
        """Without a model pin, the fallback chain SHOULD replace the model
        — this is the existing, correct behavior for unpinned jobs."""
        from hermes_cli.auth import AuthError

        # No snapshots → drift guard never engages, so the fallback path
        # is exercised end-to-end.
        job = _base_job(
            model=None,
            provider=None,
            provider_snapshot=None,
            model_snapshot=None,
        )

        success, _output, _error, model_used = _run_with_fallback(
            job,
            primary_provider="zai",
            primary_raises=AuthError("zai token expired"),
            fallback_provider="lmstudio",
            fallback_model="qwen3.8",
            tmp_path=tmp_path,
        )

        # Unpinned job: fallback model is used (existing behavior).
        assert model_used == "qwen3.8"

    def test_pinned_model_with_pinned_provider_survives_transient_network_error(
        self, tmp_path
    ):
        """Same as above but with a transient network error instead of auth."""
        import httpx

        job = _base_job(
            model="glm-4.5-air",
            provider="zai",
        )

        success, _output, _error, model_used = _run_with_fallback(
            job,
            primary_provider="zai",
            primary_raises=httpx.ConnectError("DNS resolution failed"),
            fallback_provider="lmstudio",
            fallback_model="qwen3.8",
            tmp_path=tmp_path,
        )

        assert success is True
        assert model_used == "glm-4.5-air"