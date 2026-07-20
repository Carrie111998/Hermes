"""Scrapling extract provider — contract + default-selection precedence.

The two-tier fetch (fast → stealth) needs the scrapling package + a browser
and is exercised via integration; these unit checks cover the wiring that
must hold with the package absent: provider contract, and that scrapling
becomes the default extract backend only when installed and nothing explicit
is configured.
"""

from plugins.web.scrapling.provider import ScraplingWebSearchProvider


def test_provider_contract():
    p = ScraplingWebSearchProvider()
    assert p.name == "scrapling"
    assert p.supports_extract() is True
    # Scrapling is a fetcher/parser, not a SERP engine.
    assert p.supports_search() is False
    assert p.get_setup_schema()["post_setup"] == "scrapling"


def test_extract_backend_precedence(monkeypatch):
    import tools.web_tools as wt

    orig = wt._is_backend_available
    monkeypatch.setattr(
        wt, "_is_backend_available",
        lambda b: True if b == "scrapling" else orig(b),
    )

    # No config + installed -> scrapling is the default extract backend.
    monkeypatch.setattr(wt, "_load_web_config", lambda: {})
    assert wt._get_extract_backend() == "scrapling"

    # Explicit per-capability config wins over the scrapling default.
    monkeypatch.setattr(wt, "_load_web_config", lambda: {"extract_backend": "firecrawl"})
    assert wt._get_extract_backend() == "firecrawl"

    # Explicit shared backend wins too.
    monkeypatch.setattr(wt, "_load_web_config", lambda: {"backend": "exa"})
    assert wt._get_extract_backend() == "exa"


def test_absent_package_does_not_hijack_default(monkeypatch):
    """With scrapling not installed, selection falls through unchanged."""
    import tools.web_tools as wt

    monkeypatch.setattr(wt, "_is_backend_available", lambda b: False)
    monkeypatch.setattr(wt, "_load_web_config", lambda: {})
    # Whatever the legacy default is, it must not be scrapling.
    assert wt._get_extract_backend() != "scrapling"


if __name__ == "__main__":
    import pytest, sys
    sys.exit(pytest.main([__file__, "-q"]))
