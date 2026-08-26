"""Lead discovery is web research; without a backend it invents companies.

skills/sales/lead-discovery gives the agent a search tool and an HTML
extractor. When neither resolves, the tool call is simply absent and the model
answers the discovery prompt from its own weights — it does not error, it
returns plausible company names that do not exist. That failure is silent all
the way to the operator's lead list, which is the worst possible place to find
it.

pyproject's `interfaze` extra pins the three packages that make the backend
real. These tests hold the extra to that promise: a deployment that installs
the product without them should fail here, not in a tenant's results.
"""
from __future__ import annotations

import importlib
import importlib.util
import tomllib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

# The search/extract stack the lead-discovery skill depends on. Names as the
# interpreter imports them, which is not always the distribution name.
REQUIRED_BACKENDS = {
    "ddgs": "keyless search backend resolved by tools/web_tools.py",
    "scrapling": "page fetcher behind the extract capability",
    "markdownify": "HTML-to-markdown conversion for extracted pages",
}


def _interfaze_extra() -> list[str]:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]["interfaze"]


@pytest.mark.parametrize("module", sorted(REQUIRED_BACKENDS), ids=sorted(REQUIRED_BACKENDS))
def test_search_backend_is_pinned_in_the_interfaze_extra(module: str) -> None:
    """A backend that is not pinned will not be in the image."""
    pinned = {
        requirement.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
        for requirement in _interfaze_extra()
    }
    assert module in pinned, (
        f"{module} ({REQUIRED_BACKENDS[module]}) is not pinned in pyproject's "
        "interfaze extra, so Dockerfile.interfaze-api's `uv sync --extra interfaze` "
        "will not install it"
    )


@pytest.mark.parametrize("module", sorted(REQUIRED_BACKENDS), ids=sorted(REQUIRED_BACKENDS))
def test_search_backend_is_actually_importable(module: str) -> None:
    """Pinned is not installed. This is the check that would have caught it."""
    assert importlib.util.find_spec(module) is not None, (
        f"{module} ({REQUIRED_BACKENDS[module]}) is not importable in this "
        "environment; lead discovery would answer from model memory instead of "
        "the web. Install with: uv sync --extra web --extra interfaze"
    )


def test_the_keyless_provider_reports_itself_available() -> None:
    """Installed is not usable. The provider must also say it can run.

    ddgs is the only backend that needs no credential, so it is what a fresh
    deployment falls back to. If it is importable but reports unavailable, lead
    discovery silently loses its search tool on exactly the deployments that
    have configured nothing else.
    """
    from plugins.web.ddgs.provider import DDGSWebSearchProvider

    provider = DDGSWebSearchProvider()
    assert provider.supports_search() is True, "the ddgs provider must supply search"
    assert provider.is_available() is True, (
        "the ddgs provider reports unavailable even though the package imports; "
        "lead discovery would run with no search tool"
    )


def test_the_extract_backend_reports_itself_available() -> None:
    """Search finds pages; without extract the agent never reads one."""
    from plugins.web.scrapling.provider import ScraplingWebSearchProvider

    provider = ScraplingWebSearchProvider()
    assert provider.supports_extract() is True, "the scrapling provider must supply extract"
    assert provider.is_available() is True, (
        "the scrapling provider reports unavailable even though the package "
        "imports; lead discovery could search but never read a result"
    )
