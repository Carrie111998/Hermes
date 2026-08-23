"""Tests for cron provider-model validation guard (PR #93132).

Covers the ~165 lines of branching in cron/jobs.py::_validate_provider_model_pair:
- alias handling (ox-alpha-free ↔ x-preview-f-free)
- vendor-prefix stripping
- unknown provider suggestions
- not-on-provider with candidate search
- stale/empty cache → don't block
- free-tier cross-wire (opencode-go + free model → suggest opencode-free)
"""

import time
import pytest

from cron.jobs import create_job, update_job, remove_job
from hermes_cli.models import _load_provider_models_cache, _save_provider_models_cache


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Isolate provider_models_cache to a temp HERMES_HOME so tests don't pollute real cache."""
    # Patch the cache path to tmp_path
    import hermes_cli.models as models_mod

    orig_path = models_mod._provider_models_cache_path

    def _tmp_path():
        return tmp_path / "provider_models_cache.json"

    monkeypatch.setattr(models_mod, "_provider_models_cache_path", _tmp_path)
    # Also patch cron/jobs.py's import path (it imports from hermes_cli.models at call time, so patching there is enough)
    yield tmp_path
    # Cleanup handled by tmp_path fixture


def _seed_cache(cache_data):
    _save_provider_models_cache(cache_data)


def test_alias_direct_hit_allows_opencode_free_ox_alpha(isolated_cache):
    _seed_cache({
        "opencode-free": {"at": time.time(), "fp": "test", "models": ["x-preview-f-free", "laguna-s-2.1-free"]},
        "opencode-go": {"at": time.time(), "fp": "test", "models": ["muse-spark-1.2-contributor", "ox-alpha-free"]},
    })
    # ox-alpha-free is alias for x-preview-f-free, so opencode-free should allow it
    job = create_job(prompt="test alias", schedule="every 1h", provider="opencode-free", model="ox-alpha-free")
    assert job["provider"] == "opencode-free"
    remove_job(job["id"])


def test_unknown_provider_suggests_close_match(isolated_cache):
    _seed_cache({
        "opencode-go": {"at": time.time(), "fp": "test", "models": ["muse-spark-1.2-contributor"]},
        "openai-codex": {"at": time.time(), "fp": "test", "models": ["gpt-5.6-sol"]},
    })
    with pytest.raises(ValueError, match="Unknown provider.*Did you mean"):
        create_job(prompt="test", schedule="every 1h", provider="opencode-goo", model="muse-spark-1.2-contributor")


def test_not_on_provider_suggests_correct_provider(isolated_cache):
    _seed_cache({
        "opencode-go": {"at": time.time(), "fp": "test", "models": ["muse-spark-1.2-contributor"]},
        "openai-codex": {"at": time.time(), "fp": "test", "models": ["gpt-5.6-sol", "gpt-5.6-terra"]},
        "openrouter": {"at": time.time(), "fp": "test", "models": ["openai/gpt-5.6-terra"]},
    })
    with pytest.raises(ValueError, match="not available on provider 'opencode-go'.*Available on:.*openai-codex"):
        create_job(prompt="test", schedule="every 1h", provider="opencode-go", model="gpt-5.6-terra")


def test_vendor_prefix_stripping_allows_match(isolated_cache):
    _seed_cache({
        "openrouter": {"at": time.time(), "fp": "test", "models": ["openai/gpt-5.6-terra"]},
    })
    # Request without vendor prefix should match cache entry with prefix
    job = create_job(prompt="test", schedule="every 1h", provider="openrouter", model="gpt-5.6-terra")
    assert job["model"] == "gpt-5.6-terra"
    remove_job(job["id"])


def test_stale_cache_does_not_block(isolated_cache):
    # Entry older than 7 days (SWR window) should be treated as "don't know"
    stale_at = time.time() - (8 * 24 * 3600)
    _seed_cache({
        "opencode-go": {"at": stale_at, "fp": "test", "models": ["muse-spark-1.2-contributor"]},
    })
    # Even though gpt-5.6-terra is not in the stale entry, we should not hard-reject
    job = create_job(prompt="test stale", schedule="every 1h", provider="opencode-go", model="gpt-5.6-terra")
    assert job["provider"] == "opencode-go"
    remove_job(job["id"])


def test_empty_cache_does_not_block(isolated_cache):
    _seed_cache({})
    job = create_job(prompt="test empty", schedule="every 1h", provider="opencode-go", model="any-model-xyz")
    assert job["provider"] == "opencode-go"
    remove_job(job["id"])


def test_free_tier_cross_wire_rejects_go_suggests_free(isolated_cache):
    _seed_cache({
        "opencode-go": {"at": time.time(), "fp": "test", "models": ["ox-alpha-free", "muse-spark-1.2-contributor"]},
        "opencode-free": {"at": time.time(), "fp": "test", "models": ["x-preview-f-free", "laguna-s-2.1-free"]},
    })
    with pytest.raises(ValueError, match="free-tier model.*must run via provider 'opencode-free'"):
        create_job(prompt="test", schedule="every 1h", provider="opencode-go", model="ox-alpha-free")


def test_single_axis_pin_does_not_validate(isolated_cache):
    _seed_cache({
        "opencode-go": {"at": time.time(), "fp": "test", "models": ["muse-spark-1.2-contributor"]},
    })
    # Only provider pinned, no model -> should not validate
    job = create_job(prompt="test", schedule="every 1h", provider="opencode-go", model=None)
    assert job["provider"] == "opencode-go"
    remove_job(job["id"])
    # Only model pinned, no provider -> should not validate
    job2 = create_job(prompt="test", schedule="every 1h", provider=None, model="gpt-5.6-terra")
    assert job2["model"] == "gpt-5.6-terra"
    remove_job(job2["id"])


def test_no_agent_and_base_url_skip_validation(isolated_cache):
    _seed_cache({
        "opencode-go": {"at": time.time(), "fp": "test", "models": ["muse-spark-1.2-contributor"]},
    })
    job = create_job(prompt="test", schedule="every 1h", provider="opencode-go", model="invalid-model-xyz", no_agent=True, script="test.sh")
    assert job["no_agent"] is True
    remove_job(job["id"])

    job2 = create_job(prompt="test", schedule="every 1h", provider="opencode-go", model="invalid-model-xyz", base_url="https://my-proxy.example.com/v1")
    assert job2["base_url"] == "https://my-proxy.example.com/v1"
    remove_job(job2["id"])
