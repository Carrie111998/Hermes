"""Browser caller contracts for URL-safety resolver failures."""

import json

import pytest

from tools import browser_tool, url_safety


@pytest.mark.parametrize(
    ("local_backend", "allow_private", "local_sidecar"),
    [
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_configured_doh_failure_blocks_paths_that_skip_full_ssrf(
    monkeypatch,
    local_backend,
    allow_private,
    local_sidecar,
):
    """A failed trusted resolver must not weaken the metadata floor."""

    def fail_doh(*_args, **_kwargs):
        raise url_safety.DoHResolutionError("trusted resolver unavailable")

    monkeypatch.setattr(browser_tool, "_is_local_backend", lambda: local_backend)
    monkeypatch.setattr(browser_tool, "_allow_private_urls", lambda: allow_private)
    monkeypatch.setattr(
        browser_tool, "_is_local_sidecar_key", lambda _key: local_sidecar
    )
    monkeypatch.setattr(browser_tool, "_is_safe_url", lambda _url: True)
    monkeypatch.setattr(
        url_safety,
        "_configured_doh_resolver",
        lambda: ("https://trusted.example/dns-query", 5.0),
    )
    monkeypatch.setattr(url_safety, "_resolve_hostname_via_doh", fail_doh)

    result = json.loads(
        browser_tool.browser_navigate("https://attacker-controlled.example/")
    )

    assert result["success"] is False
    assert "cloud metadata endpoint" in result["error"]
