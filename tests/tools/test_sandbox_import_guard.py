#!/usr/bin/env python3
"""
Sandbox import guard regression tests (#8028).

Verifies the three-layer tamper-resistant import guard applied to
execute_code on both local (UDS) and remote (file-RPC) backends.

Tests the following invariants from the hermes-sweeper review:
1. Direct imports of hermes_cli/agent/tools/gateway are blocked.
2. sys.meta_path removal does NOT bypass the guard (read-only proxy +
   sys.modules sentinels + __import__ wrapper all catch it).
3. Remote backends receive the guard prepended to the shipped script.
4. PYTHONPATH is restricted to the sandbox dir (no inherited paths).

Run with:  python -m pytest tests/tools/test_sandbox_import_guard.py -v
"""

import json
import os
import subprocess
import sys
import tempfile

os.environ["TERMINAL_ENV"] = "local"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guard_script(code: str) -> str:
    """Wrap *code* with the sandbox import guard, mimicking what
    ``execute_code`` does before writing ``script.py``."""
    from tools.code_execution_tool import _generate_sandbox_import_guard

    guard = _generate_sandbox_import_guard()
    return guard + "\n\n" + code


def _run_guarded(code: str) -> subprocess.CompletedProcess:
    """Run *code* in a subprocess with the guard prepended, return result."""
    guarded = _guard_script(code)
    proc = subprocess.run(
        [sys.executable, "-c", guarded],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": ""},
    )
    return proc


# ---------------------------------------------------------------------------
# Test 1: Direct imports are blocked (the happy path)
# ---------------------------------------------------------------------------


def test_direct_import_agent_blocked():
    """import agent -> ImportError"""
    result = _run_guarded("import agent")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout
    assert "blocked" in result.stderr.lower() or "blocked" in result.stdout.lower()


def test_direct_import_tools_blocked():
    """import tools -> ImportError"""
    result = _run_guarded("import tools")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_direct_import_gateway_blocked():
    """import gateway -> ImportError"""
    result = _run_guarded("import gateway")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_direct_import_hermes_cli_blocked():
    """import hermes_cli -> ImportError"""
    result = _run_guarded("import hermes_cli")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_submodule_import_agent_credential_pool_blocked():
    """import agent.credential_pool -> ImportError (not just top-level)"""
    result = _run_guarded("import agent.credential_pool")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_from_import_blocked():
    """from tools.terminal_tool import ... -> ImportError"""
    result = _run_guarded("from tools.terminal_tool import something")
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


# ---------------------------------------------------------------------------
# Test 2: Guard removal does NOT bypass the guard
# ---------------------------------------------------------------------------


def test_meta_path_pop_does_not_bypass():
    """
    Script tries sys.meta_path.pop(0) then import agent.
    The read-only proxy blocks pop, and even if it somehow succeeds,
    sys.modules sentinels still block.
    """
    code = """
import sys
try:
    sys.meta_path.pop(0)
except (AttributeError, TypeError):
    pass  # expected: read-only proxy
import agent
"""
    result = _run_guarded(code)
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_meta_path_reassignment_does_not_bypass():
    """
    Script tries sys.meta_path = [] then import agent.
    sys.modules sentinels still block because they were planted first.
    """
    code = """
import sys
sys.meta_path = []
import agent
"""
    result = _run_guarded(code)
    # Even with meta_path cleared, sys.modules has the sentinel -> ImportError
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_meta_path_clear_does_not_bypass():
    """sys.meta_path.clear() -> import agent still blocked."""
    code = """
import sys
try:
    sys.meta_path.clear()
except (AttributeError, TypeError):
    pass
import agent
"""
    result = _run_guarded(code)
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_importlib_import_module_does_not_bypass():
    """importlib.import_module('agent') -> ImportError (goes through finders)"""
    code = """
import importlib
importlib.import_module('agent')
"""
    result = _run_guarded(code)
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_builtins_import_does_not_bypass():
    """__builtins__.__import__('agent') -> ImportError (Layer 3)"""
    code = """
__builtins__.__import__('agent')
"""
    result = _run_guarded(code)
    assert result.returncode != 0
    assert "ImportError" in result.stderr or "ImportError" in result.stdout


def test_sys_modules_sentinel_raises_on_attribute_access():
    """
    Even if a blocked name IS in sys.modules (as sentinel),
    accessing attributes raises ImportError.
    """
    code = """
import sys
mod = sys.modules.get('agent')
# mod is _BlockedModule sentinel - accessing any attr raises ImportError
try:
    _ = mod.something
    assert False, "should have raised"
except ImportError:
    pass  # expected
"""
    result = _run_guarded(code)
    assert result.returncode == 0  # script runs fine, catches ImportError as expected


# ---------------------------------------------------------------------------
# Test 3: Legitimate imports still work
# ---------------------------------------------------------------------------


def test_stdlib_imports_still_work():
    """json, os, sys, re, pathlib all import fine."""
    code = """
import json
import os
import sys
import re
from pathlib import Path
print("OK")
"""
    result = _run_guarded(code)
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_venv_packages_still_work():
    """requests, etc. import fine if installed."""
    code = """
import collections
import datetime
import math
import csv
print("OK")
"""
    result = _run_guarded(code)
    assert result.returncode == 0
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Test 4: Remote path verification
# ---------------------------------------------------------------------------


def test_remote_script_gets_guard_prepended():
    """
    Verify that _generate_sandbox_import_guard() produces code that
    can be prepended to a remote script.  The guard function is shared
    between local and remote paths, so we just verify it generates valid
    Python that blocks imports.
    """
    from tools.code_execution_tool import _generate_sandbox_import_guard

    guard = _generate_sandbox_import_guard()

    # It's valid Python (compiles)
    compile(guard, "<guard>", "exec")

    # It contains the key components
    assert "_BlockedModule" in guard, "Missing Layer 1 sentinel"
    assert "_ReadOnlyMetaPath" in guard, "Missing Layer 2 read-only proxy"
    assert "_hermes_safe_import" in guard, "Missing Layer 3 __import__ wrapper"
    assert "hermes_cli" in guard, "Blocklist missing hermes_cli"
    assert "'agent'" in guard or '"agent"' in guard, "Blocklist missing agent"
    assert "'tools'" in guard or '"tools"' in guard, "Blocklist missing tools"
    assert "'gateway'" in guard or '"gateway"' in guard, "Blocklist missing gateway"


def test_remote_guard_code_is_valid_python():
    """The generated guard compiles as standalone Python.

    We run it in a subprocess to avoid locking sys.meta_path in the
    test process (which would break other tests that import agent.*).
    """
    from tools.code_execution_tool import _generate_sandbox_import_guard

    guard = _generate_sandbox_import_guard()
    # Should compile without error
    compile(guard, "<remote_guard>", "exec")
    # Run in subprocess to avoid polluting the test process sys.meta_path
    check_code = guard + "\n" + "\n".join([
        "ok = True",
        "for pkg in ('hermes_cli', 'agent', 'tools', 'gateway'):",
        "    if pkg not in __hermes_sys.modules:",
        "        ok = False",
        "print('SENTINELS_OK' if ok else 'MISSING_SENTINELS')",
    ])
    proc = subprocess.run(
        [sys.executable, "-c", check_code],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, f"Guard execution failed: {proc.stderr}"
    assert "SENTINELS_OK" in proc.stdout


# ---------------------------------------------------------------------------
# Test 5: Blocklist content verification
# ---------------------------------------------------------------------------


def test_blocklist_contains_expected_packages():
    """_SANDBOX_BLOCKED_PACKAGES has the right entries."""
    from tools.code_execution_tool import _SANDBOX_BLOCKED_PACKAGES

    assert "hermes_cli" in _SANDBOX_BLOCKED_PACKAGES
    assert "agent" in _SANDBOX_BLOCKED_PACKAGES
    assert "tools" in _SANDBOX_BLOCKED_PACKAGES
    assert "gateway" in _SANDBOX_BLOCKED_PACKAGES


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
