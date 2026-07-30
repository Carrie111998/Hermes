"""Runtime tests for the UCM mint factory caller gate.

Replaces test_ucm_mint_site_census.py, which read repository source to assert
call-site shape (banned per AGENTS.md:1370-1374). These tests exercise only
observable runtime behavior: the factory either raises PermissionError for an
unauthorized caller or succeeds for an authorized one.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tools.ucm_auth_context import (
    _AUTHORIZED_MINT_MODULES,
    EXPECTED_TOOL_NAME,
    is_ucm_auth_capability,
    mint_ucm_auth_context,
)


class TestCallerGate:
    """Caller gate tests only runtime behavior, no source-file reads."""

    def test_authorized_caller_can_mint(self):
        """This test file is in _AUTHORIZED_MINT_MODULES; minting must succeed."""
        cap = mint_ucm_auth_context([EXPECTED_TOOL_NAME], tool_call_id="gate-test")
        assert is_ucm_auth_capability(cap) is True
        assert cap.consume(EXPECTED_TOOL_NAME) is True

    def test_unauthorized_caller_raises_permission_error(self):
        """An arbitrary in-process caller is denied at mint time."""
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from tools.ucm_auth_context import mint_ucm_auth_context; "
                "mint_ucm_auth_context(['ucm_structured_process'])",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "PermissionError" in result.stderr

    def test_tool_executor_is_sole_production_mint_site(self):
        """Only agent/tool_executor.py is an authorized production caller in Phase 1."""
        production_sites = {
            m for m in _AUTHORIZED_MINT_MODULES if not m.startswith("tests/")
        }
        assert production_sites == {"agent/tool_executor.py"}

    def test_phase1_no_concurrent_dispatch_authorized(self):
        """agent/agent_runtime_helpers.py must not be authorized until Phase 2."""
        assert "agent/agent_runtime_helpers.py" not in _AUTHORIZED_MINT_MODULES

    def test_authorized_modules_are_expected_set(self):
        """_AUTHORIZED_MINT_MODULES must match the documented Phase 1 allowlist exactly."""
        expected = frozenset(
            {
                "agent/tool_executor.py",
                "tests/tools/test_ucm_auth_context.py",
                "tests/tools/test_ucm_caller_gate.py",
            }
        )
        assert _AUTHORIZED_MINT_MODULES == expected
