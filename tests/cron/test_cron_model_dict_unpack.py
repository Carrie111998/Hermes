"""Cron jobs whose "model" field is a provider-block dict must still run.

Background (deep-audit 2026-08-04): ``cronjob action=create`` with an explicit
model pins the FULL provider block under the job's "model" key::

    {"provider": "alibaba", "model": "qwen3.8-max-preview", "base_url": "..."}

while top-level ``job["provider"]`` / ``job["base_url"]`` hold the same values
duplicated. Model resolution in ``run_job`` did
``model = job.get("model") or ...`` and later asserted
``isinstance(model, str)`` — a truthy dict passes the ``or`` but fails the
isinstance check, so EVERY agent-mode job with a pinned model died with
"has no model configured" (observed: 'Daily cost monitor' failing nightly).

The fix unpacks ``job["model"]["model"]`` when the field is a dict. The
drift guard (#44585) had the same latent crash: ``(job.get("model") or
"").strip()`` raises AttributeError on a dict.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import run_job

_MODEL_BLOCK = {
    "provider": "alibaba",
    "model": "qwen3.8-max-preview",
    "base_url": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
}


def _base_job(**overrides):
    job = {
        "id": "dict-model-test",
        "name": "dict model test",
        "prompt": "hello",
        "model": None,
        "provider": None,
        "base_url": None,
        "provider_snapshot": None,
        "model_snapshot": None,
    }
    job.update(overrides)
    return job


def _run(job, tmp_path, monkeypatch, current_provider="alibaba"):
    """Drive run_job with runtime resolution pinned; capture the AIAgent kwargs."""
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    fake_db = MagicMock()
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("tools.cronjob_tools._validate_cron_base_url", return_value=None), \
         patch(
             "hermes_cli.runtime_provider.resolve_runtime_provider",
             return_value={
                 "api_key": "test-key",
                 "base_url": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
                 "provider": current_provider,
                 "api_mode": "chat_completions",
             },
         ), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent

        success, output, final_response, error = run_job(job)
        kwargs = mock_agent_cls.call_args.kwargs if mock_agent_cls.called else {}

    return success, output, final_response, error, kwargs


class TestDictModelUnpack:
    def test_dict_model_block_runs_with_inner_name(self, tmp_path, monkeypatch):
        """The regression: a pinned provider-block dict must not kill the job."""
        job = _base_job(
            model=dict(_MODEL_BLOCK),
            provider="alibaba",
            base_url="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        )
        success, _out, _final, error, kwargs = _run(job, tmp_path, monkeypatch)

        assert success is True, f"dict-model job must run, got error: {error}"
        assert error is None or "no model configured" not in str(error)
        assert kwargs.get("model") == "qwen3.8-max-preview"

    def test_dict_model_without_inner_name_falls_back_to_config(self, tmp_path, monkeypatch):
        """A dict lacking the inner 'model' key behaves like an unpinned job.

        run_job's session isolation wipes user-shell env vars (HERMES_MODEL
        included), so the fallback chain continues to config.yaml
        model.default — the real production path.
        """
        (tmp_path / "config.yaml").write_text(
            "model:\n  default: config-default-model\n  provider: alibaba\n",
            encoding="utf-8",
        )
        job = _base_job(model={"provider": "alibaba"})
        success, _out, _final, error, kwargs = _run(job, tmp_path, monkeypatch)

        assert success is True, f"unpinned-axis job must resolve from config: {error}"
        assert kwargs.get("model") == "config-default-model"

    def test_dict_model_without_inner_name_no_source_fails_cleanly(self, tmp_path, monkeypatch):
        """No job model + no env + no config default -> actionable error, not a crash."""
        job = _base_job(model={"provider": "alibaba"})
        success, _out, _final, error, _kwargs = _run(job, tmp_path, monkeypatch)

        assert success is False
        assert error is not None
        assert "no model configured" in str(error)

    def test_string_model_still_works(self, tmp_path, monkeypatch):
        """Plain string pinning is unaffected by the unpack."""
        job = _base_job(model="qwen3.8-max-preview", provider="alibaba")
        success, _out, _final, error, kwargs = _run(job, tmp_path, monkeypatch)

        assert success is True, error
        assert kwargs.get("model") == "qwen3.8-max-preview"


class TestDictModelDriftGuard:
    def test_dict_model_pinned_does_not_crash_drift_guard(self, tmp_path, monkeypatch):
        """The latent AttributeError: drift guard must not .strip() a dict.

        A pinned job (truthy model, even as dict) skips the drift check and
        runs regardless of snapshot mismatch.
        """
        job = _base_job(
            model=dict(_MODEL_BLOCK),
            provider="alibaba",
            model_snapshot="some-other-model",
            provider_snapshot="some-other-provider",
        )
        success, _out, _final, error, kwargs = _run(job, tmp_path, monkeypatch)

        assert success is True, f"pinned dict-model job must run: {error}"
        assert kwargs.get("model") == "qwen3.8-max-preview"
