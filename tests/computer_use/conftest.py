"""Shared fixtures for the computer-use backend tests."""

import pytest

from tools.computer_use import cua_backend


@pytest.fixture(autouse=True)
def _clear_mcp_invocation_cache() -> None:
    """Isolate the CLI-discovery cache between tests.

    Discovery is cached per driver path because the answer only changes when
    the binary does. Tests break that assumption on purpose: several stub a
    different driver behind the same path, so a result cached by one would be
    served to the next.
    """
    cua_backend._reset_mcp_invocation_cache()
