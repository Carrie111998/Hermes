"""Shared fixtures for tests/tools/ web-provider tests.

Per-file subprocess isolation means each test file gets a fresh interpreter,
so module-level state (like the web-search-provider registry) is empty when
a file starts.  The ``web_registry_populated`` fixture registers all bundled
providers before each test and resets the registry afterwards — tests that
depend on the registry being populated should use it explicitly or via
``@pytest.mark.usefixtures("web_registry_populated")``.
"""

import os
from unittest.mock import patch

import pytest


# Captured at import time, before any test patches os.name — so this is the
# real host value ("nt" on Windows).
_HOST_OS_NAME = os.name


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Defense-in-depth against an INTERNALERROR that aborts the whole run.

    A test that patches ``os.name`` to a non-host value (e.g. "posix", to
    exercise cross-platform code) and then fails/errors *while still patched*
    poisons pytest's own failure formatter: ``_repr_failure_py`` calls
    ``Path(os.getcwd())``, which raises
    ``NotImplementedError("cannot instantiate 'PosixPath'")`` on a Windows host
    whenever ``os.name != "nt"``. That surfaces as an INTERNALERROR and forces
    exit code 3 instead of a clean pass/fail (see test_mcp_oauth.py).

    Restoring the host ``os.name`` here — before the report's longrepr is built
    — keeps failure formatting from ever crashing. Returning ``None`` lets the
    builtin makereport hook still produce the real report; the test's own
    teardown (e.g. monkeypatch) restores the same host value afterward, so this
    is idempotent.
    """
    if os.name != _HOST_OS_NAME:
        os.name = _HOST_OS_NAME
    return None


def register_all_web_providers():
    """Register all bundled web-search providers into the global registry.

    This is the single source of truth for the provider list used by
    test classes that need the registry populated for dispatch checks.
    """
    from agent.web_search_registry import register_provider, _reset_for_tests
    from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
    from plugins.web.ddgs.provider import DDGSWebSearchProvider
    from plugins.web.exa.provider import ExaWebSearchProvider
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.parallel.provider import ParallelWebSearchProvider
    from plugins.web.searxng.provider import SearXNGWebSearchProvider
    from plugins.web.tavily.provider import TavilyWebSearchProvider
    from plugins.web.xai.provider import XAIWebSearchProvider

    _reset_for_tests()
    for cls in (
        BraveFreeWebSearchProvider,
        DDGSWebSearchProvider,
        ExaWebSearchProvider,
        FirecrawlWebSearchProvider,
        ParallelWebSearchProvider,
        SearXNGWebSearchProvider,
        TavilyWebSearchProvider,
        XAIWebSearchProvider,
    ):
        register_provider(cls())


@pytest.fixture
def web_registry_populated():
    """Populate the web-search-provider registry for one test, then reset."""
    register_all_web_providers()
    yield
    from agent.web_search_registry import _reset_for_tests
    _reset_for_tests()


@pytest.fixture
def disable_lazy_stt_install():
    """Disarm the runtime lazy-install probe so static ``_HAS_FASTER_WHISPER``
    patches accurately simulate 'faster-whisper not installed'.

    Without this, ``_try_lazy_install_stt()`` calls
    ``importlib.util.find_spec("faster_whisper")``, which returns truthy
    whenever the package is installed in the dev / CI environment —
    defeating the test's ``_HAS_FASTER_WHISPER=False`` patch.

    Opt in at module scope with
    ``pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")``.
    """
    with patch("tools.transcription_tools._try_lazy_install_stt", return_value=False):
        yield
