"""resolve_litellm_target picks the named proxy when configured."""
import importlib.util
from pathlib import Path

_script = Path(__file__).resolve().parents[2] / "scripts" / "fix-cron-ollama-base-url.py"
_spec = importlib.util.spec_from_file_location("fix_cron_ollama_base_url", _script)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
FALLBACK_BASE_URL = _mod.FALLBACK_BASE_URL
FALLBACK_PROVIDER = _mod.FALLBACK_PROVIDER
resolve_litellm_target = _mod.resolve_litellm_target
process_target_job = _mod.process_target_job


def test_fallback_without_config():
    assert resolve_litellm_target(None) == (FALLBACK_PROVIDER, FALLBACK_BASE_URL)
    assert resolve_litellm_target({}) == (FALLBACK_PROVIDER, FALLBACK_BASE_URL)


def test_named_litellm_proxy_from_config():
    cfg = {
        "custom_providers": [
            {"name": "litellm_proxy", "base_url": "http://100.86.92.99:4010/"},
        ]
    }
    provider, url = resolve_litellm_target(cfg)
    assert provider == "litellm_proxy"
    assert url == "http://100.86.92.99:4010"


def test_named_litellm_proxy_from_v12_providers_dict():
    """Config v12 stores the proxy under providers: and drops custom_providers."""
    cfg = {
        "providers": {
            "litellm_proxy": {
                "name": "LiteLLM (Tailscale)",
                "api": "http://100.86.92.99:4010/",
            }
        }
    }
    provider, url = resolve_litellm_target(cfg)
    assert provider == "litellm_proxy"
    assert url == "http://100.86.92.99:4010"


def test_named_litellm_proxy_matches_name_when_key_differs():
    cfg = {
        "providers": {
            "tailscale-proxy": {
                "name": "litellm_proxy",
                "api": "http://100.86.92.99:4010/v1",
            }
        }
    }
    provider, url = resolve_litellm_target(cfg)
    assert provider == "litellm_proxy"
    assert url == "http://100.86.92.99:4010/v1"


def test_process_target_job_resumes_paused_job_already_at_target():
    job = {
        "id": "122eda6f83b1",
        "provider": "litellm_proxy",
        "base_url": "http://100.86.92.99:4010",
        "state": "paused",
        "last_error": "unsafe provider/base_url combination; refusing to run",
        "schedule": {},
        "last_run_at": None,
    }
    wrote, resumed_from = process_target_job(
        job,
        "litellm_proxy",
        "http://100.86.92.99:4010",
        dry_run=False,
        compute_next_run=lambda schedule, last: "2026-08-26T00:00:00Z",
        current_provider="litellm_proxy",
        current_base_url="http://100.86.92.99:4010",
    )
    assert wrote is True
    assert resumed_from == "paused"
    assert job["state"] == "scheduled"
    assert job["enabled"] is True
    assert job["last_error"] is None
    assert job["next_run_at"] == "2026-08-26T00:00:00Z"
    assert job["provider"] == "litellm_proxy"
    assert job["base_url"] == "http://100.86.92.99:4010"


def test_process_target_job_skips_healthy_job_already_at_target():
    job = {
        "id": "122eda6f83b1",
        "provider": "litellm_proxy",
        "base_url": "http://100.86.92.99:4010",
        "state": "scheduled",
        "last_error": None,
    }
    wrote, resumed_from = process_target_job(
        job,
        "litellm_proxy",
        "http://100.86.92.99:4010",
        dry_run=False,
        current_provider="litellm_proxy",
        current_base_url="http://100.86.92.99:4010",
    )
    assert wrote is False
    assert resumed_from is None
    assert job["state"] == "scheduled"
