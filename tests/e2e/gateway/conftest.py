"""Fixtures + gating for the dockerized-gateway e2e probes.

These tests build the ``hermes-agent`` image, boot one gateway container per
available provider, and run real upstream LLM calls against it. They are
opt-in (local, not CI): nothing here runs unless ``HERMES_E2E=1``.

The provider matrix is captured at *import time* — see :data:`MATRIX`. This is
deliberate: the repo's root ``conftest`` installs an autouse fixture that
blanks every ``*_API_KEY`` for the duration of each test, so by the time a
fixture body runs the keys are gone. Reading them here, as the module is
imported during collection, is the one window where they're still present.
"""

from __future__ import annotations

import os

import pytest

from .docker_gateway import REPO_ROOT, GatewayContainer, docker_available, ensure_image
from .env_files import load_test_env
from .providers import discover_providers

# Populate the env from .env.test (without clobbering the real shell), then
# capture the matrix — both before the hermetic autouse fixture can blank the
# keys for the duration of each test.
load_test_env(REPO_ROOT)
MATRIX = discover_providers()

_OPT_IN = os.environ.get("HERMES_E2E") in {"1", "true", "yes"}
_OPT_IN_HINT = (
    "gateway e2e probes are opt-in; set HERMES_E2E=1 and provide at least one "
    "provider key (e.g. ANTHROPIC_API_KEY) to run them"
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "e2e: dockerized gateway end-to-end probes (opt-in, real upstream)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every e2e item unless explicitly opted in via HERMES_E2E."""
    if _OPT_IN:
        return
    skip = pytest.mark.skip(reason=_OPT_IN_HINT)
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def docker_image() -> str:
    if not docker_available():
        pytest.skip("docker CLI/daemon not available")
    return ensure_image()


@pytest.fixture(
    scope="session",
    params=MATRIX or [None],
    ids=lambda p: p.id if p is not None else "no-provider",
)
def gateway(request: pytest.FixtureRequest, tmp_path_factory):
    """A running dockerized gateway for one provider in the matrix.

    Session-scoped + parametrized: one container per provider, reused across
    all probes that target it, torn down at the end of the session.
    """
    provider = request.param
    if provider is None:
        pytest.skip(_OPT_IN_HINT)

    # Resolve the image lazily — only once we know there's a provider to test —
    # so an empty matrix skips without ever shelling out to docker.
    request.getfixturevalue("docker_image")
    home = tmp_path_factory.mktemp(f"hermes-home-{provider.id}")
    container = GatewayContainer(provider, home)
    client = container.start()
    # Let probes branch on backend-specific expectations (e.g. json_object).
    client.provider = provider
    request.addfinalizer(container.stop)
    return client
