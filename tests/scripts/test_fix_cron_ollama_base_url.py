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
