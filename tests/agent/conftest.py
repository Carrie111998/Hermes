"""Shared pytest fixtures for agent tests."""

import pytest

from agent.prompt_builder import drain_truncation_warnings


@pytest.fixture(autouse=True)
def _isolate_truncation_warnings():
    """Keep prompt truncation warnings from leaking between agent tests."""
    drain_truncation_warnings()
    yield
    drain_truncation_warnings()
